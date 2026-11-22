"""Print queue, routing, per-printer workers and printer health monitoring.

Every label job is a row in print_job, written in the same database
transaction as the check-in it belongs to. One worker thread per printer takes
jobs in order, so a job is never lost if the app restarts, and retries reuse
the same job instead of touching attendance.
"""

import datetime as dt
import json
import logging
import threading
import time

from ..db import audit
from ..util import new_id, now_iso
from .backends import make_backend, offline
from .raster import MEDIA, build_job, check_media

log = logging.getLogger("checkin.printing")

STATUS_INTERVAL = 4.0
REQUEUE_WINDOW_SECONDS = 120


class PrintError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class PrinterManager:
    def __init__(self, db, renderer, paths):
        self.db = db
        self.renderer = renderer
        self.paths = paths
        self.cond = threading.Condition()
        self.stop_event = threading.Event()
        self.status_cache: dict[str, dict] = {}
        self.device_locks: dict[str, threading.Lock] = {}
        self.workers: dict[str, threading.Thread] = {}
        self.monitor_thread = None

    # ---- configuration --------------------------------------------------------------
    def printers(self, enabled_only=True) -> list[dict]:
        sql = "SELECT * FROM printer" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY sort_order, name"
        return self.db.all(sql)

    def printer(self, name: str) -> dict | None:
        return self.db.one("SELECT * FROM printer WHERE name=?", (name,))

    def backend_for(self, row: dict):
        address = row.get("address")
        if row["backend"] == "preview" and not address:
            address = str(self.paths.preview / row["name"])
        return make_backend(row["backend"], address, row.get("media") or "62")

    def save_printer(self, values: dict, user_id=None) -> dict:
        name = (values.get("name") or "").strip().upper()
        if not name or len(name) > 30:
            raise PrintError("Printer name is required (for example LEFT-PRINTER).")
        backend = values.get("backend") or "usb"
        if backend not in ("usb", "tcp", "preview"):
            raise PrintError("Connection must be usb, tcp or preview.")
        media = values.get("media") or "62"
        if media not in MEDIA:
            raise PrintError("Unknown label roll.")
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO printer(name, backend, address, media, enabled, sort_order, created_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET backend=excluded.backend, "
                "address=excluded.address, media=excluded.media, enabled=excluded.enabled, "
                "sort_order=excluded.sort_order",
                (name, backend, (values.get("address") or "").strip() or None, media,
                 0 if values.get("enabled") is False else 1, int(values.get("sort_order") or 0), now_iso()))
            audit(conn, "printer.save", "printer", name, {"backend": backend, "address": values.get("address"),
                                                         "media": media}, user_id)
        self.status_cache.pop(name, None)
        self.sync_workers()
        return self.printer(name)

    def delete_printer(self, name: str, user_id=None) -> None:
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM print_job WHERE printer=? AND status IN ('queued','printing')",
                            (name,)).fetchone():
                raise PrintError("That printer still has labels waiting. Retry them on another printer first.", 409)
            conn.execute("DELETE FROM printer WHERE name=?", (name,))
            conn.execute("UPDATE station SET printer=NULL WHERE printer=?", (name,))
            conn.execute("UPDATE station SET fallback_printer=NULL WHERE fallback_printer=?", (name,))
            audit(conn, "printer.delete", "printer", name, None, user_id)
        self.status_cache.pop(name, None)

    # ---- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        self.recover()
        self.stop_event.clear()
        self.sync_workers()
        self.monitor_thread = threading.Thread(target=self._monitor, name="printer-monitor", daemon=True)
        self.monitor_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.cond:
            self.cond.notify_all()
        for thread in list(self.workers.values()):
            thread.join(timeout=15)
        self.workers.clear()

    def recover(self) -> int:
        """After a crash or restart: jobs that were mid-print get 'unknown' so volunteers can check and
        reprint. Very recent queued jobs still print; older queued jobs also become 'unknown'."""
        cutoff = (dt.datetime.now() - dt.timedelta(seconds=REQUEUE_WINDOW_SECONDS)).isoformat(sep=" ")
        with self.db.tx() as conn:
            changed = conn.execute(
                "UPDATE print_job SET status='unknown', finished_at=?, "
                "error='The app restarted before the printer confirmed these labels. Check them and reprint if needed.' "
                "WHERE status='printing' OR (status='queued' AND created_at < ?)", (now_iso(), cutoff)).rowcount
            if changed:
                audit(conn, "print.recovered_unknown", "print_job", None, {"jobs": changed})
        return changed

    def sync_workers(self) -> None:
        for row in self.printers():
            thread = self.workers.get(row["name"])
            if thread and thread.is_alive():
                continue
            self.device_locks.setdefault(row["name"], threading.Lock())
            thread = threading.Thread(target=self._worker, args=(row["name"],), name=f"print-{row['name']}",
                                      daemon=True)
            self.workers[row["name"]] = thread
            thread.start()
        with self.cond:
            self.cond.notify_all()

    def notify(self) -> None:
        with self.cond:
            self.cond.notify_all()

    # ---- routing and jobs -----------------------------------------------------------------
    def route(self, station: dict | None, requested: str | None = None) -> tuple[str, str | None]:
        names = [p["name"] for p in self.printers()]
        if not names:
            raise PrintError("No printer is set up. Ask an administrator to add one in Admin → Printers.", 409)
        if requested:
            match = next((n for n in names if n.upper() == requested.upper()), None)
            if not match:
                raise PrintError(f"Printer {requested} is not available.", 404)
            return match, None
        primary = station.get("printer") if station else None
        if primary not in names:
            primary = names[0]
        state = self.status_cache.get(primary)
        if state and not state["ok"] and self.db.get_setting("failover") == "auto":
            fallback = station.get("fallback_printer") if station else None
            for candidate in [fallback] + names:
                other = self.status_cache.get(candidate or "")
                if candidate and candidate != primary and other and other["ok"]:
                    return candidate, f"{primary}: {state['message']}. Sent to {candidate} instead."
        return primary, None

    def create_job(self, conn, labels: list[dict], kind: str, txn_id=None, station=None, user_id=None,
                   printer=None) -> dict:
        target, note = self.route(station, printer)
        job_id = new_id()
        conn.execute(
            "INSERT INTO print_job(id, txn_id, kind, station_id, requested_printer, printer, payload, label_count, "
            "status, route_note, created_by, created_at) VALUES(?,?,?,?,?,?,?,?, 'queued', ?,?,?)",
            (job_id, txn_id, kind, station["id"] if station else None,
             printer or (station.get("printer") if station else None), target,
             json.dumps(labels, ensure_ascii=False), len(labels), note, user_id, now_iso()))
        return {"id": job_id, "printer": target, "route_note": note}

    def test_print(self, printer_name: str, station=None, user_id=None, number: int = 1) -> dict:
        row = self.printer(printer_name)
        if not row:
            raise PrintError("Printer not found.", 404)
        media = MEDIA.get(row["media"]) or MEDIA["62"]
        now = dt.datetime.now()
        labels = [{"template": "test", "data": {
            "printer": row["name"], "number": str(number), "media": media.description,
            "date": now.strftime("%a %d %b %Y"), "time": now.strftime("%H:%M:%S"),
            "church": self.db.get_setting("church_name") or ""}}]
        with self.db.tx() as conn:
            job = self.create_job(conn, labels, "test", station=station, user_id=user_id, printer=row["name"])
            audit(conn, "print.test", "printer", row["name"], {"job": job["id"]}, user_id,
                  station["id"] if station else None)
        self.notify()
        return self.job_view(self.db.one("SELECT * FROM print_job WHERE id=?", (job["id"],)))

    def retry(self, job_id: str, printer: str | None = None, station=None, user_id=None) -> dict:
        job = self.db.one("SELECT * FROM print_job WHERE id=?", (job_id,))
        if not job:
            raise PrintError("Print job not found.", 404)
        if job["status"] in ("queued", "printing"):
            return self.job_view(job)
        if job["status"] == "cancelled":
            raise PrintError("That check-in was undone, so its labels were cancelled.", 409)
        if job["status"] == "printed" and not printer:
            raise PrintError("Those labels already printed. Use Reprint if more copies are needed.", 409)
        if job["txn_id"]:
            txn = self.db.one("SELECT status FROM checkin_txn WHERE id=?", (job["txn_id"],))
            if txn and txn["status"] != "committed":
                raise PrintError("That check-in was undone, so its labels can't be printed.", 409)
        target, note = self.route(station, printer or None)
        with self.db.tx() as conn:
            conn.execute("UPDATE print_job SET status='queued', printer=?, error=NULL, route_note=?, finished_at=NULL "
                         "WHERE id=? AND status IN ('failed','unknown','printed')", (target, note, job_id))
            audit(conn, "print.retry", "print_job", job_id, {"printer": target, "previous": job["status"],
                                                           "txn": job["txn_id"]}, user_id,
                  station["id"] if station else None)
        self.notify()
        return self.job_view(self.db.one("SELECT * FROM print_job WHERE id=?", (job_id,)))

    def resolve(self, job_id: str, outcome: str, station=None, user_id=None) -> dict:
        """A volunteer confirms an unconfirmed job did print, or dismisses a failed one."""
        if outcome not in ("printed", "dismissed"):
            raise PrintError("Outcome must be printed or dismissed.")
        job = self.db.one("SELECT * FROM print_job WHERE id=?", (job_id,))
        if not job:
            raise PrintError("Print job not found.", 404)
        if job["status"] not in ("failed", "unknown"):
            return self.job_view(job)
        status = "printed" if outcome == "printed" else "cancelled"
        note = "Confirmed printed by volunteer" if outcome == "printed" else "Dismissed by volunteer"
        with self.db.tx() as conn:
            conn.execute("UPDATE print_job SET status=?, error=?, finished_at=? WHERE id=?",
                         (status, f"{note} (was: {job['error'] or job['status']})", now_iso(), job_id))
            audit(conn, "print.resolve", "print_job", job_id, {"outcome": outcome, "previous": job["status"]},
                  user_id, station["id"] if station else None)
        self.db.bump()
        return self.job(job_id)

    def job_view(self, job: dict | None) -> dict | None:
        if not job:
            return None
        return {k: job.get(k) for k in ("id", "txn_id", "kind", "status", "printer", "requested_printer", "error",
                                        "route_note", "label_count", "attempts", "created_at", "finished_at",
                                        "print_ms")}

    def job(self, job_id: str) -> dict | None:
        return self.job_view(self.db.one("SELECT * FROM print_job WHERE id=?", (job_id,)))

    def unresolved_jobs(self, limit=20) -> list[dict]:
        rows = self.db.all(
            "SELECT j.*, t.security_code, h.name AS household_name FROM print_job j "
            "LEFT JOIN checkin_txn t ON t.id=j.txn_id LEFT JOIN household h ON h.id=t.household_id "
            "WHERE j.status IN ('failed','unknown') AND j.created_at >= ? AND (t.status IS NULL OR t.status='committed') "
            "ORDER BY j.created_at DESC LIMIT ?", (dt.date.today().isoformat(), limit))
        out = []
        for row in rows:
            view = self.job_view(row)
            view["code"] = row["security_code"]
            view["household"] = row["household_name"]
            out.append(view)
        return out

    # ---- status ---------------------------------------------------------------------------------
    def status_list(self) -> list[dict]:
        out = []
        queue = {r["printer"]: r["n"] for r in self.db.all(
            "SELECT printer, COUNT(*) AS n FROM print_job WHERE status IN ('queued','printing') GROUP BY printer")}
        for row in self.printers(enabled_only=False):
            state = self.status_cache.get(row["name"]) or {"ok": None, "state": "checking",
                                                            "message": "Checking…"}
            out.append(dict(state, name=row["name"], backend=row["backend"], address=row["address"],
                            media=row["media"], enabled=bool(row["enabled"]), queued=queue.get(row["name"], 0)))
        return out

    def refresh_status(self, name: str) -> dict:
        row = self.printer(name)
        if not row:
            raise PrintError("Printer not found.", 404)
        lock = self.device_locks.setdefault(name, threading.Lock())
        with lock:
            status = self.backend_for(row).status()
        status = check_media(status, MEDIA.get(row["media"]) or MEDIA["62"])
        self._remember(name, status)
        return self.status_cache[name]

    def _remember(self, name: str, status) -> None:
        self.status_cache[name] = dict(status.to_dict(), checked_at=time.time(), checked=now_iso())

    def _monitor(self) -> None:
        while not self.stop_event.is_set():
            for row in self.printers():
                if self.stop_event.is_set():
                    break
                lock = self.device_locks.setdefault(row["name"], threading.Lock())
                if not lock.acquire(blocking=False):
                    continue  # busy printing; the worker reports its own status
                try:
                    status = self.backend_for(row).status()
                    status = check_media(status, MEDIA.get(row["media"]) or MEDIA["62"])
                except Exception as exc:  # noqa: BLE001 - never let the monitor die
                    status = offline(f"Printer offline: {exc}")
                finally:
                    lock.release()
                self._remember(row["name"], status)
            self.stop_event.wait(STATUS_INTERVAL)

    # ---- workers ----------------------------------------------------------------------------------
    def _claim(self, name: str) -> dict | None:
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM print_job WHERE status='queued' AND printer=? "
                               "ORDER BY created_at, rowid LIMIT 1", (name,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE print_job SET status='printing', started_at=?, attempts=attempts+1 WHERE id=?",
                         (now_iso(), row["id"]))
            return dict(row)

    def _worker(self, name: str) -> None:
        while not self.stop_event.is_set():
            if not self.printer(name) or not self.printer(name)["enabled"]:
                self.workers.pop(name, None)
                return
            try:
                job = self._claim(name)
            except Exception:  # noqa: BLE001
                log.exception("claim failed")
                job = None
            if not job:
                with self.cond:
                    self.cond.wait(timeout=2.0)
                continue
            try:
                self._run(name, job)
            except Exception as exc:  # noqa: BLE001 - a bad job must not stop the printer
                log.exception("print job %s crashed", job["id"])
                self._finish(job, "failed", f"Print failed: {exc}", 0)

    def _run(self, name: str, job: dict) -> None:
        started = time.time()
        row = self.printer(name)
        labels = json.loads(job["payload"])
        if not labels:
            self._finish(job, "failed", "Nothing to print", 0)
            return
        media_keys = {self.renderer.load(l["template"]).get("media", "62") for l in labels}
        if len(media_keys) != 1 or row["media"] not in media_keys:
            self._finish(job, "failed", f"Label templates are set up for {'/'.join(sorted(media_keys))}mm but "
                                        f"{name} is set to {row['media']}. Fix in Admin → Labels/Printers.", 0)
            return
        media = MEDIA[row["media"]]
        pages = [self.renderer.render_named(l["template"], l["data"]) for l in labels]
        data = build_job(pages, media, compress=bool(self.db.get_setting("print_compression")),
                         high_quality=bool(self.db.get_setting("print_high_quality")))
        lock = self.device_locks.setdefault(name, threading.Lock())
        with lock:
            result = self.backend_for(row).print_job(data, len(pages), self.db.get_setting("print_confirm"), media)
        if result.printer_status is not None:
            if result.status == "printed":
                result.printer_status.ok, result.printer_status.state = True, "ready"
                result.printer_status.message = "Ready"
            self._remember(name, result.printer_status)
        elapsed = int((time.time() - started) * 1000)
        if result.status == "failed" and not result.sent and self._auto_reroute(job, name, result.message):
            return
        self._finish(job, result.status, None if result.status == "printed" else result.message, elapsed)
        log.info("job %s on %s: %s %s (%d ms) %s", job["id"], name, result.status, result.message, elapsed,
                 result.log[-3:] if result.log else "")

    def _auto_reroute(self, job: dict, name: str, message: str) -> bool:
        """Nothing reached the printer (offline, out of labels...). Move the job to another ready printer."""
        if self.db.get_setting("failover") != "auto":
            return False
        station = self.db.one("SELECT * FROM station WHERE id=?", (job["station_id"],)) if job["station_id"] else None
        candidates = [station.get("fallback_printer") if station else None] + [p["name"] for p in self.printers()]
        for candidate in candidates:
            state = self.status_cache.get(candidate or "")
            if candidate and candidate != name and state and state["ok"]:
                note = f"{name}: {message}. Sent to {candidate} instead."
                with self.db.tx() as conn:
                    conn.execute("UPDATE print_job SET status='queued', printer=?, route_note=? WHERE id=?",
                                 (candidate, note, job["id"]))
                    audit(conn, "print.rerouted", "print_job", job["id"], {"from": name, "to": candidate,
                                                                         "reason": message})
                self.notify()
                return True
        return False

    def _finish(self, job: dict, status: str, error: str | None, elapsed: int) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE print_job SET status=?, error=?, finished_at=?, print_ms=? WHERE id=?",
                         (status, error, now_iso(), elapsed, job["id"]))
            if status != "printed":
                audit(conn, "print." + status, "print_job", job["id"], {"printer": job["printer"], "error": error,
                                                                      "txn": job["txn_id"]})
        self.db.bump()
