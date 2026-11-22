"""One-time devices: a volunteer at any desk makes a short code, and a phone or spare iPad that types it becomes a
station until the end of the day, printing where that desk prints. No administrator is needed on a busy Sunday.

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
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkin.db import SCHEMA_VERSION  # noqa: E402
from test_acceptance import SETUP, make_app  # noqa: E402


class TestOneTimeDevices(unittest.TestCase):
    def setUp(self):
        from checkin.server import make_server
        self.tmp = tempfile.mkdtemp(prefix="checkin-onetime-")
        self.app = make_app(self.tmp, 6)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        status, admin = self.call("POST", "/api/admin/login", {"name": "Admin", "password": SETUP["admin_password"]})
        self.assertEqual(status, 200)
        status, reg = self.call("POST", "/api/admin/stations/register",
                                {"name": "iPad 1", "printer": "RIGHT-PRINTER", "fallback_printer": "LEFT-PRINTER"},
                                token=admin["token"])
        self.assertEqual(status, 200)
        self.admin = admin["token"]
        self.desk = reg["station_token"]
        self.volunteer = self.unlock(self.desk)

    def tearDown(self):
        self.server.shutdown()
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, path, body=None, station=None, token=None):
        headers = {"Content-Type": "application/json"}
        if station:
            headers["X-Station-Token"] = station
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def unlock(self, station):
        status, login = self.call("POST", "/api/login", {"pin": SETUP["volunteer_pin"]}, station=station)
        self.assertEqual(status, 200)
        return login["token"]

    def new_code(self):
        status, res = self.call("POST", "/api/stations/one-time", {}, station=self.desk, token=self.volunteer)
        self.assertEqual(status, 200)
        return res["code"]

    def join(self, code):
        return self.call("POST", "/api/stations/join", {"code": code})

    def test_schema_has_the_new_column(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 6)
        self.assertIsNone(self.app.db.scalar("SELECT expires_at FROM station WHERE name='iPad 1'"))

    def test_only_an_unlocked_desk_can_make_a_code(self):
        self.assertEqual(self.call("POST", "/api/stations/one-time", {})[0], 401)
        self.assertEqual(self.call("POST", "/api/stations/one-time", {}, station=self.desk)[0], 401)
        res = self.call("POST", "/api/stations/one-time", {}, station=self.desk, token=self.volunteer)[1]
        self.assertEqual(len(res["code"]), 6)
        self.assertEqual(res["minutes"], 10)

    def make_code(self, wifi=None):
        from checkin import network
        with mock.patch.object(network, "wifi_name", return_value=wifi) as asked:
            status, res = self.call("POST", "/api/stations/one-time", {}, station=self.desk, token=self.volunteer)
        self.assertEqual(status, 200)
        return res, asked

    def test_code_comes_with_a_qr_of_the_address_the_computer_has_now(self):
        from checkin import qr
        res, asked = self.make_code(wifi="Example-WiFi")
        self.assertEqual(res["urls"], [])  # no network address known, so no square and no Wi-Fi name
        self.assertFalse({"qr", "qr_url", "wifi"} & set(res))
        asked.assert_not_called()
        # The computer is at church when check-in starts...
        now = ["192.0.2.10"]
        self.app.lan_urls = ["http://192.0.2.10:8080/"]
        self.app.lan_url_source = lambda: [f"http://{address}:8080/" for address in now]
        res, _ = self.make_code(wifi="Example-WiFi")
        self.assertEqual((res["urls"], res["qr_url"], res["wifi"]),
                         (["http://192.0.2.10:8080/"], "http://192.0.2.10:8080/", "Example-WiFi"))
        self.assertEqual(res["qr"], qr.data_uri("http://192.0.2.10:8080/"))
        # ...and is carried to a camp without being restarted: the next code shows the new network.
        now[:] = ["10.20.30.40", "100.101.102.103"]
        res, _ = self.make_code(wifi="Example-WiFi")
        self.assertEqual((res["urls"][0], res["qr_url"], res["wifi"]),
                         ("http://10.20.30.40:8080/", "http://10.20.30.40:8080/", "Example-WiFi"))
        self.assertEqual(res["qr"], qr.data_uri("http://10.20.30.40:8080/"))
        self.assertNotEqual(res["qr"], qr.data_uri("http://192.0.2.10:8080/"))
        # Every other screen that shows the address follows.
        status, st = self.call("GET", "/api/status", station=self.desk, token=self.volunteer)
        self.assertEqual(st["server_urls"][0], "http://10.20.30.40:8080/")
        self.assertEqual(self.call("GET", "/api/admin/overview", token=self.admin)[1]["lan_urls"][0],
                         "http://10.20.30.40:8080/")

    def test_on_a_cable_the_reply_names_no_wifi(self):
        self.app.lan_url_source = lambda: ["http://192.0.2.10:8080/"]
        res, asked = self.make_code(wifi=None)
        asked.assert_called_once()
        self.assertNotIn("wifi", res)
        self.assertEqual(res["qr_url"], "http://192.0.2.10:8080/")

    def test_a_network_hiccup_still_gives_a_code(self):
        def broken():
            raise OSError("adapter went away")
        self.app.lan_urls = ["http://192.0.2.10:8080/"]
        self.app.lan_url_source = broken
        with self.assertLogs("checkin", level="ERROR"):
            res, _ = self.make_code()
        self.assertEqual(len(res["code"]), 6)
        self.assertEqual(res["urls"], ["http://192.0.2.10:8080/"])  # the last address it knew

    def test_the_wifi_name_is_given_only_to_an_unlocked_desk_and_never_kept(self):
        self.app.lan_url_source = lambda: ["http://10.20.30.40:8080/"]
        res, _ = self.make_code(wifi="Example-WiFi")
        self.assertEqual(res["wifi"], "Example-WiFi")
        from checkin import network
        with mock.patch.object(network, "wifi_name", return_value="Example-WiFi"):
            for station, token in ((None, None), (self.desk, None)):
                status, body = self.call("POST", "/api/stations/one-time", {}, station=station, token=token)
                self.assertEqual(status, 401)
                self.assertNotIn("Example-WiFi", json.dumps(body))
            status, st = self.call("GET", "/api/status", station=self.desk, token=self.volunteer)
        self.assertNotIn("Example-WiFi", json.dumps(st))
        kept = json.dumps([dict(r) for r in self.app.db.all("SELECT * FROM audit_log")], default=str)
        self.assertNotIn("Example-WiFi", kept)

    def test_the_dialog_has_words_for_each_case_in_both_languages(self):
        web = Path(__file__).resolve().parent.parent / "web"
        script = (web / "kiosk.js").read_text(encoding="utf-8")
        words = (web / "kiosk-strings.js").read_text(encoding="utf-8")
        for key in ("ot_open", "ot_open_wifi", "ot_no_address", "ot_stuck"):
            self.assertIn(f't("{key}"', script, key)
            self.assertEqual(words.count(f"      {key}: "), 2, key)  # English and Chinese
        self.assertNotIn("join-qr.png", script)
        self.assertFalse((web / "join-qr.png").exists())  # the hand-drawn square for one fixed address is gone
        self.assertIn("same Wi-Fi", words)
        self.assertIn("travel router or a phone hotspot", words)

    def test_join_makes_a_station_for_today_that_prints_at_the_desk(self):
        code = self.new_code()
        status, res = self.join(code.lower())  # typed in lower case still works
        self.assertEqual(status, 200)
        self.assertEqual(res["station"]["name"], "One-time 1")
        self.assertEqual(res["station"]["printer"], "RIGHT-PRINTER")
        self.assertEqual(res["station"]["fallback_printer"], "LEFT-PRINTER")
        self.assertTrue(res["station"]["until"].endswith("23:59:59"))
        phone = res["station_token"]
        # The device is a station now, but checking people in still needs the volunteer PIN.
        self.assertEqual(self.call("GET", "/api/search?q=1234", station=phone)[0], 401)
        token = self.unlock(phone)
        status, st = self.call("GET", "/api/status", station=phone, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(st["station"]["name"], "One-time 1")
        # A code works once.
        self.assertEqual(self.join(code)[0], 401)
        # A second device gets the next number.
        self.assertEqual(self.join(self.new_code())[1]["station"]["name"], "One-time 2")
        stations = self.call("GET", "/api/admin/stations", token=self.admin)[1]
        self.assertEqual([s["name"] for s in stations][-2:], ["One-time 1", "One-time 2"])  # listed last
        actions = [r["action"] for r in self.app.db.all("SELECT action FROM audit_log")]
        self.assertIn("station.one_time_code", actions)
        self.assertIn("station.one_time_join", actions)

    def test_wrong_codes_are_refused_then_blocked(self):
        self.new_code()
        for _ in range(5):
            self.assertEqual(self.join("AAAAAA")[0], 401)
        self.assertEqual(self.join("AAAAAA")[0], 429)

    def test_an_old_code_is_refused(self):
        code = self.new_code()
        with self.app.auth.device_codes_lock:
            for invite in self.app.auth.device_codes.values():
                invite["expires"] = time.time() - 1
        self.assertEqual(self.join(code)[0], 401)

    def test_the_device_stops_working_after_today_and_its_number_is_reused(self):
        res = self.join(self.new_code())[1]
        phone = res["station_token"]
        with self.app.db.tx() as conn:
            conn.execute("UPDATE station SET expires_at='2000-01-01 23:59:59' WHERE id=?", (res["station"]["id"],))
        status, err = self.call("GET", "/api/status", station=phone)
        self.assertEqual(status, 401)
        self.assertEqual(err["data"]["login"], "station")  # the kiosk goes back to device setup
        row = self.app.db.one("SELECT active, token_hash FROM station WHERE id=?", (res["station"]["id"],))
        self.assertEqual((row["active"], row["token_hash"]), (0, None))
        again = self.join(self.new_code())[1]
        self.assertEqual(again["station"]["name"], "One-time 1")
        self.assertEqual(again["station"]["id"], res["station"]["id"])
        self.assertEqual(self.call("GET", "/api/status", station=again["station_token"])[0], 200)

    def test_an_expired_device_nobody_used_does_not_hold_its_number(self):
        res = self.join(self.new_code())[1]
        with self.app.db.tx() as conn:
            conn.execute("UPDATE station SET expires_at='2000-01-01 23:59:59' WHERE id=?", (res["station"]["id"],))
        self.assertEqual(self.join(self.new_code())[1]["station"]["name"], "One-time 1")

    def test_a_code_from_a_revoked_desk_is_refused(self):
        code = self.new_code()
        with self.app.db.tx() as conn:
            conn.execute("UPDATE station SET active=0 WHERE name='iPad 1'")
        self.assertEqual(self.join(code)[0], 401)

    def test_a_one_time_device_cannot_make_a_device_that_outlives_it(self):
        from checkin.util import now
        import datetime as dt
        res = self.join(self.new_code())[1]
        phone = res["station_token"]
        soon = (now() + dt.timedelta(minutes=5)).isoformat(sep=" ")
        with self.app.db.tx() as conn:
            conn.execute("UPDATE station SET expires_at=? WHERE id=?", (soon, res["station"]["id"]))
        token = self.unlock(phone)
        status, made = self.call("POST", "/api/stations/one-time", {}, station=phone, token=token)
        self.assertEqual(status, 200)
        status, second = self.join(made["code"])
        self.assertEqual(status, 200)
        self.assertEqual(second["station"]["until"], soon)  # capped at the maker's own end
        # A code redeemed after the day it was made for has ended is refused (e.g. made at 23:58, typed at 00:01).
        made = self.call("POST", "/api/stations/one-time", {}, station=phone, token=token)[1]
        with self.app.auth.device_codes_lock:
            for invite in self.app.auth.device_codes.values():
                invite["until"] = "2000-01-01 23:59:59"
        self.assertEqual(self.join(made["code"])[0], 401)

    def test_volunteer_sessions_go_when_the_device_expires_or_its_number_is_reused(self):
        res = self.join(self.new_code())[1]
        sid = res["station"]["id"]
        old_token = self.unlock(res["station_token"])
        count = lambda: self.app.db.scalar("SELECT COUNT(*) FROM auth_session WHERE station_id=?", (sid,))  # noqa: E731
        self.assertEqual(count(), 1)
        with self.app.db.tx() as conn:
            conn.execute("UPDATE station SET expires_at='2000-01-01 23:59:59' WHERE id=?", (sid,))
        self.assertEqual(self.call("GET", "/api/status", station=res["station_token"])[0], 401)
        self.assertEqual(count(), 0)
        # Reuse without the expiry being noticed first: the old PIN session must not unlock the new device.
        res = self.join(self.new_code())[1]
        old_token = self.unlock(res["station_token"])
        with self.app.db.tx() as conn:
            conn.execute("UPDATE station SET expires_at='2000-01-01 23:59:59' WHERE id=?", (sid,))
        again = self.join(self.new_code())[1]
        self.assertEqual(again["station"]["id"], sid)
        self.assertEqual(count(), 0)
        self.assertEqual(self.call("GET", "/api/search?q=1234", station=again["station_token"], token=old_token)[0], 401)

    def test_registering_a_one_time_device_properly_makes_it_permanent(self):
        self.join(self.new_code())
        status, reg = self.call("POST", "/api/admin/stations/register", {"name": "One-time 1", "printer": "RIGHT-PRINTER"},
                                token=self.admin)
        self.assertEqual(status, 200)
        self.assertIsNone(self.app.db.scalar("SELECT expires_at FROM station WHERE name='One-time 1'"))


if __name__ == "__main__":
    unittest.main()
