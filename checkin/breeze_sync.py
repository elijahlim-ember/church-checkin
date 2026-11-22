"""Sync with the church database (Admin → Sync): by hand, and every 15 minutes by itself.

The church database's connector speaks the same API Breeze did, so the internal names (breeze_id, BreezeSync,
breeze_sent_at) stay. A sync does, in order:

1. People: read everyone from the church database and update the check-in roster with the saved column
   choices and leave-out rules.
2. Newcomers: people registered at check-in ("First time here?") are linked to their church database record
   when one exists, or added to it (first name, last name, mobile; family members linked together). A church
   database whose profile has the Chinese name field (from 4 Nov 2026) is also sent the visitor form's answers
   (visitor_form.py); each one it confirms is wiped here, keeping only the Chinese name and gender. Answers it
   didn't take (an older version takes only the mobile) are sent again at the start of every sync, before the
   roster is read (people/update, compatible service 1.9.0 on, which fills only blanks), and deleted 30 days after the
   visit if it still hasn't taken them; a newcomer waiting to be added keeps theirs. Each add carries the
   newcomer's one-off code (person.add_code), so a retry after a lost reply gets the same person back instead
   of a second one, and family links wait in family_link until the church database has them.
3. Attendance: everyone checked in since the sync start date is marked present on the matching event, and
   undone check-ins are removed again. People already marked are skipped, so running it twice is harmless.

IDs: check-in asks for Member IDs (compatible service 1.5 and later). The first sync that gets them moves each person and
household in the roster to their Member ID in one go, keeping the old ID out of sight (person.legacy_id,
household.legacy_key), so check-ins, families and classes stay with the same people. An older church database
sends its old IDs, which keep working; if it is rolled back to one, the next sync moves the roster back.

"Check" does everything read-only and shows what would change; nothing is written to the church database or
the roster until the administrator confirms. Check-in keeps working while a sync runs.
"""

import datetime as dt
import json
import logging
import secrets
import threading
import time
import traceback

from . import breeze, member_id, roster, visitor_form
from .db import audit
from .util import normalize_name, normalize_phone, now, now_iso, today_iso

log = logging.getLogger("checkin.breeze")

# If the church database suddenly returns far fewer people than the roster has (a partial reply), don't
# deactivate anyone.
MAX_DEACTIVATE_SHARE = 0.05

# Visitor form answers the church database hasn't taken are deleted here this many days after the visit.
KEEP_ANSWERS_DAYS = 30

# Columns with personal details: their values are never kept for the leave-out rules editor.
PERSONAL_COLUMNS = {breeze.ID_HEADER, "First Name", "Last Name", "Nickname", "Middle Name", "Family", "Mobile", "Home",
                    "Work", "Birthdate", "Chinese name"}


class SyncError(Exception):
    pass


class _TryOut(Exception):
    """Undoes the ID move a Check tries out (a Check writes nothing)."""


def _norm_event(name) -> str:
    return normalize_name(name or "")


# A newcomer still waiting to be added or linked: what newcomers() selects, for one person row.
WAITING = ("source='visitor' AND breeze_id IS NULL AND active=1 AND anonymised=0 "
           "AND COALESCE(add_code,'')<>'erased'")


def newcomers(db) -> list[dict]:
    """People registered at check-in who aren't in the church database yet, with their family and first visit.
    Leaves out anyone the church database erased after an earlier add (add_code 'erased')."""
    rows = db.all(
        "SELECT p.id, p.first_name, p.last_name, p.preferred_name, p.is_child, p.visitor_id, p.created_at, "
        "p.add_code, m.household_id, (SELECT MIN(s.date) FROM attendance a JOIN session s ON s.id=a.session_id "
        " WHERE a.person_id=p.id AND a.status<>'undone') AS first_visit "
        "FROM person p LEFT JOIN household_member m ON m.person_id=p.id "
        "WHERE p.source='visitor' AND p.breeze_id IS NULL AND p.active=1 AND p.anonymised=0 "
        "AND COALESCE(p.add_code,'')<>'erased' "
        "ORDER BY m.household_id, p.is_child, p.id")
    for row in rows:
        row["phones"] = [r["digits"] for r in db.all("SELECT digits FROM phone WHERE person_id=?", (row["id"],))]
    return rows


def _said(err) -> str:
    """What the church database said when it refused a call, as plain text."""
    errors = getattr(err, "data", {}).get("errors")
    text = " ".join(str(e) for e in errors) if isinstance(errors, list) else ""
    return text.strip() or str(err)


def _answers(text) -> dict:
    """A person's kept visitor form answers (person.visitor_details), or {} when there are none or they're unreadable."""
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _phone_for_breeze(digits: str, country: str) -> str:
    """Local numbers are stored without the country code ('91234567'); overseas ones keep theirs."""
    if country == "65" and len(digits) == 8:
        return f"+65 {digits[:4]} {digits[4:]}"
    return "+" + digits


