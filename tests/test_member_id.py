"""Member IDs at check-in. The church database gives every person and family a member ID; check-in asks for them
and, the first time they arrive, moves its roster over in one go, keeping everyone's check-ins, households and
classes. If the church database is rolled back to a version without Member IDs, the next sync moves back.

Run:  python -m unittest discover -s tests -v
"""

import json
import os

os.environ["CHECKIN_NO_USB"] = "1"
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkin import member_id, reports  # noqa: E402
import test_breeze  # noqa: E402  (as a module, so its tests don't run twice)
from test_breeze import KEY, FakeBreeze  # noqa: E402

# Made by the church database's own code (compatible service people/member_id.py): both programs must agree.
FROM_CHURCH_DATABASE = ["MEMB-7K3M-Q9XY", "MEMB-0000-0000", "MEMB-ZZZZ-ZZZC", "MEMB-A1B2-C3DZ", "MEMB-1234-567K"]


class FormatTests(unittest.TestCase):
    def test_ids_from_the_church_database_check_out(self):
        for value in FROM_CHURCH_DATABASE:
            self.assertEqual(member_id.parse(value), value)
            self.assertEqual(member_id.make(value[5:9] + value[10:13]), value)

    def test_typing_is_forgiving(self):
        value = "MEMB-7K3M-Q9XY"
        for typed in ("memb-7k3m-q9xy", "MEMB7K3MQ9XY", " memb 7K3M Q9XY ", "7k3mq9xy"):
            self.assertEqual(member_id.parse(typed, bare=True), value, typed)
        self.assertIsNone(member_id.parse("7k3mq9xy"))                 # the prefix, unless asked otherwise

    def test_typos_and_other_ids_are_refused(self):
        for text in ("MEMB-7K3M-Q9XZ", "MEMB-7K3M-9QXY", "MEMB-7K3M-Q9X", "31234567", "P00012", "", None):
            self.assertIsNone(member_id.parse(text, bare=True), text)


class FakeChurchDatabase(FakeBreeze):
    """The fake connector can return Member IDs with legacy IDs during an upgrade or rollback."""

    def __init__(self):
        super().__init__()
        self.upgraded = False
        self.new_ids = {}            # old ID -> Member ID, for people and families
        self.serial = 0

    def checkin(self, old):
        if old not in self.new_ids:
            self.serial += 1
            self.new_ids[old] = member_id.make(f"{self.serial:06d}A")
        return self.new_ids[old]

    def old(self, value):
        return next((o for o, n in self.new_ids.items() if n == value), value)

    def answer(self, path, q):
        asked = self.upgraded and q.get("ids") == "member"
        if self.upgraded and "person_id" in q:
            q = dict(q, person_id=self.old(q["person_id"]))
        if self.upgraded and "target_person_id" in q:
            q = dict(q, target_person_id=self.old(q["target_person_id"]))
        reply = super().answer(path, q)
        if not asked:
            return reply
        if path == "people":
            return [self._new_shape(p) for p in reply]
        if path == "people/add":
            (row,) = reply
            return [dict(row, id=self.checkin(row["id"]), legacy_id=row["id"],
                         details={"person_id": self.checkin(row["id"])})]
        if path == "events/attendance/list":
            return [dict(r, person_id=self.checkin(r["person_id"])) for r in reply]
        return reply

    def _new_shape(self, person):
        old = person["id"]
        row = json.loads(json.dumps(person))
        row.update(id=self.checkin(old), legacy_id=old)
        row["details"]["person_id"] = self.checkin(old)
        for member in row["family"]:
            member.update(person_id=self.checkin(member["person_id"]), legacy_family_id=member["family_id"],
                          family_id=self.checkin("F" + member["family_id"]))
        return row


