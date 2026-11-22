"""Acceptance tests that run without hardware (preview printers + a simulated Brother printer).

Run:  python -m unittest discover -s tests -v
"""

import json
import os

# Never touch real printers from tests, even when they are plugged in: tests use preview printers only.
os.environ["CHECKIN_NO_USB"] = "1"
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkin import reports, roster  # noqa: E402
from checkin.app import App  # noqa: E402
from checkin.backup import BackupError  # noqa: E402
from checkin.printing import raster  # noqa: E402
from checkin.printing.backends import BrotherBackend, Transport  # noqa: E402
from checkin.sample import families, write_sample  # noqa: E402
from checkin.service import ServiceError  # noqa: E402
from checkin.util import new_id as uuid_like, normalize_phone  # noqa: E402

SETUP = {"admin_name": "Admin", "admin_password": "test-password-1", "volunteer_pin": "2468",
         "backup_passphrase": "test backup passphrase", "station_name": "Main desk"}


def make_app(tmp: str, families_count: int = 120) -> App:
    app = App(tmp)
    app.setup(SETUP)
    app.start()
    csv_path = os.path.join(tmp, "roster.csv")
    write_sample(csv_path, families_count)
    import_file(app, csv_path)
    return app


def import_file(app, path, deactivate=False):
    text, _ = roster.decode_csv(Path(path).read_bytes())
    headers, _ = roster.read_csv(text)
    analysis = roster.analyse(app.db, text, roster.suggest_mapping(headers))
    with app.db.tx() as conn:
        batch = conn.execute("INSERT INTO import_batch(ts, filename, status) VALUES('t', ?, 'previewed')",
                             (os.path.basename(path),)).lastrowid
    return analysis, roster.commit(app.db, analysis, batch, deactivate)


