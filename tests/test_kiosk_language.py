"""The kiosk in Mandarin (draft, 14 Nov 2026): one table of words, English and Chinese side by side."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ENTRY = re.compile(r'^\s+(\w+): ("(?:[^"\\]|\\.)*"),?$', re.M)
GAP = re.compile(r"\{(\w+)\}")
CJK = re.compile("[一-鿿]")


def table():
    text = (WEB / "kiosk-strings.js").read_text(encoding="utf-8")
    import json
    en_block = text.split("    en: {", 1)[1].split("    zh: {", 1)[0]
    zh_block = text.split("    zh: {", 1)[1].split("  const SERVER", 1)[0]
    return ({k: json.loads(v) for k, v in ENTRY.findall(en_block)},
            {k: json.loads(v) for k, v in ENTRY.findall(zh_block)}, text)


class TestKioskLanguage(unittest.TestCase):
    def test_every_english_string_has_a_chinese_one_and_the_other_way(self):
        en, zh, _ = table()
        self.assertGreater(len(en), 200)
        self.assertEqual(sorted(set(en) - set(zh)), [], "missing in zh")
        self.assertEqual(sorted(set(zh) - set(en)), [], "missing in en")

    def test_chinese_is_filled_in_and_keeps_the_gaps(self):
        en, zh, _ = table()
        same_is_fine = {"whatsapp", "lang_switch", "on_days", "problem_line"}   # a brand name, the button naming the other language, a bracket round a day
        for key, text in en.items():
            self.assertTrue(zh[key].strip(), key)
            self.assertEqual(sorted(GAP.findall(text)), sorted(GAP.findall(zh[key])), f"gaps differ in {key}")
            if key not in same_is_fine:
                self.assertRegex(zh[key], CJK, f"{key} has no Chinese: {zh[key]!r}")

    def test_singular_and_plural_come_in_pairs(self):
        en, _, _ = table()
        for key in en:
            if key.endswith("_one"):
                self.assertIn(key[:-4] + "_other", en)
            if key.endswith("_other"):
                self.assertIn(key[:-6] + "_one", en)

    def test_the_kiosk_only_asks_for_words_that_exist(self):
        en, _, _ = table()
        kiosk = (WEB / "kiosk.js").read_text(encoding="utf-8")
        asked = set(re.findall(r'\bt\("(\w+)"', kiosk))
        for key in asked:
            if key in ("day_", "undo_r"):   # built from a number: day_0 to day_6, undo_r1 to undo_r4
                continue
            self.assertIn(key, en, key)
        for key in re.findall(r'\btn\("(\w+)"', kiosk) + ["job_printing", "job_waiting", "job_printed"]:
            self.assertIn(key + "_one", en, key)
        for i in range(7):
            self.assertIn(f"day_{i}", en)
        for i in range(1, 5):
            self.assertIn(f"undo_r{i}", en)

    def test_server_messages_that_are_mapped_are_real_ones(self):
        en, zh, text = table()
        server = re.split(r"const SERVER = \{", text, 1)[1].split("\n  };", 1)[0]
        pairs = re.findall(r'^\s+("(?:[^"\\]|\\.)*"): "(\w+)",$', server, re.M)
        self.assertGreater(len(pairs), 20)
        source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "checkin").glob("*.py")) \
            + (WEB / "common.js").read_text(encoding="utf-8") + (WEB / "kiosk.js").read_text(encoding="utf-8")
        import json
        for quoted, key in pairs:
            message = json.loads(quoted)
            self.assertIn(key, en, key)
            self.assertIn(message, source, f"the main computer never says {message!r}")

    def test_the_page_loads_the_table_and_the_admin_pages_stay_english(self):
        html = (WEB / "kiosk.html").read_text(encoding="utf-8")
        self.assertLess(html.index("/common.js"), html.index("/kiosk-strings.js"))
        self.assertLess(html.index("/kiosk-strings.js"), html.index("/kiosk.js"))
        self.assertTrue((WEB / "kiosk-strings.js").is_file())
        for name in ("admin.js", "admin.html"):
            text = (WEB / name).read_text(encoding="utf-8")
            self.assertNotIn("kiosk-strings", text, name)
            self.assertNotIn("KS.", text, name)

    def test_the_choice_is_kept_on_the_device_and_idle_does_not_touch_it(self):
        text = (WEB / "kiosk-strings.js").read_text(encoding="utf-8")
        self.assertIn('const KEY = "checkin.lang"', text)
        self.assertIn("HB.store.set(KEY, lang)", text)
        kiosk = (WEB / "kiosk.js").read_text(encoding="utf-8")
        idle = kiosk.split("setInterval(() => {", 1)[1]
        self.assertNotIn("setLang", idle)
        self.assertEqual(kiosk.count("setLang("), 1)   # only the language button changes it

    def test_the_button_is_off_until_admin_turns_it_on(self):
        """There just in case: every device shows English, with no button, until Admin → Settings says otherwise."""
        sys.path.insert(0, str(WEB.parent))
        from checkin.db import SETTING_DEFAULTS
        from checkin.server import EDITABLE_SETTINGS, PUBLIC_SETTINGS
        self.assertIs(SETTING_DEFAULTS["chinese_button"], False)
        self.assertIn("chinese_button", PUBLIC_SETTINGS)
        self.assertIs(EDITABLE_SETTINGS["chinese_button"], bool)
        kiosk = (WEB / "kiosk.js").read_text(encoding="utf-8")
        self.assertIn('if (settings().chinese_button === true) {\n      bar.append(h("button", { class: "chip lang"', kiosk)
        self.assertIn('KS.allow(settings().chinese_button === true)', kiosk)
        strings = (WEB / "kiosk-strings.js").read_text(encoding="utf-8")
        self.assertIn('let lang = "en";', strings)                 # English before the main computer answers
        self.assertIn('const next = on ? chosen : "en";', strings)
        self.assertIn('"chinese_button"', (WEB / "admin.js").read_text(encoding="utf-8"))

    def test_the_language_table_has_a_generic_editing_note(self):
        _, _, text = table()
        self.assertIn("fluent speaker", text[:900])


if __name__ == "__main__":
    unittest.main()
