"""Encrypted backups (database + config + label templates) and restore.

File format: b"CHECKINBK1\\n" | 16-byte salt | 12-byte nonce | AES-256-GCM ciphertext of a zip.
The key comes from the backup passphrase via scrypt. A copy of the derived key
is kept in data/keys/backup.key (outside the database) so one-click and
automatic backups work without typing; restoring on a new computer needs the
passphrase, which the church keeps somewhere safe.
"""

import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import string
import sys
import tempfile
import zipfile
from pathlib import Path

from . import __version__
from .breeze import CHMS_CA_FILE, CHMS_KEY_FILE
from .tls import CA_CERT, CA_KEY
from .db import SCHEMA_VERSION, audit, current_version
from .util import now_iso

MAGIC = b"CHECKINBK1\n"
# The PIN pepper keeps volunteer PINs working after a restore on another computer; the church database's key
# (and its CA, when it isn't trusted by Windows) keeps the sync connected; the check-in CA keeps the iPads
# trusting HTTPS. Backups are encrypted. The old Breeze key is left out, so restoring an old backup never brings it back.
KEY_FILES = ("pin-pepper", CHMS_KEY_FILE, CHMS_CA_FILE, CA_CERT, CA_KEY)


class BackupError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024,
                          dklen=32)


def _aes():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise BackupError("The 'cryptography' package is required for encrypted backups (pip install cryptography).",
                          500) from exc
    return AESGCM