def wait_job(app, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = app.printers.job(job_id)
        if job["status"] in ("printed", "failed", "unknown", "cancelled"):
            return job
        time.sleep(0.05)
    return app.printers.job(job_id)


class Base(unittest.TestCase):
    families = 120

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-test-")
        self.app = make_app(self.tmp, self.families)
        self.station = self.app.db.one("SELECT * FROM station")
        self.session = self.app.service.today_sessions()[0] if self.app.service.today_sessions() \
            else self.app.service.open_session(1)
        self.fams, _ = families(self.families, 7)

    def tearDown(self):
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def household_for(self, index):
        mobile = self.fams[index]["adults"][0]["mobile"]
        found = self.app.service.search(mobile)
        self.assertEqual(len(found), 1, f"expected one household for {mobile}")
        return self.app.service.household(found[0]["id"], self.session["id"])

    def checkin(self, hh, children=None, txn=None):
        ids = [c["id"] for c in (children or hh["people"])]
        return self.app.service.check_in(txn or uuid_like(), self.session["id"], hh["id"], ids, self.station)


class TestUtilities(unittest.TestCase):
    def test_phone_formats(self):
        for value in ("+65 9123-4567", "65 91234567", "9123 4567", "(+65) 9123 4567", "0065-9123-4567",
                      "91234567"):
            self.assertEqual(normalize_phone(value), "91234567", value)


class TestImport(Base):
    def test_sample_roster_validation_and_upsert(self):
        text, _ = roster.decode_csv(Path(self.tmp, "roster.csv").read_bytes())
        headers, _ = roster.read_csv(text)
        analysis = roster.analyse(self.app.db, text, roster.suggest_mapping(headers))
        self.assertEqual(analysis.totals["people_new"], 0)
        self.assertEqual(analysis.totals["people_updated"], 0)
        self.assertGreater(analysis.totals["people_unchanged"], 400)
        self.assertEqual(len(analysis.warnings["duplicate_ids"]), 1)
        self.assertEqual(len(analysis.warnings["missing_ids"]), 1)
        self.assertEqual(len(analysis.warnings["invalid_phones"]), 1)
        self.assertEqual(len(analysis.warnings["children_no_household"]), 1)
        self.assertGreaterEqual(self.app.db.scalar("SELECT COUNT(*) FROM household"), 100)

    def test_rename_keeps_history_and_missing_people_not_deleted(self):
        hh = self.household_for(5)
        child = hh["people"][0]
        self.checkin(hh, [child])
        person = self.app.db.one("SELECT * FROM person WHERE id=?", (child["id"],))
        lines = Path(self.tmp, "roster.csv").read_text().splitlines()
        lines = [line.replace(f"{person['breeze_id']},{person['first_name']},", f"{person['breeze_id']},Renamed,")
                 for line in lines]
        dropped = lines.pop(1)  # someone missing from the new file
        Path(self.tmp, "week2.csv").write_text("\n".join(lines) + "\n")
        _, totals = import_file(self.app, Path(self.tmp, "week2.csv"))
        self.assertGreaterEqual(totals["people_updated"], 1)
        self.assertEqual(totals["deactivated"], 0)
        renamed = self.app.db.one("SELECT * FROM person WHERE id=?", (child["id"],))
        self.assertEqual(renamed["first_name"], "Renamed")
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance WHERE person_id=?", (child["id"],)), 1)
        missing_id = dropped.split(",")[0]
        self.assertEqual(self.app.db.one("SELECT active FROM person WHERE breeze_id=?", (missing_id,))["active"], 1)
        _, totals = import_file(self.app, Path(self.tmp, "week2.csv"), deactivate=True)
        self.assertEqual(totals["deactivated"], 1)
        self.assertEqual(self.app.db.one("SELECT active FROM person WHERE breeze_id=?", (missing_id,))["active"], 0)

    def test_child_row_format(self):
        path = Path(self.tmp, "children.csv")
        write_sample(str(path), 20, "children")
        text, _ = roster.decode_csv(path.read_bytes())
        headers, _ = roster.read_csv(text)
        mapping = roster.suggest_mapping(headers)
        self.assertEqual(mapping.get("guardian_name"), "Parent Name")
        self.assertEqual(mapping.get("mobile"), "Parent Mobile")
        analysis = roster.analyse(self.app.db, text, mapping)
        self.assertEqual(analysis.warnings["children_no_household"], [])


class TestSundayFlow(Base):
    def test_shared_last_four_digits(self):
        last4 = self.fams[1]["adults"][0]["mobile"][-4:]
        results = self.app.service.search(last4)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["phone"].endswith(last4) and r["phone"].startswith("••••") for r in results))

    def test_search_full_number_and_names(self):
        mobile = self.fams[3]["adults"][0]["mobile"]
        self.assertEqual(len(self.app.service.search(f"+65 {mobile[:4]}-{mobile[4:]}")), 1)
        self.assertTrue(self.app.service.search(self.fams[3]["kids"][0]["first"]))
        self.assertTrue(self.app.service.search(self.fams[3]["surname"].upper()))

    def test_admin_people_search_by_part_of_a_mobile(self):
        mobile = self.fams[3]["adults"][0]["mobile"]
        found = reports.people_search(self.app.db, mobile[2:7])
        self.assertTrue(any(mobile in row["mobiles"] for row in found))
        row = next(r for r in found if mobile in r["mobiles"])
        self.assertTrue(row["phones"][0].startswith("••••"))
        self.assertTrue(reports.people_search(self.app.db, f"+65 {mobile[:4]} {mobile[4:]}"))
        self.assertEqual(reports.people_search(self.app.db, "123"), [])        # under 4 digits: names only

    def test_one_person_gets_one_name_label(self):
        hh = self.household_for(0)
        adult = next(p for p in hh["people"] if not p["is_child"])
        result = self.checkin(hh, [adult])
        job = wait_job(self.app, result["job"]["id"])
        self.assertEqual(job["status"], "printed")
        self.assertEqual(job["label_count"], 1)
        payload = json.loads(self.app.db.one("SELECT payload FROM print_job WHERE id=?", (job["id"],))["payload"])
        self.assertEqual([l["template"] for l in payload], ["name"])
        self.assertEqual(payload[0]["data"]["code"], "")
        self.assertEqual(payload[0]["data"]["name"], adult["name"])
        self.assertNotIn("phone", json.dumps(payload).lower())

    def test_preferred_name_is_printed(self):
        hh = self.household_for(1)
        person = hh["people"][0]
        with self.app.db.tx() as conn:
            conn.execute("UPDATE person SET preferred_name='Benny' WHERE id=?", (person["id"],))
        result = self.checkin(self.app.service.household(hh["id"], self.session["id"]), [person])
        payload = json.loads(self.app.db.one("SELECT payload FROM print_job WHERE id=?",
                                             (result["job"]["id"],))["payload"])
        self.assertEqual(payload[0]["data"]["name"], "Benny")

    def test_children_only_mode_with_pickup_codes(self):
        self.app.db.set_setting("checkin_mode", "children")
        self.app.db.set_setting("pickup_codes", True)
        hh = self.household_for(2)
        self.assertTrue(all(p["is_child"] for p in hh["people"]))
        result = self.checkin(hh)
        job = wait_job(self.app, result["job"]["id"])
        payload = json.loads(self.app.db.one("SELECT payload FROM print_job WHERE id=?", (job["id"],))["payload"])
        self.assertEqual([l["template"] for l in payload], ["name"] * len(hh["people"]) + ["guardian"])
        self.assertEqual(payload[0]["data"]["code"], result["code"])

    def test_whole_family_one_transaction(self):
        hh = next(self.household_for(i) for i in range(len(self.fams)) if len(self.fams[i]["kids"]) >= 3)
        self.assertGreaterEqual(len(hh["people"]), 4)
        result = self.checkin(hh)
        job = wait_job(self.app, result["job"]["id"])
        self.assertEqual(job["label_count"], len(hh["people"]))
        self.assertEqual(len({c["id"] for c in result["children"]}), len(hh["people"]))

    def test_double_click_and_second_checkin(self):
        hh = self.household_for(2)
        txn = uuid_like()
        first = self.checkin(hh, txn=txn)
        again = self.checkin(hh, txn=txn)
        self.assertTrue(again["replay"])
        self.assertEqual(first["code"], again["code"])
        with self.assertRaises(ServiceError):
            self.checkin(hh)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance"), len(hh["people"]))
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM print_job"), 1)

    def test_two_stations_at_once(self):
        hh = self.household_for(4)
        errors, results = [], []

        def worker():
            try:
                results.append(self.checkin(hh))
            except ServiceError as exc:
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 5)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance"), len(hh["people"]))

    def test_reprint_and_retry_do_not_duplicate(self):
        hh = self.household_for(6)
        result = self.checkin(hh)
        wait_job(self.app, result["job"]["id"])
        one = hh["people"][0]["id"]
        job = self.app.service.reprint(result["txn_id"], "all", self.station, person_ids=[one])
        self.assertEqual(wait_job(self.app, job["id"])["status"], "printed")
        self.assertEqual(job["label_count"], 1)
        with self.assertRaises(ServiceError):
            self.app.service.reprint(result["txn_id"], "guardian", self.station)
        self.assertEqual(self.app.db.one("SELECT reprint_count FROM checkin_txn WHERE id=?",
                                         (result["txn_id"],))["reprint_count"], 1)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance"), len(hh["people"]))

    def test_undo_with_reason(self):
        hh = self.household_for(7)
        result = self.checkin(hh)
        with self.assertRaises(ServiceError):
            self.app.service.undo(result["txn_id"], "", self.station)
        self.app.service.undo(result["txn_id"], "Wrong family", self.station)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance WHERE status<>'undone'"), 0)
        self.assertTrue(self.app.db.one("SELECT * FROM audit_log WHERE action='checkin.undo'"))
        again = self.checkin(hh)  # child can be checked in again after an undo
        self.assertEqual(len(again["children"]), len(hh["people"]))

    def test_checkout_keeps_history(self):
        self.app.db.set_setting("pickup_codes", True)
        hh = self.household_for(8)
        result = self.checkin(hh)
        lookup = self.app.service.checkout_lookup(result["code"].lower())
        ids = [c["attendance_id"] for c in lookup["children"]]
        done = self.app.service.check_out(ids, self.station)
        self.assertEqual(len(done["checked_out"]), len(ids))
        self.assertEqual(self.app.service.check_out(ids, self.station)["checked_out"], [])
        kept = self.app.db.scalar("SELECT COUNT(*) FROM attendance WHERE id IN (%s) AND status<>'undone'"
                                  % ",".join("?" * len(ids)), ids)
        self.assertEqual(kept, len(ids))

    def test_rooms_and_totals_have_one_row_per_group(self):
        """Adults (no class) and children with no class were both called Unassigned, in two rows."""
        hh = self.household_for(2)
        self.assertTrue(any(p["is_child"] for p in hh["people"]) and any(not p["is_child"] for p in hh["people"]))
        self.checkin(hh)
        rooms = {r["class_name"]: r for r in self.app.service.room_counts(self.session["id"])}
        self.assertEqual(len(rooms), len(self.app.service.room_counts(self.session["id"])))
        adults = sum(1 for p in hh["people"] if not p["is_child"])
        self.assertEqual(rooms["Adults"]["present"], adults)
        self.assertEqual(rooms["Unassigned"]["present"], len(hh["people"]) - adults)
        totals = [r for r in reports.report(self.app.db, "totals")["rows"] if r["Service"] != "TOTAL"]
        self.assertEqual(sorted(r["Class"] for r in totals), ["Adults", "Unassigned"])

    def test_visitor_registered_and_printed(self):
        with self.app.db.tx() as conn:
            conn.execute("INSERT INTO classroom(name) VALUES('Primary')")
        rooms = self.app.service.classrooms()
        res = self.app.service.register_visitor({
            "guardian_name": "Visitor Parent", "phone": "+65 9000 1234",
            "members": [{"first_name": "Vee", "is_child": True, "classroom_id": rooms[0]["id"], "alert": True,
                         "alert_note": "Eggs"}],
        }, self.station)
        self.assertEqual(len(res["person_ids"]), 2)
        hh = self.app.service.household(res["household_id"], self.session["id"])
        self.assertTrue(hh["visitor"])
        self.assertEqual(len(hh["people"]), 2)
        visitor = self.app.db.one("SELECT * FROM person WHERE id=?", (res["child_ids"][0],))
        self.assertTrue(visitor["visitor_id"].startswith("VIS-"))
        self.assertIsNone(visitor["breeze_id"])
        result = self.checkin(hh)
        self.assertEqual(wait_job(self.app, result["job"]["id"])["status"], "printed")
        self.assertEqual(len(reports.report(self.app.db, "visitors")["rows"]), 2)
        alone = self.app.service.register_visitor({"guardian_name": "Solo Visitor", "phone": "93334444"}, self.station)
        self.assertEqual(len(alone["person_ids"]), 1)
        with self.assertRaises(ServiceError) as ctx:  # same number again: offer the existing family
            self.app.service.register_visitor({"guardian_name": "X", "phone": "90001234",
                                               "children": [{"first_name": "Y"}]}, self.station)
        self.assertEqual(ctx.exception.status, 409)

    def test_allergy_alerts_hidden_unless_switched_on(self):
        hh = self.household_for(10)
        person = hh["people"][0]
        with self.app.db.tx() as conn:
            conn.execute("UPDATE person SET medical_alert=1, medical_note='Peanuts' WHERE id=?", (person["id"],))
        shown = self.app.service.household(hh["id"], self.session["id"])["people"][0]
        self.assertFalse(shown["alert"])
        self.assertEqual(shown["alert_note"], "")
        self.app.db.set_setting("medical_alerts", True)
        shown = self.app.service.household(hh["id"], self.session["id"])["people"][0]
        self.assertTrue(shown["alert"])


