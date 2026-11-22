"""File locations and the small bootstrap config (port, bind address, allowed networks).

Everything else (classes, printers, label options, event names...) lives in
the database so it is included in backups.
"""

import json
import os
import secrets
import shutil
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATES = APP_DIR / "templates"
WEB_DIR = APP_DIR / "web"

DEFAULT_CONFIG = {
    "port": 8080,
    # 0.0.0.0 lets the iPads reach the main computer. Requests from anywhere
    # outside allowed_networks are refused before any handler runs.
    "bind": "0.0.0.0",
    "allowed_networks": ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
}


class AppPaths:
    def __init__(self, data_dir=None):
        base = data_dir or os.environ.get("CHECKIN_DATA") or (APP_DIR / "data")
        self.data = Path(base).resolve()
        self.db = self.data / "checkin.db"
        self.config = self.data / "config.json"
        self.templates = self.data / "templates"
        self.backups = self.data / "backups"
        self.keys = self.data / "keys"
        self.preview = self.data / "label-preview"
        self.exports = self.data / "exports"
        self.logs = self.data / "logs"

    def ensure(self) -> None:
        for path in (self.data, self.templates, self.backups, self.keys, self.preview, self.exports, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        for template in DEFAULT_TEMPLATES.glob("*.json"):
            target = self.templates / template.name
            if not target.exists():
                shutil.copyfile(template, target)
        if not self.config.exists():
            self.config.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")

    def load_config(self) -> dict:
        config = dict(DEFAULT_CONFIG)
        try:
            config.update(json.loads(self.config.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        return config

    def secret(self, name: str, size: int = 32) -> bytes:
        """Read (or create once) a random key kept outside the database file."""
        path = self.keys / name
        if path.exists():
            return path.read_bytes()
        value = secrets.token_bytes(size)
        path.write_bytes(value)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value
