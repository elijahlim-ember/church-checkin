"""Services linked to the church database's by number (1.10.0): a typo or rename can't stop a day's attendance."""

import unittest

from test_breeze import KEY, SyncBase


class TestServiceLink(SyncBase):
    def setUp(self):
        super().setUp()
        self.sync.connect_chms(self.fake.url, KEY)
        self.fake.events[0]["name"] = "Sunday Service"   # the same name as check-in's own service, to begin with

    def send(self):
        result = self.run_kind("sync")
        return result.get("summary", result)

    def write(self, sql, values):
        with self.app.db.tx() as conn:
            conn.execute(sql, values)

    def linked(self):
        return self.app.db.scalar("SELECT chms_service_id FROM service WHERE id=?", (self.session["service_id"],))

    def rename_database_event(self, name="Sample Linked Service"):
        self.fake.events[0]["name"] = name

    def one_checkin(self, index=0):
        hh = self.family(index)
        self.checkin(hh, [hh["people"][0]])

    def test_a_name_match_remembers_the_number(self):
        self.assertIsNone(self.linked())
        self.one_checkin()
        self.run_kind("check")
        self.assertIsNone(self.linked())           # a check changes nothing
        result = self.send()
        self.assertEqual((result["attendance_sent"], result["problems"]), (1, []))
        self.assertEqual(self.linked(), "7")

    def test_once_linked_a_rename_in_the_database_still_matches(self):
        self.one_checkin()
        self.send()
        self.rename_database_event()
        self.one_checkin(1)
        result = self.send()
        self.assertEqual((result["attendance_sent"], result["problems"]), (1, []))
        self.assertEqual(len(self.fake.attendance["300000001"]), 2)

    def test_unlinked_a_typo_is_still_a_problem(self):
        self.rename_database_event()
        self.one_checkin()
        result = self.send()
        self.assertEqual(result["attendance_sent"], 0)
        self.assertIn("No 'Sunday Service' event", result["problems"][0])
        self.assertIsNone(self.linked())

    def test_a_link_picked_in_admin_beats_the_name(self):
        self.rename_database_event()
        self.write("UPDATE service SET chms_service_id='7' WHERE id=?", (self.session["service_id"],))
        self.one_checkin()
        result = self.send()
        self.assertEqual((result["attendance_sent"], result["problems"]), (1, []))

    def test_a_day_changed_by_hand_goes_by_its_own_name(self):
        self.one_checkin()
        self.send()                        # linked to 7
        self.fake.events.append(dict(self.fake.events[0], id="300000002", event_id="0", name="Combined Service"))
        self.write("UPDATE session SET breeze_event_name='Combined Service' WHERE id=?", (self.session["id"],))
        self.one_checkin(1)
        result = self.send()
        self.assertEqual(result["problems"], [])
        self.assertGreaterEqual(len(self.fake.attendance["300000002"]), 1)   # by the day's own name, not the link

    def test_admin_list_is_empty_for_an_older_database(self):
        self.assertEqual(self.sync.chms_services(), [])
        self.fake.services = [{"id": "7", "name": "Sample Sunday Service", "weekday": 6, "start_time": "09:30",
                               "congregation": "Sample Group"}]
        self.assertEqual(self.sync.chms_services(), [{"id": "7", "name": "Sample Sunday Service"}])


if __name__ == "__main__":
    unittest.main()
