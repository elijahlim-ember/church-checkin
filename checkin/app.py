"""Application object: wires storage, auth, printing and services together."""

import logging
import logging.handlers
import os
import socket
import threading

from .auth import Auth
from .backup import BackupError, BackupManager
from .breeze_sync import BreezeSync
from .config import AppPaths
from .db import Database, audit, current_version, pending_migrations
from .labels import LabelRenderer
from .printing.backends import discover_usb
from .printing.manager import PrinterManager
from .service import CheckinService
from .service import _after_report
from .util import host_sgt_offset_warning, now_iso, sg_now

log = logging.getLogger("checkin")
SKIP_BACKUP_ENV = "CHECKIN_UPGRADE_WITHOUT_BACKUP"
SKIP_RESTORE_BACKUP_ENV = "CHECKIN_RESTORE_WITHOUT_BACKUP"


class UpgradeBlocked(RuntimeError):
    """An existing database needs an upgrade, but its safety backup could not be made."""


class RestoreBlocked(BackupError):
    """A restore would replace the data, but the safety backup of the current data could not be made."""


class App:
    def __init__(self, data_dir=None):
        self.paths = AppPaths(data_dir)
        self.paths.ensure()
        self._setup_logging()
        self.config = self.paths.load_config()
        self.db = Database(str(self.paths.db))
        self.backups = BackupManager(self.db, self.paths)
        self._migrate()
        self.renderer = LabelRenderer(self.paths.templates)
        self.printers = PrinterManager(self.db, self.renderer, self.paths)
        self.service = CheckinService(self.db, self.printers)
        self.auth = Auth(self.db, self.paths)
        self.breeze = BreezeSync(self)
        self.internet = {"ok": None, "checked": None}
        self.started_at = now_iso()
        self.lan_urls: list[str] = []
        self.lan_url_source = None  # set by `serve`: gives this computer's addresses as they are right now
        self._stop = threading.Event()
        self.restore_lock = threading.Lock()

    def _setup_logging(self) -> None:
        root = logging.getLogger("checkin")
        if root.handlers:
            return
        root.setLevel(logging.INFO)
        handler = logging.handlers.RotatingFileHandler(self.paths.logs / "checkin.log", maxBytes=2_000_000,
                                                       backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)

    def _migrate(self) -> None:
        """Upgrade the database. An existing database is backed up first, and isn't upgraded if that fails."""
        pending = pending_migrations(self.db.conn)
        if pending and current_version(self.db.conn) > 0 and self.backups.has_key():
            try:
                self.backups.create("pre-migration")
            except Exception as exc:  # noqa: BLE001 - any failure means there is no safety copy
                log.exception("pre-migration backup failed")
                if os.environ.get(SKIP_BACKUP_ENV) != "1":
                    self.db.close()
                    raise UpgradeBlocked(
                        f"The database was not upgraded because the backup before the upgrade failed ({exc}). "
                        f"Check there is free disk space and that {self.paths.backups} can be written, then start "
                        f"again. Nothing was changed. Only if that can't be fixed: copy the whole data folder "
                        f"somewhere safe, then start once with {SKIP_BACKUP_ENV}=1.") from exc
                log.warning("upgrading without a backup (%s=1)", SKIP_BACKUP_ENV)
        self.db.migrate()

    # ---- lifecycle ----------------------------------------------------------------------
    def start(self) -> None:
        if warning := host_sgt_offset_warning():
            log.warning("%s", warning)
        self.printers.start()
        threading.Thread(target=self._internet_monitor, name="internet", daemon=True).start()
        threading.Thread(target=self._auto_sync, name="auto-sync", daemon=True).start()
        self._retention = threading.Thread(target=self._daily_retention, name="retention", daemon=True)
        self._retention.start()
        self._sunday_wrapup = threading.Thread(target=self._sunday_wrapup_loop, name="sunday-wrap-up", daemon=True)
        self._sunday_wrapup.start()
        self.service.ensure_today_sessions()
        log.info("started; data folder %s", self.paths.data)

    def stop(self) -> None:
        self._stop.set()
        retention = getattr(self, "_retention", None)
        if retention is not None and retention is not threading.current_thread():
            retention.join(timeout=10)  # let a clean-up in flight finish before the database closes
        wrapup = getattr(self, "_sunday_wrapup", None)
        if wrapup is not None and wrapup is not threading.current_thread():
            wrapup.join(timeout=10)
        self.printers.stop()
        self.db.close()

    def _internet_monitor(self) -> None:
        """Information only: the check-in never depends on the internet."""
        while not self._stop.is_set():
            ok = False
            for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
                try:
                    with socket.create_connection((host, port), timeout=2):
                        ok = True
                        break
                except OSError:
                    continue
            self.internet = {"ok": ok, "checked": now_iso()}
            self._stop.wait(60)

    def _auto_sync(self) -> None:
        """With the church database connected, sync every 15 minutes."""
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                self.breeze.auto_tick()
            except Exception:  # noqa: BLE001 - the timer must never stop check-in
                log.exception("automatic sync failed to start")
            self._stop.wait(15 * 60)

    def _daily_retention(self) -> None:
        """The retention clean-up (Admin → Settings) once a day by itself, so nobody has to remember the button:
        old visitors anonymised, old audit entries and label contents removed, as the settings say."""
        from . import reports
        if self._stop.wait(10 * 60):
            return
        while not self._stop.is_set():
            try:
                with self.restore_lock:  # never while a backup is being restored over the database
                    if not self._stop.is_set() and not self.needs_setup():
                        result = reports.run_retention(self.db)
                        if any(result.values()):
                            log.info("daily retention clean-up: %s", result)
            except Exception:  # noqa: BLE001 - the timer must never stop check-in
                log.exception("daily retention clean-up failed")
            self._stop.wait(24 * 60 * 60)

    def _sunday_wrapup_loop(self) -> None:
        while not self._stop.wait(30):
            try:
                with self.restore_lock:
                    if self._stop.is_set() or self.needs_setup():
                        continue
                    now = sg_now()
                    rows = self.db.all("SELECT DISTINCT s.date FROM session s LEFT JOIN service v ON v.id=s.service_id "
                                       "LEFT JOIN day_report r ON r.date=s.date WHERE "
                                       "(v.weekday=6 OR strftime('%w', s.date)='0') AND "
                                       "strftime('%w', s.date)='0' AND "
                                       "((s.status='open' AND s.date<=?) OR r.date IS NOT NULL) ORDER BY s.date",
                                       (now.date().isoformat(),))
                    candidates = []
                    for row in rows:
                        if row["date"] == now.date().isoformat() and (now.hour, now.minute) < (23, 59):
                            continue
                        report = self.db.one("SELECT built_at FROM day_report WHERE date=?", (row["date"],))
                        if report:
                            sessions = self.db.all("SELECT closed_at FROM session WHERE date=?", (row["date"],))
                            if not any(s["closed_at"] and _after_report(s["closed_at"], report["built_at"])
                                       for s in sessions):
                                continue
                        candidates.append(row["date"])
                for on_date in candidates:
                    self.service.wrap_up_sunday(on_date, breeze=self.breeze, clock=sg_now,
                                                restore_lock=self.restore_lock)
                self.breeze.deliver_pending_reports(now=now)
            except Exception:  # noqa: BLE001 - a reporting or sync issue must never stop check-in
                log.exception("Sunday wrap-up scheduler failed")

    # ---- first-run setup ----------------------------------------------------------------------
    def refresh_lan_urls(self) -> list[str]:
        """The addresses other devices use, read again now: a laptop may have changed network since it started."""
        if self.lan_url_source:
            try:
                urls = self.lan_url_source()
            except Exception:  # never let a network hiccup stop a volunteer adding a device
                log.exception("could not read this computer's network addresses")
                return self.lan_urls
            if urls != self.lan_urls:
                log.info("this computer's addresses changed: iPads now use %s", ", ".join(urls) or "(none)")
                self.lan_urls = urls
        return self.lan_urls

    def needs_setup(self) -> bool:
        return not self.auth.has_admin()

    def setup(self, form: dict) -> dict:
        """Create the first administrator, volunteer PIN, backup passphrase, a default service,
        printers and this computer's station. Only allowed while no administrator exists."""
        if not self.needs_setup():
            raise BackupError("Setup has already been completed.", 409)
        self.auth.validate_secret("admin", form.get("admin_password") or "")
        self.auth.validate_secret("volunteer", form.get("volunteer_pin") or "")
        if len(form.get("backup_passphrase") or "") < 10:
            raise BackupError("The backup passphrase must be at least 10 characters.")
        church = (form.get("church_name") or "").strip()
        if church:
            self.db.set_setting("church_name", church)
        self.backups.set_passphrase(form.get("backup_passphrase") or "")
        admin_id = self.auth.create_user(form.get("admin_name") or "Admin", "admin", form.get("admin_password") or "")
        self.auth.create_user("Sunday volunteers", "volunteer", form.get("volunteer_pin") or "", admin_id)
        # No children's classes by default: check-in is for everyone. Classes can be added later in
        # Admin → Classes & services, and children's labels then show their class.
        if not self.db.scalar("SELECT COUNT(*) FROM service"):
            with self.db.tx() as conn:
                conn.execute("INSERT INTO service(name, weekday, start_time, breeze_event_name) VALUES(?,?,?,?)",
                             ("Sunday Service", 6, "09:30", form.get("breeze_event_name") or "Sunday Service"))
        printers = self.add_detected_printers(admin_id, preview_if_none=bool(form.get("preview_printers", True)))
        token, station = self.auth.register_station(form.get("station_name") or "Main desk",
                                                    printers[0] if printers else None,
                                                    printers[1] if len(printers) > 1 else None, admin_id)
        with self.db.tx() as conn:
            audit(conn, "setup.complete", None, None, {"printers": printers}, admin_id, station["id"])
        self.service.ensure_today_sessions()
        return {"station_token": token, "station": station, "printers": printers}

    def add_detected_printers(self, user_id=None, preview_if_none=False) -> list[str]:
        existing = {(p["backend"], (p["address"] or "").upper()) for p in self.printers.printers(False)}
        names = [p["name"] for p in self.printers.printers(False)]
        added = []
        found = discover_usb()
        for device in found:
            key = ("usb", (device["address"] or "").upper())
            if key in existing:
                continue
            name = ["LEFT-PRINTER", "RIGHT-PRINTER"][len(names)] if len(names) < 2 else f"PRINTER-{len(names) + 1}"
            # Prefer the USB serial number; without one, pin the device path only if there are several.
            address = device["serial"] or (device["address"] if len(found) > 1 else None)
            self.printers.save_printer({"name": name, "backend": "usb", "address": address, "media": "62"}, user_id)
            names.append(name)
            added.append(name)
        if not names and preview_if_none:
            for name in ("LEFT-PRINTER", "RIGHT-PRINTER"):
                self.printers.save_printer({"name": name, "backend": "preview", "media": "62"}, user_id)
                names.append(name)
                added.append(name)
        return names

    # ---- restore ----------------------------------------------------------------------------
    def restore(self, data: bytes, passphrase: str | None, user_id=None, user_name: str = "") -> dict:
        with self.restore_lock:
            self.backups.verify(data, passphrase)  # fail before touching anything
            safety = None
            if self.backups.has_key():
                try:
                    safety = self.backups.create("pre-restore", user_id=user_id)["name"]
                except Exception as exc:  # noqa: BLE001 - any failure means there is no safety copy
                    log.exception("pre-restore backup failed")
                    if os.environ.get(SKIP_RESTORE_BACKUP_ENV) != "1":
                        raise RestoreBlocked(
                            f"Nothing was restored because the backup of the current data failed ({exc}). Check "
                            f"there is free disk space and that {self.paths.backups} can be written, then try "
                            f"again. Only if that can't be fixed: copy the whole data folder somewhere safe, then "
                            f"restore with run.py restore and {SKIP_RESTORE_BACKUP_ENV}=1.") from exc
                    log.warning("restoring without a safety backup (%s=1)", SKIP_RESTORE_BACKUP_ENV)
            self.printers.stop()
            self.db.close()
            try:
                manifest = self.backups.restore_files(data, passphrase)
            finally:
                self.db.reopen()
                self.config = self.paths.load_config()
                self.auth.pepper = self.paths.secret("pin-pepper")
                self.printers.start()
            with self.db.tx() as conn:
                audit(conn, "backup.restore", "backup", manifest.get("created_at"),
                      {"by": user_name, "safety_backup": safety, "counts": manifest.get("counts")})
            self.db.bump()
            manifest["safety_backup"] = safety
            return manifest