class SwitchTests(unittest.TestCase):
    families = 30
    run_kind, family, checkin = test_breeze.TestBreezeSync.run_kind, test_breeze.TestBreezeSync.family, test_breeze.TestBreezeSync.checkin

    def setUp(self):
        test_breeze.TestBreezeSync.setUp(self)
        plain, self.fake = self.fake, FakeChurchDatabase()
        self.fake.people, self.fake.events = plain.people, plain.events
        plain.close()
        with self.app.db.tx() as conn:
            conn.execute("UPDATE session SET breeze_event_name='Sample Sunday Service'")
        self.sync.connect_chms(self.fake.url, KEY)

    def tearDown(self):
        test_breeze.TestBreezeSync.tearDown(self)

    def roster(self):
        return {r["id"]: r for r in self.app.db.all(
            "SELECT p.id, p.breeze_id, p.legacy_id, p.active, p.classroom_id, m.household_id, h.family_key "
            "FROM person p LEFT JOIN household_member m ON m.person_id=p.id AND m.active=1 "
            "LEFT JOIN household h ON h.id=m.household_id WHERE p.breeze_id IS NOT NULL AND p.active=1")}

    def switch(self):
        """Before: synced on old IDs, one family's check-in sent, another's waiting. Then the upgrade."""
        self.run_kind("sync")
        self.sent, self.waiting = self.family(0), self.family(1)
        self.checkin(self.sent)
        self.run_kind("sync")
        self.checkin(self.waiting)
        self.before = self.roster()
        self.fake.upgraded = True

    def test_a_check_shows_nothing_new_and_changes_nothing(self):
        self.switch()
        plan = self.run_kind("check")
        totals = plan["people"]["totals"]
        self.assertEqual((totals["people_new"], plan["people"]["not_in_breeze"]), (0, 0))
        self.assertTrue(plan["people"]["deactivate"])
        self.assertEqual(plan["people"]["new_ids"]["people"], len(self.before))
        self.assertEqual(self.roster(), self.before)                 # a check writes nothing
        self.assertFalse(self.households())
        self.run_kind("sync")                                         # the check counted what the sync moves
        self.assertEqual(self.households(), plan["people"]["new_ids"]["households"])
        self.assertTrue(self.households())

    def households(self):
        return self.app.db.scalar("SELECT COUNT(*) FROM household WHERE family_key LIKE '_:MEMB-%'")

    def test_the_first_sync_moves_everyone_to_member_ids_and_keeps_everything_else(self):
        self.switch()
        result = self.run_kind("sync")["summary"]
        self.assertEqual((result["people_new"], result["deactivated"], result["problems"]), (0, 0, []))
        self.assertEqual(result["new_ids"], len(self.before))
        after = self.roster()
        self.assertEqual(after.keys(), self.before.keys())            # the same people, not new copies
        for pid, row in after.items():
            old = self.before[pid]
            self.assertEqual(row["breeze_id"], self.fake.checkin(old["breeze_id"]))
            self.assertEqual(row["legacy_id"], old["breeze_id"])
            self.assertEqual((row["active"], row["household_id"], row["classroom_id"]),
                             (old["active"], old["household_id"], old["classroom_id"]))
            if old["family_key"].startswith("F:"):
                self.assertEqual(row["family_key"], "F:" + self.fake.checkin("F" + old["family_key"][2:]))
        # The waiting check-ins went with Member IDs; the ones sent before weren't sent twice.
        self.assertEqual(result["attendance_sent"], len(self.waiting["people"]))
        self.assertEqual(result["attendance_already"], 0)
        self.assertEqual(len(self.fake.attendance["300000001"]), len(self.sent["people"]) + len(self.waiting["people"]))
        again = self.run_kind("sync")["summary"]
        self.assertEqual((again["people_new"], again["people_updated"], again["new_ids"]), (0, 0, 0))

    def test_undoing_a_check_in_sent_before_the_switch_still_removes_it(self):
        self.switch()
        self.run_kind("sync")
        txn = self.app.db.scalar("SELECT txn_id FROM attendance WHERE person_id=?", (self.sent["people"][0]["id"],))
        self.app.service.undo(txn, "Test check-in", self.app.db.one("SELECT * FROM station"))
        self.assertEqual(self.run_kind("sync")["summary"]["attendance_removed"], len(self.sent["people"]))
        self.assertEqual(len(self.fake.attendance["300000001"]), len(self.waiting["people"]))

    def test_a_rollback_of_the_church_database_moves_back_to_the_old_ids(self):
        self.switch()
        self.run_kind("sync")
        self.fake.upgraded = False
        result = self.run_kind("sync")["summary"]
        self.assertEqual((result["people_new"], result["deactivated"]), (0, 0))
        after = self.roster()
        for pid, row in after.items():
            self.assertEqual((row["breeze_id"], row["family_key"], row["household_id"]),
                             (self.before[pid]["breeze_id"], self.before[pid]["family_key"],
                              self.before[pid]["household_id"]))
        self.fake.upgraded = True                                     # and forward again
        self.assertEqual(self.run_kind("sync")["summary"]["deactivated"], 0)
        self.assertTrue(all(r["breeze_id"].startswith("MEMB-") for r in self.roster().values()))

    def test_newcomers_added_after_the_switch_get_their_member_id(self):
        self.switch()
        self.run_kind("sync")
        station = self.app.db.one("SELECT * FROM station")
        visitor = self.app.service.register_visitor({
            "guardian_name": "Nova Newcomer", "phone": "9000 0001",
            "members": [{"first_name": "Nia", "last_name": "Newcomer"}]}, station)
        self.checkin(self.app.service.household(visitor["household_id"], self.session["id"]))
        result = self.run_kind("sync")["summary"]
        self.assertEqual((result["added"], result["problems"]), (2, []))
        added = self.app.db.all("SELECT breeze_id FROM person WHERE source='breeze' AND visitor_id IS NOT NULL")
        self.assertEqual(len(added), 2)
        for row in added:
            self.assertEqual(member_id.parse(row["breeze_id"]), row["breeze_id"])
        self.assertEqual(self.fake.families[-1][0], "families/create")
        self.assertTrue(all(i.startswith("MEMB-") for i in self.fake.families[-1][1]))
        self.run_kind("sync")
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM person WHERE source='breeze' AND visitor_id IS NOT "
                                            "NULL AND legacy_id IS NOT NULL"), 2)

    def test_a_link_chosen_with_an_old_id_before_the_switch_still_links(self):
        self.run_kind("sync")
        existing = self.fake.people[0]
        self.app.service.register_visitor({"guardian_name": f"{existing['force_first_name']} {existing['last_name']}",
                                           "phone": "9876 5432"}, self.station)
        (newcomer,) = self.run_kind("check")["newcomers"]
        old = newcomer["candidates"][0]["breeze_id"]
        self.assertFalse(old.startswith("MEMB-"))
        self.fake.upgraded = True
        result = self.run_kind("sync", {str(newcomer["id"]): "link:" + old})["summary"]
        self.assertEqual((result["linked"], result["added"]), (1, 0))
        retired = self.app.db.one("SELECT active, notes FROM person WHERE id=?", (newcomer["id"],))
        self.assertEqual(retired["active"], 0)                        # merged into the member, by Member ID
        self.assertTrue(retired["notes"].endswith("Linked to " + self.fake.checkin(old)), retired["notes"])
        self.assertFalse(self.app.db.scalar("SELECT COUNT(*) FROM person WHERE breeze_id=?", (old,)))

    def test_an_older_church_database_keeps_working_as_before(self):
        self.run_kind("sync")
        self.run_kind("sync")
        self.assertFalse(self.app.db.scalar("SELECT COUNT(*) FROM person WHERE breeze_id LIKE 'MEMB-%'"))
        asked = [q for path, q in self.fake.calls if path in ("/api/people", "/api/events/attendance/list")]
        self.assertTrue(asked and all(q.get("ids") == "member" for q in asked))