class BreezeSync:
    """Runs one check or sync at a time in a background thread; the admin page polls status()."""

    def __init__(self, app, client_factory=None):
        self.app = app
        self.db = app.db
        self.client_factory = client_factory or (lambda: breeze.BreezeClient.from_app(self.db, app.paths))
        self.lock = threading.Lock()
        self.state = {"state": "idle"}

    # ---- control ---------------------------------------------------------------------------------------

    def connected(self) -> bool:
        return bool(breeze.load_key(self.app.paths, breeze.CHMS_KEY_FILE))

    def status(self) -> dict:
        with self.lock:
            state = json.loads(json.dumps(self.state, default=str))
        state["connected"] = self.connected()
        state["chms_url"] = self.db.get_setting("chms_url") or breeze.DEFAULT_CHMS_URL
        state["chms_auto_sync"] = self.db.get_setting("chms_auto_sync") is not False
        state["since"] = self.since()
        state["last"] = self.db.get_setting("breeze_last_sync")
        return state

    def start(self, kind: str, user_id=None, decisions=None, wait=False, auto=False) -> dict:
        with self.lock:
            if self.state.get("state") == "running":
                raise SyncError("A sync is already running.")
            self.state = {"state": "running", "kind": kind, "started": now_iso(), "step": "Starting",
                          "done": 0, "total": 0, "log": [], "auto": auto}
        thread = threading.Thread(target=self._run, args=(kind, user_id, decisions or {}, auto), daemon=True,
                                  name="breeze-sync")
        thread.start()
        if wait:
            thread.join()
        return self.status()

    def since(self) -> str:
        return self.db.get_setting("breeze_sync_since") or (dt.date.today() - dt.timedelta(days=7)).isoformat()

    def chms_services(self) -> list[dict]:
        """The church database's services, for Admin's pick list. Empty when it can't be reached or is older than
        1.12.0 (no such call): the admin page then offers the name box alone, as before."""
        try:
            found = self.client_factory().services()
        except breeze.BreezeError:
            return []
        return [{"id": str(s.get("id")), "name": str(s.get("name") or "")} for s in found
                if isinstance(s, dict) and s.get("id")]

    def connect_chms(self, url: str, key: str, user_id=None, auto=True) -> dict:
        """Connect to the church database, after testing its key. auto=False leaves the automatic 15-minute
        sync off (for a rehearsal copy that should only ever Check)."""
        url = (url or "").strip().rstrip("/") or breeze.DEFAULT_CHMS_URL
        if "://" not in url:
            url = "http://" + url
        if not url.startswith(("http://", "https://")):
            raise breeze.BreezeError("Enter the church database's address, for example http://127.0.0.1:8091.", 400)
        client = breeze.BreezeClient("chms", key.strip(), base_url=url, min_interval=0, label="the external service",
                                     ca_file=self.app.paths.keys / breeze.CHMS_CA_FILE)
        client.profile_fields()
        breeze.save_key(self.app.paths, key, breeze.CHMS_KEY_FILE)
        self.db.set_setting("chms_url", url)
        self.db.set_setting("sync_target", "chms")
        self.db.set_setting("chms_auto_sync", bool(auto))
        if not self.db.get_setting("breeze_sync_since"):
            self.db.set_setting("breeze_sync_since", self.since())
        with self.db.tx() as conn:
            audit(conn, "chms.connect", None, None, {"url": url}, user_id)
        return {"target": "chms"}

    def disconnect(self, user_id=None) -> None:
        """Delete the church database's key from this computer, and any key left from Breeze."""
        breeze.save_key(self.app.paths, None, breeze.CHMS_KEY_FILE)
        breeze.save_key(self.app.paths, None, breeze.KEY_FILE)
        with self.db.tx() as conn:
            audit(conn, "chms.disconnect", None, None, None, user_id)

    def resend_from(self, day: str, user_id=None, dry_run=False) -> dict:
        """After the church database is restored from a backup, send check-ins from `day` (the backup's date)
        again at the next sync. Undone check-ins are not sent; their removal is sent again, which is harmless.
        dry_run only counts them, for the confirm step."""
        if not self.connected():
            raise SyncError("Connect the church database first.")
        in_range = "SELECT id FROM session WHERE date>=? AND date<=?"
        params = (day, today_iso())
        with self.lock:
            if self.state.get("state") == "running":
                raise SyncError("A sync is running. Try again when it has finished.")
            count = self.db.scalar(f"SELECT COUNT(*) FROM attendance WHERE status<>'undone' "
                                   f"AND session_id IN ({in_range})", params)
            if dry_run:
                return {"from": day, "count": count, "since": self.since()}
            with self.db.tx() as conn:
                conn.execute(f"UPDATE attendance SET breeze_sent_at=NULL WHERE status<>'undone' "
                             f"AND session_id IN ({in_range})", params)
                conn.execute(f"UPDATE attendance SET breeze_sent_at=COALESCE(breeze_sent_at, ?) WHERE "
                             f"status='undone' AND breeze_instance IS NOT NULL AND session_id IN ({in_range})",
                             (now_iso(),) + params)
                audit(conn, "chms.resend", None, None, {"from": day, "count": count}, user_id)
            if day < self.since():
                self.db.set_setting("breeze_sync_since", day)
        self.db.bump()
        return {"from": day, "count": count, "since": self.since()}

    def auto_tick(self, wait=False) -> bool:
        """Called every 15 minutes: sync with the church database when connected and nothing is running."""
        if not self.connected() or self.db.get_setting("chms_auto_sync") is False:
            return False
        with self.lock:
            if self.state.get("state") == "running":
                return False
        try:
            self.start("sync", wait=wait, auto=True)
        except SyncError:
            return False
        return True

    def deliver_pending_reports(self, now=None) -> int:
        """Send reports whose retry time has arrived. The report contains counts only."""
        from .util import SGT, sg_now
        now = (now or sg_now()).astimezone(SGT)
        rows = self.db.all("SELECT * FROM day_report WHERE delivered_at IS NULL AND "
                           "(next_try IS NULL OR next_try<=?) ORDER BY date", (now.isoformat(),))
        if not rows or not self.connected():
            return 0
        client = self.client_factory()
        delivered = 0
        for row in rows:
            try:
                client.sunday_report(json.loads(row["payload"]))
            except Exception as exc:  # noqa: BLE001 - one malformed report must not block later reports
                attempts = row["attempts"] + 1
                next_try = (now + dt.timedelta(minutes=15)).isoformat()
                error = f"{type(exc).__name__}: report delivery failed"
                with self.db.tx() as conn:
                    conn.execute("UPDATE day_report SET delivery_error=?,attempts=?,next_try=? WHERE date=?",
                                 (error, attempts, next_try, row["date"]))
                log.warning("Sunday report delivery failed for %s (%s)", row["date"], type(exc).__name__)
            else:
                with self.db.tx() as conn:
                    conn.execute("UPDATE day_report SET delivered_at=?,delivery_error=NULL,next_try=NULL WHERE date=?",
                                 (now.isoformat(), row["date"]))
                delivered += 1
        return delivered

    def _progress(self, step=None, done=None, total=None, note=None):
        with self.lock:
            if step is not None:
                self.state["step"] = step
            if done is not None:
                self.state["done"] = done
            if total is not None:
                self.state["total"] = total
            if note:
                self.state["log"].append(note)
                log.info("Sync: %s", note)

    def _run(self, kind, user_id, decisions, auto=False):
        try:
            client = self.client_factory()
            result = self.check(client) if kind == "check" else self.sync(client, user_id, decisions, auto=auto)
            with self.lock:
                self.state.update(state="done", finished=now_iso(), result=result, calls=client.calls)
        except (breeze.BreezeError, SyncError) as err:
            with self.lock:
                self.state.update(state="error", finished=now_iso(), error=str(err))
        except Exception as err:  # noqa: BLE001 - shown to the administrator, details in the log
            log.error("Sync failed:\n%s", traceback.format_exc())
            with self.lock:
                self.state.update(state="error", finished=now_iso(), error=f"Unexpected problem: {err}")
        if kind == "sync":
            with self.lock:
                summary = {k: self.state.get(k) for k in ("state", "finished", "error", "auto")}
                if self.state.get("result"):
                    summary["result"] = self.state["result"].get("summary")
            self.db.set_setting("breeze_last_sync", summary)

    # ---- the three steps, read-only -------------------------------------------------------------------

    def _people(self, client, write=False, user_id=None):
        self._progress("Reading people from the church database")
        profile = client.profile_fields()
        people = client.people()
        if not people:
            raise SyncError("The church database sent no people, so nothing was changed.")
        headers, rows = breeze.people_table(profile, people)
        mapping, rules, extra = roster.default_choices(self.db, headers, rows)
        # The church database marks everyone who mustn't show (left, deceased, staff-only, hidden) in its
        # Status field. Read it whatever columns were chosen before, and never treat a reply without it as
        # "everyone shows".
        if "Status" not in headers or any(not row.get("Status") for row in rows):
            raise SyncError("The church database didn't say who shows at check-in, so nothing was changed. "
                            "Update the church database program, then sync again.")
        mapping = dict(mapping, status="Status", breeze_id=breeze.ID_HEADER)
        if "Chinese name" in headers:   # a column choice saved before check-in knew about Chinese names
            mapping.setdefault("chinese_name", "Chinese name")
        if "first_name" not in mapping:
            raise SyncError("The church database's reply has no ID or first name column, so nothing was changed. "
                            "Update the church database program, then sync again.")
        # For Admin → Sync → Leave out at check-in: the columns, and the values of the category-like ones.
        self.db.set_setting("sync_columns", headers)
        self.db.set_setting("sync_column_values", roster.column_value_options(
            [h for h in headers if h not in PERSONAL_COLUMNS], rows))
        legacy = breeze.legacy_ids(people)
        switch = self._id_switch(people, legacy)
        moved = {"people": 0, "households": 0, "skipped": 0}

        def analyse():
            return roster.analyse(self.db, (headers, rows), mapping, None, rules=rules, extra=extra)

        if not switch:
            analysis = analyse()
        elif write:
            with self.db.tx() as conn:
                moved = self._apply_switch(conn, switch, user_id)
            analysis = analyse()
        else:
            try:
                with self.db.tx() as conn:   # try the move, see what the roster would look like, undo it
                    moved = self._apply_switch(conn, switch)
                    analysis = analyse()
                    raise _TryOut
            except _TryOut:
                pass
        active = self.db.scalar("SELECT COUNT(*) FROM person WHERE source='breeze' AND active=1") or 0
        safe = len(analysis.deactivate) <= max(5, active * MAX_DEACTIVATE_SHARE)
        summary = roster.summary(analysis)
        info = {"totals": summary["totals"], "left_out": summary["left_out"],
                "not_in_breeze": len(analysis.deactivate), "deactivate": safe,
                "invalid_phones": len(analysis.warnings.get("invalid_phones") or []),
                "new_ids": {"people": moved["people"], "households": moved["households"]},
                "ids_back": bool(switch and not switch["forward"])}
        if moved["skipped"]:
            info["id_warning"] = (f"{moved['skipped']} people or households couldn't move to their new ID because "
                                  "someone else in the roster already has it. Check Admin → People.")
        if not safe:
            info["warning"] = (f"{len(analysis.deactivate)} people in the roster aren't in the church database's "
                               "reply. They'll be left as they are; check the church database, then sync again.")
        return profile, rows, (mapping, rules, extra), analysis, info, (legacy or ({}, {}))[0]

    def _id_switch(self, people, legacy) -> dict | None:
        """The roster's move to the IDs the church database now sends, or None when nothing needs moving.
        Forward: it sends Member IDs with the old IDs beside them (the first sync after its upgrade).
        Back: it sends only old IDs (rolled back to an older version) and the roster has Member IDs."""
        moves, homes, fills = [], [], []
        if legacy:
            persons, families = legacy
            before = {new: old for old, new in persons.items()}
            for row in self.db.all("SELECT id, breeze_id, legacy_id FROM person WHERE breeze_id IS NOT NULL"):
                new = persons.get(row["breeze_id"])
                if new and new != row["breeze_id"]:
                    moves.append((row["id"], row["breeze_id"], new))
                elif not row["legacy_id"] and before.get(row["breeze_id"]):
                    fills.append((row["id"], before[row["breeze_id"]]))   # added since the move
            for row in self.db.all("SELECT id, family_key FROM household"):
                kind, _, value = row["family_key"].partition(":")
                new = (families if kind == "F" else persons if kind in ("G", "S") else {}).get(value)
                if new and new != value:
                    homes.append((row["id"], row["family_key"], f"{kind}:{new}"))
        else:
            if any(member_id.parse(str(p.get("id") or "")) for p in people):
                return None       # Member IDs without the old ones beside them: nothing to move back to
            for row in self.db.all("SELECT id, breeze_id, legacy_id FROM person WHERE legacy_id IS NOT NULL "
                                   "AND breeze_id LIKE 'MEMB-%'"):
                moves.append((row["id"], row["breeze_id"], row["legacy_id"]))
            for row in self.db.all("SELECT id, family_key, legacy_key FROM household WHERE legacy_key IS NOT NULL "
                                   "AND family_key LIKE '_:MEMB-%'"):
                homes.append((row["id"], row["family_key"], row["legacy_key"]))
        if not (moves or homes or fills):
            return None
        return {"forward": bool(legacy), "people": moves, "households": homes, "fills": fills}

    @staticmethod
    def _apply_switch(conn, switch, user_id=None) -> dict:
        """Move people and households to their new IDs, in the caller's transaction. One that would clash
        with someone already holding that ID is left as it is (and counted)."""
        ts, forward = now_iso(), switch["forward"]
        moved = {"people": 0, "households": 0, "skipped": 0}
        for pid, old, new in switch["people"]:
            if conn.execute("SELECT 1 FROM person WHERE breeze_id=? AND id<>?", (new, pid)).fetchone():
                moved["skipped"] += 1
                continue
            conn.execute("UPDATE person SET breeze_id=?, legacy_id=?, updated_at=? WHERE id=?",
                         (new, old if forward else None, ts, pid))
            conn.execute("UPDATE family_link SET breeze_id=? WHERE breeze_id=?", (new, old))
            moved["people"] += 1
        for pid, old in switch["fills"]:
            conn.execute("UPDATE person SET legacy_id=? WHERE id=?", (old, pid))
        for hid, old, new in switch["households"]:
            if conn.execute("SELECT 1 FROM household WHERE family_key=? AND id<>?", (new, hid)).fetchone():
                moved["skipped"] += 1
                continue
            conn.execute("UPDATE household SET family_key=?, legacy_key=?, updated_at=? WHERE id=?",
                         (new, old if forward else None, ts, hid))
            # Guardians from a file import are keyed by their household: L:<household key>:<name>.
            prefix = f"L:{old}:"
            conn.execute("UPDATE person SET local_key=? || substr(local_key, ?) WHERE substr(local_key, 1, ?)=?",
                         (f"L:{new}:", len(prefix) + 1, len(prefix), prefix))
            moved["households"] += 1
        if moved["people"] or moved["households"]:
            audit(conn, "chms.ids_forward" if forward else "chms.ids_back", None, None, moved, user_id)
        return moved

    def _newcomer_plan(self, rows, country):
        """For each newcomer, church database people who look like the same person (same name, or a shared mobile)."""
        by_phone, by_name = {}, {}
        for row in rows:
            for header in ("Mobile", "Home", "Work"):
                digits = normalize_phone(row.get(header), country)
                if digits:
                    by_phone.setdefault(digits, []).append(row)
            last = normalize_name(row.get("Last Name"))
            for first in {normalize_name(row.get("First Name")), normalize_name(row.get("Nickname"))} - {""}:
                by_name.setdefault((first, last), []).append(row)
        plan = []
        for person in newcomers(self.db):
            last = normalize_name(person["last_name"])
            names = {normalize_name(person["first_name"]), normalize_name(person.get("preferred_name"))} - {""}
            same_name = {r[breeze.ID_HEADER]: r for n in names for r in by_name.get((n, last), [])}
            same_phone = {r[breeze.ID_HEADER]: r for d in person["phones"] for r in by_phone.get(d, [])}
            candidates = []
            for bid in list(same_name) + [b for b in same_phone if b not in same_name]:
                r = same_name.get(bid) or same_phone.get(bid)
                candidates.append({"breeze_id": bid, "name": " ".join(filter(None, [
                    r.get("Nickname") or r.get("First Name"), r.get("Last Name")])),
                    "same_name": bid in same_name, "same_phone": bid in same_phone})
            sure = [c for c in candidates if c["same_name"] and c["same_phone"]]
            if len(sure) == 1:
                default = "link:" + sure[0]["breeze_id"]
            elif candidates:
                default = ""          # the administrator must choose
            else:
                default = "add"
            plan.append({"id": person["id"], "name": " ".join(filter(None, [person["first_name"], person["last_name"]])),
                         "visitor_id": person["visitor_id"],
                         "household_id": person["household_id"], "is_child": bool(person["is_child"]),
                         "first_visit": person["first_visit"], "has_phone": bool(person["phones"]),
                         "candidates": candidates[:5], "default": default})
        return plan

    def _attendance_plan(self, client, since):
        """Sessions with check-ins to send (or undo) and the event each one goes to."""
        sessions = self.db.all(
            "SELECT s.id, s.date, s.name, COALESCE(NULLIF(s.breeze_event_name,''), NULLIF(v.breeze_event_name,''), "
            "s.name) AS event_name, s.service_id, v.chms_service_id AS linked, "
            "COALESCE(NULLIF(v.breeze_event_name,''), v.name) AS service_event "
            "FROM session s LEFT JOIN service v ON v.id=s.service_id "
            "WHERE s.date>=? AND s.date<=? AND EXISTS (SELECT 1 FROM attendance a JOIN person p ON p.id=a.person_id "
            "WHERE a.session_id=s.id AND COALESCE(p.add_code,'')<>'erased' AND "
            "((a.status<>'undone' AND a.breeze_sent_at IS NULL) OR (a.status='undone' AND a.breeze_sent_at IS NOT NULL))) "
            "ORDER BY s.date, s.start_time", (since, today_iso()))
        if not sessions:
            return []
        events = client.events(sessions[0]["date"], sessions[-1]["date"])
        where = getattr(client, "name", "the external service")
        plan = []
        for s in sessions:
            same_day = [e for e in events if str(e.get("start_datetime") or "")[:10] == s["date"]]
            # The service's number in the church database decides, when the service is linked to one. A day whose
            # own event was changed by hand (a one-off) goes by that name instead.
            linked = str(s["linked"] or "")
            one_off = s["service_event"] is not None and _norm_event(s["event_name"]) != _norm_event(s["service_event"])
            match = ([e for e in same_day if str(e.get("event_id") or "") == linked] if linked and not one_off else [])
            by_number = len(match) == 1
            if not by_number:
                match = [e for e in same_day if _norm_event(e.get("name")) == _norm_event(s["event_name"])]
            counts = self.db.one(
                "SELECT SUM(a.status<>'undone' AND a.breeze_sent_at IS NULL AND p.breeze_id IS NOT NULL) AS to_send, "
                "SUM(a.status<>'undone' AND a.breeze_sent_at IS NULL AND p.breeze_id IS NULL "
                "AND COALESCE(p.add_code,'')<>'erased') AS waiting, "
                "SUM(a.status='undone' AND a.breeze_sent_at IS NOT NULL) AS to_remove "
                "FROM attendance a JOIN person p ON p.id=a.person_id WHERE a.session_id=?", (s["id"],))
            item = {"session_id": s["id"], "date": s["date"], "name": s["name"], "event_name": s["event_name"],
                    "service_id": s["service_id"], "to_send": counts["to_send"] or 0,
                    "waiting_newcomers": counts["waiting"] or 0, "to_remove": counts["to_remove"] or 0,
                    "breeze_events": sorted({e.get("name") for e in same_day if e.get("name")}),
                    "instance_ids": {str(e.get("id")) for e in same_day if e.get("id")}}
            if len(match) == 1:
                item["instance_id"] = str(match[0]["id"])
                number = str(match[0].get("event_id") or "")
                if not by_number and not linked and not one_off and s["service_id"] and number not in ("", "0"):
                    item["learn"] = number  # matched by name: remember the number, so a rename can't unmatch it
            elif len(match) > 1:
                item["problem"] = f"{where[:1].upper() + where[1:]} has {len(match)} '{s['event_name']}' events on this day."
            else:
                item["problem"] = (f"No '{s['event_name']}' event in {where} on this day."
                                   + (f" {where[:1].upper() + where[1:]} has: " + ", ".join(item["breeze_events"]) + "."
                                      if same_day else ""))
            plan.append(item)
        return plan

    def check(self, client) -> dict:
        country = self.db.get_setting("country_code") or "65"
        _, rows, _, _, people, _ = self._people(client)
        self._progress("Looking for newcomers")
        new = self._newcomer_plan(rows, country)
        self._progress("Checking attendance")
        attendance = self._attendance_plan(client, self.since())
        return {"people": people, "newcomers": new, "attendance": attendance, "since": self.since()}

    # ---- sync (writes) ----------------------------------------------------------------------------------

    def sync(self, client, user_id, decisions, auto=False) -> dict:
        country = self.db.get_setting("country_code") or "65"
        summary = {"people_new": 0, "people_updated": 0, "deactivated": 0, "linked": 0, "added": 0,
                   "attendance_sent": 0, "attendance_already": 0, "attendance_removed": 0, "new_ids": 0,
                   "problems": []}
        backup = None
        # An automatic sync (every 15 minutes) backs up only once a day; a sync by hand always does.
        if self.app.backups.has_key() and (not auto or self.db.get_setting("auto_sync_backup_day") != today_iso()):
            backup = self.app.backups.create("pre-sync", user_id=user_id)["name"]
            if auto:
                self.db.set_setting("auto_sync_backup_day", today_iso())

        # Before the roster is read: visitor form answers kept past KEEP_ANSWERS_DAYS are deleted, and newcomers
        # already in the church database get the rest, so their Chinese name and gender are there when the roster
        # brings its values back.
        self._delete_old_answers(summary, user_id)
        self._send_kept_answers(client, summary, user_id)

        # 1. People
        profile, rows, choices, analysis, people, new_id_for = self._people(client, write=True, user_id=user_id)
        mapping, rules, extra = choices
        summary["new_ids"] = people["new_ids"]["people"]
        if summary["new_ids"]:
            where = "their Member IDs" if not people["ids_back"] else "their old IDs (the church database is older)"
            self._progress(note=f"IDs: {people['new_ids']['people']} people and {people['new_ids']['households']} "
                                f"households moved to {where}")
        if people.get("id_warning"):
            summary["problems"].append(people["id_warning"])
        self._progress("Updating the roster")
        with self.db.tx() as conn:
            batch = conn.execute(
                "INSERT INTO import_batch(ts, user_id, filename, sha256, mapping, status) VALUES(?,?,?,?,?,?)",
                (now_iso(), user_id, "Sync", "", json.dumps({"mapping": mapping, "rules": rules,
                                                                   "extra": extra}), "previewed")).lastrowid
        totals = roster.commit(self.db, analysis, batch, people["deactivate"], user_id)
        summary.update(people_new=totals["people_new"], people_updated=totals["people_updated"],
                       deactivated=totals.get("deactivated", 0))
        if people.get("warning"):
            summary["problems"].append(people["warning"])
        self._progress(note=f"Roster: {totals['people_new']} new, {totals['people_updated']} updated, "
                            f"{totals.get('deactivated', 0)} no longer in the church database")

        # 2. Newcomers
        plan = self._newcomer_plan(rows, country)
        phone_field = next((str(f["field_id"]) for s in profile for f in s.get("fields") or []
                            if f.get("field_type") == "phone"), None)
        takes_answers = visitor_form.takes_answers(profile)
        self._progress("Newcomers", 0, len(plan))
        for index, item in enumerate(plan, 1):
            choice = decisions.get(str(item["id"]), item["default"]) or "skip"
            if choice.startswith("link:"):
                bid = choice[5:]
                bid = new_id_for.get(bid) or member_id.parse(bid, bare=True) or bid   # an old ID typed: its Member ID
                if bid not in {c["breeze_id"] for c in item["candidates"]} and not bid.isdigit() \
                        and not member_id.parse(bid):
                    continue
                if self._link(item["id"], bid, user_id, household_id=item["household_id"]):
                    summary["linked"] += 1
            elif choice == "add":
                person = self._claim_add_code(item["id"])
                if person is None:
                    continue    # another sync added or linked them meanwhile
                phones = [r["digits"] for r in self.db.all("SELECT digits FROM phone WHERE person_id=?", (item["id"],))]
                fields = []
                if phone_field and phones:
                    fields = [{"field_id": phone_field, "field_type": "phone", "response": "true",
                               "details": {"phone_mobile": _phone_for_breeze(phones[0], country)}}]
                answers_sent = visitor_form.connector_fields(person) if takes_answers else []
                fields += answers_sent
                try:
                    reply = client.add_person(person["first_name"], person["last_name"] or "", fields or None,
                                              add_code=person["add_code"])
                except breeze.BreezeRefused as err:
                    if err.data.get("erased"):
                        self._erased(item["id"], user_id)
                        visitor = item.get("visitor_id") or "(no visitor ID)"
                        summary["problems"].append(
                            f"Newcomer {visitor} was added before and has since been erased from the church "
                            "database, so check-in won't add them again. To remove them from check-in too: "
                            f"Admin → People, search {visitor}, then Anonymise this person.")
                    else:
                        summary["problems"].append(f"{item['name']} wasn't added to the church database. "
                                                   f"It said: {_said(err)}")
                    continue
                bid, legacy = self._new_person_id(reply), self._new_person_legacy_id(reply)
                if not bid:
                    summary["problems"].append(f"{getattr(client, 'Name', 'The church database')} didn't confirm adding {item['name']}.")
                    continue
                everything_saved = takes_answers and visitor_form.confirmed(reply, answers_sent)
                if not self._link(item["id"], bid, user_id, added=True, legacy_id=legacy,
                                  keep_answers=not everything_saved, household_id=item["household_id"]):
                    continue    # another sync, sending the same code, linked them to this person first
                if takes_answers and answers_sent and not everything_saved:
                    unsaved = visitor_form.not_saved(reply, answers_sent)
                    self._keep_only(item["id"], unsaved)    # what it saved goes; the rest waits for 30 days
                    left = ", ".join(unsaved) or "the visitor form's answers"
                    summary["problems"].append(
                        f"{item['name']} was added, but the church database didn't save: {left}. Check-in kept "
                        "the answers; add them in the church database by hand.")
                summary["added"] += 1
            self._progress(done=index)
        self._families(client, summary)
        if summary["linked"] or summary["added"]:
            self._progress(note=f"Newcomers: {summary['added']} added to the church database, {summary['linked']} linked")

        # 3. Attendance
        attendance = self._attendance_plan(client, self.since())
        self._progress("Sending attendance", 0, sum(a["to_send"] + a["to_remove"] for a in attendance))
        done = 0
        for item in attendance:
            if not item.get("instance_id"):
                summary["problems"].append(f"{item['name']} {item['date']}: {item['problem']}")
                continue
            instance = item["instance_id"]
            if item.get("learn"):
                self._exec("UPDATE service SET chms_service_id=? WHERE id=? AND chms_service_id IS NULL",
                           (item["learn"], item["service_id"]))
            already = {str(r.get("person_id")) for r in client.attendance(instance) or []}
            pending = self.db.all(
                "SELECT a.id, a.status, a.breeze_instance, p.breeze_id FROM attendance a JOIN person p ON p.id=a.person_id "
                "WHERE a.session_id=? AND p.breeze_id IS NOT NULL AND ((a.status<>'undone' AND a.breeze_sent_at IS NULL) "
                "OR (a.status='undone' AND a.breeze_sent_at IS NOT NULL)) ORDER BY a.id", (item["session_id"],))
            for row in pending:
                if row["status"] == "undone":
                    # Remove it from the event it was sent to, even if the session now maps to another one.
                    # A recorded event this side doesn't have that day (sent to the other system before a
                    # switch) falls back to today's event. Always ask: its list may not show a recent check-in.
                    sent_to = row["breeze_instance"] if row["breeze_instance"] in item["instance_ids"] else instance
                    client.get("events/attendance/delete", person_id=row["breeze_id"], instance_id=sent_to)
                    if sent_to == instance:
                        already.discard(row["breeze_id"])
                    self._exec("UPDATE attendance SET breeze_sent_at=NULL WHERE id=?", (row["id"],))
                    summary["attendance_removed"] += 1
                else:
                    if row["breeze_id"] in already or client.add_attendance(row["breeze_id"], instance) == "duplicate":
                        summary["attendance_already"] += 1
                    else:
                        summary["attendance_sent"] += 1
                    already.add(row["breeze_id"])
                    self._exec("UPDATE attendance SET breeze_sent_at=?, breeze_instance=? WHERE id=?",
                                    (now_iso(), instance, row["id"]))
                done += 1
                self._progress(done=done)
            if item["waiting_newcomers"]:
                waiting = self.db.scalar(
                    "SELECT COUNT(*) FROM attendance a JOIN person p ON p.id=a.person_id WHERE a.session_id=? "
                    "AND a.status<>'undone' AND p.breeze_id IS NULL AND COALESCE(p.add_code,'')<>'erased'",
                    (item["session_id"],))
                if waiting:
                    summary["problems"].append(f"{item['name']} {item['date']}: {waiting} newcomer(s) not in "
                                               f"{getattr(client, 'name', 'the church database')} yet; their attendance will be "
                                               "sent once they are added.")
        self._progress(note=f"Attendance: {summary['attendance_sent']} sent, {summary['attendance_already']} "
                            f"already there, {summary['attendance_removed']} removed")
        with self.db.tx() as conn:
            audit(conn, "breeze.sync", None, None, summary, user_id)
        self.db.bump()
        return {"summary": summary, "backup": backup}

    def _exec(self, sql, params=()):
        with self.db.tx() as conn:
            conn.execute(sql, params)

    @staticmethod
    def _new_person_id(reply):
        items = reply if isinstance(reply, list) else [reply]
        for item in items:
            if isinstance(item, dict):
                bid = item.get("id") or (item.get("details") or {}).get("person_id")
                if bid:
                    return str(bid)
        return None

    @staticmethod
    def _new_person_legacy_id(reply):
        """The old ID beside the Member ID, from a church database that sends both."""
        items = reply if isinstance(reply, list) else [reply]
        return next((str(i["legacy_id"]) for i in items if isinstance(i, dict) and i.get("legacy_id")), None)

    def _claim_add_code(self, person_id: int) -> dict | None:
        """A newcomer about to be added, with their add code: the same code on every try, saved before the first,
        so if a reply is lost the church database sends back the person that try added instead of adding them
        again. Claimed in one write, so two syncs at once (run.py sync --run while the automatic sync runs) send
        the same code. None when they aren't waiting any more: another sync added or linked them meanwhile."""
        with self.db.tx() as conn:
            # Only the WAITING constant from the top of this file goes into the text, never anything from a request;
            # the values are bound with ?.
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            conn.execute(f"UPDATE person SET add_code=? WHERE id=? AND COALESCE(add_code,'')='' AND {WAITING}",
                         (secrets.token_hex(16), person_id))
            row = conn.execute("SELECT first_name, last_name, chinese_name, sex, visitor_details, add_code FROM person "
                               f"WHERE id=? AND {WAITING}", (person_id,)).fetchone()
        return dict(row) if row else None

    def _link(self, person_id: int, breeze_id: str, user_id, added=False, legacy_id=None, keep_answers=False,
              household_id=None) -> bool:
        """Connect a newcomer to their church database record. If that person is already in the roster, the
        newcomer's check-ins move to them and the newcomer record is retired.

        The visitor form's answers are wiped, unless keep_answers: an older church database took the newcomer
        without them. A newcomer linked to an existing record never sends them (the connector only adds).
        household_id: their check-in household, kept in family_link for the families step (_families).
        False, changing nothing, when they aren't waiting any more: another sync linked them meanwhile."""
        ts = now_iso()
        with self.db.tx() as conn:
            # Only the WAITING constant from the top of this file goes into the text, never anything from a request;
            # the values are bound with ?.
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            if not conn.execute(f"SELECT 1 FROM person WHERE id=? AND {WAITING}", (person_id,)).fetchone():
                return False
            if not keep_answers:
                conn.execute("UPDATE person SET visitor_details=NULL WHERE id=?", (person_id,))
            conn.execute("UPDATE person SET add_code=NULL WHERE id=?", (person_id,))
            if household_id is not None:
                conn.execute("INSERT OR REPLACE INTO family_link(person_id, household_id, breeze_id, added, created_at) "
                             "VALUES(?,?,?,?,?)", (person_id, household_id, breeze_id, 1 if added else 0, ts))
            existing = conn.execute("SELECT id FROM person WHERE breeze_id=?", (breeze_id,)).fetchone()
            if existing and existing["id"] != person_id:
                target = existing["id"]
                conn.execute(
                    "UPDATE attendance SET person_id=? WHERE person_id=? AND (status='undone' OR NOT EXISTS ("
                    "SELECT 1 FROM attendance b WHERE b.person_id=? AND b.session_id=attendance.session_id "
                    "AND b.status<>'undone'))", (target, person_id, target))
                conn.execute("UPDATE attendance SET status='undone', undone_at=? WHERE person_id=? AND status<>'undone'",
                             (ts, person_id))
                conn.execute("DELETE FROM phone WHERE person_id=?", (person_id,))
                conn.execute("UPDATE person SET active=0, notes=TRIM(COALESCE(notes,'') || ' Linked to ' || ?), "
                             "updated_at=? WHERE id=?", (breeze_id, ts, person_id))
                conn.execute("UPDATE household_member SET active=0 WHERE person_id=?", (person_id,))
            else:
                conn.execute("UPDATE person SET breeze_id=?, legacy_id=?, source='breeze', updated_at=? WHERE id=?",
                             (breeze_id, legacy_id, ts, person_id))
            conn.execute(
                "UPDATE household SET active=0 WHERE source='visitor' AND id NOT IN (SELECT m.household_id FROM "
                "household_member m JOIN person p ON p.id=m.person_id WHERE p.active=1 AND m.active=1)")
            audit(conn, "breeze.newcomer_added" if added else "breeze.newcomer_linked", "person", person_id,
                  {"breeze_id": breeze_id}, user_id)
        return True

    def _erased(self, person_id: int, user_id):
        """The church database erased this newcomer after an earlier try added them (that reply was lost).
        Check-in never adds them again and wipes the visitor form's answers; the check-in record stays until an
        administrator anonymises it (Admin → People)."""
        with self.db.tx() as conn:
            conn.execute("UPDATE person SET add_code='erased', visitor_details=NULL, updated_at=? WHERE id=?",
                         (now_iso(), person_id))
            audit(conn, "breeze.newcomer_erased", "person", person_id, None, user_id)

    def _keep_only(self, person_id: int, names, user_id=None, audit_as=None) -> list[str]:
        """Keeps only these of a person's visitor form answers (the ones the church database didn't save) and
        deletes the rest; none left means no answers at all. audit_as: also logged, with counts only."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT visitor_details FROM person WHERE id=?", (person_id,)).fetchone()
            kept = _answers(row["visitor_details"]) if row else {}
            left = {name: value for name, value in kept.items() if name in names}
            conn.execute("UPDATE person SET visitor_details=? WHERE id=?",
                         (json.dumps(left, ensure_ascii=False) if left else None, person_id))
            if audit_as:
                audit(conn, audit_as, "person", person_id, {"saved": len(kept) - len(left), "kept": len(left)},
                      user_id)
        return list(left)

    def _send_kept_answers(self, client, summary, user_id):
        """Newcomers check-in added while the church database couldn't take all their visitor form answers (an
        older version took only the mobile, or it didn't save some) are sent the answers check-in kept, at every
        sync (people/update, compatible service 1.9.0 on). It fills only its blanks and says which answers the person now
        has, whoever entered them: those are deleted here. The rest wait, until KEEP_ANSWERS_DAYS after the visit."""
        rows = []
        for row in self.db.all("SELECT id, first_name, last_name, chinese_name, sex, visitor_details, breeze_id "
                               "FROM person WHERE visitor_details IS NOT NULL AND breeze_id IS NOT NULL "
                               "AND active=1 AND anonymised=0 ORDER BY id"):
            if _answers(row["visitor_details"]):
                rows.append(row)
            else:
                self._keep_only(row["id"], [])    # nothing usable in it
        taken = 0
        for row in rows:
            kept = _answers(row["visitor_details"])
            name = " ".join(filter(None, [row["first_name"], row["last_name"]]))
            fields = visitor_form.connector_fields({**row, "visitor_details": json.dumps(kept, ensure_ascii=False)})
            try:
                reply = client.update_person(row["breeze_id"], fields)
            except breeze.BreezeRefused as err:
                if "unknown endpoint" in _said(err).lower():
                    summary["problems"].append(
                        f"The church database is too old to take the visitor form answers check-in kept for "
                        f"{len(rows)} newcomer(s). Update it, and the next sync sends them. Answers still here "
                        f"{KEEP_ANSWERS_DAYS} days after the visit are deleted unsent.")
                    return
                summary["problems"].append(
                    f"{name}'s visitor form answers didn't go to the church database. It said: {_said(err)} "
                    f"Check-in keeps them until {KEEP_ANSWERS_DAYS} days after the visit.")
                continue
            except breeze.BreezeError as err:
                summary["problems"].append(f"The visitor form answers check-in kept weren't sent this time. {err}")
                return
            left = [n for n in visitor_form.not_saved(reply, fields) if n in kept]
            if len(left) == len(kept):
                continue    # it took none of them: sent again next time, deleted after KEEP_ANSWERS_DAYS
            self._keep_only(row["id"], left, user_id, audit_as="breeze.visitor_answers_sent")
            taken += 1
            if left:
                summary["problems"].append(
                    f"{name}'s visitor form answers went to the church database, but it didn't save: "
                    f"{', '.join(left)}. Check-in keeps those until {KEEP_ANSWERS_DAYS} days after the visit.")
        if taken:
            self._progress(note=f"Visitor form answers: {taken} newcomer(s) now have them in the church database")

    def _delete_old_answers(self, summary, user_id):
        """Deletes the visitor form answers still here KEEP_ANSWERS_DAYS after the visit: the church database
        never took them. A newcomer still waiting to be added keeps theirs, to send when they are."""
        cutoff = (now() - dt.timedelta(days=KEEP_ANSWERS_DAYS)).isoformat(sep=" ")
        rows = self.db.all("SELECT id, first_name, last_name, visitor_details FROM person "
                           "WHERE visitor_details IS NOT NULL AND COALESCE(created_at,'') < ? ORDER BY id", (cutoff,))
        if not rows:
            return
        waiting = {r["id"] for r in newcomers(self.db)}
        with self.db.tx() as conn:
            for row in rows:
                if row["id"] in waiting:
                    continue
                names = list(_answers(row["visitor_details"]))
                conn.execute("UPDATE person SET visitor_details=NULL WHERE id=?", (row["id"],))
                audit(conn, "breeze.visitor_answers_deleted", "person", row["id"], {"fields": names}, user_id)
                if names:
                    who = " ".join(filter(None, [row["first_name"], row["last_name"]]))
                    summary["problems"].append(
                        f"Check-in deleted {who}'s visitor form answers ({', '.join(names)}) {KEEP_ANSWERS_DAYS} "
                        "days after the visit; the church database didn't take them.")

    def _families(self, client, summary):
        """Put each household's newcomers into one family in the church database: with the family member who
        was there already (families/add), or together (families/create). Their links stay in family_link until
        the church database has answered, so a sync that stops halfway sends them next time, and while anyone in
        the household is still waiting to be added, so one added at a later sync joins the family too (joined:
        already in it). A refusal is shown as a problem and not tried again."""
        rows = self.db.all(
            "SELECT f.household_id, f.breeze_id, f.added, f.joined, p.first_name, p.last_name, "
            "COALESCE(p.anonymised, 1) AS gone FROM family_link f LEFT JOIN person p ON p.id=f.person_id "
            "ORDER BY f.household_id, p.is_child, f.person_id")
        households = {}
        for row in rows:
            households.setdefault(row["household_id"], []).append(row)
        for household_id, members in households.items():
            members = [m for m in members if not m["gone"]]
            joined = {m["breeze_id"] for m in members if m["joined"]}
            new = list(dict.fromkeys(m["breeze_id"] for m in members if m["added"]))
            existing = [m["breeze_id"] for m in members if not m["added"] and m["breeze_id"] not in new]
            # The family to join: an existing record's (one already joined first, else one just linked, which
            # every newcomer moves to), else that of a newcomer added at an earlier sync. With none, the
            # newcomers added together make one.
            target, moving = None, [b for b in new if b not in joined]
            if any(b in joined for b in existing):
                target = [b for b in existing if b in joined][-1]
            elif existing:
                target, moving = existing[-1], new
            elif joined.intersection(new):
                target = next(b for b in new if b in joined)
            try:
                if target and moving:
                    client.get("families/add", people_ids_json=moving, target_person_id=target)
                elif not target and len(moving) > 1:
                    client.get("families/create", people_ids_json=moving)
            except breeze.BreezeRefused as err:
                names = ", ".join(" ".join(filter(None, [m["first_name"], m["last_name"]])) for m in members)
                summary["problems"].append(f"{names} weren't put into one family in the church database. "
                                           f"It said: {_said(err)} Put them in one family there by hand.")
                self._exec("DELETE FROM family_link WHERE household_id=?", (household_id,))
                continue
            with self.db.tx() as conn:
                if conn.execute(f"SELECT 1 FROM person WHERE {WAITING} AND id IN (SELECT person_id FROM "
                                "household_member WHERE household_id=?)", (household_id,)).fetchone():
                    conn.execute("UPDATE family_link SET joined=1 WHERE household_id=?", (household_id,))
                else:
                    conn.execute("DELETE FROM family_link WHERE household_id=?", (household_id,))
