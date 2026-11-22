"""The visitor form's optional answers: Chinese name, email, gender, age group, preferred language, what brings
them, and whether (and how) church may get in touch. Check-in keeps the Chinese name and gender; the rest go to
the church database at the next sync and are then wiped here.

Run:  python -m unittest discover -s tests -v
"""

import datetime as dt
import json
import os

os.environ["CHECKIN_NO_USB"] = "1"
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkin import reports  # noqa: E402
from checkin.service import ServiceError  # noqa: E402
from checkin.util import today_iso  # noqa: E402
from test_acceptance import import_file, make_app  # noqa: E402
from test_breeze import KEY, FakeBreeze, SyncBase  # noqa: E402

FULL_FORM = {"guardian_name": "Vera Visit", "phone": "9000 0011", "chinese_name": " 示例姓名 ", "email": "parent@example.com",
             "sex": "Female", "age_group": "20s30s", "preferred_language": "zh", "visit_reason": "church",
             "wants_contact": "yes", "contact_by": "whatsapp"}


def details(app, person_id):
    raw = app.db.scalar("SELECT visitor_details FROM person WHERE id=?", (person_id,))
    return json.loads(raw) if raw else None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-visitor-")
        self.app = make_app(self.tmp, 12)
        self.station = self.app.db.one("SELECT * FROM station")
        self.session = self.app.service.today_sessions()[0] if self.app.service.today_sessions() \
            else self.app.service.open_session(1)

    def tearDown(self):
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def register(self, **form):
        return self.app.service.register_visitor(dict(FULL_FORM, **form), self.station)


class TestVisitorForm(Base):
    def test_schema_has_the_new_columns(self):
        columns = {r["name"] for r in self.app.db.all("PRAGMA table_info(person)")}
        self.assertTrue({"chinese_name", "visitor_details"} <= columns)

    def test_the_form_keeps_every_answer(self):
        res = self.register()
        person = self.app.db.one("SELECT * FROM person WHERE id=?", (res["person_ids"][0],))
        self.assertEqual((person["first_name"], person["chinese_name"], person["sex"]), ("Vera", "示例姓名", "Female"))
        self.assertEqual(details(self.app, person["id"]), {
            "email": "parent@example.com", "age_group": "20s30s", "preferred_language": "zh", "visit_reason": "church",
            "wants_contact": "yes", "contact_by": "whatsapp", "contact_consent_on": today_iso()})

    def test_only_name_and_mobile_are_needed(self):
        res = self.app.service.register_visitor({"guardian_name": "Solo Visitor", "phone": "93334444"}, self.station)
        person = self.app.db.one("SELECT chinese_name, sex, visitor_details FROM person WHERE id=?",
                                 (res["person_ids"][0],))
        self.assertEqual(tuple(person.values()), (None, None, None))
        with self.assertRaises(ServiceError) as ctx:
            self.register(phone="")
        self.assertEqual(str(ctx.exception), "Enter a valid mobile number.")

    def test_a_wrong_email_is_refused(self):
        with self.assertRaises(ServiceError) as ctx:
            self.register(email="vera at example")
        self.assertEqual(str(ctx.exception), "Enter a valid email address, or leave it blank.")

    def test_answers_it_does_not_know_are_left_out(self):
        res = self.register(age_group="ancient", preferred_language="klingon", visit_reason="", sex="Other",
                            wants_contact="maybe", contact_by="pigeon", email="")
        person = self.app.db.one("SELECT sex FROM person WHERE id=?", (res["person_ids"][0],))
        self.assertIsNone(person["sex"])
        self.assertIsNone(details(self.app, res["person_ids"][0]))

    def test_no_to_contact_keeps_no_how_and_no_date(self):
        res = self.register(wants_contact="no")
        self.assertEqual(details(self.app, res["person_ids"][0]), {
            "email": "parent@example.com", "age_group": "20s30s", "preferred_language": "zh", "visit_reason": "church",
            "wants_contact": "no"})

    def test_how_without_a_yes_is_dropped(self):
        res = self.register(wants_contact="", contact_by="call")
        self.assertNotIn("contact_by", details(self.app, res["person_ids"][0]))

    def test_long_answers_are_cut(self):
        res = self.register(chinese_name="陈" * 80)
        person = self.app.db.one("SELECT chinese_name FROM person WHERE id=?", (res["person_ids"][0],))
        self.assertEqual(len(person["chinese_name"]), 40)
        with self.assertRaises(ServiceError):   # too long to be a real address
            self.register(email="a" * 300 + "@example.com", phone="9000 0012")

    def test_family_members_are_unchanged(self):
        res = self.register(members=[{"first_name": "Nia", "last_name": "Visit"}])
        member = self.app.db.one("SELECT chinese_name, visitor_details FROM person WHERE id=?",
                                 (res["person_ids"][1],))
        self.assertEqual(tuple(member.values()), (None, None))

    def test_search_finds_a_chinese_name_whole_or_in_part(self):
        res = self.register()
        for query in ("示例姓名", "示例", "姓名"):
            found = self.app.service.search(query)
            self.assertIn(res["household_id"], [h["id"] for h in found], query)

    def test_household_shows_the_chinese_name(self):
        res = self.register()
        hh = self.app.service.household(res["household_id"], self.session["id"])
        self.assertEqual(hh["people"][0]["chinese_name"], "示例姓名")

    def test_removing_a_person_removes_their_answers(self):
        res = self.register()
        reports.anonymise_person(self.app.db, res["person_ids"][0])
        person = self.app.db.one("SELECT chinese_name, sex, visitor_details FROM person WHERE id=?",
                                 (res["person_ids"][0],))
        self.assertEqual(tuple(person.values()), (None, None, None))

    def test_the_answers_never_reach_the_audit_log(self):
        self.register()
        log = json.dumps(self.app.db.all("SELECT * FROM audit_log"), ensure_ascii=False)
        for text in ("示例姓名", "parent@example.com", "whatsapp", "20s30s"):
            self.assertNotIn(text, log)

    def test_the_api_takes_the_form(self):
        from checkin import server
        ctx = type("C", (), {"app": self.app, "body": dict(FULL_FORM), "station": self.station, "user_id": None,
                                 "session": {"role": "admin"}})
        res = server.visitors(ctx)
        self.assertEqual(details(self.app, res["person_ids"][0])["contact_by"], "whatsapp")


