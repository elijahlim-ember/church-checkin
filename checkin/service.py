"""Sunday operations: sessions, search, households, check-in, check-out, undo, reprint, visitors."""

import datetime as dt
from contextlib import nullcontext
import json
import logging
import re
import secrets

from . import visitor_form
from .db import audit
from .roster import match_class_rule
from .util import (CODE_ALPHABET, SGT, age_on, clip, mask_phone, name_pattern, new_security_code,
                   normalize_name, normalize_phone, normalize_school_year, now_iso, phone_is_plausible, search_name,
                   sg_now, today_iso)

TXN_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
log = logging.getLogger("checkin")


def _parse_sgt_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    return parsed.replace(tzinfo=SGT) if parsed.tzinfo is None else parsed.astimezone(SGT)


def _after_report(closed_at: str, built_at: str) -> bool:
    try:
        return _parse_sgt_iso(closed_at) > _parse_sgt_iso(built_at)
    except (TypeError, ValueError):
        return False


class ServiceError(Exception):
    def __init__(self, message, status=400, data=None):
        super().__init__(message)
        self.status = status
        self.data = data


def display_name(person: dict) -> str:
    return (person.get("preferred_name") or person.get("first_name") or "").strip()


def full_name(person: dict) -> str:
    return f"{display_name(person)} {person.get('last_name') or ''}".strip()


def household_title(household: dict, members: list[dict]) -> str:
    """'Tan family' when the family shares a surname; otherwise the members' names, e.g. 'Michael & Grace'.

    Built from active members only, so people left out of check-in (for example the deceased) never show.
    """
    surnames = {m.get("last_name") for m in members if m.get("last_name")}
    name = household.get("name") or ""
    if name and name in surnames:
        return f"{name} family" if len(members) > 1 else full_name(members[0])
    names = [display_name(m) for m in members]
    if not names:
        return name or "Family"
    return " & ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")


