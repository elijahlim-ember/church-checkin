"""Regression tests for the October 2026 audit: restore keeps the church database connection, an upgrade
stops when its safety backup fails, and the optional HTTPS listener for the iPads.

Run:  python -m unittest discover -s tests -v
"""

import json
import os

os.environ["CHECKIN_NO_USB"] = "1"  # never touch real printers from tests
import shutil
import socket
import sqlite3
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkin import breeze, tls  # noqa: E402
from checkin.app import SKIP_BACKUP_ENV, SKIP_RESTORE_BACKUP_ENV, App, RestoreBlocked, UpgradeBlocked  # noqa: E402
from checkin.backup import BackupManager  # noqa: E402
from checkin.db import MIGRATIONS, _split_sql  # noqa: E402
from test_acceptance import SETUP, make_app  # noqa: E402

FAKE_KEY = "dummy-church-database-key-for-tests"


class TestRestoreKeepsChurchDatabase(unittest.TestCase):
    def test_replacement_computer_stays_connected_to_church_database(self):
        tmp = tempfile.mkdtemp(prefix="compatible service-keys-")
        fresh_dir = tempfile.mkdtemp(prefix="checkin-new-pc-")
        try:
            app = make_app(tmp, 5)
            breeze.save_key(app.paths, FAKE_KEY, breeze.CHMS_KEY_FILE)
            (app.paths.keys / breeze.CHMS_CA_FILE).write_text("-----BEGIN CERTIFICATE-----\ntest\n", encoding="utf-8")
            tls.ensure_certificates(app.paths.keys, ["main-pc"], ["192.0.2.10"])
            ca = (app.paths.keys / tls.CA_CERT).read_bytes()
            self.assertTrue(app.breeze.connected())
            data = Path(app.backups.create("test")["path"]).read_bytes()
            app.stop()

            fresh = App(fresh_dir)
            fresh.start()
            self.assertFalse(fresh.breeze.connected())
            fresh.restore(data, SETUP["backup_passphrase"])
            self.assertTrue(fresh.breeze.connected())
            self.assertEqual(breeze.load_key(fresh.paths, breeze.CHMS_KEY_FILE), FAKE_KEY)
            self.assertTrue((fresh.paths.keys / breeze.CHMS_CA_FILE).is_file())
            self.assertEqual((fresh.paths.keys / tls.CA_CERT).read_bytes(), ca)  # iPads keep trusting it
            self.assertTrue((fresh.paths.keys / tls.CA_KEY).is_file())
            fresh.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(fresh_dir, ignore_errors=True)


class TestUpgradeNeedsBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-upgrade-backup-")
        keys = Path(self.tmp, "keys")
        keys.mkdir(parents=True)
        (keys / "backup.key").write_bytes(os.urandom(48))  # a backup passphrase was set
        conn = sqlite3.connect(os.path.join(self.tmp, "checkin.db"))
        for statement in _split_sql(MIGRATIONS[0]):
            conn.execute(statement)
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
        conn.execute("INSERT INTO person(first_name, last_name, breeze_id) VALUES('Kept', 'Safe', '9')")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def version(self):
        conn = sqlite3.connect(os.path.join(self.tmp, "checkin.db"))
        try:
            return conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        finally:
            conn.close()

    def test_upgrade_makes_a_backup_first(self):
        app = App(self.tmp)
        self.assertEqual(self.version(), str(len(MIGRATIONS)))
        self.assertTrue(any("pre-migration" in p.name for p in app.paths.backups.iterdir()))
        app.db.close()

    def test_upgrade_from_1_8_0_adds_the_add_code_and_family_links(self):
        os.remove(os.path.join(self.tmp, "checkin.db"))
        conn = sqlite3.connect(os.path.join(self.tmp, "checkin.db"))
        for migration in MIGRATIONS[:6]:                         # schema 6: check-in 1.8.0
            for statement in _split_sql(migration):
                conn.execute(statement)
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '6')")
        conn.execute("INSERT INTO person(first_name, last_name, source, visitor_id) "
                     "VALUES('Vera', 'Visitor', 'visitor', 'VIS-ABC123')")
        conn.commit()
        conn.close()
        app = App(self.tmp)
        self.assertEqual(self.version(), "9")                    # v9 adds count-only Sunday reports
        self.assertTrue(any("pre-migration" in p.name for p in app.paths.backups.iterdir()))
        self.assertEqual(app.db.scalar("SELECT COUNT(*) FROM service WHERE chms_service_id IS NOT NULL"), 0)
        vera = app.db.one("SELECT first_name, add_code FROM person WHERE visitor_id='VIS-ABC123'")
        self.assertEqual((vera["first_name"], vera["add_code"]), ("Vera", None))   # not added yet: no code
        self.assertEqual(app.db.scalar("SELECT COUNT(*) FROM family_link"), 0)
        app.db.close()

    def test_failed_backup_stops_the_upgrade(self):
        with mock.patch.object(BackupManager, "create", side_effect=OSError("No space left on device")), \
                mock.patch.dict(os.environ, {SKIP_BACKUP_ENV: ""}):
            with self.assertRaises(UpgradeBlocked) as err:
                App(self.tmp)
        self.assertIn("No space left on device", str(err.exception))
        self.assertIn(SKIP_BACKUP_ENV, str(err.exception))
        self.assertEqual(self.version(), "1")  # nothing changed

    def test_override_upgrades_without_backup(self):
        with mock.patch.object(BackupManager, "create", side_effect=OSError("No space left on device")), \
                mock.patch.dict(os.environ, {SKIP_BACKUP_ENV: "1"}):
            app = App(self.tmp)
        self.assertEqual(self.version(), str(len(MIGRATIONS)))
        self.assertEqual(app.db.one("SELECT first_name FROM person WHERE breeze_id='9'")["first_name"], "Kept")
        app.db.close()

    def test_command_line_reports_blocked_upgrade(self):
        from checkin.__main__ import main
        with mock.patch.object(BackupManager, "create", side_effect=OSError("disk full")), \
                mock.patch.dict(os.environ, {SKIP_BACKUP_ENV: ""}), \
                mock.patch("sys.stderr") as stderr:
            self.assertEqual(main(["--data", self.tmp, "serve"]), 2)
        self.assertIn("Not started", "".join(c.args[0] for c in stderr.write.call_args_list))
        self.assertEqual(self.version(), "1")


