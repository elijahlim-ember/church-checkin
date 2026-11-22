"""Sync with the church database against a fake connector on localhost. The connector answers the same API
Breeze ChMS did, so the fake still speaks Breeze's shapes (checked against some services before the move).

Run:  python -m unittest discover -s tests -v
"""

import json
import os

os.environ["CHECKIN_NO_USB"] = "1"
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkin import breeze  # noqa: E402
from checkin.util import new_id  # noqa: E402
from test_acceptance import make_app  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"
PHONE_FIELD, STATUS_FIELD = "1625788384", "941820589"
MAPPING = {"breeze_id": "Breeze ID", "first_name": "First Name", "preferred_name": "Nickname",
           "last_name": "Last Name", "family_id": "Family", "family_role": "Family Role", "mobile": "Mobile",
           "status": "Status"}


class FakeBreeze:
    """Just enough of Breeze's API, in the shapes the real one returns (checked against some services)."""

    def __init__(self):
        self.people, self.events, self.attendance, self.families, self.calls = [], [], {}, [], []
        self.services = None         # the church database's own services; None: an older database, no such call
        self.next_id = 90000000
        self.fail_next = []          # HTTP status codes to answer the next calls with
        self.stale_cache = True
        # A lost reply, like compatible service 1.9.0 sees it: (path, done) drops the connection with no reply, after
        # doing the call (done=True) or before it; (path, status) does the call, then answers that HTTP status.
        self.lose, self.error_after = [], []
        self.refusals = {}   # path, or (path, first name): the refusal to answer instead
        self.add_codes, self.erased_codes = {}, set()   # add_code -> the ID it added; codes of erased people
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.calls.append((url.path, q))
                if fake.fail_next:
                    self.send_response(fake.fail_next.pop(0))
                    self.end_headers()
                    return
                if self.headers.get("Api-Key") != KEY:
                    self.send_response(401)
                    self.end_headers()
                    return
                path = url.path.removeprefix("/api/").rstrip("/")
                lost = next((x for x in fake.lose if x[0] == path), None)
                if lost:
                    fake.lose.remove(lost)
                    if lost[1]:
                        fake.answer(path, q)
                    return                   # no reply at all: the connection just closes
                failed = next((x for x in fake.error_after if x[0] == path), None)
                if failed:
                    fake.error_after.remove(failed)
                    fake.answer(path, q)
                    self.send_response(failed[1])
                    self.end_headers()
                    return
                body = json.dumps(fake.answer(path, q)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def person(self, bid, first, last, phone=None, nick="", family=None, role="Adult", status="Member"):
        details = {"person_id": bid, STATUS_FIELD: {"value": "1", "name": status}}
        if phone:
            details[PHONE_FIELD] = [{"field_type": "phone", "phone_number": phone, "phone_type": "mobile",
                                     "do_not_text": "0", "is_private": "0"}]
        person = {"id": bid, "first_name": nick or first, "force_first_name": first, "last_name": last,
                  "nick_name": nick, "middle_name": "", "maiden_name": "", "path": "", "details": details,
                  "family": []}
        self.people.append(person)
        if family:
            person["_family"], person["_role"] = family, role
        return person

    def _families(self):
        groups = {}
        for p in self.people:
            if p.get("_family"):
                groups.setdefault(p["_family"], []).append(p)
        for fid, members in groups.items():
            entries = [{"id": "1", "oid": "1", "person_id": m["id"], "family_id": fid, "family_role_id": "3",
                        "role_name": m["_role"], "role_id": "3", "order": "1", "details": {}} for m in members]
            for m in members:
                m["family"] = entries

    def answer(self, path, q):
        refusal = self.refusals.get((path, q.get("first"))) or self.refusals.get(path)
        if refusal:
            return refusal
        if path == "profile":
            return [{"id": "1", "name": "Contact", "fields": [
                {"field_id": PHONE_FIELD, "field_type": "phone", "name": "Phone"},
                {"field_id": STATUS_FIELD, "field_type": "multiple_choice", "name": "Status", "options": []},
                {"field_id": "1798479180", "field_type": "paragraph", "name": "Long help text"}]}]
        if path == "people":
            self._families()
            return [{k: v for k, v in p.items() if not k.startswith("_")} for p in self.people]
        if path == "people/add":
            code = q.get("add_code")
            if code in self.erased_codes:
                return {"success": False, "errors": ["This newcomer was added before and has since been erased "
                                                     "in the church database."], "erased": True}
            if code in self.add_codes:   # the same code again: the person it added, not a second one
                bid = self.add_codes[code]
            else:
                self.next_id += 1
                bid = str(self.next_id)
                fields = json.loads(q.get("fields_json") or "[]")
                phone = next((f["details"]["phone_mobile"] for f in fields if f.get("field_type") == "phone"), None)
                self.person(bid, q["first"], q.get("last", ""), phone, status="Visitor")
                if code:
                    self.add_codes[code] = bid
            return [{"id": bid, "first_name": q["first"], "last_name": q.get("last", ""),
                     "details": {"person_id": bid}}]
        if path in ("families/create", "families/add"):
            self.families.append((path, json.loads(q["people_ids_json"]), q.get("target_person_id")))
            return {"success": True}
        if path == "events":
            return [e for e in self.events if q["start"] <= e["start_datetime"][:10] <= q["end"]]
        if path == "services" and self.services is not None:   # compatible service 1.12.0 or later
            return self.services
        if path == "events/attendance/list":
            if self.stale_cache and "_" not in q:
                return []                # like the real Breeze: a repeated address gets an old reply
            return [{"instance_id": q["instance_id"], "person_id": pid, "check_out": "0000-00-00 00:00:00",
                     "created_on": "x", "details": {"id": pid}}
                    for pid in sorted(self.attendance.get(q["instance_id"], set()))]
        if path == "events/attendance/add":
            marked = self.attendance.setdefault(q["instance_id"], set())
            if q["person_id"] in marked:
                return "duplicate"       # what the real Breeze answers
            marked.add(q["person_id"])
            return True
        if path == "events/attendance/delete":
            self.attendance.get(q["instance_id"], set()).discard(q["person_id"])
            return True
        return {"success": False, "errors": ["unknown endpoint " + path]}


class SyncBase(unittest.TestCase):
    """A check-in roster mirrored in a fake church database. `fake_class` picks which fake answers."""
    families = 40
    fake_class = FakeBreeze

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-breeze-")
        self.app = make_app(self.tmp, self.families)
        self.station = self.app.db.one("SELECT * FROM station")
        self.session = self.app.service.today_sessions()[0] if self.app.service.today_sessions() \
            else self.app.service.open_session(1)
        self.fake = getattr(self, "fake_class", FakeBreeze)()   # test_member_id borrows this setUp
        # Breeze mirrors the roster the sample import created.
        for p in self.app.db.all("SELECT p.*, h.family_key FROM person p LEFT JOIN household_member m "
                                 "ON m.person_id=p.id LEFT JOIN household h ON h.id=m.household_id "
                                 "WHERE p.breeze_id IS NOT NULL AND p.active=1"):
            phone = self.app.db.scalar("SELECT digits FROM phone WHERE person_id=?", (p["id"],))
            family = p["family_key"][2:] if (p["family_key"] or "").startswith("F:") else None
            self.fake.person(p["breeze_id"], p["first_name"], p["last_name"], f"+65 {phone}" if phone else None,
                             p["preferred_name"] or "", family, "Child" if p["is_child"] else "Adult")
        self.fake.events.append({"id": "300000001", "oid": "1", "event_id": "7", "name": "Sample Sunday Service",
                                 "category_id": "0", "settings_id": "1",
                                 "start_datetime": self.session["date"] + " 09:30:00",
                                 "end_datetime": self.session["date"] + " 11:30:00", "is_modified": "0",
                                 "created_on": "x"})
        self.app.db.set_setting("import_mapping", MAPPING)
        self.app.db.set_setting("import_rules", [])
        self.sync = self.app.breeze

    def tearDown(self):
        self.app.stop()
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_kind(self, kind, decisions=None):
        state = self.sync.start(kind, decisions=decisions, wait=True)
        self.assertEqual(state["state"], "done", state.get("error"))
        return state["result"]

    def family(self, index=0):
        row = self.app.db.one("SELECT h.id FROM household h JOIN household_member m ON m.household_id=h.id "
                              "JOIN person p ON p.id=m.person_id WHERE h.active=1 AND p.active=1 AND p.breeze_id "
                              "IS NOT NULL GROUP BY h.id ORDER BY COUNT(*) DESC, h.id LIMIT 1 OFFSET ?", (index,))
        return self.app.service.household(row["id"], self.session["id"])

    def checkin(self, hh, people=None):
        ids = [p["id"] for p in (people or hh["people"])]
        return self.app.service.check_in(new_id(), self.session["id"], hh["id"], ids, self.station)


class TestBreezeSync(SyncBase):
    def test_connect_rejects_a_wrong_key_and_keeps_the_key_private(self):
        with self.assertRaises(breeze.BreezeError) as err:
            self.sync.connect_chms(self.fake.url, "wrong-key-wrong-key")
        self.assertIn("external service", str(err.exception))
        self.assertFalse(self.sync.status()["connected"])
        self.sync.connect_chms(self.fake.url, KEY)
        status = self.sync.status()
        self.assertEqual((status["connected"], status["chms_url"]), (True, self.fake.url))
        self.assertNotIn(KEY, json.dumps(status))
        self.assertTrue(all(call[0] == "/api/profile" for call in self.fake.calls))
        self.assertEqual(breeze.load_key(self.app.paths, breeze.CHMS_KEY_FILE), KEY)
        backup = self.app.backups.create("test")["name"]
        self.assertTrue(backup)

    def test_weekly_sync_people_newcomers_and_attendance(self):
        self.sync.connect_chms(self.fake.url, KEY)
        first = self.run_kind("check")
        self.assertEqual(first["people"]["totals"]["people_new"], 0)
        self.run_kind("sync")                                     # first sync: same roster, nothing to send
        self.assertEqual(self.run_kind("check")["people"]["totals"]["people_updated"], 0)

        members = self.family(0)
        self.checkin(members)
        visitor = self.app.service.register_visitor({
            "guardian_name": "Nova Newcomer", "phone": "9000 0001",
            "members": [{"first_name": "Nia", "last_name": "Newcomer"}]}, self.station)
        self.checkin(self.app.service.household(visitor["household_id"], self.session["id"]))

        plan = self.run_kind("check")
        self.assertEqual([n["default"] for n in plan["newcomers"]], ["add", "add"])
        (day,) = plan["attendance"]
        self.assertIn("Sample Sunday Service", day["problem"])   # service still says "Sunday Service"
        self.assertEqual(day["breeze_events"], ["Sample Sunday Service"])
        self.assertEqual(len(self.fake.attendance), 0)             # a check writes nothing

        from checkin import server
        ctx = type("C", (), {"app": self.app, "body": {"session_id": self.session["id"],
                                                         "event_name": "Sample Sunday Service"}, "user_id": None})
        server.breeze_settings(ctx)
        (day,) = self.run_kind("check")["attendance"]
        self.assertEqual(day["instance_id"], "300000001")
        self.assertEqual(day["to_send"], len(members["people"]))
        self.assertEqual(day["waiting_newcomers"], 2)

        result = self.run_kind("sync")["summary"]
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["attendance_sent"], len(members["people"]) + 2)
        self.assertEqual(result["problems"], [])
        added = [c for c in self.fake.calls if c[0] == "/api/people/add"]
        self.assertEqual({c[1]["first"] for c in added}, {"Nova", "Nia"})
        self.assertIn("+65 9000 0001", "".join(c[1].get("fields_json", "") for c in added))
        self.assertEqual(self.fake.families[0][0], "families/create")
        self.assertEqual(len(self.fake.families[0][1]), 2)
        marked = self.fake.attendance["300000001"]
        self.assertEqual(len(marked), len(members["people"]) + 2)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM person WHERE source='visitor' AND breeze_id IS NULL"
                                            " AND active=1"), 0)

        again = self.run_kind("sync")["summary"]                   # running twice is harmless
        self.assertEqual((again["attendance_sent"], again["added"]), (0, 0))
        self.assertEqual(self.run_kind("check")["people"]["totals"]["people_new"], 0)  # newcomers now matched

        txn = self.app.db.scalar("SELECT txn_id FROM attendance WHERE person_id=?", (members["people"][0]["id"],))
        self.app.service.undo(txn, "Test check-in", self.station)
        removed = self.run_kind("sync")["summary"]
        self.assertEqual(removed["attendance_removed"], len(members["people"]))
        self.assertEqual(len(self.fake.attendance["300000001"]), 2)

    def test_attendance_already_in_breeze_is_not_counted_twice(self):
        self.sync.connect_chms(self.fake.url, KEY)
        self.app.db.set_setting("import_rules", [])
        with self.app.db.tx() as conn:
            conn.execute("UPDATE session SET breeze_event_name='Sample Sunday Service'")
        members = self.family(0)
        self.checkin(members)
        first = self.app.db.scalar("SELECT breeze_id FROM person WHERE id=?", (members["people"][0]["id"],))
        self.fake.attendance["300000001"] = {first}               # already marked by hand in Breeze
        self.fake.stale_cache = True
        result = self.run_kind("sync")["summary"]
        self.assertEqual(result["attendance_already"], 1)
        self.assertEqual(result["attendance_sent"], len(members["people"]) - 1)
        self.assertEqual(len(self.fake.attendance["300000001"]), len(members["people"]))

    def test_undo_removes_attendance_from_the_event_it_was_sent_to(self):
        self.sync.connect_chms(self.fake.url, KEY)
        self.app.db.set_setting("import_rules", [])
        self.fake.events.append(dict(self.fake.events[0], id="300000002", event_id="8", name="Sample Youth Service"))
        with self.app.db.tx() as conn:
            conn.execute("UPDATE session SET breeze_event_name='Sample Sunday Service'")
        members = self.family(0)
        self.checkin(members)
        self.run_kind("sync")
        sent = len(members["people"])
        self.assertEqual(len(self.fake.attendance["300000001"]), sent)
        with self.app.db.tx() as conn:                             # the service is then mapped to another event
            conn.execute("UPDATE session SET breeze_event_name='Sample Youth Service'")
        txn = self.app.db.scalar("SELECT txn_id FROM attendance WHERE person_id=?", (members["people"][0]["id"],))
        self.app.service.undo(txn, "Test check-in", self.station)
        removed = self.run_kind("sync")["summary"]
        self.assertEqual(removed["attendance_removed"], sent)
        self.assertEqual(self.fake.attendance["300000001"], set())  # taken off the event it was sent to
        self.assertFalse(self.fake.attendance.get("300000002"))

    def test_newcomer_who_is_already_in_breeze_is_linked_not_duplicated(self):
        self.sync.connect_chms(self.fake.url, KEY)
        self.app.db.set_setting("import_rules", [])
        self.run_kind("sync")
        existing = self.fake.people[0]
        phone = existing["details"][PHONE_FIELD][0]["phone_number"]
        visitor = self.app.service.register_visitor({
            "guardian_name": f"{existing['force_first_name']} {existing['last_name']}", "phone": phone,
            "force": True}, self.station)
        self.checkin(self.app.service.household(visitor["household_id"], self.session["id"]))
        plan = self.run_kind("check")
        (newcomer,) = plan["newcomers"]
        self.assertEqual(newcomer["default"], "link:" + existing["id"])
        result = self.run_kind("sync")["summary"]
        self.assertEqual((result["linked"], result["added"]), (1, 0))
        self.assertFalse([c for c in self.fake.calls if c[0] == "/api/people/add"])
        self.assertEqual(self.app.db.scalar("SELECT active FROM person WHERE id=?", (visitor["person_ids"][0],)), 0)
        moved = self.app.db.one("SELECT p.breeze_id FROM attendance a JOIN person p ON p.id=a.person_id "
                                "WHERE a.session_id=? AND a.status<>'undone'", (self.session["id"],))
        self.assertEqual(moved["breeze_id"], existing["id"])

    def test_unclear_newcomer_must_be_chosen(self):
        self.sync.connect_chms(self.fake.url, KEY)
        self.run_kind("sync")
        existing = self.fake.people[0]
        self.app.service.register_visitor({"guardian_name": f"{existing['force_first_name']} {existing['last_name']}",
                                           "phone": "9876 5432"}, self.station)
        (newcomer,) = self.run_kind("check")["newcomers"]
        self.assertEqual(newcomer["default"], "")                 # same name, different mobile: ask
        self.assertTrue(newcomer["candidates"][0]["same_name"])
        result = self.run_kind("sync")["summary"]                 # no choice made: left for next time
        self.assertEqual((result["linked"], result["added"]), (0, 0))

    def test_partial_reply_never_deactivates_people(self):
        self.sync.connect_chms(self.fake.url, KEY)
        del self.fake.people[5:]
        plan = self.run_kind("check")
        self.assertFalse(plan["people"]["deactivate"])
        self.assertIn("aren't in the church database", plan["people"]["warning"])
        before = self.app.db.scalar("SELECT COUNT(*) FROM person WHERE active=1")
        self.run_kind("sync")
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM person WHERE active=1"), before)

    def test_phone_numbers_sent_to_breeze(self):
        from checkin.breeze_sync import _phone_for_breeze
        self.assertEqual(_phone_for_breeze("91234567", "65"), "+65 9123 4567")
        self.assertEqual(_phone_for_breeze("61412345678", "65"), "+61412345678")   # overseas keeps its code

    def test_slows_down_when_told_too_many_requests(self):
        waits = []
        client = breeze.BreezeClient("chms", KEY, base_url=self.fake.url, min_interval=0, sleep=waits.append)
        self.fake.fail_next = [429]
        self.assertEqual(client.profile_fields()[0]["name"], "Contact")
        self.assertGreaterEqual(client.min_interval, 3.5)
        self.assertIn(30, waits)
        self.fake.fail_next = [500]
        with self.assertRaises(breeze.BreezeError):
            client.profile_fields()