class TestCheckinMode(Base):
    """Individual (the default), whole family or children only. The church is mostly adults."""

    def test_individual_is_the_default_and_adults_check_in(self):
        settings = self.app.db.settings()
        self.assertEqual(settings["checkin_mode"], "individual")
        self.assertTrue(settings["checkin_everyone"])
        hh = self.household_for(5)
        adult = next(p for p in hh["people"] if not p["is_child"])
        result = self.checkin(hh, [adult])
        self.assertEqual([c["id"] for c in result["children"]], [adult["id"]])

    def test_phone_search_marks_the_person_it_matched(self):
        fam = self.fams[5]
        found = self.app.service.search(fam["adults"][0]["mobile"])
        self.assertEqual(len(found), 1)
        hh = self.app.service.household(found[0]["id"], self.session["id"])
        matched = [p for p in hh["people"] if p["id"] in found[0]["matched"]]
        self.assertEqual([p["name"] for p in matched], [fam["adults"][0]["first"]])

    def test_phone_search_prefers_the_adult_when_a_child_shares_the_number(self):
        mobile = self.fams[5]["adults"][0]["mobile"]
        hh = self.household_for(5)
        child = next(p for p in hh["people"] if p["is_child"])
        with self.app.db.tx() as conn:
            conn.execute("INSERT INTO phone(person_id, digits, last4) VALUES(?,?,?)", (child["id"], mobile, mobile[-4:]))
        for query in (mobile, mobile[-4:]):
            entry = next(f for f in self.app.service.search(query) if f["id"] == hh["id"])
            self.assertEqual(len(entry["matched"]), 1, query)
            self.assertNotIn(child["id"], entry["matched"], query)

    def test_name_search_marks_the_named_person(self):
        hh = self.household_for(5)
        child = next(p for p in hh["people"] if p["is_child"])
        found = self.app.service.search(f"{child['name']} {child['last_name']}")
        entry = next(f for f in found if f["id"] == hh["id"])
        self.assertIn(child["id"], entry["matched"])
        adults = {p["id"] for p in hh["people"] if not p["is_child"]}
        self.assertFalse(adults & set(entry["matched"]))

    def test_children_only_mode_hides_adults(self):
        self.app.db.set_setting("checkin_mode", "children")
        self.assertFalse(self.app.service.everyone())
        self.assertFalse(self.app.db.settings()["checkin_everyone"])
        hh = self.household_for(5)
        self.assertTrue(hh["people"])
        self.assertTrue(all(p["is_child"] for p in hh["people"]))

    def test_whole_family_mode_lets_adults_check_in(self):
        self.app.db.set_setting("checkin_mode", "family")
        self.assertTrue(self.app.service.everyone())
        hh = self.household_for(5)
        self.assertTrue(any(not p["is_child"] for p in hh["people"]))

    def test_the_old_on_off_setting_carries_over(self):
        self.app.db.set_setting("checkin_everyone", False)
        self.assertEqual(self.app.db.settings()["checkin_mode"], "children")
        self.assertEqual(self.app.db.get_setting("checkin_mode"), "children")
        self.assertFalse(self.app.service.everyone())
        self.app.db.set_setting("checkin_everyone", True)
        self.assertEqual(self.app.db.get_setting("checkin_mode"), "individual")
        # Once a mode is saved, the old setting no longer counts.
        self.app.db.set_setting("checkin_mode", "family")
        self.app.db.set_setting("checkin_everyone", False)
        self.assertEqual(self.app.db.get_setting("checkin_mode"), "family")
        self.assertTrue(self.app.db.get_setting("checkin_everyone"))
        self.assertTrue(self.app.service.everyone())