class TestRestoreNeedsBackup(unittest.TestCase):
    """A restore replaces all the data, so it stops if the safety copy of the current data fails (part 2, #2)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-restore-backup-")
        self.app = make_app(self.tmp, 3)
        self.data = Path(self.app.backups.create("test")["path"]).read_bytes()
        self.app.db.set_setting("test_marker", "after the backup")

    def tearDown(self):
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failed_safety_backup_stops_the_restore(self):
        with mock.patch.object(BackupManager, "create", side_effect=OSError("No space left on device")), \
                mock.patch.dict(os.environ, {SKIP_RESTORE_BACKUP_ENV: ""}):
            with self.assertRaises(RestoreBlocked) as err:
                self.app.restore(self.data, None)
        self.assertIn("No space left on device", str(err.exception))
        self.assertIn(SKIP_RESTORE_BACKUP_ENV, str(err.exception))
        self.assertEqual(self.app.db.get_setting("test_marker"), "after the backup")  # nothing changed
        self.assertFalse(self.app.restore_lock.locked())

    def test_override_restores_without_safety_backup(self):
        with mock.patch.object(BackupManager, "create", side_effect=OSError("No space left on device")), \
                mock.patch.dict(os.environ, {SKIP_RESTORE_BACKUP_ENV: "1"}):
            manifest = self.app.restore(self.data, None)
        self.assertIsNone(manifest["safety_backup"])
        self.assertIsNone(self.app.db.get_setting("test_marker"))

    def test_command_line_reports_blocked_restore(self):
        from checkin.__main__ import main
        chosen = Path(self.tmp, "chosen.checkinbak")
        chosen.write_bytes(self.data)
        with mock.patch.object(BackupManager, "create", side_effect=OSError("disk full")), \
                mock.patch.dict(os.environ, {SKIP_RESTORE_BACKUP_ENV: ""}), \
                mock.patch("getpass.getpass", return_value=SETUP["backup_passphrase"]), \
                mock.patch("builtins.input", return_value="RESTORE"), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr") as stderr:
            self.assertEqual(main(["--data", self.tmp, "restore", str(chosen)]), 2)
        self.assertIn("Not restored", "".join(c.args[0] for c in stderr.write.call_args_list))
        self.assertEqual(self.app.db.get_setting("test_marker"), "after the backup")


class TestCertificates(unittest.TestCase):
    def setUp(self):
        self.keys = Path(tempfile.mkdtemp(prefix="checkin-tls-"))

    def tearDown(self):
        shutil.rmtree(self.keys, ignore_errors=True)

    def test_made_once_and_renewed_when_needed(self):
        from cryptography import x509
        first = tls.ensure_certificates(self.keys, ["main-pc"], ["192.0.2.10"])
        ca = first.ca_cert.read_bytes()
        server = first.server_cert.read_bytes()
        self.assertEqual(tls.ensure_certificates(self.keys, ["main-pc"], ["192.0.2.10"]).server_cert.read_bytes(),
                         server)                                                  # still good: kept
        cert = x509.load_pem_x509_certificate(server)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn("main-pc", san.get_values_for_type(x509.DNSName))
        self.assertIn("192.0.2.10", [str(a) for a in san.get_values_for_type(x509.IPAddress)])
        self.assertLessEqual((cert.not_valid_after_utc - cert.not_valid_before_utc).days, tls.SERVER_DAYS)
        tls.ensure_certificates(self.keys, ["main-pc"], ["192.168.0.11"])           # new address: reissued
        self.assertNotEqual(first.server_cert.read_bytes(), server)
        later = datetime.now(timezone.utc) + timedelta(days=tls.SERVER_DAYS - tls.RENEW_DAYS + 1)
        renewed = first.server_cert.read_bytes()
        tls.ensure_certificates(self.keys, ["main-pc"], ["192.168.0.11"], now=later)  # near expiry: renewed
        self.assertNotEqual(first.server_cert.read_bytes(), renewed)
        self.assertEqual(first.ca_cert.read_bytes(), ca)                           # the CA never changes
        if os.name == "posix":
            self.assertEqual((self.keys / tls.CA_KEY).stat().st_mode & 0o777, 0o600)

    def test_restored_ca_replaces_server_certificate(self):
        tls.ensure_certificates(self.keys, ["main-pc"], ["192.0.2.10"])
        other = Path(tempfile.mkdtemp(prefix="checkin-tls-other-"))
        try:
            tls.ensure_certificates(other, ["main-pc"], ["192.0.2.10"])
            for name in (tls.CA_CERT, tls.CA_KEY):                                 # as a restore does
                shutil.copy(other / name, self.keys / name)
            files = tls.ensure_certificates(self.keys, ["main-pc"], ["192.0.2.10"])
            from cryptography import x509
            cert = x509.load_pem_x509_certificate(files.server_cert.read_bytes())
            cert.verify_directly_issued_by(x509.load_pem_x509_certificate((other / tls.CA_CERT).read_bytes()))
        finally:
            shutil.rmtree(other, ignore_errors=True)


class TestHttpsListener(unittest.TestCase):
    def test_ipad_api_over_https_and_ca_download(self):
        from checkin.server import make_server
        tmp = tempfile.mkdtemp(prefix="checkin-https-")
        app = make_app(tmp, 5)
        files = tls.ensure_certificates(app.paths.keys, [], [])
        server = make_server(app, "127.0.0.1", 0, tls.server_context(files))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        trusted = ssl.create_default_context(cafile=str(files.ca_cert))
        try:
            with urllib.request.urlopen(f"https://localhost:{port}/api/ping", context=trusted) as resp:
                self.assertEqual(resp.status, 200)
            req = urllib.request.Request(f"https://127.0.0.1:{port}/api/admin/login", method="POST",
                                         headers={"Content-Type": "application/json"},
                                         data=json.dumps({"name": "Admin",
                                                          "password": SETUP["admin_password"]}).encode())
            with urllib.request.urlopen(req, context=trusted) as resp:
                self.assertIn("token", json.loads(resp.read()))
            with urllib.request.urlopen(f"https://localhost:{port}/ca.crt", context=trusted) as resp:
                self.assertEqual(resp.read(), files.ca_cert.read_bytes())
                self.assertEqual(resp.headers["Content-Type"], "application/x-x509-ca-cert")
            with self.assertRaises(urllib.error.URLError):                         # untrusted: refused
                urllib.request.urlopen(f"https://localhost:{port}/api/ping", context=ssl.create_default_context())
            with socket.create_connection(("127.0.0.1", port), timeout=5) as plain:  # http:// to the https port
                plain.sendall(b"GET /api/ping HTTP/1.1\r\nHost: x\r\n\r\n")
                try:
                    self.assertNotIn(b"200", plain.recv(100))                      # dropped, not answered
                except ConnectionResetError:
                    pass
            with urllib.request.urlopen(f"https://localhost:{port}/api/ping", context=trusted) as resp:
                self.assertEqual(resp.status, 200)                                 # still serving others
        finally:
            server.shutdown()
            server.server_close()
            app.stop()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_ca_until_https_is_on(self):
        from checkin.server import make_server
        tmp = tempfile.mkdtemp(prefix="checkin-http-ca-")
        app = make_app(tmp, 5)
        server = make_server(app, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/ca.crt")
            self.assertEqual(err.exception.code, 404)
            err.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            app.stop()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_serve_keeps_plain_http_on_this_computer_only(self):
        from checkin import __main__ as cli
        tmp = tempfile.mkdtemp(prefix="checkin-serve-")
        try:
            Path(tmp, "config.json").write_text(json.dumps({"port": 8080, "https_port": 8443}), encoding="utf-8")
            made = []

            def fake_make_server(app, host, port, context=None):
                made.append((host, port, context is not None))
                server = mock.Mock()
                server.serve_forever.side_effect = KeyboardInterrupt if context is None else None
                return server
            with mock.patch("checkin.server.make_server", fake_make_server), \
                    mock.patch.object(cli, "_lan_addresses", return_value=["192.0.2.10"]), \
                    mock.patch("builtins.print"):
                cli.main(["--data", tmp, "serve"])
            self.assertEqual(sorted(made), [("0.0.0.0", 8443, True),     # HTTPS: the iPads
                                            ("127.0.0.1", 8080, False)])  # plain HTTP: this computer only
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestExportsAndRestorePaths(unittest.TestCase):
    def test_report_csv_keeps_formulas_as_text_only_for_excel(self):
        from checkin.reports import to_csv
        rows = [{"Name": "=HYPERLINK(\"http://x\")", "Phone": "+65 9123 4567", "Class": "Adults"}]
        report = to_csv(["Name", "Phone", "Class"], rows, for_excel=True)
        self.assertIn("'=HYPERLINK", report)
        self.assertIn("'+65 9123 4567", report)
        self.assertIn(",Adults", report)
        self.assertNotIn("'", to_csv(["Name", "Phone", "Class"], rows))

    def test_report_csv_keeps_full_width_formula_signs_as_text(self):
        from checkin.reports import to_csv
        rows = [{"Name": "＝1+1"}, {"Name": "＋65"}, {"Name": "－1"}, {"Name": "＠SUM(1)"}]
        report = to_csv(["Name"], rows, for_excel=True)
        for sign in "＝＋－＠":
            self.assertIn("'" + sign, report)

    def test_report_refuses_a_date_that_is_not_a_date(self):
        from checkin.reports import ReportError, report
        tmp = tempfile.mkdtemp(prefix="checkin-report-date-")
        try:
            app = make_app(tmp, 2)
            for bad in ("2026-11-05\r\nSet-Cookie: x=1", "2026-11-05\"", "../x", "05/11/2026"):
                with self.assertRaises(ReportError) as caught:
                    report(app.db, "present", bad)
                self.assertEqual(caught.exception.status, 400)
            self.assertEqual(report(app.db, "present", "2026-11-05")["date"], "2026-11-05")
            app.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_download_filename_cannot_break_the_header(self):
        from checkin.server import download
        header = download("a,b", 'present-2026"\r\nSet-Cookie: x=1.csv').headers["Content-Disposition"]
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertEqual(header.count('"'), 2)
        self.assertTrue(header.startswith('attachment; filename="present-2026'))

    def test_restore_skips_template_paths_outside_the_folder(self):
        import io
        import zipfile
        tmp = tempfile.mkdtemp(prefix="checkin-zip-")
        try:
            app = make_app(tmp, 5)
            outside = Path(tmp).parent / f"{Path(tmp).name}-outside.json"
            manager = app.backups
            plain = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(manager.build_archive("test"))) as good, \
                    zipfile.ZipFile(plain, "w") as bad:
                for name in good.namelist():
                    bad.writestr(name, good.read(name))
                bad.writestr("templates/" + str(outside), b"{}")  # an absolute path inside the archive
            salt, key = manager._key()
            with mock.patch.object(manager, "decrypt", return_value=(plain.getvalue(), salt, key)):
                manager.restore_files(b"ignored")
            self.assertFalse(outside.exists())
        finally:
            app.stop()
            shutil.rmtree(tmp, ignore_errors=True)


class TestRequestBody(unittest.TestCase):
    def test_negative_length_refused_not_read_to_the_end(self):
        """Content-Length: -1 used to read until the sender hung up: no size limit, and no login needed."""
        from checkin.server import make_server
        tmp = tempfile.mkdtemp(prefix="checkin-body-")
        app = make_app(tmp, 5)
        server = make_server(app, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(b"POST /api/admin/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                             b"Content-Length: -1\r\n\r\n{}")  # the connection stays open
                self.assertTrue(sock.recv(100).startswith(b"HTTP/1.1 400"))
        finally:
            server.shutdown()
            server.server_close()
            app.stop()
            shutil.rmtree(tmp, ignore_errors=True)


class TestAdminLockout(unittest.TestCase):
    """Wrong administrator passwords lock the account (not only the iPad that typed them), for longer each time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-lock-")
        self.app = make_app(self.tmp, 5)
        self.auth = self.app.auth

    def tearDown(self):
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wrong(self, n, start=0):
        from checkin.auth import AuthError
        for i in range(start, start + n):
            with self.assertRaises(AuthError):
                self.auth.login_admin(SETUP["admin_name"], "wrong", None, f"admin:10.0.0.{i}")

    def right(self, client="admin:10.0.1.1", local=False):
        return self.auth.login_admin(SETUP["admin_name"], SETUP["admin_password"], None, client, local)

    def row(self):
        return self.app.db.one("SELECT * FROM user WHERE name=?", (SETUP["admin_name"],))

    def test_fifth_wrong_password_locks_even_from_other_ipads(self):
        from checkin.auth import AuthError
        self.wrong(4)
        self.assertIsNone(self.row()["locked_until"])
        self.wrong(1, start=4)
        with self.assertRaises(AuthError) as err:
            self.right()  # right password, new iPad: still locked
        self.assertEqual(err.exception.status, 429)
        self.assertIn("locked", str(err.exception))
        self.assertTrue(self.app.db.one("SELECT 1 FROM audit_log WHERE action='user.locked'"))

    def test_lock_grows_and_is_capped(self):
        from checkin.auth import account_lock_seconds
        self.assertEqual([account_lock_seconds(n) for n in (5, 6, 7)], [60, 120, 240])
        self.assertEqual(account_lock_seconds(50), 3600)
        self.wrong(5)
        with self.app.db.tx() as conn:  # let the first lock run out
            conn.execute("UPDATE user SET locked_until='2000-01-01 00:00:00'")
        self.wrong(1, start=5)
        left = datetime.fromisoformat(self.row()["locked_until"]) - datetime.now()
        self.assertGreater(left.total_seconds(), 100)

    def test_main_computer_can_still_sign_in(self):
        self.wrong(5)
        token, _ = self.right(client="admin:127.0.0.1", local=True)
        self.assertTrue(token)
        self.assertEqual((self.row()["failed_attempts"], self.row()["locked_until"]), (0, None))

    def test_success_resets_the_count(self):
        self.wrong(4)
        self.right()
        self.wrong(4, start=10)
        self.assertIsNone(self.row()["locked_until"])

    def test_new_password_unlocks(self):
        self.wrong(5)
        self.auth.update_user(self.row()["id"], secret="another-password-2")
        self.assertIsNone(self.row()["locked_until"])

    def test_reset_admin_unlocks(self):
        from argparse import Namespace
        from checkin.__main__ import cmd_reset_admin
        self.wrong(5)
        self.app.stop()
        with mock.patch("getpass.getpass", return_value="another-password-2"), \
                mock.patch("builtins.print"):
            cmd_reset_admin(Namespace(data=self.tmp, name=SETUP["admin_name"]))
        self.app = App(self.tmp)
        self.app.start()
        self.assertEqual((self.row()["failed_attempts"], self.row()["locked_until"]), (0, None))