class TestNewcomersAddedOnce(SyncBase):
    """A newcomer goes into the church database once, even when its reply is lost (the add code, compatible service
    1.9.0), and a household's family links survive a sync that stops halfway."""

    def setUp(self):
        super().setUp()
        self.sync.connect_chms(self.fake.url, KEY)
        self.sync.client_factory = lambda: breeze.BreezeClient.from_app(   # tries again without the pauses
            self.app.db, self.app.paths, sleep=lambda seconds: None)
        self.run_kind("sync")                         # the roster first: only the newcomers below are new
        with self.app.db.tx() as conn:
            conn.execute("UPDATE session SET breeze_event_name='Sample Sunday Service'")

    def newcomer(self, name, phone, members=(), **answers):
        visitor = self.app.service.register_visitor(
            dict(answers, guardian_name=name, phone=phone, members=list(members)), self.station)
        self.checkin(self.app.service.household(visitor["household_id"], self.session["id"]))
        return visitor

    def adds(self):
        return [q for path, q in self.fake.calls if path == "/api/people/add"]

    def in_fake(self, first):
        return [p for p in self.fake.people if p["force_first_name"] == first]

    def person(self, person_id):
        return self.app.db.one("SELECT * FROM person WHERE id=?", (person_id,))

    def test_a_lost_reply_is_tried_again_and_gets_the_same_person(self):
        pid = self.newcomer("Nova Newcomer", "9000 0001")["person_ids"][0]
        self.fake.lose = [("people/add", True)]                 # added, but the reply never arrives
        summary = self.run_kind("sync")["summary"]
        self.assertEqual((summary["added"], summary["problems"]), (1, []))
        first, again = self.adds()
        self.assertEqual(len(first["add_code"]), 32)
        self.assertEqual(again["add_code"], first["add_code"])   # the same code on the second try
        (added,) = self.in_fake("Nova")                          # one person, not two
        self.assertEqual((self.person(pid)["breeze_id"], self.person(pid)["add_code"]), (added["id"], None))
        self.assertEqual(self.fake.attendance["300000001"], {added["id"]})

    def test_a_sync_that_stopped_after_an_add_sends_the_same_code_next_time(self):
        pid = self.newcomer("Nova Newcomer", "9000 0001")["person_ids"][0]
        self.fake.error_after = [("people/add", 500)]           # added, then an error: this sync stops
        state = self.sync.start("sync", wait=True)
        self.assertEqual(state["state"], "error")
        code = self.person(pid)["add_code"]
        self.assertEqual(self.adds()[0]["add_code"], code)      # saved before the call
        (added,) = self.in_fake("Nova")
        (item,) = self.run_kind("check")["newcomers"]
        self.assertEqual(item["default"], "link:" + added["id"])   # same name and mobile: linked by default
        summary = self.run_kind("sync", {str(pid): "add"})["summary"]   # and even "add" doesn't duplicate
        self.assertEqual((summary["added"], summary["problems"]), (1, []))
        self.assertEqual(self.adds()[1]["add_code"], code)
        self.assertEqual(len(self.in_fake("Nova")), 1)
        self.assertEqual(self.fake.attendance["300000001"], {added["id"]})

    def second_sync(self):
        """A sync from a second process on this data folder (run.py sync --run while the automatic sync runs).
        It plans the newcomers, then this App's sync runs from start to end before it goes on."""
        from checkin.app import App
        other = App(self.tmp)
        self.addCleanup(other.db.close)
        other.breeze.client_factory = lambda: breeze.BreezeClient.from_app(
            other.db, other.paths, sleep=lambda seconds: None)
        progress = other.breeze._progress

        def first_sync_meanwhile(*args, **kwargs):
            if args[:1] == ("Newcomers",):
                self.run_kind("sync")
            progress(*args, **kwargs)
        other.breeze._progress = first_sync_meanwhile
        state = other.breeze.start("sync", wait=True)
        self.assertEqual(state["state"], "done", state.get("error"))
        return state["result"]["summary"]

    def test_two_syncs_at_once_add_a_newcomer_once(self):
        pid = self.newcomer("Nova Newcomer", "9000 0001")["person_ids"][0]
        summary = self.second_sync()                             # it planned to add Nova too
        (added,) = self.in_fake("Nova")                          # one person, not two
        self.assertEqual(len(self.adds()), 1)
        self.assertEqual((summary["added"], summary["problems"]), (0, []))
        self.assertEqual(self.person(pid)["breeze_id"], added["id"])
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='breeze.newcomer_added'"), 1)

    def test_two_syncs_at_once_link_a_newcomer_once(self):
        existing = self.fake.people[0]
        phone = existing["details"][PHONE_FIELD][0]["phone_number"]
        pid = self.newcomer(f"{existing['force_first_name']} {existing['last_name']}", phone,
                            force=True)["person_ids"][0]
        summary = self.second_sync()                             # it planned to link them too
        self.assertEqual((summary["linked"], summary["problems"]), (0, []))
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='breeze.newcomer_linked'"), 1)
        self.assertEqual(self.person(pid)["notes"].count("Linked to"), 1)
        self.assertFalse(self.adds())

    def test_a_refused_newcomer_does_not_stop_the_others(self):
        refused = self.newcomer("Rhea Refused", "9000 0002")["person_ids"][0]
        self.newcomer("Wren Welcome", "9000 0003")
        self.fake.refusals[("people/add", "Rhea")] = {"success": False, "errors": ["Not this one just now."]}
        summary = self.run_kind("sync")["summary"]
        self.assertEqual((summary["added"], summary["attendance_sent"]), (1, 1))
        self.assertEqual(len(self.in_fake("Wren")), 1)
        self.assertIn("Rhea Refused wasn't added to the church database. It said: Not this one just now.",
                      summary["problems"])
        code = self.person(refused)["add_code"]
        self.assertTrue(code)                                    # kept for the next try
        del self.fake.refusals[("people/add", "Rhea")]
        again = self.run_kind("sync")["summary"]
        self.assertEqual((again["added"], again["attendance_sent"], again["problems"]), (1, 1, []))
        self.assertEqual(self.adds()[-1]["add_code"], code)

    def test_a_newcomer_erased_since_their_add_is_never_added_again(self):
        pid = self.newcomer("Esme Erased", "9000 0004", preferred_language="en", visit_reason="new")["person_ids"][0]
        self.assertIsNotNone(self.person(pid)["visitor_details"])
        self.fake.error_after = [("people/add", 500)]           # added, but check-in never heard
        self.assertEqual(self.sync.start("sync", wait=True)["state"], "error")
        # The church database erases them (they asked to be forgotten) before the next sync.
        self.fake.people = [p for p in self.fake.people if p["force_first_name"] != "Esme"]
        self.fake.erased_codes.add(self.person(pid)["add_code"])
        summary = self.run_kind("sync")["summary"]
        (line,) = summary["problems"]                            # and no "waiting to be added" line
        visitor_id = self.person(pid)["visitor_id"]
        self.assertTrue(visitor_id.startswith("VIS-"))
        self.assertIn(f"Newcomer {visitor_id} was added before and has since been erased", line)
        self.assertIn(f"search {visitor_id}, then Anonymise this person", line)
        self.assertNotIn("Esme", line)                           # no name: they asked to be forgotten
        self.assertEqual((self.person(pid)["add_code"], self.person(pid)["visitor_details"]), ("erased", None))
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='breeze.newcomer_erased'"), 1)
        plan = self.run_kind("check")
        self.assertEqual((plan["newcomers"], plan["attendance"]), ([], []))
        self.assertEqual(self.run_kind("sync")["summary"]["problems"], [])
        self.assertEqual(len(self.adds()), 2)                    # never tried again
        from checkin import reports
        reports.anonymise_person(self.app.db, pid)               # the administrator removes them here too
        self.assertEqual(self.person(pid)["add_code"], "erased")
        self.assertEqual(self.run_kind("check")["newcomers"], [])

    def test_a_refused_family_is_a_problem_and_not_tried_again(self):
        self.newcomer("Fern Family", "9000 0005", [{"first_name": "Finn", "last_name": "Family"}])
        self.fake.refusals["families/create"] = {
            "success": False, "errors": ["Only newcomers added by check-in can be put into a family."]}
        summary = self.run_kind("sync")["summary"]
        self.assertEqual((summary["added"], summary["attendance_sent"]), (2, 2))
        self.assertEqual(summary["problems"], [
            "Fern Family, Finn Family weren't put into one family in the church database. It said: Only "
            "newcomers added by check-in can be put into a family. Put them in one family there by hand."])
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM family_link"), 0)
        self.assertEqual(self.run_kind("sync")["summary"]["problems"], [])
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "/api/families/create"]), 1)

    def test_family_links_wait_for_the_next_sync_when_the_family_call_fails(self):
        self.newcomer("Gail Group", "9000 0006", [{"first_name": "Gus", "last_name": "Group"}])
        self.fake.lose = [("families/create", False)] * 3     # the church database can't be reached
        state = self.sync.start("sync", wait=True)
        self.assertEqual(state["state"], "error")
        self.assertIn("Can't reach", state["error"])
        self.assertEqual((self.fake.families, self.app.db.scalar("SELECT COUNT(*) FROM family_link")), ([], 2))
        summary = self.run_kind("sync")["summary"]
        self.assertEqual((summary["added"], summary["attendance_sent"], summary["problems"]), (0, 2, []))
        ((path, ids, _),) = self.fake.families
        self.assertEqual(path, "families/create")
        self.assertEqual(set(ids), {p["id"] for p in self.fake.people if p["last_name"] == "Group"})
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM family_link"), 0)

    def test_a_newcomer_added_at_a_later_sync_joins_their_family(self):
        visitor = self.newcomer("Pia Parent", "9000 0011", [{"first_name": "Cody", "last_name": "Parent"},
                                                             {"first_name": "Dot", "last_name": "Parent"}])
        pia, cody, dot = visitor["person_ids"]
        wait = {str(dot): "skip"}                                  # Dot waits (an unclear match, say)
        self.assertEqual(self.run_kind("sync", wait)["summary"]["added"], 2)
        ids = {pid: self.person(pid)["breeze_id"] for pid in (pia, cody)}
        self.assertEqual(self.fake.families, [("families/create", [ids[pia], ids[cody]], None)])
        self.run_kind("sync", wait)                                # nothing sent again while Dot waits
        self.assertEqual(len(self.fake.families), 1)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM family_link"), 2)
        self.assertEqual(self.run_kind("sync")["summary"]["added"], 1)   # Dot is added, into their family
        self.assertEqual(self.fake.families[1], ("families/add", [self.person(dot)["breeze_id"]], ids[pia]))
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM family_link"), 0)

    def test_a_family_member_linked_later_brings_the_newcomers_to_their_family(self):
        self.fake.person("90000500", "Kit", "Parent")              # Kit is in the church database already
        visitor = self.newcomer("Pia Parent", "9000 0011", [{"first_name": "Kit", "last_name": "Parent"}])
        pia, kit = visitor["person_ids"]
        self.run_kind("sync", {str(kit): "skip"})                  # not sure it's them yet: Kit waits
        self.assertEqual(self.fake.families, [])
        self.run_kind("sync", {str(kit): "link:90000500"})         # it is
        self.assertEqual(self.fake.families, [("families/add", [self.person(pia)["breeze_id"]], "90000500")])
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM family_link"), 0)

    def test_after_a_lost_reply_the_next_automatic_sync_links_the_person_it_added(self):
        pid = self.newcomer("Nova Newcomer", "9000 0001")["person_ids"][0]
        self.fake.error_after = [("people/add", 500)]           # added, but check-in never heard
        self.assertEqual(self.sync.start("sync", wait=True)["state"], "error")
        (added,) = self.in_fake("Nova")
        self.assertTrue(self.sync.auto_tick(wait=True))         # the next automatic sync: no one chooses
        state = self.sync.status()
        self.assertEqual(state["state"], "done", state.get("error"))
        summary = state["result"]["summary"]
        self.assertEqual((summary["linked"], summary["added"], summary["problems"]), (1, 0, []))
        self.assertEqual((len(self.adds()), len(self.in_fake("Nova"))), (1, 1))   # not sent again, no second Nova
        self.assertIsNone(self.person(pid)["add_code"])
        self.assertEqual(self.fake.attendance["300000001"], {added["id"]})

    def test_new_ids_move_waiting_family_links_too(self):
        from checkin.breeze_sync import BreezeSync
        visitor = self.newcomer("Hal Held", "9000 0007")
        pid = visitor["person_ids"][0]
        with self.app.db.tx() as conn:
            conn.execute("UPDATE person SET breeze_id='90000123' WHERE id=?", (pid,))
            conn.execute("INSERT INTO family_link(person_id, household_id, breeze_id, added, created_at) "
                         "VALUES(?, ?, '90000123', 1, '2026-11-11T09:00:00')", (pid, visitor["household_id"]))
            BreezeSync._apply_switch(conn, {"forward": True, "people": [(pid, "90000123", "MEMB-123")],
                                            "households": [], "fills": []})
        self.assertEqual(self.app.db.scalar("SELECT breeze_id FROM family_link WHERE person_id=?", (pid,)),
                         "MEMB-123")