class TestBreezeExcel(unittest.TestCase):
    """Spreadsheet import coverage for invisible last names, day-first dates and inactive records."""

    def setUp(self):
        import openpyxl
        self.tmp = tempfile.mkdtemp(prefix="checkin-xlsx-")
        self.app = App(self.tmp)
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Simple"
        sheet.append(["Breeze ID", "First Name", "Last Name", "Nickname", "A long section description heading "
                      "that Breeze puts in as a column of its own", "Birthdate", "Family", "Family Role", "Mobile",
                      "Church Membership Type", "Transfer Type", "Date of Decease", "Primary Congregation", "Grade"])
        rows = [
            (101, "Alex", "​", "Alex", None, "25/12/1980", 7001, "Head of Household", "9123 4567", "Member", None, None, "Sample Group", False),
            (102, "Jordan Example", "​", None, None, "03/04/1982", 7001, "Spouse", 98765432, "Member", "Transfer out", None, "Example Group", False),
            (103, "Test Member", "​", None, None, "01/01/1930", None, None, 81112222, "Deceased", None, "05/05/2024", "Sample Group", False),
            (104, "Moved Sample", "​", None, None, "01/02/1990", None, None, 81113333, "Removed", "Transfer out", None, "Sample Group", False),
            (105, "Casey", "Sample", None, None, "13/07/1995", None, None, "+65 8111 4567", "New visitor", None, None, "Sample Group", False),
        ]
        for row in rows:
            sheet.append(list(row))
        self.path = Path(self.tmp, "people.xlsx")
        book.save(self.path)

    def tearDown(self):
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_excel_import_rules_and_titles(self):
        headers, rows, _ = roster.load_table(self.path.read_bytes(), "people.xlsx")
        mapping, rules, _ = roster.default_choices(self.app.db, headers, rows)
        self.assertEqual(mapping["preferred_name"], "Nickname")
        self.assertNotIn("birthdate", mapping)  # not needed for adult check-in, so not stored
        self.assertNotIn("school_year", mapping)
        self.assertNotIn("Transfer Type", [r["column"] for r in rules])
        columns = {r["column"]: r for r in rules}
        self.assertEqual(columns["Date of Decease"]["op"], "any")
        self.assertEqual(sorted(columns["Church Membership Type"]["values"]), ["Deceased", "Removed"])
        analysis = roster.analyse(self.app.db, (headers, rows), mapping, rules=rules,
                                  extra=["Primary Congregation"])
        with self.app.db.tx() as conn:
            batch = conn.execute("INSERT INTO import_batch(ts, status) VALUES('t','previewed')").lastrowid
        roster.commit(self.app.db, analysis, batch, False)
        people = {p["breeze_id"]: p for p in self.app.db.all("SELECT * FROM person")}
        self.assertEqual(people["101"]["last_name"], "")          # zero-width space removed
        self.assertEqual(people["101"]["preferred_name"], "Alex")
        self.assertEqual(people["102"]["active"], 1)              # past transfer, still a member
        self.assertNotIn("103", people)                           # deceased: not even stored
        self.assertNotIn("104", people)                           # transferred out: not stored
        self.assertEqual(json.loads(people["102"]["extra"]), {"x_primary_congregation": "Example Group"})
        found = self.app.service.search("4567")                   # Alex's family and Casey share it
        titles = sorted(f["title"] for f in found)
        self.assertEqual(titles, ["Alex & Jordan Example", "Casey Sample"])
        self.assertEqual(self.app.service.search("81112222"), [])  # the deceased person is not found
        # A member who passes away later: the next import hides them and drops their phone number.
        rows[0]["Church Membership Type"] = "Deceased"
        later = roster.analyse(self.app.db, (headers, rows), mapping, rules=rules)
        with self.app.db.tx() as conn:
            batch = conn.execute("INSERT INTO import_batch(ts, status) VALUES('t','previewed')").lastrowid
        roster.commit(self.app.db, later, batch, False)
        dan = self.app.db.one("SELECT * FROM person WHERE breeze_id='101'")
        self.assertEqual(dan["active"], 0)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM phone WHERE person_id=?", (dan["id"],)), 0)
        self.assertEqual([f["title"] for f in self.app.service.search("98765432")], ["Jordan Example"])
        rows[0]["Church Membership Type"] = "Member"
        mapping["birthdate"] = "Birthdate"
        dated = roster.analyse(self.app.db, (headers, rows), mapping, rules=rules)
        self.assertTrue(dated.day_first)
        self.assertEqual(dated.people["B:101"]["birthdate"], "1980-12-25")
        self.assertEqual(dated.people["B:102"]["birthdate"], "1982-04-03")


