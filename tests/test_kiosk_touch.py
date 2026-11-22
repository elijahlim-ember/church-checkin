"""Regression checks for station sign-in and kiosk touch interactions."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KIOSK = (ROOT / "web/kiosk.js").read_text(encoding="utf-8")
KIOSK_CSS = (ROOT / "web/kiosk.css").read_text(encoding="utf-8")
STYLE = (ROOT / "web/style.css").read_text(encoding="utf-8")


class TestStationLogin(unittest.TestCase):
    def test_administrator_is_default_and_code_login_is_toggled(self):
        self.assertIn('class: "admin-panel"', KIOSK)
        self.assertIn('class: "code-panel hidden", id: "code-panel"', KIOSK)
        self.assertIn('class: "btn big ghost code-toggle", type: "button", "aria-controls": "code-panel"', KIOSK)
        self.assertIn('"aria-expanded": "false"', KIOSK)
        self.assertIn('"Log in with one-time code"', KIOSK)
        self.assertIn('"Back to administrator sign-in"', KIOSK)
        self.assertIn('classList.toggle("hidden", !open)', KIOSK)
        self.assertIn('if (e.key === "Enter") joinBtn.click()', KIOSK)
        self.assertRegex(KIOSK, re.compile(r'if \(joinCode\) \{[\s\S]*?codePanel\.classList\.remove\("hidden"\);[\s\S]*?joinBtn\.click\(\);'))
        self.assertEqual(KIOSK.count("joinBtn.click()"), 2)  # code Enter and the QR join path
        self.assertIn('api("POST", "/api/admin/login"', KIOSK)
        self.assertIn('await chooseStation(login.token)', KIOSK)
        self.assertIn("input.focus();\n    if (initial) setTimeout(() => input.dispatchEvent(new Event(\"input\")), 50);", KIOSK)

    def test_administrator_fields_share_a_responsive_row(self):
        self.assertRegex(KIOSK, re.compile(
            r'class: "admin-panel"[\s\S]*?h\("div", \{ class: "row" \},\s*'
            r'field\("Administrator name", name\), field\("Password", password\)\)'))


class TestTouchRegressions(unittest.TestCase):
    def test_status_bar_is_memoised_without_replacing_the_clock(self):
        self.assertIn('let barKey = ""', KIOSK)
        self.assertRegex(KIOSK, re.compile(r'if \(\["lock", "register", "first-run", "setup"\]\.includes\(name\)\) barKey = "";'))
        self.assertRegex(KIOSK, re.compile(r'barKey = "";\s*clear\(bar\);\s*barKey = key;'))
        self.assertRegex(KIOSK, re.compile(r"const key = JSON\.stringify\(\[[\s\S]*?KS\.lang\(\)[\s\S]*?\]\);\s*if \(key === barKey\) return;"))
        self.assertIn('bar.querySelector(".clock")', KIOSK)
        self.assertNotIn("function field(", KIOSK)

    def test_identical_household_refresh_does_not_render(self):
        self.assertRegex(KIOSK, re.compile(r"JSON\.stringify\(\{ people: hh\.people, transactions: hh\.transactions, session_id: S\.sessionId \}\)"))
        self.assertIn("if (!first && signature === S.householdSignature) { S.household = hh; return; }", KIOSK)

    def test_touch_targets_and_toasts(self):
        self.assertIn(".bar .chip { min-height: 44px", KIOSK_CSS)
        self.assertIn(".chip, .card.tap, .row, input, select, textarea { touch-action: manipulation; }", KIOSK_CSS)
        self.assertIn(".card.tap, .chip, .row, input, select, textarea { touch-action: manipulation; }", STYLE)
        self.assertRegex(STYLE, re.compile(r"\.toast-wrap\s*\{[^}]*pointer-events: none;"))
        self.assertIn("background: rgba(0, 13, 38, .55); padding: 20px;", KIOSK_CSS)
        self.assertIn("--frost: blur(12px);", KIOSK_CSS)
        self.assertIn("e.currentTarget.disabled = true", KIOSK)
        self.assertNotIn('document.querySelector(".overlay .btn-row button:last-child")', KIOSK)
        self.assertIn("if (e.currentTarget.disabled) return", KIOSK)
        self.assertIn("e.currentTarget.disabled = false", KIOSK)


if __name__ == "__main__":
    unittest.main()