class TestChurchDatabaseTarget(SyncBase):
    """Connecting, the automatic sync and sending again; the fake connector stands in for the church database."""

    def backups(self):
        return [b for b in self.app.backups.list() if "pre-sync" in b["name"]]

    def test_chms_target_syncs_without_pauses(self):
        self.sync.connect_chms(self.fake.url, KEY)
        client = breeze.BreezeClient.from_app(self.app.db, self.app.paths)
        self.assertEqual((client.min_interval, client.base), (0, self.fake.url + "/api/"))
        self.run_kind("sync")
        members = self.family(0)
        self.checkin(members)
        from checkin import server
        ctx = type("C", (), {"app": self.app, "body": {"session_id": self.session["id"],
                                                         "event_name": "Sample Sunday Service"}, "user_id": None})
        server.breeze_settings(ctx)
        result = self.run_kind("sync")["summary"]
        self.assertEqual(result["attendance_sent"], len(members["people"]))

    def test_chms_status_hides_people_even_without_the_breeze_status_choice(self):
        self.sync.connect_chms(self.fake.url, KEY)
        self.app.db.set_setting("import_mapping", {k: v for k, v in MAPPING.items() if k != "status"})
        self.app.db.set_setting("import_rules", [])
        self.run_kind("sync")
        hidden = self.fake.people[0]
        self.assertEqual(self.app.db.one("SELECT active FROM person WHERE breeze_id=?", (hidden["id"],))["active"], 1)
        hidden["details"][STATUS_FIELD] = {"value": "2", "name": "No Longer Attends"}   # as the connector sends it
        self.run_kind("sync")
        self.assertEqual(self.app.db.one("SELECT active FROM person WHERE breeze_id=?", (hidden["id"],))["active"], 0)
        hidden["details"][STATUS_FIELD] = {"value": "1", "name": "Active"}
        self.fake.people[1]["details"].pop(STATUS_FIELD)           # an older church database: no status
        state = self.sync.start("sync", wait=True)
        self.assertEqual(state["state"], "error")
        self.assertIn("didn't say who shows", state["error"])
        self.assertEqual(self.app.db.one("SELECT active FROM person WHERE breeze_id=?", (hidden["id"],))["active"], 0)

    def test_auto_tick_only_when_connected_and_idle(self):
        self.assertFalse(self.sync.auto_tick(wait=True))                          # not connected yet
        self.sync.connect_chms(self.fake.url, KEY)
        self.sync.state = {"state": "running"}
        self.assertFalse(self.sync.auto_tick(wait=True))
        self.sync.state = {"state": "idle"}
        self.assertTrue(self.sync.auto_tick(wait=True))
        self.assertEqual(self.sync.status()["state"], "done")
        self.assertIn("auto", self.sync.status()["last"])

    def test_automatic_sync_can_be_switched_off(self):
        from checkin import server
        self.sync.connect_chms(self.fake.url, KEY, auto=False)      # a rehearsal copy: Check by hand only
        self.assertFalse(self.sync.status()["chms_auto_sync"])
        self.assertFalse(self.sync.auto_tick(wait=True))
        server.breeze_settings(type("C", (), {"app": self.app, "user_id": None, "body": {"chms_auto_sync": True}}))
        self.assertTrue(self.sync.auto_tick(wait=True))

    def test_auto_sync_backs_up_once_a_day(self):
        self.sync.connect_chms(self.fake.url, KEY)
        self.assertTrue(self.app.backups.has_key())
        self.sync.auto_tick(wait=True)
        self.sync.auto_tick(wait=True)
        self.assertEqual(len(self.backups()), 1)
        time.sleep(1.1)                                         # backup names are per second
        self.run_kind("sync")                                                       # a sync by hand always backs up
        self.assertEqual(len(self.backups()), 2)

    def test_event_choice_can_be_for_one_day_only(self):
        from checkin import server
        service_id = self.session["service_id"]
        before = self.app.db.scalar("SELECT breeze_event_name FROM service WHERE id=?", (service_id,))
        ctx = type("C", (), {"app": self.app, "user_id": None, "body": {
            "session_id": self.session["id"], "event_name": "TEST", "scope": "day"}})
        server.breeze_settings(ctx)
        self.assertEqual(self.app.db.scalar("SELECT breeze_event_name FROM session WHERE id=?",
                                            (self.session["id"],)), "TEST")
        self.assertEqual(self.app.db.scalar("SELECT breeze_event_name FROM service WHERE id=?", (service_id,)),
                         before)                                  # later Sundays keep their event

    def test_church_database_certificate_file_is_trusted(self):
        import datetime as dt
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Church Check-in CA")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=30))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        (self.app.paths.keys / breeze.CHMS_CA_FILE).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.sync.connect_chms(self.fake.url, KEY)
        client = breeze.BreezeClient.from_app(self.app.db, self.app.paths)
        subjects = [dict(x[0] for x in c["subject"]).get("commonName") for c in client._tls.get_ca_certs()]
        self.assertIn("Church Check-in CA", subjects)

    def test_disconnect_deletes_the_key_and_any_old_breeze_key(self):
        self.sync.connect_chms(self.fake.url, KEY)
        breeze.save_key(self.app.paths, KEY, breeze.KEY_FILE)              # left over from before the move
        self.sync.disconnect()
        self.assertIsNone(breeze.load_key(self.app.paths, breeze.CHMS_KEY_FILE))
        self.assertIsNone(breeze.load_key(self.app.paths, breeze.KEY_FILE))
        self.assertFalse(self.sync.status()["connected"])

    def test_backups_leave_out_the_old_breeze_key(self):
        import io
        import zipfile
        self.sync.connect_chms(self.fake.url, KEY)
        breeze.save_key(self.app.paths, KEY, breeze.KEY_FILE)
        names = zipfile.ZipFile(io.BytesIO(self.app.backups.build_archive("test"))).namelist()
        self.assertIn("keys/" + breeze.CHMS_KEY_FILE, names)
        self.assertNotIn("keys/" + breeze.KEY_FILE, names)

    # ---- sending check-ins again after the church database is restored from a backup -----------------------

    def two_sundays_sent(self):
        """Check-ins three days ago and today, both already sent to the church database."""
        import datetime as dt
        from checkin import server
        self.sync.connect_chms(self.fake.url, KEY)
        self.run_kind("sync")
        server.breeze_settings(type("C", (), {"app": self.app, "user_id": None, "body": {
            "session_id": self.session["id"], "event_name": "Sample Sunday Service"}}))
        earlier = (dt.date.fromisoformat(self.session["date"]) - dt.timedelta(days=3)).isoformat()
        with self.app.db.tx() as conn:
            past = conn.execute("INSERT INTO session(service_id, date, name, start_time, breeze_event_name, status) "
                                "VALUES(NULL, ?, 'Sunday Service', '09:00', 'Sample Sunday Service', 'closed')",
                                (earlier,)).lastrowid
        self.fake.events.append(dict(self.fake.events[0], id="300000000", start_datetime=earlier + " 09:30:00"))
        then, now = self.family(1), self.family(0)
        txn_then, txn_now = new_id(), new_id()
        self.app.service.check_in(txn_then, self.session["id"], then["id"], [p["id"] for p in then["people"]],
                                  self.station)
        with self.app.db.tx() as conn:
            conn.execute("UPDATE attendance SET session_id=? WHERE txn_id=?", (past, txn_then))
        self.app.service.check_in(txn_now, self.session["id"], now["id"], [p["id"] for p in now["people"]],
                                  self.station)
        sent = self.run_kind("sync")["summary"]
        self.assertEqual(sent["attendance_sent"], len(then["people"]) + len(now["people"]))
        return {"earlier": earlier, "today": self.session["date"], "then": len(then["people"]),
                "now": len(now["people"]), "txn_now": txn_now}

    def test_check_ins_from_a_chosen_day_are_sent_again_after_a_restore(self):
        days = self.two_sundays_sent()
        self.fake.attendance.clear()              # restored from a backup made before either Sunday was sent
        preview = self.sync.resend_from(days["today"], dry_run=True)
        self.assertEqual(preview["count"], days["now"])
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance WHERE breeze_sent_at IS NULL"), 0)
        result = self.sync.resend_from(days["today"], user_id=None)
        self.assertEqual(result["count"], days["now"])
        summary = self.run_kind("sync")["summary"]
        self.assertEqual(summary["attendance_sent"], days["now"])
        self.assertEqual(len(self.fake.attendance["300000001"]), days["now"])
        self.assertNotIn("300000000", self.fake.attendance)          # the earlier day was not chosen
        entry = self.app.db.one("SELECT details FROM audit_log WHERE action='chms.resend'")
        self.assertEqual(json.loads(entry["details"])["from"], days["today"])
        self.assertEqual(json.loads(entry["details"])["count"], days["now"])

    def test_sending_again_twice_sends_each_check_in_once(self):
        days = self.two_sundays_sent()
        self.fake.attendance.clear()
        self.sync.resend_from(days["earlier"])
        self.assertEqual(self.sync.resend_from(days["earlier"])["count"], days["then"] + days["now"])
        self.assertEqual(self.run_kind("sync")["summary"]["attendance_sent"], days["then"] + days["now"])
        again = self.run_kind("sync")["summary"]
        self.assertEqual((again["attendance_sent"], again["attendance_already"]), (0, 0))

    def test_undone_check_ins_are_not_sent_again_but_their_removal_is(self):
        days = self.two_sundays_sent()
        marked = set(self.fake.attendance["300000001"])
        self.app.service.undo(days["txn_now"], "Test check-in", self.station)
        self.assertEqual(self.run_kind("sync")["summary"]["attendance_removed"], days["now"])
        self.fake.attendance["300000001"] = set(marked)  # the backup was made before the undo reached it
        self.assertEqual(self.sync.resend_from(days["today"])["count"], 0)
        summary = self.run_kind("sync")["summary"]
        self.assertEqual((summary["attendance_sent"], summary["attendance_removed"]), (0, days["now"]))
        self.assertEqual(self.fake.attendance["300000001"], set())

    def test_sending_again_from_before_the_start_date_moves_it_back(self):
        days = self.two_sundays_sent()
        self.app.db.set_setting("breeze_sync_since", days["today"])
        self.fake.attendance.clear()
        self.sync.resend_from(days["earlier"])
        self.assertEqual(self.sync.since(), days["earlier"])
        self.assertEqual(self.run_kind("sync")["summary"]["attendance_sent"], days["then"] + days["now"])

    def test_sending_again_needs_a_connection_and_not_during_a_sync(self):
        from checkin.breeze_sync import SyncError
        with self.assertRaises(SyncError):
            self.sync.resend_from(self.session["date"])             # not connected yet
        self.sync.connect_chms(self.fake.url, KEY)
        self.sync.state = {"state": "running"}
        with self.assertRaises(SyncError):
            self.sync.resend_from(self.session["date"])
        self.sync.state = {"state": "idle"}
        self.assertEqual(self.sync.resend_from(self.session["date"])["count"], 0)

    def test_send_again_is_for_administrators_and_needs_a_real_day(self):
        import datetime as dt
        from checkin import server
        levels = {(m, r.pattern): level for m, r, level, _ in server.ROUTES}
        self.assertEqual(levels[("GET", "^/api/admin/breeze/resend$")], "admin")
        self.assertEqual(levels[("POST", "^/api/admin/breeze/resend$")], "admin")
        self.sync.connect_chms(self.fake.url, KEY)
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        for day in ("", "25/10/2026", tomorrow):
            ctx = type("C", (), {"app": self.app, "user_id": None, "body": {"from": day},
                                 "q": lambda self, name, default=None, day=day: day})()
            for handler in (server.breeze_resend_preview, server.breeze_resend):
                with self.assertRaises(server.HttpError) as err:
                    handler(ctx)
                self.assertEqual(err.exception.status, 400)
        ctx = type("C", (), {"app": self.app, "user_id": None, "body": {"from": self.session["date"]}})()
        self.assertEqual(server.breeze_resend(ctx)["count"], 0)


if __name__ == "__main__":
    unittest.main()