class TestRateLimiter(unittest.TestCase):
    def test_blocks_double_up_to_the_cap_and_success_resets(self):
        from checkin.auth import AuthError, RateLimiter
        clock = [1000.0]
        limiter = RateLimiter()
        with mock.patch("checkin.auth.time.time", side_effect=lambda: clock[0]):
            def block_length():
                for _ in range(5):
                    limiter.fail("ipad")
                return limiter.blocked["ipad"] - clock[0]
            self.assertEqual([block_length() for _ in range(6)], [60, 120, 240, 480, 900, 900])
            with self.assertRaises(AuthError):
                limiter.check("ipad")
            limiter.ok("ipad")
            clock[0] += 1000
            self.assertEqual(block_length(), 60)


class TestFormLabels(unittest.TestCase):
    def test_labels_point_at_their_inputs(self):
        web = Path(__file__).resolve().parent.parent / "web"
        common = (web / "common.js").read_text()
        self.assertIn("label.htmlFor = control.id", common)
        for name in ("admin.js", "kiosk.js"):
            text = (web / name).read_text()
            self.assertNotIn("function field(", text, name)  # uses the shared HB.field
            self.assertRegex(text, r"const \{[^}]*\bfield\b[^}]*\} = HB;", name)


if __name__ == "__main__":
    unittest.main()
