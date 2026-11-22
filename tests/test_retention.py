"""The retention clean-up, which now also runs by itself once a day. Made-up visitors only."""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ["CHECKIN_NO_USB"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkin import reports  # noqa: E402
from test_acceptance import make_app  # noqa: E402


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-retention-")
        self.app = make_app(self.tmp, 5)
        self.station = self.app.db.one("SELECT * FROM station")

    def tearDown(self):
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_old_visitors_are_anonymised_and_recent_ones_kept(self):
        self.app.service.register_visitor({"guardian_name": "Old Visitor", "phone": "93330001"}, self.station)
        self.app.service.register_visitor({"guardian_name": "New Visitor", "phone": "93330002"}, self.station)
        with self.app.db.tx() as conn:
            old = conn.execute("SELECT id FROM person WHERE first_name='Old'").fetchone()["id"]
            conn.execute("UPDATE person SET created_at='2020-01-01 09:00:00' WHERE id=?", (old,))
            conn.execute("UPDATE attendance SET checked_in_at='2020-01-01 09:00:00' WHERE person_id=?", (old,))
        result = reports.run_retention(self.app.db)
        self.assertEqual(result["visitors_anonymised"], 1)
        rows = {r["id"]: r["anonymised"] for r in self.app.db.all("SELECT id, anonymised FROM person WHERE source='visitor'")}
        self.assertEqual(rows[old], 1)
        self.assertEqual(sum(rows.values()), 1)
        self.assertEqual(reports.run_retention(self.app.db)["visitors_anonymised"], 0)

    def test_the_daily_clean_up_is_running(self):
        self.assertIn("retention", [t.name for t in threading.enumerate() if t.is_alive()])


if __name__ == "__main__":
    unittest.main()
