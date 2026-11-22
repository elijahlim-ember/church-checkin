"""Sunday close and count-only report regressions."""

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ["CHECKIN_NO_USB"] = "1"
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkin.breeze import BreezeError
from checkin.service import _after_report
from checkin.util import SGT
from test_acceptance import make_app


class TestSundayWrapup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-wrapup-")
        self.app = make_app(self.tmp, 3)
        self.service_id = self.app.db.scalar("SELECT id FROM service ORDER BY id LIMIT 1")
        self.sunday = dt.date(2026, 11, 22)

    def tearDown(self):
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clock(self, day=None, hour=23, minute=59):
        return lambda: dt.datetime.combine(day or self.sunday, dt.time(hour, minute), SGT)

    def session(self, day=None, weekday=6):
        day = day or self.sunday
        existing = self.app.db.one("SELECT id FROM session WHERE service_id=? AND date=?", (self.service_id, day.isoformat()))
        if existing:
            with self.app.db.tx() as conn:
                conn.execute("UPDATE session SET status='open',closed_at=NULL WHERE id=?", (existing["id"],))
            return existing["id"]
        with self.app.db.tx() as conn:
            cur = conn.execute("INSERT INTO session(service_id,date,name,start_time,status,opened_at) "
                               "VALUES(?,?,?,?, 'open',?)",
                               (self.service_id, day.isoformat(), "Sunday Service", "09:00", "test"))
        return cur.lastrowid

    def test_2358_leaves_open_and_2359_closes_sunday_sessions(self):
        self.session()
        self.assertIsNone(self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=self.clock(minute=58)))
        self.assertEqual(self.app.db.scalar("SELECT status FROM session WHERE date=?", (self.sunday.isoformat(),)), "open")
        report = self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=self.clock(minute=59))
        self.assertEqual(report["sessions_closed"], 1)
        self.assertEqual(self.app.db.scalar("SELECT status FROM session WHERE date=?", (self.sunday.isoformat(),)), "closed")

    def test_non_sunday_sessions_are_not_closed(self):
        saturday = dt.date(2026, 11, 21)
        with self.app.db.tx() as conn:
            conn.execute("INSERT INTO session(service_id,date,name,start_time,status,opened_at) "
                         "VALUES(?,?,?,?, 'open','test')", (self.service_id, saturday.isoformat(), "Saturday", "09:00"))
        self.assertIsNone(self.app.service.wrap_up_sunday(saturday.isoformat(), clock=self.clock(saturday, 23, 59)))
        self.assertEqual(self.app.db.scalar("SELECT status FROM session WHERE date=?", (saturday.isoformat(),)), "open")

    def test_naive_closed_at_is_compared_as_singapore_time(self):
        self.assertFalse(_after_report("2026-11-22T23:50:00", "2026-11-22T23:59:00+08:00"))
        self.assertTrue(_after_report("2026-11-23T00:01:00", "2026-11-22T23:59:00+08:00"))

    def test_scheduler_skips_non_sunday_date_with_sunday_service(self):
        saturday = dt.date(2026, 11, 21)
        self.session(saturday, weekday=6)
        calls = []

        class OneTick:
            def __init__(inner): inner.calls = 0
            def wait(inner, _seconds):
                inner.calls += 1
                return inner.calls > 1
            def is_set(inner): return False

        old_stop = self.app._stop
        self.app._stop = OneTick()
        self.app.service.wrap_up_sunday = lambda *args, **kwargs: calls.append(args)
        self.app.breeze.deliver_pending_reports = lambda **kwargs: 0
        try:
            with patch("checkin.app.sg_now", return_value=dt.datetime(2026, 11, 21, 23, 59, tzinfo=SGT)):
                self.app._sunday_wrapup_loop()
        finally:
            self.app._stop = old_stop
        self.assertEqual(calls, [])

    def test_repeating_closed_report_is_idempotent(self):
        self.session()
        clock = self.clock()
        first = self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=clock)
        stored = self.app.db.one("SELECT * FROM day_report WHERE date=?", (self.sunday.isoformat(),))
        self.assertIsNone(self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=clock))
        again = self.app.db.one("SELECT * FROM day_report WHERE date=?", (self.sunday.isoformat(),))
        self.assertEqual(stored, again)
        self.assertEqual(first["sessions_closed"], 1)

    def add_attendance(self, session_id, person_id, status="in", txn_id="wrap-txn-0001"):
        with self.app.db.tx() as conn:
            conn.execute("INSERT INTO checkin_txn(id,session_id,security_code,code_date,status,created_at) "
                         "VALUES(?,?,?,?,'committed','test')", (txn_id, session_id, "ACDE", self.sunday.isoformat()))
            conn.execute("INSERT INTO attendance(session_id,person_id,txn_id,status,checked_in_at,class_name) "
                         "VALUES(?,?,?,?,?,?)", (session_id, person_id, txn_id, status, "test", "Adults"))

    def test_reopen_and_new_attendance_rebuilds_and_clears_delivery(self):
        sid = self.session()
        person = self.app.db.scalar("SELECT id FROM person WHERE active=1 ORDER BY id LIMIT 1")
        self.add_attendance(sid, person)
        self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=self.clock())
        with self.app.db.tx() as conn:
            conn.execute("UPDATE day_report SET delivered_at='delivered' WHERE date=?", (self.sunday.isoformat(),))
            conn.execute("UPDATE session SET status='open',closed_at=NULL WHERE id=?", (sid,))
        second_person = self.app.db.scalar("SELECT id FROM person WHERE active=1 AND id<>? ORDER BY id LIMIT 1", (person,))
        self.add_attendance(sid, second_person, txn_id="wrap-txn-0002")
        rebuilt = self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=self.clock())
        row = self.app.db.one("SELECT delivered_at,payload FROM day_report WHERE date=?", (self.sunday.isoformat(),))
        self.assertIsNone(row["delivered_at"])
        self.assertEqual(rebuilt["services"][0]["checked_in"], 2)

    def test_undone_rows_count_only_as_undone(self):
        sid = self.session()
        people = self.app.db.all("SELECT id FROM person WHERE active=1 ORDER BY id LIMIT 2")
        self.add_attendance(sid, people[0]["id"], "undone", "wrap-undone-0001")
        self.add_attendance(sid, people[1]["id"], "in", "wrap-current-0001")
        report = self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=self.clock())
        service = report["services"][0]
        self.assertEqual(service["checked_in"], 1)
        self.assertEqual(service["undone"], 1)

    def test_count_only_payload_and_wrapup_log_have_no_seeded_pii(self):
        sid = self.session()
        visitor = self.app.service.register_visitor({"guardian_name": "Wrapup Private Name", "phone": "93334442"},
                                                    self.app.db.one("SELECT * FROM station"))
        person = self.app.db.one("SELECT id,visitor_id FROM person WHERE id=?", (visitor["person_ids"][0],))
        self.add_attendance(sid, person["id"])
        with self.assertLogs("checkin", level="INFO") as captured:
            report = self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=self.clock())
        serialised = json.dumps(report)
        visitor_phone = "93334442"
        for private in ("Wrapup Private Name", visitor_phone, person["visitor_id"]):
            self.assertNotIn(private, serialised)
            self.assertNotIn(private, "\n".join(captured.output))
        self.assertEqual(len(captured.output), 1)

    def test_breeze_failure_is_recorded_and_a_later_retry_delivers(self):
        attempts = []
        class Client:
            def sunday_report(inner, _payload):
                attempts.append(1)
                if len(attempts) == 1:
                    raise BreezeError("temporary report failure")
        self.session()
        self.app.breeze.connected = lambda: True
        self.app.breeze.auto_tick = lambda wait=False: False
        self.app.breeze.client_factory = lambda: Client()
        self.app.service.wrap_up_sunday(self.sunday.isoformat(), breeze=self.app.breeze, clock=self.clock())
        failed = self.app.db.one("SELECT delivery_error,attempts,next_try FROM day_report WHERE date=?", (self.sunday.isoformat(),))
        self.assertIn("BreezeError", failed["delivery_error"])
        self.assertEqual(failed["attempts"], 1)
        self.assertIsNotNone(failed["next_try"])
        self.assertEqual(self.app.breeze.deliver_pending_reports(now=self.clock()() + dt.timedelta(minutes=16)), 1)
        delivered = self.app.db.one("SELECT delivered_at,delivery_error,next_try FROM day_report WHERE date=?", (self.sunday.isoformat(),))
        self.assertIsNotNone(delivered["delivered_at"])
        self.assertEqual(delivered["delivery_error"], None)
        self.assertEqual(delivered["next_try"], None)

    def test_corrupt_report_does_not_block_later_report(self):
        with self.app.db.tx() as conn:
            conn.execute("INSERT INTO day_report(date,built_at,payload,late,attempts) VALUES(?,?,?,?,0)",
                         ("2026-11-15", "2026-11-15T23:59:00+08:00", "{bad", 0))
            conn.execute("INSERT INTO day_report(date,built_at,payload,late,attempts) VALUES(?,?,?,?,0)",
                         ("2026-11-22", "2026-11-22T23:59:00+08:00", "{}", 0))
        delivered = []

        class Client:
            def sunday_report(inner, payload): delivered.append(payload)

        self.app.breeze.connected = lambda: True
        self.app.breeze.client_factory = lambda: Client()
        self.assertEqual(self.app.breeze.deliver_pending_reports(now=self.clock()()), 1)
        failed = self.app.db.one("SELECT delivery_error,attempts,next_try FROM day_report WHERE date='2026-11-15'")
        self.assertIn("JSONDecodeError", failed["delivery_error"])
        self.assertEqual(failed["attempts"], 1)
        self.assertIsNotNone(failed["next_try"])
        self.assertEqual(delivered, [{}])

    def test_monday_catch_up_marks_report_late(self):
        self.session()
        monday = self.sunday + dt.timedelta(days=1)
        report = self.app.service.wrap_up_sunday(self.sunday.isoformat(), clock=self.clock(monday, 0, 1))
        self.assertTrue(report["late"])
        self.assertEqual(self.app.db.scalar("SELECT late FROM day_report WHERE date=?", (self.sunday.isoformat(),)), 1)


if __name__ == "__main__":
    unittest.main()