class TestUpgrade(unittest.TestCase):
    def test_old_database_is_upgraded_in_place(self):
        import sqlite3
        from checkin.db import MIGRATIONS, _split_sql
        tmp = tempfile.mkdtemp(prefix="checkin-upgrade-")
        try:
            Path(tmp, "keys").mkdir(parents=True)
            conn = sqlite3.connect(os.path.join(tmp, "checkin.db"))
            for statement in _split_sql(MIGRATIONS[0]):
                conn.execute(statement)
            conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
            conn.execute("INSERT INTO person(first_name, last_name, breeze_id) VALUES('Kept', 'Safe', '9')")
            conn.commit()
            conn.close()
            app = App(tmp)
            columns = [r["name"] for r in app.db.all("PRAGMA table_info(person)")]
            self.assertIn("extra", columns)
            self.assertEqual(app.db.one("SELECT first_name FROM person WHERE breeze_id='9'")["first_name"], "Kept")
            self.assertEqual(app.db.scalar("SELECT value FROM meta WHERE key='schema_version'"), str(len(MIGRATIONS)))
            app.db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestReliability(Base):
    def test_100_consecutive_families(self):
        codes, slowest = set(), 0.0
        expected_children = 0
        jobs = []
        for index in range(100):
            hh = self.household_for(index)
            started = time.perf_counter()
            result = self.checkin(hh)
            slowest = max(slowest, time.perf_counter() - started)
            codes.add(result["code"])
            jobs.append(result["job"]["id"])
            expected_children += len(hh["people"])
        self.assertEqual(len(codes), 100)
        self.assertLess(slowest, 0.5, "check-in commit must be under 500 ms")
        statuses = [wait_job(self.app, j, timeout=60)["status"] for j in jobs]
        self.assertEqual(statuses.count("printed"), 100)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance"), expected_children)
        self.assertEqual(self.app.db.scalar(
            "SELECT COUNT(*) FROM (SELECT person_id FROM attendance GROUP BY session_id, person_id HAVING COUNT(*)>1)"), 0)

    def test_restart_recovers_data_and_flags_unknown_prints(self):
        hh = self.household_for(10)
        result = self.checkin(hh)
        wait_job(self.app, result["job"]["id"])
        with self.app.db.tx() as conn:  # pretend the app died mid-print
            conn.execute("UPDATE print_job SET status='printing' WHERE id=?", (result["job"]["id"],))
        self.app.stop()
        self.app = App(self.tmp)
        self.app.start()
        self.assertEqual(self.app.printers.job(result["job"]["id"])["status"], "unknown")
        self.assertEqual(len(self.app.printers.unresolved_jobs()), 1)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance"), len(hh["people"]))

    def test_failover_and_retry_on_other_printer(self):
        left = Path(self.app.paths.preview, "LEFT-PRINTER")
        left.mkdir(parents=True, exist_ok=True)
        (left / "simulate").write_text("no_media")
        self.app.printers.refresh_status("LEFT-PRINTER")
        hh = self.household_for(11)
        result = self.checkin(hh)
        job = wait_job(self.app, result["job"]["id"])
        self.assertEqual(job["printer"], "RIGHT-PRINTER")
        self.assertIn("Out of labels", job["route_note"])
        (left / "simulate").write_text("fail")
        self.app.printers.refresh_status("LEFT-PRINTER")
        hh2 = self.household_for(12)
        result2 = self.checkin(hh2)
        job2 = wait_job(self.app, result2["job"]["id"])
        self.assertEqual(job2["status"], "failed")
        retried = self.app.printers.retry(job2["id"], "RIGHT-PRINTER", self.station)
        self.assertEqual(retried["id"], job2["id"])
        self.assertEqual(wait_job(self.app, job2["id"])["status"], "printed")
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance"),
                         len(hh["people"]) + len(hh2["people"]))

    def test_backup_restore_and_replacement_computer(self):
        hh = self.household_for(13)
        self.checkin(hh)
        backup = self.app.backups.create("test")
        data = Path(backup["path"]).read_bytes()
        self.assertTrue(self.app.backups.verify(data)["verified"]["integrity"] == "ok")
        with self.assertRaises(BackupError):
            self.app.backups.verify(data, "wrong passphrase!!")
        self.checkin(self.household_for(14))
        self.app.restore(data, None)
        self.assertEqual(self.app.db.scalar("SELECT COUNT(*) FROM attendance"), len(hh["people"]))
        fresh_dir = tempfile.mkdtemp(prefix="checkin-new-pc-")
        try:
            fresh = App(fresh_dir)
            fresh.start()
            fresh.restore(data, SETUP["backup_passphrase"])
            self.assertEqual(fresh.db.scalar("SELECT COUNT(*) FROM attendance"), len(hh["people"]))
            self.assertFalse(fresh.needs_setup())
            self.assertTrue(fresh.backups.has_key())
            _, user = fresh.auth.login_pin(SETUP["volunteer_pin"], None, "test")
            self.assertEqual(user["role"], "volunteer")
            fresh.backups.create("after-move")
            fresh.stop()
        finally:
            shutil.rmtree(fresh_dir, ignore_errors=True)