class BackupManager:
    def __init__(self, db, paths):
        self.db = db
        self.paths = paths
        self.key_file = paths.keys / "backup.key"

    # ---- key ----------------------------------------------------------------------
    def has_key(self) -> bool:
        return self.key_file.exists()

    def set_passphrase(self, passphrase: str, user_id=None) -> None:
        if len(passphrase or "") < 10:
            raise BackupError("The backup passphrase must be at least 10 characters.")
        salt = os.urandom(16)
        self._write_key(salt, derive_key(passphrase, salt))
        with self.db.tx() as conn:
            audit(conn, "backup.passphrase_set", None, None, None, user_id)

    def _write_key(self, salt: bytes, key: bytes) -> None:
        self.key_file.write_bytes(salt + key)
        try:
            os.chmod(self.key_file, 0o600)
        except OSError:
            pass

    def _key(self) -> tuple[bytes, bytes]:
        if not self.has_key():
            raise BackupError("Set a backup passphrase first (Admin → Backup).", 409)
        raw = self.key_file.read_bytes()
        return raw[:16], raw[16:48]

    # ---- create ------------------------------------------------------------------------
    def snapshot_db(self) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "snapshot.db")
            dest = sqlite3.connect(target)
            with self.db.lock:
                self.db.conn.backup(dest)
            dest.close()
            return Path(target).read_bytes()

    def build_archive(self, reason: str) -> bytes:
        db_bytes = self.snapshot_db()
        counts = {
            "people": self.db.scalar("SELECT COUNT(*) FROM person"),
            "attendance": self.db.scalar("SELECT COUNT(*) FROM attendance"),
            "sessions": self.db.scalar("SELECT COUNT(*) FROM session"),
        }
        manifest = {"app": "church-checkin", "app_version": __version__, "schema_version": SCHEMA_VERSION,
                    "created_at": now_iso(), "reason": reason, "counts": counts,
                    "db_sha256": hashlib.sha256(db_bytes).hexdigest()}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.writestr("checkin.db", db_bytes)
            if self.paths.config.exists():
                archive.write(self.paths.config, "config.json")
            for name in KEY_FILES:
                if (self.paths.keys / name).exists():
                    archive.write(self.paths.keys / name, "keys/" + name)
            for path in sorted(self.paths.templates.rglob("*")):
                if path.is_file():
                    archive.write(path, "templates/" + path.relative_to(self.paths.templates).as_posix())
        return buffer.getvalue()

    def encrypt(self, plain: bytes) -> bytes:
        salt, key = self._key()
        nonce = os.urandom(12)
        return MAGIC + salt + nonce + _aes()(key).encrypt(nonce, plain, MAGIC + salt)

    def create(self, reason: str = "manual", dest_dir: str | None = None, user_id=None) -> dict:
        data = self.encrypt(self.build_archive(reason))
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_reason = "".join(c for c in reason if c.isalnum() or c in "-_")[:20] or "backup"
        name = f"checkin-backup-{stamp}-{safe_reason}.checkinbak"
        local = self.paths.backups / name
        local.write_bytes(data)
        copied = None
        if dest_dir:
            dest = Path(dest_dir)
            if not dest.is_dir():
                raise BackupError(f"Folder not found: {dest_dir}", 404)
            copied = dest / name
            shutil.copyfile(local, copied)
        self.prune()
        with self.db.tx() as conn:
            audit(conn, "backup.create", "backup", name, {"reason": reason, "copied_to": str(copied) if copied else None,
                                                         "bytes": len(data)}, user_id)
        return {"name": name, "path": str(local), "copied_to": str(copied) if copied else None, "bytes": len(data)}

    def prune(self) -> None:
        keep = max(3, int(self.db.get_setting("backup_keep") or 20))
        files = sorted(self.paths.backups.glob("checkin-backup-*.checkinbak"), key=lambda p: p.name, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    def list(self) -> list[dict]:
        out = []
        for path in sorted(self.paths.backups.glob("checkin-backup-*.checkinbak"), key=lambda p: p.name, reverse=True):
            out.append({"name": path.name, "bytes": path.stat().st_size,
                        "modified": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(sep=" ",
                                                                                              timespec="seconds")})
        return out

    def read_local(self, name: str) -> bytes:
        path = self.paths.backups / Path(name).name
        if not path.exists() or not path.name.endswith(".checkinbak"):
            raise BackupError("Backup not found.", 404)
        return path.read_bytes()

    # ---- verify / restore ------------------------------------------------------------------
    def decrypt(self, data: bytes, passphrase: str | None = None) -> tuple[bytes, bytes, bytes]:
        if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 28:
            raise BackupError("This is not a Church Check-in backup file.")
        salt = data[len(MAGIC):len(MAGIC) + 16]
        nonce = data[len(MAGIC) + 16:len(MAGIC) + 28]
        body = data[len(MAGIC) + 28:]
        if passphrase:
            key = derive_key(passphrase, salt)
        else:
            stored_salt, key = self._key()
            if stored_salt != salt:
                raise BackupError("This backup was made with a different passphrase. Enter that passphrase.", 409)
        try:
            plain = _aes()(key).decrypt(nonce, body, MAGIC + salt)
        except Exception as exc:  # noqa: BLE001 - InvalidTag
            raise BackupError("Wrong passphrase, or the backup file is damaged.", 403) from exc
        return plain, salt, key

    def verify(self, data: bytes, passphrase: str | None = None) -> dict:
        plain, _, _ = self.decrypt(data, passphrase)
        return self._check_archive(plain)[0]

    def _check_archive(self, plain: bytes) -> tuple[dict, zipfile.ZipFile]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(plain))
            manifest = json.loads(archive.read("manifest.json"))
            db_bytes = archive.read("checkin.db")
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise BackupError("The backup archive is incomplete.") from exc
        if hashlib.sha256(db_bytes).hexdigest() != manifest.get("db_sha256"):
            raise BackupError("The database inside the backup failed its checksum.")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "check.db")
            Path(path).write_bytes(db_bytes)
            conn = sqlite3.connect(path)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                version = current_version(conn)
                people = conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
                attendance = conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
            finally:
                conn.close()
        if result != "ok":
            raise BackupError(f"The database inside the backup is damaged ({result}).")
        if version > SCHEMA_VERSION:
            raise BackupError("This backup is from a newer version of the app. Update the app first.", 409)
        manifest["verified"] = {"integrity": result, "schema_version": version, "people": people,
                                "attendance": attendance}
        return manifest, archive

    def restore_files(self, data: bytes, passphrase: str | None = None) -> dict:
        """Write a verified backup over the data folder. The caller must have closed the database."""
        plain, salt, key = self.decrypt(data, passphrase)
        manifest, archive = self._check_archive(plain)
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(self.paths.db) + suffix)
            if target.exists():
                target.unlink()
        self.paths.db.write_bytes(archive.read("checkin.db"))
        if "config.json" in archive.namelist():
            self.paths.config.write_bytes(archive.read("config.json"))
        for name in KEY_FILES:
            if "keys/" + name in archive.namelist():
                target = self.paths.keys / name
                target.write_bytes(archive.read("keys/" + name))
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
        for name in archive.namelist():
            if name.startswith("templates/") and not name.endswith("/"):
                rel = Path(name[len("templates/"):])
                if ".." in rel.parts or rel.is_absolute() or rel.drive:
                    continue
                target = self.paths.templates / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        if passphrase:
            # Moving to a new computer: keep this passphrase's key for future backups.
            self._write_key(salt, key)
        return manifest


def list_drives() -> list[dict]:
    """Removable drives (USB sticks) where a backup can be copied."""
    drives = []
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        for index, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << index):
                root = f"{letter}:\\"
                kind = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
                if kind == 2:  # DRIVE_REMOVABLE
                    label = ctypes.create_unicode_buffer(261)
                    kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), label, 261, None, None, None, None, 0)
                    drives.append({"path": root, "label": label.value or "USB drive"})
    else:
        for base in ("/Volumes", "/media", "/run/media"):
            root = Path(base)
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.is_dir() and not entry.is_symlink() and os.access(entry, os.W_OK):
                    if base == "/Volumes" and entry.name.startswith("Macintosh"):
                        continue
                    drives.append({"path": str(entry), "label": entry.name})
    return drives
