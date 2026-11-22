"""Breeze is an optional legacy integration: no screen or message names it, except these labels for old
data. Internal names (breeze_id, breeze_sync, the /api/admin/breeze routes, setting keys) stay."""

import ast
import os
import re
import sys
import unittest
from pathlib import Path

os.environ["CHECKIN_NO_USB"] = "1"
ROOT = Path(__file__).resolve().parent.parent
ALLOWED = ("Breeze event ID", "Breeze ID", "Imported from Breeze")


def leftover(text: str) -> list[str]:
    for phrase in ALLOWED:
        text = text.replace(phrase, "")
    return re.findall(r".{0,40}Breeze.{0,40}", text)


def docstrings(tree) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


class TestNoBreeze(unittest.TestCase):
    def test_screens(self):
        for path in sorted((ROOT / "web").glob("*.*")):
            if path.suffix in (".js", ".html", ".css"):
                text = re.sub(r"/\*.*?\*/|<!--.*?-->|^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.S | re.M)
                self.assertEqual(leftover(text), [], path.name)

    def test_messages(self):
        for path in sorted((ROOT / "checkin").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            skip = docstrings(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                    self.assertEqual(leftover(node.value), [], f"{path.relative_to(ROOT)}:{node.lineno}")

    def test_console_commands(self):
        sys.path.insert(0, str(ROOT))
        from checkin.__main__ import __doc__ as usage
        self.assertEqual(leftover(usage), [])
        self.assertNotIn("breeze-", usage)


if __name__ == "__main__":
    unittest.main()