class TestPerformance(unittest.TestCase):
    def test_search_4000_people_under_300ms(self):
        # 4,000 rows leaves room for a large congregation.
        tmp = tempfile.mkdtemp(prefix="checkin-perf-")
        try:
            app = make_app(tmp, 1000)
            people = app.db.scalar("SELECT COUNT(*) FROM person")
            self.assertGreaterEqual(people, 4000)
            fams, _ = families(1000, 7)
            worst = 0.0
            for fam in fams[:200]:
                for query in (fam["adults"][0]["mobile"][-4:], fam["surname"], fam["kids"][0]["first"]):
                    started = time.perf_counter()
                    app.service.search(query)
                    worst = max(worst, time.perf_counter() - started)
            self.assertLess(worst, 0.3, f"slowest search {worst * 1000:.0f} ms")
            app.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FakePrinter(Transport):
    """Behaves like a QL-820NWB on the wire: replies to status requests, reports each printed page."""

    def __init__(self, fail_after=None, silent=False, error_bits=(0, 0), one_completion=False, error_after_job=None):
        self.out = b""
        self.fail_after = fail_after
        self.silent = silent
        self.error_bits = error_bits
        self.one_completion = one_completion      # like the real QL-820NWB: one confirmation per job
        self.error_after_job = error_after_job    # status error bits reported once a job has been sent
        self.job_sent = False

    @staticmethod
    def packet(status_type, phase=0, err1=0, err2=0, width=62, kind=0x0A):
        p = bytearray(32)
        p[0], p[1], p[2], p[3], p[4], p[5] = 0x80, 0x20, 0x42, 0x34, 0x41, 0x30
        p[8], p[9], p[10], p[11], p[18], p[19] = err1, err2, width, kind, status_type, phase
        return bytes(p)

    def write(self, data, timeout=10.0):
        if data.endswith(b"\x1b\x69\x53"):
            bits = self.error_after_job if (self.job_sent and self.error_after_job) else self.error_bits
            self.out += self.packet(0x00, 0, *bits)
            return
        self.job_sent = True
        if self.silent:
            return
        pages = data.count(b"\x0c") + data.count(b"\x1a")
        if self.one_completion:
            self.out += self.packet(0x06, 1) + self.packet(0x01, 1) + self.packet(0x06, 0)
            return
        self.out += self.packet(0x06, 1)
        for page in range(pages):
            if self.fail_after is not None and page >= self.fail_after:
                self.out += self.packet(0x02, 0, 0x01, 0)
                return
            self.out += self.packet(0x01, 1)
        self.out += self.packet(0x06, 0)

    def read(self, size=64, timeout=0.5):
        chunk, self.out = self.out[:size], self.out[size:]
        if not chunk:
            time.sleep(min(timeout, 0.01))
        return chunk


class FakeBackend(BrotherBackend):
    def __init__(self, printer):
        super().__init__(None)
        self.fake = printer

    def open(self):
        return self.fake


class TestBrotherProtocol(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.media = raster.MEDIA["62"]
        self.pages = [Image.new("L", (696, 600), 255) for _ in range(3)]
        self.job = raster.build_job(self.pages, self.media)

    def test_job_structure(self):
        self.assertTrue(self.job.startswith(b"\x00" * 400 + b"\x1b\x40\x1b\x69\x61\x01\x1b\x69\x7a"))
        self.assertEqual(self.job.count(b"\x0c"), 2)
        self.assertTrue(self.job.endswith(b"\x1a"))
        info = self.job.index(b"\x1b\x69\x7a")
        self.assertEqual(self.job[info + 4], 0x0A)  # continuous tape
        self.assertEqual(self.job[info + 5], 62)
        self.assertEqual(int.from_bytes(self.job[info + 7:info + 11], "little"), 600)
        self.assertEqual(len(raster.decode_job(self.job)), 3)
        compressed = raster.build_job(self.pages, self.media, compress=True)
        self.assertLess(len(compressed), len(self.job) / 10)
        self.assertEqual(len(raster.decode_job(compressed)), 3)

    def test_status_decoding(self):
        ok = raster.parse_status(FakePrinter.packet(0x00))
        self.assertTrue(ok.ok)
        self.assertEqual(ok.model, "QL-820NWB")
        self.assertEqual(raster.parse_status(FakePrinter.packet(0x02, 0, 0x01, 0)).state, "no_media")
        self.assertEqual(raster.parse_status(FakePrinter.packet(0x02, 0, 0, 0x10)).message, "Cover open")
        narrow = raster.parse_status(FakePrinter.packet(0x00, width=29))
        self.assertEqual(raster.check_media(narrow, self.media).state, "media_mismatch")

    def test_print_confirmed_per_label(self):
        result = FakeBackend(FakePrinter()).print_job(self.job, 3, "per_label", self.media)
        self.assertEqual(result.status, "printed")
        self.assertEqual(result.confirmed, 3)

    def test_early_reports_dropped_like_the_real_printer(self):
        # Regression: a multi-label job may return reports for only its final label.
        started = time.time()
        result = FakeBackend(FakePrinter(one_completion=True)).print_job(self.job, 3, "per_label", self.media)
        self.assertEqual(result.status, "printed")
        self.assertEqual(result.confirmed, 3)
        self.assertLess(time.time() - started, 3)

    def test_out_of_labels_mid_job(self):
        result = FakeBackend(FakePrinter(fail_after=1)).print_job(self.job, 3, "per_label", self.media)
        self.assertEqual(result.status, "failed")
        self.assertIn("Out of labels", result.message)
        self.assertTrue(result.sent)

    def test_refused_before_sending(self):
        result = FakeBackend(FakePrinter(error_bits=(0, 0x10))).print_job(self.job, 3, "per_label", self.media)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.sent)
        self.assertEqual(result.message, "Cover open")

    def test_no_confirmation_is_unknown(self):
        backend = FakeBackend(FakePrinter(silent=True))
        started = time.time()
        result = backend.print_job(self.job, 1, "per_label", self.media)
        self.assertEqual(result.status, "unknown")
        self.assertLess(time.time() - started, 15)