class AdminTests(unittest.TestCase):
    families = 5
    setUp, tearDown = test_breeze.TestBreezeSync.setUp, test_breeze.TestBreezeSync.tearDown

    def put(self, pid, value):
        from checkin import server
        ctx = type("C", (), {"app": self.app, "params": (str(pid),), "body": {"breeze_id": value}, "user_id": None})
        try:
            server.edit_person(ctx)
        except server.HttpError as err:
            return err.status, str(err)
        return 200, self.app.db.one("SELECT breeze_id, legacy_id FROM person WHERE id=?", (pid,))

    def test_an_administrator_can_type_an_member_id_any_way_but_not_with_a_typo(self):
        a, b = [r["id"] for r in self.app.db.all("SELECT id FROM person WHERE breeze_id IS NOT NULL LIMIT 2")]
        with self.app.db.tx() as conn:
            conn.execute("UPDATE person SET legacy_id='31234567' WHERE id=?", (a,))
        self.assertEqual(self.put(a, "MEMB-7K3M-Q9XZ"), (400, "That member ID has a typo. Check it and try again."))
        status, row = self.put(a, "memb 7k3m q9xy")
        self.assertEqual((status, row["breeze_id"], row["legacy_id"]), (200, "MEMB-7K3M-Q9XY", None))
        self.assertEqual(self.put(b, "7K3MQ9XY"), (409, "Another person already has that member ID."))

    def test_the_admin_screen_and_reports_say_member_id(self):
        root = Path(__file__).resolve().parent.parent
        admin = (root / "web" / "admin.js").read_text(encoding="utf-8")
        self.assertIn('field("Member ID"', admin)
        self.assertNotIn('field("Breeze ID"', admin)
        source = (root / "checkin" / "reports.py").read_text(encoding="utf-8")
        self.assertIn("'Member ID'", source)
        self.assertNotIn("'Breeze ID'", source)

    def test_admin_people_search_finds_an_member_id_however_typed_and_an_old_id(self):
        person = self.app.db.one("SELECT id FROM person WHERE breeze_id IS NOT NULL ORDER BY id LIMIT 1")
        with self.app.db.tx() as conn:
            conn.execute("UPDATE person SET legacy_id=breeze_id, breeze_id='MEMB-7K3M-Q9XY' WHERE id=?", (person["id"],))
        old = self.app.db.scalar("SELECT legacy_id FROM person WHERE id=?", (person["id"],))
        for typed in ("MEMB-7K3M-Q9XY", "memb7k3mq9xy", "7k3m q9xy", old):
            self.assertEqual([r["id"] for r in reports.people_search(self.app.db, typed)], [person["id"]], typed)


if __name__ == "__main__":
    unittest.main()