class CheckinService:
    def __init__(self, db, printers):
        self.db = db
        self.printers = printers

    # ---- sessions -----------------------------------------------------------
    def ensure_today_sessions(self) -> None:
        """Open today's sessions for every service scheduled on this weekday."""
        today = dt.date.today()
        services = self.db.all("SELECT * FROM service WHERE active=1 AND weekday=?", (today.weekday(),))
        missing = [s for s in services if not self.db.one(
            "SELECT id FROM session WHERE service_id=? AND date=?", (s["id"], today.isoformat()))]
        for service in missing:
            self.open_session(service["id"], auto=True)

    def open_session(self, service_id: int, user_id=None, station_id=None, auto=False) -> dict:
        service = self.db.one("SELECT * FROM service WHERE id=? AND active=1", (service_id,))
        if not service:
            raise ServiceError("Service not found.", 404)
        today = today_iso()
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM session WHERE service_id=? AND date=?", (service_id, today)).fetchone()
            if row:
                if row["status"] != "open":
                    conn.execute("UPDATE session SET status='open', closed_at=NULL WHERE id=?", (row["id"],))
                    audit(conn, "session.reopen", "session", row["id"], None, user_id, station_id)
                session_id = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO session(service_id, date, name, start_time, breeze_event_name, status, opened_at) "
                    "VALUES(?,?,?,?,?, 'open', ?)",
                    (service_id, today, service["name"], service["start_time"], service["breeze_event_name"],
                     now_iso()))
                session_id = cur.lastrowid
                audit(conn, "session.open_auto" if auto else "session.open", "session", session_id,
                      {"service": service["name"], "date": today}, user_id, station_id)
        self.db.bump()
        return self.db.one("SELECT * FROM session WHERE id=?", (session_id,))

    def close_session(self, session_id: int, user_id=None, auto: bool = False) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE session SET status='closed', closed_at=? WHERE id=?",
                         (sg_now().isoformat(sep=" "), session_id))
            audit(conn, "session.close_auto" if auto else "session.close", "session", session_id, None, user_id)
        self.db.bump()

    def build_sunday_report(self, on_date: str, *, late: bool = False, clock=sg_now) -> dict:
        day = dt.date.fromisoformat(on_date)
        if day.weekday() != 6:
            raise ValueError("Sunday reports can only be built for Sundays.")
        sessions = self.db.all(
            "SELECT s.*, v.chms_service_id, v.weekday FROM session s LEFT JOIN service v ON v.id=s.service_id "
            "WHERE s.date=? AND (v.weekday=6 OR ?=6) ORDER BY s.start_time, s.id", (on_date, day.weekday()))
        services = []
        session_ids = [row["id"] for row in sessions]
        for session in sessions:
            attendees = self.db.all(
                "SELECT a.person_id, a.status, a.class_name, p.is_child, p.source, p.active, p.breeze_id, "
                "(SELECT MIN(s2.date) FROM attendance a2 JOIN session s2 ON s2.id=a2.session_id "
                "WHERE a2.person_id=a.person_id AND a2.status<>'undone') AS first_date "
                "FROM attendance a JOIN person p ON p.id=a.person_id WHERE a.session_id=?", (session["id"],))
            current = [row for row in attendees if row["status"] != "undone"]
            by_class = {}
            for row in current:
                key = row["class_name"] or "Adults"
                by_class[key] = by_class.get(key, 0) + 1
            services.append({
                "service_name": session["name"],
                "chms_service_id": int(session["chms_service_id"]) if session["chms_service_id"] and str(session["chms_service_id"]).isdigit() else None,
                "start_time": session["start_time"],
                "checked_in": len({r["person_id"] for r in current}),
                "adults": len({r["person_id"] for r in current if not r["is_child"]}),
                "children": len({r["person_id"] for r in current if r["is_child"]}),
                "visitors": len({r["person_id"] for r in current if r["source"] == "visitor"}),
                "first_timers": len({r["person_id"] for r in current if r["source"] == "visitor" and r["first_date"] == on_date}),
                "undone": sum(1 for r in attendees if r["status"] == "undone"),
                "by_class": by_class,
            })
        ids = tuple(session_ids)
        placeholders = ",".join("?" for _ in ids) or "NULL"
        unsent = self.db.scalar(
            f"SELECT COUNT(*) FROM attendance a JOIN session s ON s.id=a.session_id WHERE s.date=? "
            f"AND a.breeze_sent_at IS NULL AND a.status<>'undone' AND a.session_id IN ({placeholders})",
            (on_date, *ids)) if ids else 0
        waiting = self.db.scalar(
            f"SELECT COUNT(DISTINCT p.id) FROM attendance a JOIN session s ON s.id=a.session_id "
            f"JOIN person p ON p.id=a.person_id WHERE s.date=? AND a.status<>'undone' AND p.source='visitor' "
            f"AND p.active=1 AND p.breeze_id IS NULL AND a.session_id IN ({placeholders})", (on_date, *ids)) if ids else 0
        closed = self.db.scalar(f"SELECT COUNT(*) FROM session WHERE date=? AND status='closed' AND id IN ({placeholders})",
                                (on_date, *ids)) if ids else 0
        total_people = sum(item["checked_in"] for item in services)
        return {"date": on_date, "built_at": clock().astimezone(SGT).replace(microsecond=0).isoformat(),
                "late": bool(late), "sessions_closed": closed, "unsent_attendance": unsent,
                "newcomers_waiting": waiting, "services": services}

    def wrap_up_sunday(self, on_date: str | None = None, *, breeze=None, clock=sg_now,
                       restore_lock=None) -> dict | None:
        now = clock().astimezone(SGT)
        if on_date is None:
            on_date = now.date().isoformat()
        day = dt.date.fromisoformat(on_date)
        if day.weekday() != 6:
            return None
        late = on_date < now.date().isoformat()
        due = late or (on_date == now.date().isoformat() and (now.hour, now.minute) >= (23, 59))
        if not due:
            return None
        with (restore_lock or nullcontext()):
            sessions = self.db.all("SELECT s.id, s.status, s.closed_at FROM session s LEFT JOIN service v ON v.id=s.service_id "
                                   "WHERE s.date=? AND (v.weekday=6 OR ?=6)", (on_date, day.weekday()))
            if not sessions:
                return None
            previous = self.db.one("SELECT * FROM day_report WHERE date=?", (on_date,))
            rebuilt_since = previous and any(s["closed_at"] and _after_report(s["closed_at"], previous["built_at"])
                                             for s in sessions)
            open_ids = [s["id"] for s in sessions if s["status"] == "open"]
            if not open_ids and previous and not rebuilt_since:
                return None
        if breeze is not None and breeze.connected():
            try:
                breeze.auto_tick(wait=True)
            except Exception:
                pass
        with (restore_lock or nullcontext()):
            # Re-read under the restore lock; a restore may have replaced the database since the first check.
            sessions = self.db.all("SELECT s.id, s.status, s.closed_at FROM session s LEFT JOIN service v ON v.id=s.service_id "
                                   "WHERE s.date=? AND (v.weekday=6 OR ?=6)", (on_date, day.weekday()))
            if not sessions:
                return None
            previous = self.db.one("SELECT * FROM day_report WHERE date=?", (on_date,))
            rebuilt_since = previous and any(s["closed_at"] and _after_report(s["closed_at"], previous["built_at"])
                                             for s in sessions)
            open_ids = [s["id"] for s in sessions if s["status"] == "open"]
            if not open_ids and previous and not rebuilt_since:
                return None
            for session_id in open_ids:
                self.close_session(session_id, auto=True)
            payload = self.build_sunday_report(on_date, late=late, clock=clock)
            previous_attempts = previous["attempts"] if previous else 0
            with self.db.tx() as conn:
                conn.execute("INSERT INTO day_report(date,built_at,payload,late,delivered_at,delivery_error,attempts,next_try) "
                             "VALUES(?,?,?,?,NULL,NULL,?,NULL) ON CONFLICT(date) DO UPDATE SET "
                             "built_at=excluded.built_at,payload=excluded.payload,late=excluded.late,delivered_at=NULL,"
                             "delivery_error=NULL,attempts=excluded.attempts,next_try=NULL",
                             (on_date, payload["built_at"], json.dumps(payload, ensure_ascii=False), int(late), previous_attempts))
        delivered = False
        if breeze is not None and breeze.connected():
            delivered = bool(breeze.deliver_pending_reports(now=clock()))
        log_line_count = sum(s["checked_in"] for s in payload["services"])
        log.info("wrap-up %s: closed %d session(s), %d people, delivered=%s",
                 on_date, len(open_ids), log_line_count, delivered)
        return payload

    def today_sessions(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM session WHERE date=? ORDER BY start_time, id", (today_iso(),))
        for row in rows:
            row["checked_in"] = self.db.scalar(
                "SELECT COUNT(*) FROM attendance WHERE session_id=? AND status<>'undone'", (row["id"],))
        return rows

    def default_session(self, sessions: list[dict]) -> dict | None:
        open_sessions = [s for s in sessions if s["status"] == "open"]
        if not open_sessions:
            return None
        current = dt.datetime.now().strftime("%H:%M")
        started = [s for s in open_sessions if (s["start_time"] or "00:00") <= _add_minutes(current, 45)]
        return started[-1] if started else open_sessions[0]

    def _session(self, conn, session_id) -> dict:
        row = conn.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ServiceError("Session not found. Choose today's service again.", 404)
        if row["status"] != "open" or row["date"] != today_iso():
            raise ServiceError("This session is closed. Choose today's service again.", 409)
        return dict(row)

    # ---- search --------------------------------------------------------------
    def search(self, query: str, limit: int = 20) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        compact = re.sub(r"[\s+().-]", "", query)
        country = self.db.get_setting("country_code") or "65"
        if compact.isdigit():
            digits = normalize_phone(compact, country) if len(compact) > 4 else compact
            if len(digits) < 4:
                return []
            if len(digits) == 4:
                rows = self.db.all(
                    "SELECT DISTINCT m.household_id AS hid, ph.digits, p.id AS pid, p.is_child FROM phone ph "
                    "JOIN person p ON p.id=ph.person_id AND p.active=1 "
                    "JOIN household_member m ON m.person_id=p.id AND m.active=1 "
                    "WHERE ph.last4=? ORDER BY ph.id", (digits,))
            else:
                rows = self.db.all(
                    "SELECT DISTINCT m.household_id AS hid, ph.digits, p.id AS pid, p.is_child FROM phone ph "
                    "JOIN person p ON p.id=ph.person_id AND p.active=1 "
                    "JOIN household_member m ON m.person_id=p.id AND m.active=1 "
                    "WHERE ph.digits LIKE ? ORDER BY ph.id", (f"%{digits}%",))
            matched_phone, matched = {}, {}
            for row in rows:
                matched_phone.setdefault(row["hid"], row["digits"])
                matched.setdefault(row["hid"], []).append(row)
            # A child often carries a parent's number: the adult who owns it is the one checking in.
            for hid, people in matched.items():
                adults = [r for r in people if not r["is_child"]]
                matched[hid] = list(dict.fromkeys(r["pid"] for r in (adults or people)))
            return self._household_summaries(list(matched_phone)[:limit], matched_phone, matched)
        tokens = normalize_name(query).split()
        if not tokens:
            return []
        clauses = " AND ".join("(' ' || p.search_name) LIKE ?" for _ in tokens)
        params = [name_pattern(t) for t in tokens]
        rows = self.db.all(
            f"SELECT DISTINCT m.household_id AS hid, p.id AS pid FROM person p "
            f"JOIN household_member m ON m.person_id=p.id AND m.active=1 "
            f"WHERE p.active=1 AND {clauses} LIMIT ?", (*params, limit * 4))
        matched = {}
        for row in rows:
            matched.setdefault(row["hid"], []).append(row["pid"])
        return self._household_summaries(list(matched)[:limit], {}, matched)

    def _household_summaries(self, household_ids: list[int], matched_phone: dict, matched: dict) -> list[dict]:
        """One entry per household. `matched` holds the people the search found: in individual mode the
        kiosk ticks only them."""
        out = []
        for hid in household_ids:
            household = self.db.one("SELECT * FROM household WHERE id=? AND active=1", (hid,))
            if not household:
                continue
            members = self._members(hid)
            guardians = [m for m in members if not m["is_child"]]
            children = [m for m in members if m["is_child"]]
            phone = matched_phone.get(hid)
            if not phone:
                phone = self.db.scalar(
                    "SELECT ph.digits FROM phone ph JOIN household_member m ON m.person_id=ph.person_id "
                    "JOIN person p ON p.id=ph.person_id WHERE m.household_id=? ORDER BY p.is_child, ph.id LIMIT 1",
                    (hid,))
            if not members:
                continue
            out.append({
                "id": hid,
                "name": household["name"],
                "title": household_title(household, members),
                "visitor": household["source"] == "visitor",
                "guardians": [full_name(g) for g in guardians],
                "children": [display_name(c) for c in children],
                "members": [display_name(m) for m in members],
                "phone": mask_phone(phone or ""),
                "matched": [pid for pid in matched.get(hid, []) if any(m["id"] == pid for m in members)],
            })
        out.sort(key=lambda h: (h["title"].lower(), h["id"]))
        return out

    def _members(self, household_id: int) -> list[dict]:
        return self.db.all(
            "SELECT p.*, m.role FROM household_member m JOIN person p ON p.id=m.person_id "
            "WHERE m.household_id=? AND m.active=1 AND p.active=1 "
            "ORDER BY p.is_child, p.birthdate IS NULL, p.birthdate, p.first_name", (household_id,))

    # ---- classes -------------------------------------------------------------
    def classrooms(self) -> list[dict]:
        return self.db.all("SELECT * FROM classroom WHERE active=1 ORDER BY sort_order, name")

    def child_class(self, person: dict, classrooms: list[dict], on_date: str) -> dict | None:
        if person.get("classroom_id"):
            match = next((c for c in classrooms if c["id"] == person["classroom_id"]), None)
            if match:
                return match
        return match_class_rule(classrooms, person.get("birthdate"), person.get("school_year"), on_date)

    def everyone(self) -> bool:
        """True when the whole family checks in (adults too); False for children-only check-in."""
        return bool(self.db.get_setting("checkin_everyone"))

    def eligible(self, person: dict, room: dict | None, on_date: str) -> bool:
        if self.everyone():
            return True
        if not person["is_child"]:
            return False
        if room is not None or person.get("source") == "visitor":
            return True
        age = age_on(person.get("birthdate"), on_date)
        max_age = int(self.db.get_setting("child_max_age") or 12)
        return age is None or age[0] <= max_age

    # ---- household screen ----------------------------------------------------------
    def household(self, household_id: int, session_id: int | None) -> dict:
        household = self.db.one("SELECT * FROM household WHERE id=?", (household_id,))
        if not household:
            raise ServiceError("Family not found.", 404)
        on_date = today_iso()
        classrooms = self.classrooms()
        members = self._members(household_id)
        attendance = {}
        txns = []
        if session_id:
            for row in self.db.all(
                    "SELECT a.*, t.security_code FROM attendance a JOIN checkin_txn t ON t.id=a.txn_id "
                    "WHERE a.session_id=? AND a.status<>'undone' AND a.person_id IN "
                    "(SELECT person_id FROM household_member WHERE household_id=?)", (session_id, household_id)):
                attendance[row["person_id"]] = row
            for txn in self.db.all(
                    "SELECT * FROM checkin_txn WHERE session_id=? AND household_id=? AND status='committed' "
                    "ORDER BY created_at DESC", (session_id, household_id)):
                txns.append(self._txn_brief(txn))
        everyone = self.everyone()
        medical = bool(self.db.get_setting("medical_alerts"))
        people = []
        for person in members:
            room = self.child_class(person, classrooms, on_date) if person["is_child"] else None
            if not self.eligible(person, room, on_date):
                continue
            age = age_on(person.get("birthdate"), on_date)
            att = attendance.get(person["id"])
            status = "not_in"
            if att:
                status = "in" if att["status"] == "in" else "out"
            class_name = room["name"] if room else ("" if everyone or not person["is_child"] else "Unassigned")
            people.append({
                "id": person["id"],
                "name": display_name(person),
                "last_name": person["last_name"],
                "chinese_name": person.get("chinese_name") or "",
                "is_child": bool(person["is_child"]),
                "class": class_name,
                "room": room["room"] if room else "",
                "no_class": bool(person["is_child"]) and room is None and not everyone,
                "age": age[0] if age else None,
                "school_year": person.get("school_year") or "",
                "alert": medical and bool(person["medical_alert"]),
                "alert_note": clip(person.get("medical_note") or "", 60) if medical else "",
                "visitor": person["source"] == "visitor",
                "status": status,
                "attendance_id": att["id"] if att else None,
                "checked_in_at": att["checked_in_at"][11:16] if att else None,
                "checked_out_at": (att["checked_out_at"] or "")[11:16] if att else None,
                "code": att["security_code"] if att else None,
                "txn_id": att["txn_id"] if att else None,
            })
        guardians = [full_name(m) for m in members if not m["is_child"]]
        phones = self.db.all(
            "SELECT DISTINCT ph.digits FROM phone ph JOIN household_member m ON m.person_id=ph.person_id "
            "JOIN person p ON p.id=ph.person_id AND p.active=1 WHERE m.household_id=? AND m.active=1",
            (household_id,))
        return {
            "id": household_id,
            "name": household["name"],
            "title": household_title(household, members),
            "visitor": household["source"] == "visitor",
            "guardians": guardians,
            "phones": [mask_phone(p["digits"]) for p in phones],
            "people": people,
            "transactions": txns,
            "change_seq": self.db.change_seq,
        }

    def _txn_brief(self, txn: dict) -> dict:
        names = self.db.all(
            "SELECT p.first_name, p.preferred_name FROM attendance a JOIN person p ON p.id=a.person_id "
            "WHERE a.txn_id=? ORDER BY a.id", (txn["id"],))
        job = self.db.one("SELECT id, status, printer, error FROM print_job WHERE txn_id=? "
                          "ORDER BY created_at DESC LIMIT 1", (txn["id"],))
        return {
            "id": txn["id"],
            "code": txn["security_code"],
            "time": txn["created_at"][11:16],
            "status": txn["status"],
            "children": [display_name(n) for n in names],
            "reprints": txn["reprint_count"],
            "job": job,
        }

    # ---- check in ----------------------------------------------------------------
    def check_in(self, txn_id: str, session_id: int, household_id: int, person_ids: list[int], station: dict,
                 user_id=None) -> dict:
        if not TXN_ID_RE.match(txn_id or ""):
            raise ServiceError("Invalid transaction id.")
        person_ids = sorted({int(p) for p in person_ids or []})
        if not person_ids:
            raise ServiceError("Select at least one child.")
        classrooms = self.classrooms()
        on_date = today_iso()
        settings = self.db.settings()
        with self.db.tx() as conn:
            existing = conn.execute("SELECT * FROM checkin_txn WHERE id=?", (txn_id,)).fetchone()
            if existing:
                # Same transaction id again (double tap, network retry): return the original result.
                return self.txn_result(txn_id, replay=True)
            session = self._session(conn, session_id)
            members = {m["id"]: m for m in self._members(household_id)}
            everyone = self.everyone()
            children = []
            for pid in person_ids:
                person = members.get(pid)
                if not person or (not person["is_child"] and not everyone):
                    raise ServiceError("Someone selected is not part of this family. Search again.", 409)
                children.append(person)
            already = {r[0] for r in conn.execute(
                f"SELECT person_id FROM attendance WHERE session_id=? AND status<>'undone' "
                f"AND person_id IN ({','.join('?' * len(person_ids))})", (session_id, *person_ids))}
            todo = [c for c in children if c["id"] not in already]
            skipped = [display_name(c) for c in children if c["id"] in already]
            if not todo:
                raise ServiceError("Already checked in: " + ", ".join(skipped), 409)
            code = self._unique_code(conn, on_date)
            created = now_iso()
            conn.execute(
                "INSERT INTO checkin_txn(id, session_id, household_id, station_id, user_id, security_code, "
                "code_date, status, created_at) VALUES(?,?,?,?,?,?,?, 'committed', ?)",
                (txn_id, session_id, household_id, station["id"] if station else None, user_id, code, on_date,
                 created))
            checked = []
            for child in todo:
                room = self.child_class(child, classrooms, on_date) if child["is_child"] else None
                class_name = room["name"] if room else ("Unassigned" if child["is_child"] else None)
                conn.execute(
                    "INSERT INTO attendance(session_id, person_id, txn_id, classroom_id, class_name, status, "
                    "checked_in_at) VALUES(?,?,?,?,?, 'in', ?)",
                    (session_id, child["id"], txn_id, room["id"] if room else None, class_name, created))
                checked.append({"id": child["id"], "name": display_name(child), "class": class_name or ""})
            labels = self._labels(conn, txn_id, session, settings, which="all")
            self.printers.create_job(conn, labels, kind="checkin", txn_id=txn_id, station=station,
                                     user_id=user_id)
            audit(conn, "checkin", "checkin_txn", txn_id,
                  {"household_id": household_id, "children": [c["id"] for c in todo], "code": code,
                   "skipped": skipped, "session_id": session_id}, user_id, station["id"] if station else None)
        self.db.bump()
        self.printers.notify()
        result = self.txn_result(txn_id)
        result["skipped"] = skipped
        return result

    def _unique_code(self, conn, on_date: str) -> str:
        for length in (4, 4, 4, 4, 5, 5, 6):
            code = new_security_code(length)
            if not conn.execute("SELECT 1 FROM checkin_txn WHERE code_date=? AND security_code=?",
                                (on_date, code)).fetchone():
                return code
        raise ServiceError("Could not create a security code. Try again.", 500)

    def txn_result(self, txn_id: str, replay: bool = False) -> dict:
        txn = self.db.one("SELECT * FROM checkin_txn WHERE id=?", (txn_id,))
        if not txn:
            raise ServiceError("Check-in not found.", 404)
        children = self.db.all(
            "SELECT a.id AS attendance_id, a.status, a.class_name, p.id, p.first_name, p.preferred_name, "
            "p.last_name FROM attendance a JOIN person p ON p.id=a.person_id WHERE a.txn_id=? ORDER BY a.id",
            (txn_id,))
        job = self.db.one("SELECT * FROM print_job WHERE txn_id=? ORDER BY created_at DESC LIMIT 1", (txn_id,))
        return {
            "txn_id": txn_id,
            "replay": replay,
            "code": txn["security_code"],
            "status": txn["status"],
            "session_id": txn["session_id"],
            "household_id": txn["household_id"],
            "created_at": txn["created_at"],
            "children": [{"id": c["id"], "name": display_name(c), "class": c["class_name"],
                          "status": c["status"]} for c in children],
            "job": self.printers.job_view(job) if job else None,
        }

    def _labels(self, conn, txn_id: str, session: dict, settings: dict, which: str = "all",
                person_ids: list[int] | None = None) -> list[dict]:
        txn = conn.execute("SELECT * FROM checkin_txn WHERE id=?", (txn_id,)).fetchone()
        rows = conn.execute(
            "SELECT a.class_name, a.classroom_id, p.* FROM attendance a JOIN person p ON p.id=a.person_id "
            "WHERE a.txn_id=? AND a.status<>'undone' ORDER BY a.id", (txn_id,)).fetchall()
        rows = [dict(r) for r in rows]
        if person_ids:
            rows = [r for r in rows if r["id"] in person_ids]
        rooms = {c["id"]: c for c in self.classrooms()}
        day = dt.date.fromisoformat(session["date"])
        pickup = bool(settings.get("pickup_codes"))
        common = {
            "code": txn["security_code"] if pickup else "",
            "date": day.strftime("%a %d %b %Y"),
            "date_short": day.strftime("%d %b"),
            "time": txn["created_at"][11:16],
            "service": session["name"],
            "church": settings.get("church_name") or "",
        }
        medical = bool(settings.get("medical_alerts"))
        labels = []
        if which in ("all", "child"):
            for row in rows:
                age = age_on(row.get("birthdate"), session["date"])
                room = rooms.get(row["classroom_id"]) or {}
                try:
                    # Extra roster columns chosen at import, usable in templates as {x_column_name}.
                    extra = json.loads(row.get("extra") or "{}")
                except ValueError:
                    extra = {}
                data = dict(common, **extra, **{
                    "name": display_name(row),
                    "first_name": row["first_name"],
                    "last_name": row["last_name"],
                    "full_name": full_name(row),
                    "class": (row["class_name"] or "") if row["class_name"] != "Unassigned" else "",
                    "room": room.get("room") or "",
                    "age": f"Age {age[0]}" if age else "",
                    "school_year": row.get("school_year") or "",
                    "alert": "ALLERGY / MEDICAL" if (medical and row["medical_alert"]) else "",
                    "alert_note": clip(row.get("medical_note") or "", 40)
                    if (medical and row["medical_alert"] and settings.get("print_alert_note")) else "",
                    "visitor": "VISITOR" if row["source"] == "visitor" else "",
                })
                for _ in range(max(1, int(settings.get("child_labels_per_child") or 1))):
                    labels.append({"template": "name", "data": data})
        if which in ("all", "guardian") and rows and pickup:
            names = [display_name(r) for r in rows]
            data = dict(common, **{
                "children": ", ".join(names),
                "count": str(len(names)),
                "household": rows[0]["last_name"],
                "pickup_note": clip(next((r["pickup_note"] for r in rows if r.get("pickup_note")), "") or "", 50),
            })
            for _ in range(max(0, int(settings.get("guardian_labels_per_checkin") or 0))):
                labels.append({"template": "guardian", "data": data})
            if which == "guardian" and not labels:
                labels.append({"template": "guardian", "data": data})
        return labels

    # ---- reprint / retry / undo -------------------------------------------------------
    def reprint(self, txn_id: str, which: str, station: dict, printer: str | None = None, user_id=None,
                person_ids: list[int] | None = None) -> dict:
        if which not in ("all", "child", "guardian"):
            raise ServiceError("Choose which labels to reprint.")
        settings = self.db.settings()
        if which == "guardian" and not settings.get("pickup_codes"):
            raise ServiceError("Pickup tags are switched off.")
        with self.db.tx() as conn:
            txn = conn.execute("SELECT * FROM checkin_txn WHERE id=?", (txn_id,)).fetchone()
            if not txn or txn["status"] != "committed":
                raise ServiceError("That check-in was undone or not found.", 404)
            session = dict(conn.execute("SELECT * FROM session WHERE id=?", (txn["session_id"],)).fetchone())
            labels = self._labels(conn, txn_id, session, settings, which,
                                  [int(p) for p in person_ids] if person_ids else None)
            if not labels:
                raise ServiceError("No labels to print for that check-in.", 409)
            job = self.printers.create_job(conn, labels, kind="reprint", txn_id=txn_id, station=station,
                                           user_id=user_id, printer=printer)
            conn.execute("UPDATE checkin_txn SET reprint_count=reprint_count+1 WHERE id=?", (txn_id,))
            audit(conn, "reprint", "checkin_txn", txn_id, {"which": which, "job": job["id"], "people": person_ids,
                                                         "printer": job["printer"]},
                  user_id, station["id"] if station else None)
        self.printers.notify()
        return self.printers.job_view(self.db.one("SELECT * FROM print_job WHERE id=?", (job["id"],)))

    def undo(self, txn_id: str, reason: str, station: dict, user_id=None) -> dict:
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise ServiceError("Please give a reason for the undo.")
        with self.db.tx() as conn:
            txn = conn.execute("SELECT * FROM checkin_txn WHERE id=?", (txn_id,)).fetchone()
            if not txn:
                raise ServiceError("Check-in not found.", 404)
            if txn["status"] == "undone":
                return {"txn_id": txn_id, "status": "undone", "already": True}
            if txn["code_date"] != today_iso():
                raise ServiceError("Only today's check-ins can be undone here. Ask an administrator.", 409)
            out = conn.execute("SELECT COUNT(*) FROM attendance WHERE txn_id=? AND status='out'", (txn_id,)).fetchone()[0]
            if out:
                raise ServiceError("A child in this check-in has already been checked out, so it cannot be undone.",
                                   409)
            ts = now_iso()
            conn.execute("UPDATE checkin_txn SET status='undone', undone_at=?, undone_by=?, undo_reason=? WHERE id=?",
                         (ts, user_id, reason, txn_id))
            conn.execute("UPDATE attendance SET status='undone', undone_at=? WHERE txn_id=? AND status='in'",
                         (ts, txn_id))
            cancelled = conn.execute(
                "UPDATE print_job SET status='cancelled', finished_at=?, error='Check-in undone' "
                "WHERE txn_id=? AND status='queued'", (ts, txn_id)).rowcount
            audit(conn, "checkin.undo", "checkin_txn", txn_id, {"reason": reason, "cancelled_jobs": cancelled},
                  user_id, station["id"] if station else None)
        self.db.bump()
        return {"txn_id": txn_id, "status": "undone"}

    # ---- check out ---------------------------------------------------------------
    def checkout_lookup(self, code: str) -> dict:
        code = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
        if len(code) < 3:
            raise ServiceError("Enter the code printed on the parent's pickup tag.")
        txns = self.db.all(
            "SELECT * FROM checkin_txn WHERE code_date=? AND security_code=? AND status='committed'",
            (today_iso(), code))
        if not txns:
            raise ServiceError("No check-in today with that code.", 404)
        txn = txns[0]
        result = self.txn_result(txn["id"])
        household = self.db.one("SELECT name FROM household WHERE id=?", (txn["household_id"],))
        result["household_name"] = household["name"] if household else ""
        result["children"] = [dict(c, attendance_id=a["id"], checked_out_at=(a["checked_out_at"] or "")[11:16])
                              for c, a in zip(result["children"], self.db.all(
                                  "SELECT id, checked_out_at FROM attendance WHERE txn_id=? ORDER BY id",
                                  (txn["id"],)))]
        return result

    def check_out(self, attendance_ids: list[int], station: dict, user_id=None) -> dict:
        ids = sorted({int(a) for a in attendance_ids or []})
        if not ids:
            raise ServiceError("Select at least one child to check out.")
        done, already = [], []
        with self.db.tx() as conn:
            ts = now_iso()
            for att_id in ids:
                row = conn.execute(
                    "SELECT a.*, p.first_name, p.preferred_name FROM attendance a JOIN person p ON p.id=a.person_id "
                    "WHERE a.id=?", (att_id,)).fetchone()
                if not row:
                    continue
                name = display_name(dict(row))
                if row["status"] == "out":
                    already.append(name)
                    continue
                if row["status"] != "in":
                    continue
                conn.execute("UPDATE attendance SET status='out', checked_out_at=?, checkout_user=?, "
                             "checkout_station=? WHERE id=?",
                             (ts, user_id, station["id"] if station else None, att_id))
                done.append(name)
                audit(conn, "checkout", "attendance", att_id, {"person_id": row["person_id"], "txn": row["txn_id"]},
                      user_id, station["id"] if station else None)
        self.db.bump()
        return {"checked_out": done, "already": already}

    # ---- visitors ------------------------------------------------------------------
    def register_visitor(self, form: dict, station: dict, user_id=None) -> dict:
        """Register a first-time family: the person filling the form plus anyone with them.

        form: guardian_name, phone, notes, pickup_name, members: [{first_name, last_name, is_child,
        birthdate, school_year, classroom_id, alert, alert_note}] ('children' is accepted as members
        who are all children). The person filling it in may also give a Chinese name, email, gender, age
        group, preferred language, what brings them and whether church may get in touch (visitor_form.py).
        """
        country = self.db.get_setting("country_code") or "65"
        guardian_name = (form.get("guardian_name") or "").strip()
        phone = normalize_phone(form.get("phone"), country)
        members = [dict(m) for m in form.get("members") or [] if (m.get("first_name") or "").strip()]
        members += [dict(c, is_child=True) for c in form.get("children") or [] if (c.get("first_name") or "").strip()]
        if not guardian_name:
            raise ServiceError("Enter your name.")
        if not phone_is_plausible(phone, country):
            raise ServiceError("Enter a valid mobile number.")
        if not members and not self.everyone():
            raise ServiceError("Add at least one child.")
        try:
            answers = visitor_form.read(form)
        except visitor_form.FormError as exc:
            raise ServiceError(str(exc)) from None
        if not form.get("force"):
            existing = self.search(phone)
            if existing:
                raise ServiceError("This mobile number is already registered.", 409, {"existing": existing})
        parts = guardian_name.split()
        g_first, g_last = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (parts[0], "")
        ts = now_iso()
        with self.db.tx() as conn:
            household_key = self._visitor_id(conn, "VIS-HH-", "household", "family_key")
            cur = conn.execute(
                "INSERT INTO household(family_key, name, source, active, created_at, updated_at) "
                "VALUES(?,?, 'visitor', 1, ?, ?)", (household_key, g_last or guardian_name, ts, ts))
            household_id = cur.lastrowid
            guardian_id = self._insert_visitor(conn, {
                "first_name": g_first, "last_name": g_last, "is_child": 0,
                "notes": (form.get("notes") or "").strip() or None, **answers}, ts)
            conn.execute("INSERT INTO household_member(household_id, person_id, role) VALUES(?,?, 'guardian')",
                         (household_id, guardian_id))
            conn.execute("INSERT INTO phone(person_id, digits, last4, kind) VALUES(?,?,?, 'mobile')",
                         (guardian_id, phone, phone[-4:]))
            pickup = (form.get("pickup_name") or "").strip()
            person_ids = [guardian_id]
            child_ids = []
            for member in members:
                is_child = bool(member.get("is_child"))
                note = (member.get("alert_note") or "").strip()
                class_id = member.get("classroom_id") or None
                person_id = self._insert_visitor(conn, {
                    "first_name": member["first_name"].strip(),
                    "preferred_name": (member.get("preferred_name") or "").strip() or None,
                    "last_name": (member.get("last_name") or "").strip() or g_last,
                    "is_child": 1 if is_child else 0,
                    "birthdate": member.get("birthdate") or None,
                    "school_year": normalize_school_year(member.get("school_year")) or None,
                    "sex": member.get("sex") or None,
                    "classroom_id": int(class_id) if class_id and is_child else None,
                    "class_source": "manual" if class_id and is_child else None,
                    "medical_alert": 1 if (member.get("alert") or note) else 0,
                    "medical_note": note or None,
                    "pickup_note": f"Pickup: {pickup}" if pickup and is_child else None,
                }, ts)
                conn.execute("INSERT INTO household_member(household_id, person_id, role) VALUES(?,?,?)",
                             (household_id, person_id, "child" if is_child else "guardian"))
                person_ids.append(person_id)
                if is_child:
                    child_ids.append(person_id)
            audit(conn, "visitor.register", "household", household_id,
                  {"people": len(person_ids), "visitor_key": household_key}, user_id,
                  station["id"] if station else None)
        self.db.bump()
        everyone = self.everyone()
        return {"household_id": household_id, "child_ids": child_ids,
                "person_ids": person_ids if everyone else child_ids}

    def _visitor_id(self, conn, prefix: str, table: str, column: str) -> str:
        while True:
            value = prefix + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if not conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (value,)).fetchone():
                return value

    def _insert_visitor(self, conn, values: dict, ts: str) -> int:
        values = dict(values)
        values["visitor_id"] = self._visitor_id(conn, "VIS-", "person", "visitor_id")
        values["source"] = "visitor"
        values["search_name"] = search_name(values.get("first_name"), values.get("preferred_name"),
                                            values.get("last_name"), values.get("chinese_name"))
        values["created_at"] = values["updated_at"] = ts
        cols = ", ".join(values)
        cur = conn.execute(f"INSERT INTO person({cols}) VALUES({', '.join('?' * len(values))})",
                           tuple(values.values()))
        return cur.lastrowid

    # ---- station helpers --------------------------------------------------------------
    def recent_transactions(self, station_id: int, limit: int = 12) -> list[dict]:
        rows = self.db.all(
            "SELECT t.*, h.name AS household_name FROM checkin_txn t LEFT JOIN household h ON h.id=t.household_id "
            "WHERE t.code_date=? AND (t.station_id=? OR ? IS NULL) ORDER BY t.created_at DESC LIMIT ?",
            (today_iso(), station_id, station_id, limit))
        out = []
        for row in rows:
            brief = self._txn_brief(row)
            brief["household"] = row["household_name"]
            out.append(brief)
        return out

    def room_counts(self, session_id: int) -> list[dict]:
        return self.db.all(
            "SELECT COALESCE(a.class_name, 'Adults') AS class_name, "  # no class: adults (children: 'Unassigned')
            "SUM(CASE WHEN status='in' THEN 1 ELSE 0 END) AS present, "
            "SUM(CASE WHEN status='out' THEN 1 ELSE 0 END) AS collected, COUNT(*) AS total "
            "FROM attendance a WHERE session_id=? AND status<>'undone' GROUP BY COALESCE(a.class_name, 'Adults') "
            "ORDER BY a.class_name IS NULL, 1",
            (session_id,))


def _add_minutes(hhmm: str, minutes: int) -> str:
    base = dt.datetime.strptime(hhmm, "%H:%M") + dt.timedelta(minutes=minutes)
    return base.strftime("%H:%M") if base.day == 1 else "23:59"