class TestHttpApi(unittest.TestCase):
    def test_station_and_pin_required_then_checkin(self):
        from checkin.server import make_server
        tmp = tempfile.mkdtemp(prefix="checkin-http-")
        app = make_app(tmp, 20)
        server = make_server(app, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def call(method, path, body=None, station=None, token=None):
            headers = {"Content-Type": "application/json"}
            if station:
                headers["X-Station-Token"] = station
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())
        try:
            self.assertEqual(call("GET", "/api/search?q=1234")[0], 401)
            status, admin = call("POST", "/api/admin/login", {"name": "Admin", "password": SETUP["admin_password"]})
            self.assertEqual(status, 200)
            status, reg = call("POST", "/api/admin/stations/register", {"name": "iPad 1", "printer": "RIGHT-PRINTER"},
                               token=admin["token"])
            station = reg["station_token"]
            self.assertEqual(call("GET", "/api/search?q=1234", station=station)[0], 401)
            self.assertEqual(call("POST", "/api/login", {"pin": "0000"}, station=station)[0], 401)
            status, login = call("POST", "/api/login", {"pin": SETUP["volunteer_pin"]}, station=station)
            token = login["token"]
            fams, _ = families(20, 7)
            status, found = call("GET", "/api/search?q=" + fams[0]["adults"][0]["mobile"], station=station, token=token)
            hid = found["results"][0]["id"]
            status, st = call("GET", "/api/status", station=station, token=token)
            sid = st["default_session_id"] or call("POST", "/api/sessions/open", {"service_id": 1}, station, token)[1]["id"]
            status, hh = call("GET", f"/api/households/{hid}?session_id={sid}", station=station, token=token)
            body = {"txn_id": uuid_like(), "session_id": sid, "household_id": hid,
                    "person_ids": [c["id"] for c in hh["people"]]}
            status, result = call("POST", "/api/checkin", body, station, token)
            self.assertEqual(status, 200)
            self.assertEqual(result["job"]["printer"], "RIGHT-PRINTER")
            status, again = call("POST", "/api/checkin", body, station, token)
            self.assertTrue(again["replay"])
            self.assertEqual(call("GET", "/api/admin/overview", station=station, token=token)[0], 401)
            handler = server.RequestHandlerClass
            self.assertFalse(handler._allowed(handler, "8.8.8.8"))
            self.assertTrue(handler._allowed(handler, "192.168.1.20"))
        finally:
            server.shutdown()
            app.stop()
            shutil.rmtree(tmp, ignore_errors=True)


def serve(app):
    """Start the HTTP server on a free port; returns (server, call)."""
    from checkin.server import make_server
    server = make_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(method, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())
    return server, call