class TestRosterChineseNames(Base):
    def test_a_chinese_name_column_is_read_and_searchable(self):
        csv_path = os.path.join(self.tmp, "roster2.csv")
        Path(csv_path).write_text("Breeze ID,First Name,Last Name,Chinese name,Mobile\n"
                                  "777001,Alex,Sample,示例姓名,9123 4567\n", encoding="utf-8")
        import_file(self.app, csv_path)
        person = self.app.db.one("SELECT id, chinese_name FROM person WHERE breeze_id='777001'")
        self.assertEqual(person["chinese_name"], "示例姓名")
        self.assertTrue(self.app.service.search("示例"))


class ChmsFake(FakeBreeze):
    """The church database's connector from 4 Nov 2026: a Chinese name field, and `saved_fields` in its reply
    to people/add. people/update (1.9.0) gives a newcomer it added the answers check-in kept, filling only
    blanks, the contact answers together. With new=False it answers like the version before (set new=True to
    update it). `held`: each person's answers there, by field id; the Chinese name is in the people list too.
    `refuse`: answers it can't use (as the connector does with an email its validator refuses)."""

    ANSWER_IDS = {"1004", "1005", "1006", "1007", "1008", "1009", "1010", "1011", "1012"}
    CONTACT_IDS = {"1010", "1011", "1012"}
    refuse = set()

    def __init__(self, new=True):
        super().__init__()
        self.new = new
        self.held, self.added = {}, set()

    def _sent(self, q):
        return {str(f["field_id"]): f["response"] for f in json.loads(q.get("fields_json") or "[]")
                if str(f.get("field_id")) in self.ANSWER_IDS}

    def _hold(self, bid, answers):
        held = self.held.setdefault(bid, {})
        held.update(answers)
        if "1004" in held:
            next(p for p in self.people if p["id"] == bid)["details"]["1004"] = held["1004"]

    def answer(self, path, q):
        if self.new and path == "people/update" and not self.refusals.get(path):
            return self._update(q)
        reply = super().answer(path, q)
        if self.new and path == "profile":
            reply[0]["fields"].append({"field_id": "1004", "field_type": "text", "name": "Chinese name"})
        if path == "people/add" and isinstance(reply, list):
            self.added.add(reply[0]["id"])
            if self.new:
                usable = {k: v for k, v in self._sent(q).items() if k not in self.refuse}
                self._hold(reply[0]["id"], usable)
                reply[0]["saved_fields"] = sorted(usable)
        return reply

    def _update(self, q):
        bid = q.get("person_id")
        if bid not in self.added:
            return {"success": False, "errors": ["Only newcomers added by check-in, and not merged into another "
                                                 "record since, can be given visitor form answers."]}
        sent = self._sent(q)
        usable = {k: v for k, v in sent.items() if k not in self.refuse}
        held = self.held.setdefault(bid, {})
        filled = {k: v for k, v in usable.items() if k not in self.CONTACT_IDS and k not in held}
        if "1010" in usable and not held.keys() & self.CONTACT_IDS:
            filled.update({k: v for k, v in usable.items() if k in self.CONTACT_IDS})
        self._hold(bid, filled)
        has_contact = bool(held.keys() & self.CONTACT_IDS)
        return [{"id": bid, "saved_fields": sorted(k for k in sent
                                                   if (has_contact if k in self.CONTACT_IDS else k in held))}]


class SyncCase(SyncBase):
    families = 12

    def visitor(self, **form):
        res = self.app.service.register_visitor(dict(FULL_FORM, **form), self.station)
        self.checkin(self.app.service.household(res["household_id"], self.session["id"]))
        return res["person_ids"][0]

    def sent(self):
        (call,) = [c for c in self.fake.calls if c[0] == "/api/people/add"]
        return {f["field_id"]: f for f in json.loads(call[1]["fields_json"])}


class TestSyncSendsTheAnswers(SyncCase):
    fake_class = ChmsFake

    def test_the_answers_go_and_are_then_wiped_here(self):
        self.sync.connect_chms(self.fake.url, KEY)
        person_id = self.visitor()
        self.run_kind("sync")
        sent = self.sent()
        self.assertEqual({k: v["response"] for k, v in sent.items() if v["field_type"] == "text"}, {
            "1004": "示例姓名", "1005": "parent@example.com", "1006": "F", "1007": "20s30s", "1008": "zh",
            "1009": "church", "1010": "yes", "1011": "whatsapp", "1012": today_iso()})
        self.assertEqual([f["details"]["phone_mobile"] for f in sent.values() if f["field_type"] == "phone"],
                         ["+65 9000 0011"])
        person = self.app.db.one("SELECT chinese_name, sex, visitor_details, breeze_id FROM person WHERE id=?",
                                 (person_id,))
        self.assertEqual((person["chinese_name"], person["sex"], person["visitor_details"]), ("示例姓名", "Female", None))
        self.assertTrue(person["breeze_id"])

    def test_a_visitor_with_no_answers_sends_only_the_mobile(self):
        self.sync.connect_chms(self.fake.url, KEY)
        res = self.app.service.register_visitor({"guardian_name": "Solo Visitor", "phone": "93334444"}, self.station)
        self.checkin(self.app.service.household(res["household_id"], self.session["id"]))
        self.run_kind("sync")
        self.assertEqual([f["field_type"] for f in self.sent().values()], ["phone"])

    def test_chinese_names_from_the_church_database_reach_the_roster(self):
        self.sync.connect_chms(self.fake.url, KEY)
        member = self.fake.people[0]
        member["details"]["1004"] = "林恩典"
        self.run_kind("sync")
        person = self.app.db.one("SELECT id, chinese_name FROM person WHERE breeze_id=?", (member["id"],))
        self.assertEqual(person["chinese_name"], "林恩典")
        self.assertTrue(self.app.service.search("恩典"))
        self.assertNotIn("林恩典", json.dumps(self.app.db.get_setting("sync_column_values") or {}, ensure_ascii=False))

    def test_a_newcomer_linked_to_someone_already_there_keeps_no_answers(self):
        self.sync.connect_chms(self.fake.url, KEY)
        self.run_kind("sync")
        existing = self.fake.people[0]
        person_id = self.visitor(guardian_name=f"{existing['force_first_name']} {existing['last_name']}",
                                 phone="9000 0099")
        self.run_kind("sync", {str(person_id): "link:" + existing["id"]})
        self.assertIsNone(details(self.app, person_id))
        self.assertFalse([c for c in self.fake.calls if c[0] == "/api/people/add"])