class TestModeSettings(unittest.TestCase):
    """Children-only check-in hides every adult, so turning it on has to be confirmed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="checkin-mode-")
        self.app = make_app(self.tmp, 10)
        self.server, self.call = serve(self.app)
        status, login = self.call("POST", "/api/admin/login", {"name": "Admin", "password": SETUP["admin_password"]})
        self.assertEqual(status, 200)
        self.token = login["token"]

    def tearDown(self):
        self.server.shutdown()
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, body):
        return self.call("PUT", "/api/admin/settings", body, self.token)

    def test_children_only_is_refused_without_confirming(self):
        status, body = self.put({"checkin_mode": "children", "church_name": "Changed"})
        self.assertEqual(status, 409)
        self.assertIn("adults", body["error"])
        settings = self.app.db.settings()
        self.assertEqual(settings["checkin_mode"], "individual")
        self.assertNotEqual(settings["church_name"], "Changed")  # nothing in that save was kept

    def test_children_only_once_confirmed(self):
        status, body = self.put({"checkin_mode": "children", "confirm_children_only": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["checkin_mode"], "children")
        self.assertFalse(body["checkin_everyone"])
        details = self.app.db.scalar("SELECT details FROM audit_log WHERE action='settings.update' ORDER BY id DESC")
        self.assertEqual(json.loads(details).get("checkin_mode"), "children")
        self.assertNotIn("confirm_children_only", json.loads(details))
        # Saving other settings while it is already on needs no second confirmation.
        status, _ = self.put({"checkin_mode": "children", "church_name": "Your Church"})
        self.assertEqual(status, 200)

    def test_individual_and_whole_family_need_no_confirming(self):
        for mode in ("family", "individual"):
            status, body = self.put({"checkin_mode": mode})
            self.assertEqual(status, 200, mode)
            self.assertEqual(body["checkin_mode"], mode)
            self.assertTrue(body["checkin_everyone"])

    def test_unknown_mode_is_refused(self):
        status, _ = self.put({"checkin_mode": "everyone"})
        self.assertEqual(status, 400)
        self.assertEqual(self.app.db.settings()["checkin_mode"], "individual")

    def test_the_old_on_off_setting_is_no_longer_editable(self):
        status, body = self.put({"checkin_everyone": False})
        self.assertEqual(status, 200)
        self.assertEqual(body["checkin_mode"], "individual")

    def test_stations_are_told_the_mode(self):
        from checkin.server import PUBLIC_SETTINGS
        self.assertIn("checkin_mode", PUBLIC_SETTINGS)
        self.assertIn("checkin_everyone", PUBLIC_SETTINGS)  # iPads still running the old page

    def test_pages_use_the_mode(self):
        web = Path(__file__).resolve().parent.parent / "web"
        admin = (web / "admin.js").read_text(encoding="utf-8")
        kiosk = (web / "kiosk.js").read_text(encoding="utf-8")
        self.assertIn('["checkin_mode"', admin)
        self.assertNotIn('["checkin_everyone"', admin)
        self.assertIn("confirm_children_only", admin)
        self.assertIn("checkin_mode", kiosk)
        self.assertIn(".matched", kiosk)


class TestLook(unittest.TestCase):
    """The same look as the church database: Figtree from this computer, flat colours, line icons."""
    WEB = Path(__file__).resolve().parent.parent / "web"

    def test_font_is_served_as_a_font(self):
        from checkin.server import make_server
        tmp = tempfile.mkdtemp(prefix="checkin-look-")
        app = make_app(tmp, 2)
        server = make_server(app, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/fonts/Figtree-latin.woff2") as resp:
                self.assertEqual(resp.headers["Content-Type"], "font/woff2")
                self.assertEqual(resp.read(4), b"wOF2")
        finally:
            server.shutdown()
            app.stop()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stylesheets_use_figtree_and_no_gradients(self):
        style = (self.WEB / "style.css").read_text(encoding="utf-8")
        self.assertIn("Figtree", style)
        self.assertIn(":focus-visible", style)
        self.assertIn("prefers-reduced-motion", style)
        for css in self.WEB.glob("*.css"):
            self.assertNotIn("gradient", css.read_text(encoding="utf-8"), css.name)

    def test_buttons_use_line_icons_not_emoji(self):
        import re
        emoji = re.compile("[☀-➿\U0001f300-\U0001faff＋←↵⌫]")
        sprite = (self.WEB / "icons.svg").read_text(encoding="utf-8")
        defined = set(re.findall(r'<symbol id="([a-z-]+)"', sprite))
        used = set()
        for name in ("kiosk.html", "admin.html", "kiosk.js", "admin.js", "common.js"):
            text = (self.WEB / name).read_text(encoding="utf-8")
            self.assertEqual(emoji.findall(text), [], name)
            used |= set(re.findall(r'icon\("([a-z-]+)"', text))
        self.assertTrue(used)
        self.assertEqual(used - defined, set())

    def test_admin_boxes_are_finger_sized_and_named(self):
        """On a phone: boxes at least 44px, typing text at least 16px (iOS zooms in below that), and every box
        has a name a screen reader can read out."""
        import re
        css = (self.WEB / "admin.css").read_text(encoding="utf-8")
        admin = (self.WEB / "admin.js").read_text(encoding="utf-8")
        self.assertRegex(css, r"select \{[^}]*min-height: 44px")
        self.assertRegex(css, r"\.check input \{[^}]*flex: none")  # long labels squeezed the Sync tick box to 13px
        touch = "".join(re.findall(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, re.S))
        self.assertRegex(touch, r"textarea\.code \{ font-size: 16px; \}")
        self.assertRegex(touch, r"\.check \{ min-height: 44px; \}")
        self.assertRegex(touch, r"\.linklike \{ min-height: 44px; \}")
        self.assertIn('h("input", { placeholder: "Name, phone or VIS- id", "aria-label": "Search people" })', admin)
        self.assertIn('h("textarea", { class: "code", spellcheck: false, "aria-label": "Label template" })', admin)
        self.assertIn('input.setAttribute("aria-label", label);', admin)  # Settings boxes sit beside their name

    def test_kiosk_prompt_wraps_evenly_and_form_boxes_are_big(self):
        css = (self.WEB / "kiosk.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.prompt \{[^}]*text-wrap: balance")  # no lone "digits" on the second line
        self.assertRegex(css, r"\.form input:not\(\[type=checkbox\]\), \.form select \{ min-height: 56px; \}")

    def test_settings_rows_tick_from_anywhere_on_the_row(self):
        """Each Settings row is a label, so tapping the name or description ticks the box (it was a bare 24px box)."""
        css = (self.WEB / "admin.css").read_text(encoding="utf-8")
        admin = (self.WEB / "admin.js").read_text(encoding="utf-8")
        self.assertIn('return h("label", { class: "setting" }', admin)
        self.assertRegex(css, r"\.setting \{[^}]*margin: 0; font-weight: 400;")  # not the bold, spaced form label look

    def test_collected_counts_only_when_check_out_is_on(self):
        """Without pickup codes there is no check-out, so the menu shouldn't say '0 collected'."""
        kiosk = (self.WEB / "kiosk.js").read_text(encoding="utf-8")
        for line in kiosk.splitlines():
            if "collected`" in line:
                self.assertIn("pickupOn() ?", line, line.strip())


if __name__ == "__main__":
    unittest.main()