class OldChmsFake(ChmsFake):
    def __init__(self):
        super().__init__(new=False)


class TestSyncWithAnOlderChurchDatabase(SyncCase):
    """The older connector has no Chinese name field and doesn't confirm answers: only the mobile goes, and the
    answers stay here until the church database is updated."""
    fake_class = OldChmsFake

    def test_only_the_mobile_goes_and_the_answers_stay(self):
        self.sync.connect_chms(self.fake.url, KEY)
        person_id = self.visitor()
        self.run_kind("sync")
        self.assertEqual([f["field_type"] for f in self.sent().values()], ["phone"])
        self.assertEqual(details(self.app, person_id)["contact_by"], "whatsapp")
        self.assertEqual(self.app.db.scalar("SELECT chinese_name FROM person WHERE id=?", (person_id,)), "示例姓名")


class PartialChmsFake(ChmsFake):
    """Saves every answer except the email, and says so in saved_fields."""
    refuse = {"1005"}


class EmptyAckChmsFake(PartialChmsFake):
    refuse = ChmsFake.ANSWER_IDS


class TestPartialAcknowledgement(SyncCase):
    """Audit C4 (5 Nov 2026): only answers the church database says it saved may be wiped here."""
    fake_class = PartialChmsFake

    def test_an_answer_that_was_not_saved_stays_and_is_reported(self):
        self.sync.connect_chms(self.fake.url, KEY)
        person_id = self.visitor()
        result = self.run_kind("sync")
        kept = details(self.app, person_id)
        self.assertIsNotNone(kept)
        self.assertEqual(kept["email"], "parent@example.com")
        self.assertTrue(self.app.db.scalar("SELECT breeze_id FROM person WHERE id=?", (person_id,)))
        problems = " ".join(result["summary"]["problems"])
        self.assertIn("email", problems.lower())
        self.assertNotIn("parent@example.com", problems)  # the problem list never holds the answer itself


class TestEmptyAcknowledgement(TestPartialAcknowledgement):
    fake_class = EmptyAckChmsFake


class TestFullAcknowledgementStillWipes(SyncCase):
    fake_class = ChmsFake

    def test_everything_saved_means_nothing_kept_and_no_problem(self):
        self.sync.connect_chms(self.fake.url, KEY)
        person_id = self.visitor()
        result = self.run_kind("sync")
        self.assertIsNone(details(self.app, person_id))
        self.assertFalse([m for m in result["summary"]["problems"] if "didn't save" in m])
        self.assertEqual(result["summary"]["added"], 1)


class TestTheAddKeepsOnlyWhatWasNotSaved(SyncCase):
    """Audit C3 (5 Nov 2026): each answer the church database confirms is deleted here at once."""
    fake_class = PartialChmsFake

    def test_only_the_email_stays(self):
        self.sync.connect_chms(self.fake.url, KEY)
        person_id = self.visitor()
        self.run_kind("sync")
        self.assertEqual(details(self.app, person_id), {"email": "parent@example.com"})


class TestKeptAnswersGoLater(SyncCase):
    """Audit C3 (5 Nov 2026): answers check-in kept because the church database couldn't take them go at a later
    sync (people/update, compatible service 1.9.0), and are deleted 30 days after the visit if they never do."""
    fake_class = OldChmsFake

    def setUp(self):
        super().setUp()
        self.sync.connect_chms(self.fake.url, KEY)

    def updates(self):
        return [json.loads(q["fields_json"]) for path, q in self.fake.calls if path == "/api/people/update"]

    def added_with_only_the_mobile(self, **form):
        person_id = self.visitor(**form)
        self.run_kind("sync")
        return person_id, self.app.db.scalar("SELECT breeze_id FROM person WHERE id=?", (person_id,))

    def age(self, person_id, days):
        then = (dt.datetime.now() - dt.timedelta(days=days)).replace(microsecond=0).isoformat(sep=" ")
        with self.app.db.tx() as conn:
            conn.execute("UPDATE person SET created_at=? WHERE id=?", (then, person_id))

    def lines(self, result, text="visitor form"):
        return [m for m in result["summary"]["problems"] if text in m]

    def test_an_older_church_database_keeps_them_and_says_so_once(self):
        first, _ = self.added_with_only_the_mobile()
        second, _ = self.added_with_only_the_mobile(guardian_name="Wren Visit", phone="9000 0012")
        self.fake.calls.clear()
        result = self.run_kind("sync")
        (line,) = self.lines(result)
        self.assertIn("too old to take the visitor form answers check-in kept for 2 newcomer(s)", line)
        self.assertEqual(len(self.updates()), 1)   # it stops at the first "unknown endpoint"
        self.assertEqual(details(self.app, first)["contact_by"], "whatsapp")
        self.assertEqual(details(self.app, second)["contact_by"], "whatsapp")

    def test_once_it_is_updated_the_next_sync_sends_them_and_deletes_them_here(self):
        person_id, bid = self.added_with_only_the_mobile()
        self.fake.new = True
        self.fake.calls.clear()
        result = self.run_kind("sync")
        (fields,) = self.updates()
        self.assertEqual({f["field_id"]: f["response"] for f in fields}, {
            "1004": "示例姓名", "1005": "parent@example.com", "1006": "F", "1007": "20s30s", "1008": "zh",
            "1009": "church", "1010": "yes", "1011": "whatsapp", "1012": today_iso()})
        self.assertEqual(self.fake.held[bid]["1005"], "parent@example.com")
        self.assertIsNone(details(self.app, person_id))
        self.assertFalse(self.lines(result))
        # Sent before the roster is read, so the Chinese name the roster brings back is the one they gave.
        paths = [path for path, _ in self.fake.calls]
        self.assertLess(paths.index("/api/people/update"), paths.index("/api/people"))
        self.assertEqual(self.app.db.scalar("SELECT chinese_name FROM person WHERE id=?", (person_id,)), "示例姓名")
        sent = self.app.db.all("SELECT * FROM audit_log WHERE action='breeze.visitor_answers_sent'")
        self.assertEqual(len(sent), 1)
        log = json.dumps(self.app.db.all("SELECT * FROM audit_log"), ensure_ascii=False)
        for text in ("示例姓名", "parent@example.com", "whatsapp", "20s30s"):
            self.assertNotIn(text, log)
        self.fake.calls.clear()
        self.run_kind("sync")
        self.assertEqual(self.updates(), [])   # nothing left to send
        self.assertEqual(self.app.db.scalar("SELECT chinese_name FROM person WHERE id=?", (person_id,)), "示例姓名")

    def test_what_staff_entered_there_stays(self):
        person_id, bid = self.added_with_only_the_mobile()
        self.fake.held[bid] = {"1005": "office@example.com", "1010": "no"}
        self.fake.new = True
        self.run_kind("sync")
        self.assertEqual(self.fake.held[bid]["1005"], "office@example.com")
        self.assertEqual(self.fake.held[bid]["1010"], "no")
        self.assertNotIn("1011", self.fake.held[bid])   # the contact answers go together or not at all
        self.assertEqual(self.fake.held[bid]["1007"], "20s30s")
        self.assertIsNone(details(self.app, person_id))  # it has an answer to each question now

    def test_a_refusal_keeps_them_and_names_the_newcomer(self):
        person_id, _ = self.added_with_only_the_mobile()
        self.fake.new = True
        self.fake.refusals["people/update"] = {"success": False, "errors": [
            "Only newcomers added by check-in, and not merged into another record since, can be given visitor "
            "form answers."]}
        result = self.run_kind("sync")
        self.assertEqual(details(self.app, person_id)["contact_by"], "whatsapp")
        (line,) = self.lines(result)
        self.assertIn("Vera Visit's visitor form answers didn't go to the church database. It said: Only newcomers "
                      "added by check-in", line)
        self.assertIn("until 30 days after the visit", line)
        self.assertNotIn("parent@example.com", line)

    def test_an_answer_it_cant_use_stays_alone_and_is_reported_once(self):
        person_id, _ = self.added_with_only_the_mobile()
        self.fake.new, self.fake.refuse = True, {"1005"}
        result = self.run_kind("sync")
        self.assertEqual(details(self.app, person_id), {"email": "parent@example.com"})
        (line,) = self.lines(result)
        self.assertIn("didn't save: email.", line)
        self.assertNotIn("parent@example.com", line)
        self.fake.calls.clear()
        result = self.run_kind("sync")
        self.assertEqual(len(self.updates()), 1)   # sent again, and nothing new to say
        self.assertFalse(self.lines(result))
        self.assertEqual(details(self.app, person_id), {"email": "parent@example.com"})

    def test_after_30_days_they_are_deleted_by_name_never_by_value(self):
        added, _ = self.added_with_only_the_mobile()
        waiting = self.visitor(guardian_name="Wren Visit", phone="9000 0012")
        self.run_kind("sync", {str(waiting): "skip"})
        self.age(added, 29)
        self.age(waiting, 31)
        self.run_kind("sync", {str(waiting): "skip"})
        self.assertEqual(details(self.app, added)["contact_by"], "whatsapp")
        self.age(added, 31)
        result = self.run_kind("sync", {str(waiting): "skip"})
        self.assertIsNone(details(self.app, added))
        self.assertEqual(details(self.app, waiting)["contact_by"], "whatsapp")   # still to be added: kept for that
        (line,) = self.lines(result, "Check-in deleted")
        self.assertIn("Check-in deleted Vera Visit's visitor form answers (email, age_group, preferred_language, "
                      "visit_reason, wants_contact, contact_by, contact_consent_on) 30 days after the visit", line)
        self.assertFalse(self.lines(result, "too old"))
        deleted = self.app.db.all("SELECT * FROM audit_log WHERE action='breeze.visitor_answers_deleted'")
        self.assertEqual(len(deleted), 1)
        self.assertIn("contact_by", json.dumps(deleted))
        everything = json.dumps([deleted, result["summary"]["problems"]], ensure_ascii=False)
        for text in ("parent@example.com", "whatsapp", "20s30s"):
            self.assertNotIn(text, everything)

    def test_check_sends_nothing_and_deletes_nothing(self):
        person_id, _ = self.added_with_only_the_mobile()
        self.age(person_id, 31)
        self.fake.new = True
        self.fake.calls.clear()
        self.run_kind("check")
        self.assertEqual(self.updates(), [])
        self.assertEqual(details(self.app, person_id)["contact_by"], "whatsapp")

    def test_an_error_reply_waits_for_the_next_sync_and_the_rest_still_goes(self):
        with self.app.db.tx() as conn:
            conn.execute("UPDATE session SET breeze_event_name='Sample Sunday Service'")
        person_id, _ = self.added_with_only_the_mobile()
        self.fake.new = True
        self.fake.error_after.append(("people/update", 500))   # it saved them, but the reply was an error
        members = self.family()
        self.checkin(members)
        result = self.run_kind("sync")
        self.assertEqual(details(self.app, person_id)["contact_by"], "whatsapp")
        (line,) = self.lines(result)
        self.assertEqual(line, "The visitor form answers check-in kept weren't sent this time. The external service "
                               "replied with an error (500). Try again later.")
        self.assertEqual(result["summary"]["attendance_sent"], len(members["people"]))
        result = self.run_kind("sync")
        self.assertIsNone(details(self.app, person_id))
        self.assertFalse(self.lines(result))


if __name__ == "__main__":
    unittest.main()
