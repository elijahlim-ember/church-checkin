"""Local HTTP server: JSON API for the kiosk/admin screens plus the static web files.

Standard library only. Every request is checked against the allowed networks;
kiosk calls need a station (device) token and a volunteer login; admin calls
need an administrator login.
"""

import base64
import datetime as dt
import ipaddress
import json
import logging
import mimetypes
import re
import ssl
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, member_id, network, qr, reports, tls
from .auth import AuthError
from .backup import BackupError, list_drives
from .breeze import BreezeError
from .breeze_sync import SyncError
from .config import WEB_DIR
from .db import CHECKIN_MODES, audit
from .labels import SAMPLE_DATA, TemplateError
from .printing.backends import discover_usb
from .printing.manager import PrintError
from .printing.raster import MEDIA
from .reports import ReportError
from .roster import ImportError_
from .service import ServiceError
from .util import host_sgt_offset_warning, now_iso, today_iso

log = logging.getLogger("checkin.server")
MAX_BODY = 25 * 1024 * 1024
KNOWN_ERRORS = (AuthError, ServiceError, PrintError, ReportError, BackupError)
PUBLIC_SETTINGS = ("church_name", "success_return_seconds", "idle_return_seconds", "preselect_children",
                   "allow_volunteer_visitors", "country_code", "checkin_mode", "checkin_everyone", "pickup_codes",
                   "medical_alerts", "chinese_button")
EDITABLE_SETTINGS = {
    "church_name": str, "country_code": str, "child_max_age": int, "preselect_children": bool,
    "checkin_mode": str, "pickup_codes": bool, "medical_alerts": bool, "chinese_button": bool,
    "guardian_labels_per_checkin": int, "child_labels_per_child": int, "print_alert_note": bool,
    "success_return_seconds": int, "idle_return_seconds": int, "volunteer_session_hours": int, "failover": str,
    "backup_keep": int,
    "retention_visitor_months": int, "retention_audit_months": int, "retention_print_payload_days": int,
    "print_confirm": str, "print_compression": bool, "print_high_quality": bool, "allow_volunteer_visitors": bool,
}


class HttpError(Exception):
    def __init__(self, status, message, data=None):
        super().__init__(message)
        self.status = status
        self.data = data


class Ctx:
    def __init__(self, handler, app, params, query, body, raw):
        self.handler = handler
        self.app = app
        self.params = params
        self.query = query
        self.body = body
        self.raw = raw
        self.ip = handler.client_address[0]
        self.station = None
        self.session = None

    def q(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    @property
    def user_id(self):
        return self.session["user_id"] if self.session else None

    def need(self, *keys):
        missing = [k for k in keys if self.body.get(k) in (None, "")]
        if missing:
            raise HttpError(400, "Missing: " + ", ".join(missing))


class Response:
    def __init__(self, body: bytes, content_type: str, status=200, headers=None):
        self.body, self.content_type, self.status, self.headers = body, content_type, status, headers or {}


def download(content: str | bytes, filename: str, content_type="text/csv; charset=utf-8") -> Response:
    data = content.encode("utf-8") if isinstance(content, str) else content
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)  # nothing that could end the header or the quotes
    return Response(data, content_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


ROUTES = []


def route(method, pattern, level="volunteer"):
    regex = re.compile("^" + pattern + "$")

    def wrap(func):
        ROUTES.append((method, regex, level, func))
        return func
    return wrap


# =========================================================================================
# Public / setup
# =========================================================================================
@route("GET", "/api/ping", "public")
def ping(c):
    return {"ok": True, "version": __version__, "time": now_iso(), "setup_required": c.app.needs_setup(),
            "church_name": c.app.db.get_setting("church_name") or "Your Church"}


@route("POST", "/api/setup", "local")
def setup(c):
    c.need("admin_name", "admin_password", "volunteer_pin", "backup_passphrase")
    result = c.app.setup(c.body)
    return {"station_token": result["station_token"], "station": _station_view(result["station"]),
            "printers": result["printers"]}


@route("POST", "/api/setup/restore", "local")
def setup_restore(c):
    if not c.app.needs_setup():
        raise HttpError(409, "Setup is already complete. Use Admin → Backup to restore.")
    c.need("data_b64", "passphrase")
    manifest = c.app.restore(base64.b64decode(c.body["data_b64"]), c.body["passphrase"])
    return {"restored": manifest}


# =========================================================================================
# Station status and login
# =========================================================================================
def _station_view(station):
    if not station:
        return None
    return {"id": station["id"], "name": station["name"], "printer": station["printer"],
            "fallback_printer": station["fallback_printer"], "until": station.get("expires_at")}


@route("GET", "/api/status", "station")
def status(c):
    app = c.app
    app.service.ensure_today_sessions()
    sessions = app.service.today_sessions()
    default = app.service.default_session(sessions)
    settings = app.db.settings()
    printers = [{k: p.get(k) for k in ("name", "ok", "state", "message", "queued", "media_width", "enabled")}
                for p in app.printers.status_list() if p["enabled"]]
    user = None
    if c.session:
        user = {"name": c.session["user_name"], "role": c.session["role"]}
    return {
        "time": now_iso(), "date": today_iso(), "change_seq": app.db.change_seq, "version": __version__,
        "station": _station_view(c.station), "user": user, "sessions": sessions,
        "default_session_id": default["id"] if default else None, "printers": printers,
        "internet": app.internet, "settings": {k: settings.get(k) for k in PUBLIC_SETTINGS},
        "server_urls": app.lan_urls,
        "print_problems": len(app.printers.unresolved_jobs()) if user else 0,
    }


@route("POST", "/api/login", "station")
def login(c):
    token, user = c.app.auth.login_pin(str(c.body.get("pin") or ""), c.station["id"], "pin:" + c.ip)
    return {"token": token, "user": {"name": user["name"], "role": user["role"]}}


@route("POST", "/api/stations/one-time")
def one_time_code(c):
    """Any volunteer signed in on a station can let one more device (a phone or spare iPad) check people in today."""
    result = c.app.auth.create_device_code(c.station, c.user_id)
    # Where the other device goes today: asked afresh, because the computer may have changed network since it
    # started. The square is a plain picture of the address; the code is still typed in on the device.
    urls = result["urls"] = c.app.refresh_lan_urls()
    if urls:
        result["qr"], result["qr_url"] = qr.data_uri(urls[0]), urls[0]
        wifi = network.wifi_name()  # the name only, to this unlocked desk; never kept or logged
        if wifi:
            result["wifi"] = wifi
    return result


@route("POST", "/api/stations/join", "public")
def join_station(c):
    token, station = c.app.auth.join_with_code(str(c.body.get("code") or ""), "join:" + c.ip)
    return {"station_token": token, "station": _station_view(station)}


@route("POST", "/api/admin/login", "public")
def admin_login(c):
    station_id = c.station["id"] if c.station else None
    local = ipaddress.ip_address(c.ip.split("%")[0]).is_loopback
    token, user = c.app.auth.login_admin(c.body.get("name"), c.body.get("password"), station_id, "admin:" + c.ip,
                                         local)
    return {"token": token, "user": {"name": user["name"], "role": user["role"]}}


@route("POST", "/api/logout", "public")
def logout(c):
    token = c.handler.bearer()
    if token:
        c.app.auth.logout(token)
    return {"ok": True}


# =========================================================================================
# Sunday operations (volunteer)
# =========================================================================================
@route("GET", "/api/services")
def services(c):
    return c.app.db.all("SELECT * FROM service WHERE active=1 ORDER BY weekday, start_time")


@route("POST", "/api/sessions/open")
def open_session(c):
    c.need("service_id")
    return c.app.service.open_session(int(c.body["service_id"]), c.user_id, c.station["id"])


@route("GET", "/api/search")
def search(c):
    started = time.perf_counter()
    results = c.app.service.search(c.q("q", ""))
    return {"results": results, "ms": round((time.perf_counter() - started) * 1000, 1)}


@route("GET", r"/api/households/(\d+)")
def household(c):
    session_id = c.q("session_id")
    return c.app.service.household(int(c.params[0]), int(session_id) if session_id else None)


@route("POST", "/api/checkin")
def checkin(c):
    c.need("txn_id", "session_id", "household_id", "person_ids")
    started = time.perf_counter()
    result = c.app.service.check_in(str(c.body["txn_id"]), int(c.body["session_id"]), int(c.body["household_id"]),
                                    c.body["person_ids"], c.station, c.user_id)
    result["commit_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


@route("GET", r"/api/txn/([A-Za-z0-9-]+)")
def txn(c):
    return c.app.service.txn_result(c.params[0])


@route("POST", r"/api/txn/([A-Za-z0-9-]+)/undo")
def undo(c):
    return c.app.service.undo(c.params[0], c.body.get("reason"), c.station, c.user_id)


@route("POST", r"/api/txn/([A-Za-z0-9-]+)/reprint")
def reprint(c):
    return c.app.service.reprint(c.params[0], c.body.get("which") or "all", c.station, c.body.get("printer"),
                                 c.user_id, c.body.get("person_ids") or None)


@route("GET", r"/api/jobs/([a-f0-9]+)")
def job(c):
    view = c.app.printers.job(c.params[0])
    if not view:
        raise HttpError(404, "Print job not found.")
    return view


@route("POST", r"/api/jobs/([a-f0-9]+)/retry")
def retry(c):
    return c.app.printers.retry(c.params[0], c.body.get("printer"), c.station, c.user_id)


@route("POST", r"/api/jobs/([a-f0-9]+)/resolve")
def resolve_job(c):
    return c.app.printers.resolve(c.params[0], c.body.get("outcome"), c.station, c.user_id)


@route("GET", "/api/print-problems")
def print_problems(c):
    return c.app.printers.unresolved_jobs()


@route("GET", r"/api/checkout/([A-Za-z0-9]+)")
def checkout_lookup(c):
    return c.app.service.checkout_lookup(c.params[0])


@route("POST", "/api/checkout")
def checkout(c):
    return c.app.service.check_out(c.body.get("attendance_ids") or [], c.station, c.user_id)


@route("POST", "/api/visitors")
def visitors(c):
    if c.session["role"] != "admin" and not c.app.db.get_setting("allow_volunteer_visitors"):
        raise HttpError(403, "Visitor registration is switched off for volunteers.")
    return c.app.service.register_visitor(c.body, c.station, c.user_id)


@route("GET", "/api/classrooms")
def classrooms(c):
    return [{"id": r["id"], "name": r["name"], "room": r["room"]} for r in c.app.service.classrooms()]


@route("GET", "/api/recent")
def recent(c):
    return c.app.service.recent_transactions(c.station["id"] if c.q("all") != "1" else None)


@route("GET", "/api/room-counts")
def room_counts(c):
    session_id = c.q("session_id")
    return c.app.service.room_counts(int(session_id)) if session_id else []


@route("POST", r"/api/printers/([A-Za-z0-9_-]+)/test")
def test_print(c):
    return c.app.printers.test_print(c.params[0], c.station, c.user_id)


# =========================================================================================
# Administration
# =========================================================================================
@route("GET", "/api/admin/overview", "admin")
def overview(c):
    db = c.app.db
    last_import = db.one("SELECT ts, filename, totals, status FROM import_batch WHERE status='committed' "
                         "ORDER BY id DESC LIMIT 1")
    backups = c.app.backups.list()
    warnings = []
    if timezone_warning := host_sgt_offset_warning():
        warnings.append(timezone_warning)
    if not c.app.backups.has_key():
        warnings.append("No backup passphrase set. Backups cannot be made.")
    if not db.scalar("SELECT COUNT(*) FROM printer WHERE enabled=1"):
        warnings.append("No printers configured.")
    if not db.scalar("SELECT COUNT(*) FROM person WHERE active=1"):
        warnings.append("No people yet. Connect the church database in Admin → Sync.")
    breeze_state = c.app.breeze.status()
    if breeze_state["connected"]:
        unsent = db.scalar(
            "SELECT COUNT(*) FROM attendance a JOIN session s ON s.id=a.session_id WHERE a.status<>'undone' "
            "AND a.breeze_sent_at IS NULL AND s.date>=? AND s.date<?", (breeze_state["since"], today_iso()))
        if unsent:
            warnings.append(f"{unsent} check-ins from earlier days aren't in the church database yet. "
                             "Open Sync and press Check.")
    service, day = c.app.service, today_iso()
    last_report = db.one("SELECT date,built_at,late,delivered_at,attempts,delivery_error FROM day_report ORDER BY date DESC LIMIT 1")
    if last_report:
        last_report = {"date": last_report["date"], "built_at": last_report["built_at"],
                       "late": bool(last_report["late"]), "delivered": bool(last_report["delivered_at"]),
                       "attempts": last_report["attempts"], "delivery_error": bool(last_report["delivery_error"])}
    rooms = service.classrooms()
    unassigned = sum(1 for p in db.all("SELECT * FROM person WHERE is_child=1 AND active=1")
                     if service.child_class(p, rooms, day) is None and service.eligible(p, None, day))
    return {
        "people": db.scalar("SELECT COUNT(*) FROM person WHERE active=1"),
        "children": db.scalar("SELECT COUNT(*) FROM person WHERE active=1 AND is_child=1"),
        "households": db.scalar("SELECT COUNT(*) FROM household WHERE active=1"),
        "visitors": db.scalar("SELECT COUNT(*) FROM person WHERE source='visitor' AND anonymised=0"),
        "children_without_class": unassigned,
        "sessions_today": c.app.service.today_sessions(),
        "printers": c.app.printers.status_list(),
        "last_import": last_import,
        "last_backup": backups[0] if backups else None,
        "backup_key": c.app.backups.has_key(),
        "warnings": warnings,
        "data_folder": str(c.app.paths.data),
        "lan_urls": c.app.refresh_lan_urls(),
        "started_at": c.app.started_at,
        "sunday_report": last_report,
        "version": __version__,
    }


@route("GET", "/api/admin/settings", "admin")
def get_settings(c):
    return c.app.db.settings()


@route("PUT", "/api/admin/settings", "admin")
def put_settings(c):
    mode = c.body.get("checkin_mode")
    if mode is not None and mode not in CHECKIN_MODES:
        raise HttpError(400, "checkin_mode must be individual, family or children")
    if mode == "children" and c.app.db.get_setting("checkin_mode") != "children" \
            and c.body.get("confirm_children_only") is not True:
        raise HttpError(409, "Children-only check-in hides all adults at check-in, so only children can check in. "
                             "Confirm to turn it on.")
    changed = {}
    for key, value in c.body.items():
        kind = EDITABLE_SETTINGS.get(key)
        if not kind:
            continue
        try:
            value = (value in (True, "true", "1", 1, "on")) if kind is bool else kind(value)
        except (TypeError, ValueError):
            raise HttpError(400, f"Invalid value for {key}")
        if key == "failover" and value not in ("auto", "ask"):
            raise HttpError(400, "failover must be auto or ask")
        if key == "print_confirm" and value not in ("per_label", "job", "none"):
            raise HttpError(400, "print_confirm must be per_label, job or none")
        c.app.db.set_setting(key, value)
        changed[key] = value
    with c.app.db.tx() as conn:
        audit(conn, "settings.update", None, None, changed, c.user_id)
    return c.app.db.settings()


# ---- classes, services, sessions --------------------------------------------------------------
def _class_values(body):
    def num(key):
        value = body.get(key)
        return None if value in (None, "") else int(value)
    return ((body.get("name") or "").strip(), (body.get("room") or "").strip() or None, num("min_age_months"),
            num("max_age_months"), (body.get("school_years") or "").strip() or None,
            (body.get("breeze_event_name") or "").strip() or None, int(body.get("sort_order") or 0),
            0 if body.get("active") is False else 1)


@route("GET", "/api/admin/classrooms", "admin")
def admin_classrooms(c):
    rows = c.app.db.all("SELECT * FROM classroom ORDER BY sort_order, name")
    for row in rows:
        row["children"] = c.app.db.scalar("SELECT COUNT(*) FROM person WHERE classroom_id=? AND active=1",
                                          (row["id"],))
    return rows


@route("POST", "/api/admin/classrooms", "admin")
def add_classroom(c):
    values = _class_values(c.body)
    if not values[0]:
        raise HttpError(400, "Class name is required.")
    with c.app.db.tx() as conn:
        cur = conn.execute("INSERT INTO classroom(name, room, min_age_months, max_age_months, school_years, "
                           "breeze_event_name, sort_order, active) VALUES(?,?,?,?,?,?,?,?)", values)
        audit(conn, "class.create", "classroom", cur.lastrowid, {"name": values[0]}, c.user_id)
    return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/classrooms/(\d+)", "admin")
def edit_classroom(c):
    values = _class_values(c.body)
    with c.app.db.tx() as conn:
        conn.execute("UPDATE classroom SET name=?, room=?, min_age_months=?, max_age_months=?, school_years=?, "
                     "breeze_event_name=?, sort_order=?, active=? WHERE id=?", (*values, int(c.params[0])))
        audit(conn, "class.update", "classroom", c.params[0], {"name": values[0]}, c.user_id)
    return {"ok": True}


@route("GET", "/api/admin/services", "admin")
def admin_services(c):
    return c.app.db.all("SELECT * FROM service ORDER BY active DESC, weekday, start_time")


def _service_values(body):
    weekday = body.get("weekday")
    start = (body.get("start_time") or "").strip() or None
    if start and not re.fullmatch(r"\d{2}:\d{2}", start):
        raise HttpError(400, "Start time must look like 09:30")
    name = (body.get("name") or "").strip()
    if not name:
        raise HttpError(400, "Service name is required.")
    linked = str(body.get("chms_service_id") or "").strip()
    if linked and not (linked.isascii() and linked.isdigit() and len(linked) <= 12):
        raise HttpError(400, "Choose the service from the list.")
    return (name, None if weekday in (None, "") else int(weekday), start,
            (body.get("breeze_event_name") or "").strip() or name, 0 if body.get("active") is False else 1,
            linked or None)


@route("GET", "/api/admin/chms-services", "admin")
def chms_services(c):
    """The church database's own services, to link each of check-in's to one."""
    return c.app.breeze.chms_services()


@route("POST", "/api/admin/services", "admin")
def add_service(c):
    values = _service_values(c.body)
    with c.app.db.tx() as conn:
        cur = conn.execute("INSERT INTO service(name, weekday, start_time, breeze_event_name, active, "
                           "chms_service_id) VALUES(?,?,?,?,?,?)", values)
        audit(conn, "service.create", "service", cur.lastrowid, {"name": values[0]}, c.user_id)
    return {"id": cur.lastrowid}


@route("PUT", r"/api/admin/services/(\d+)", "admin")
def edit_service(c):
    values = _service_values(c.body)
    with c.app.db.tx() as conn:
        conn.execute("UPDATE service SET name=?, weekday=?, start_time=?, breeze_event_name=?, active=?, "
                     "chms_service_id=? WHERE id=?", (*values, int(c.params[0])))
        audit(conn, "service.update", "service", c.params[0], {"name": values[0]}, c.user_id)
    return {"ok": True}


@route("GET", "/api/admin/sessions", "admin")
def admin_sessions(c):
    date = c.q("date")
    sql = ("SELECT s.*, (SELECT COUNT(*) FROM attendance a WHERE a.session_id=s.id AND a.status<>'undone') "
           "AS checked_in FROM session s ")
    if date:
        return c.app.db.all(sql + "WHERE s.date=? ORDER BY s.start_time", (date,))
    return c.app.db.all(sql + "ORDER BY s.date DESC, s.start_time LIMIT 60")


@route("PUT", r"/api/admin/sessions/(\d+)", "admin")
def edit_session(c):
    event = (c.body.get("breeze_event_name") or "").strip()
    with c.app.db.tx() as conn:
        if event:
            conn.execute("UPDATE session SET breeze_event_name=? WHERE id=?", (event, int(c.params[0])))
        audit(conn, "session.update", "session", c.params[0], {"breeze_event_name": event}, c.user_id)
    if c.body.get("status") == "closed":
        c.app.service.close_session(int(c.params[0]), c.user_id)
    return {"ok": True}


# ---- printers and stations -----------------------------------------------------------------------
@route("GET", "/api/admin/printers", "admin")
def admin_printers(c):
    return {"printers": c.app.printers.status_list(),
            "media": [{"key": m.key, "description": m.description} for m in MEDIA.values()]}


@route("GET", "/api/admin/printers/detect", "admin")
def detect_printers(c):
    return discover_usb()


@route("POST", "/api/admin/printers", "admin")
def save_printer(c):
    return c.app.printers.save_printer(c.body, c.user_id)


@route("DELETE", r"/api/admin/printers/([A-Za-z0-9_-]+)", "admin")
def delete_printer(c):
    c.app.printers.delete_printer(c.params[0], c.user_id)
    return {"ok": True}


@route("POST", r"/api/admin/printers/([A-Za-z0-9_-]+)/test", "admin")
def admin_test_print(c):
    return c.app.printers.test_print(c.params[0], c.station, c.user_id)


@route("POST", r"/api/admin/printers/([A-Za-z0-9_-]+)/refresh", "admin")
def refresh_printer(c):
    return c.app.printers.refresh_status(c.params[0])


@route("GET", "/api/admin/stations", "admin")
def admin_stations(c):
    return c.app.db.all("SELECT id, name, printer, fallback_printer, active, last_seen, last_ip, created_at, "
                        "expires_at FROM station ORDER BY expires_at IS NOT NULL, name")


@route("POST", "/api/admin/stations/register", "admin")
def register_station(c):
    c.need("name")
    token, station = c.app.auth.register_station(c.body["name"], c.body.get("printer") or None,
                                                 c.body.get("fallback_printer") or None, c.user_id)
    return {"station_token": token, "station": _station_view(station)}


@route("PUT", r"/api/admin/stations/(\d+)", "admin")
def edit_station(c):
    sid = int(c.params[0])
    with c.app.db.tx() as conn:
        conn.execute("UPDATE station SET printer=?, fallback_printer=?, active=? WHERE id=?",
                     (c.body.get("printer") or None, c.body.get("fallback_printer") or None,
                      0 if c.body.get("active") is False else 1, sid))
        if c.body.get("active") is False:
            conn.execute("UPDATE station SET token_hash=NULL WHERE id=?", (sid,))
        audit(conn, "station.update", "station", sid, c.body, c.user_id)
    return {"ok": True}


# ---- users ------------------------------------------------------------------------------------------
@route("GET", "/api/admin/users", "admin")
def admin_users(c):
    return c.app.db.all("SELECT id, name, role, active, created_at FROM user ORDER BY role, name")


@route("POST", "/api/admin/users", "admin")
def add_user(c):
    c.need("name", "role", "secret")
    return {"id": c.app.auth.create_user(c.body["name"], c.body["role"], str(c.body["secret"]), c.user_id)}


@route("PUT", r"/api/admin/users/(\d+)", "admin")
def edit_user(c):
    active = c.body.get("active")
    c.app.auth.update_user(int(c.params[0]), str(c.body.get("secret") or "") or None,
                           None if active is None else bool(active), c.user_id)
    return {"ok": True}


# ---- people ------------------------------------------------------------------------------------------------
@route("GET", "/api/admin/people", "admin")
def people(c):
    return reports.people_search(c.app.db, c.q("q", ""))


@route("GET", r"/api/admin/people/(\d+)", "admin")
def person(c):
    row = c.app.db.one("SELECT * FROM person WHERE id=?", (int(c.params[0]),))
    if not row:
        raise HttpError(404, "Person not found.")
    row["phones"] = [p["digits"] for p in c.app.db.all("SELECT digits FROM phone WHERE person_id=?", (row["id"],))]
    row["households"] = c.app.db.all(
        "SELECT h.id, h.name, h.family_key, m.role FROM household_member m JOIN household h ON h.id=m.household_id "
        "WHERE m.person_id=? AND m.active=1", (row["id"],))
    row["attendance"] = c.app.db.all(
        "SELECT s.date, s.name, a.class_name, a.status FROM attendance a JOIN session s ON s.id=a.session_id "
        "WHERE a.person_id=? ORDER BY s.date DESC LIMIT 20", (row["id"],))
    with c.app.db.tx() as conn:
        audit(conn, "person.view", "person", row["id"], None, c.user_id)
    return row


@route("PUT", r"/api/admin/people/(\d+)", "admin")
def edit_person(c):
    pid = int(c.params[0])
    current = c.app.db.one("SELECT * FROM person WHERE id=?", (pid,))
    if not current:
        raise HttpError(404, "Person not found.")
    fields = {}
    for key in ("preferred_name", "medical_note", "pickup_note", "notes", "school_year", "birthdate"):
        if key in c.body:
            fields[key] = (str(c.body[key]).strip() or None) if c.body[key] is not None else None
    for key in ("medical_alert", "active", "is_child"):
        if key in c.body:
            fields[key] = 1 if c.body[key] else 0
    if "classroom_id" in c.body:
        fields["classroom_id"] = int(c.body["classroom_id"]) if c.body["classroom_id"] else None
        fields["class_source"] = "manual" if fields["classroom_id"] else None
    if "breeze_id" in c.body:
        typed = str(c.body["breeze_id"] or "").strip()
        bid = member_id.parse(typed, bare=True) or typed or None
        if member_id.looks_like_one(typed) and not member_id.parse(typed):
            raise HttpError(400, "That member ID has a typo. Check it and try again.")
        if bid and c.app.db.one("SELECT id FROM person WHERE breeze_id=? AND id<>?", (bid, pid)):
            raise HttpError(409, "Another person already has that member ID.")
        if bid != current["breeze_id"]:
            fields["breeze_id"] = bid
            fields["legacy_id"] = None    # the old ID belonged to the record it was linked to before
    if not fields:
        return {"ok": True}
    fields["updated_at"] = now_iso()
    with c.app.db.tx() as conn:
        # Only column names go into the text, and they come from the fixed list at the top of this function,
        # never from the request; every value is still bound with ?. Check that still holds before adding a
        # field whose name is taken from the body.
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        conn.execute(f"UPDATE person SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), pid))
        audit(conn, "person.update", "person", pid,
              {k: ("(changed)" if k in ("medical_note", "notes") else v) for k, v in fields.items()}, c.user_id)
    c.app.db.bump()
    return {"ok": True}


@route("POST", r"/api/admin/people/(\d+)/anonymise", "admin")
def anonymise(c):
    if len((c.body.get("reason") or "").strip()) < 3:
        raise HttpError(400, "Give a reason (for example 'Request from family').")
    reports.anonymise_person(c.app.db, int(c.params[0]), c.user_id, c.body["reason"])
    return {"ok": True}


# ---- reports -----------------------------------------------------------------------------------------
@route("GET", r"/api/admin/reports/([a-z_]+)", "admin")
def get_report(c):
    session_id = c.q("session_id")
    data = reports.report(c.app.db, c.params[0], c.q("date"), int(session_id) if session_id else None)
    if c.params[0] in ("medical", "visitors", "attendance"):
        with c.app.db.tx() as conn:
            audit(conn, "report.view", "report", c.params[0], {"date": data["date"], "format": c.q("format")},
                  c.user_id)
    if c.q("format") == "csv":
        return download(reports.to_csv(data["columns"], data["rows"], for_excel=True),
                        f"{c.params[0]}-{data['date']}.csv")
    return data


# ---- sync with the church database (internal names stay breeze_*) ------------------------------------------------------------------------------------
def _breeze_call(func, *args):
    try:
        return func(*args)
    except BreezeError as err:
        raise HttpError(400 if err.status >= 400 and err.status < 500 else 502, str(err))
    except SyncError as err:
        raise HttpError(409, str(err))


@route("GET", "/api/admin/breeze", "admin")
def breeze_status(c):
    return c.app.breeze.status()


@route("POST", "/api/admin/breeze/connect-chms", "admin")
def chms_connect(c):
    c.need("api_key")
    return _breeze_call(c.app.breeze.connect_chms, str(c.body.get("url") or ""), str(c.body["api_key"]), c.user_id,
                        c.body.get("auto") is not False)


@route("POST", "/api/admin/breeze/disconnect", "admin")
def breeze_disconnect(c):
    c.app.breeze.disconnect(c.user_id)
    return {"ok": True}


@route("POST", "/api/admin/breeze/check", "admin")
def breeze_check(c):
    return _breeze_call(c.app.breeze.start, "check", c.user_id)


@route("POST", "/api/admin/breeze/sync", "admin")
def breeze_sync(c):
    decisions = c.body.get("decisions") if isinstance(c.body.get("decisions"), dict) else {}
    return _breeze_call(c.app.breeze.start, "sync", c.user_id, {str(k): str(v) for k, v in decisions.items()})


def _resend_day(value) -> str:
    try:
        day = dt.date.fromisoformat(str(value or "")).isoformat()
    except ValueError:
        raise HttpError(400, "Choose a date.")
    if day > today_iso():
        raise HttpError(400, "Choose today or an earlier day.")
    return day


@route("GET", "/api/admin/breeze/resend", "admin")
def breeze_resend_preview(c):
    return _breeze_call(c.app.breeze.resend_from, _resend_day(c.q("from")), c.user_id, True)


@route("POST", "/api/admin/breeze/resend", "admin")
def breeze_resend(c):
    return _breeze_call(c.app.breeze.resend_from, _resend_day(c.body.get("from")), c.user_id)


@route("PUT", "/api/admin/breeze/settings", "admin")
def breeze_settings(c):
    db = c.app.db
    if c.body.get("since"):
        try:
            since = dt.date.fromisoformat(str(c.body["since"])).isoformat()
        except ValueError:
            raise HttpError(400, "Choose a date.")
        db.set_setting("breeze_sync_since", since)
    if "chms_auto_sync" in c.body:
        db.set_setting("chms_auto_sync", bool(c.body["chms_auto_sync"]))
        with db.tx() as conn:
            audit(conn, "chms.auto_sync", None, None, {"on": bool(c.body["chms_auto_sync"])}, c.user_id)
    if c.body.get("event_name") and c.body.get("session_id"):
        session = db.one("SELECT s.id, s.service_id, v.breeze_event_name AS old FROM session s "
                         "LEFT JOIN service v ON v.id=s.service_id WHERE s.id=?", (int(c.body["session_id"]),))
        if not session:
            raise HttpError(404, "Session not found.")
        name = str(c.body["event_name"]).strip()
        with db.tx() as conn:
            if session["service_id"] and c.body.get("scope") != "day":
                # From now on this service's check-ins go to that event, including earlier days
                # that still had the old name. scope="day" changes only this day (a one-off event).
                # The link by number goes too: the name chosen here decides, and its number is learned again.
                conn.execute("UPDATE service SET breeze_event_name=?, chms_service_id=NULL WHERE id=?",
                             (name, session["service_id"]))
                conn.execute("UPDATE session SET breeze_event_name=? WHERE service_id=? AND "
                             "(breeze_event_name IS NULL OR breeze_event_name='' OR breeze_event_name=?)",
                             (name, session["service_id"], session["old"] or ""))
            conn.execute("UPDATE session SET breeze_event_name=? WHERE id=?", (name, session["id"]))
            audit(conn, "breeze.event_name", "session", session["id"], {"event_name": name}, c.user_id)
    return c.app.breeze.status()


@route("GET", "/api/admin/breeze/rules", "admin")
def sync_rules(c):
    db = c.app.db
    return {"rules": db.get_setting("import_rules") or [], "columns": db.get_setting("sync_columns") or [],
            "column_values": db.get_setting("sync_column_values") or {}}


@route("PUT", "/api/admin/breeze/rules", "admin")
def put_sync_rules(c):
    """Leave-out rules: people matching any rule are not checked in. The next sync applies them."""
    db = c.app.db
    columns = db.get_setting("sync_columns") or []
    rules = c.body.get("rules")
    if not isinstance(rules, list):
        raise HttpError(400, "Send a list of rules.")
    clean = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("column") not in columns or rule.get("op") not in ("any", "in"):
            raise HttpError(400, "Pick a column for each rule. Press Check first if the list is empty.")
        values = rule.get("values") or []
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise HttpError(400, "Each rule's values must be text.")
        if rule["op"] == "in" and not values:
            raise HttpError(400, f"Pick at least one value for {rule['column']}.")
        clean.append({"column": rule["column"], "op": rule["op"], "values": values if rule["op"] == "in" else []})
    db.set_setting("import_rules", clean)
    with db.tx() as conn:
        audit(conn, "sync.rules", None, None, {"rules": clean}, c.user_id)
    return {"rules": clean}


@route("GET", "/api/admin/jobs", "admin")
def admin_jobs(c):
    date = c.q("date") or today_iso()
    rows = c.app.db.all("SELECT * FROM print_job WHERE substr(created_at,1,10)=? ORDER BY created_at DESC LIMIT 300",
                        (date,))
    return [c.app.printers.job_view(r) for r in rows]


@route("GET", "/api/admin/audit", "admin")
def admin_audit(c):
    return reports.report(c.app.db, "audit", c.q("date"))


@route("POST", "/api/admin/retention/run", "admin")
def retention(c):
    return reports.run_retention(c.app.db, c.user_id)


# ---- backups ------------------------------------------------------------------------------------------------------
@route("GET", "/api/admin/backups", "admin")
def backups(c):
    return {"backups": c.app.backups.list(), "has_key": c.app.backups.has_key(), "drives": list_drives(),
            "folder": str(c.app.paths.backups)}


@route("POST", "/api/admin/backups", "admin")
def make_backup(c):
    return c.app.backups.create("manual", c.body.get("dest_dir") or None, c.user_id)


@route("GET", r"/api/admin/backups/([A-Za-z0-9._-]+\.checkinbak)", "admin")
def get_backup(c):
    data = c.app.backups.read_local(c.params[0])
    with c.app.db.tx() as conn:
        audit(conn, "backup.download", "backup", c.params[0], None, c.user_id)
    return download(data, c.params[0], "application/octet-stream")


def _backup_bytes(c) -> bytes:
    if c.body.get("name"):
        return c.app.backups.read_local(c.body["name"])
    if c.body.get("data_b64"):
        return base64.b64decode(c.body["data_b64"])
    raise HttpError(400, "Choose a backup.")


@route("POST", "/api/admin/backups/verify", "admin")
def verify_backup(c):
    return c.app.backups.verify(_backup_bytes(c), c.body.get("passphrase") or None)


@route("POST", "/api/admin/restore", "admin")
def restore(c):
    data = _backup_bytes(c)
    if c.body.get("confirm") != "RESTORE":
        raise HttpError(400, "Type RESTORE to confirm.")
    return c.app.restore(data, c.body.get("passphrase") or None, c.user_id, c.session["user_name"])


@route("POST", "/api/admin/backup-passphrase", "admin")
def backup_passphrase(c):
    c.app.backups.set_passphrase(c.body.get("passphrase") or "", c.user_id)
    return {"ok": True}


# ---- label templates ----------------------------------------------------------------------------------------------
@route("GET", "/api/admin/templates", "admin")
def templates(c):
    return c.app.renderer.names()


@route("GET", r"/api/admin/templates/([a-z0-9_-]+)", "admin")
def get_template(c):
    path = c.app.renderer.path(c.params[0])
    if not path.exists():
        raise HttpError(404, "Template not found.")
    return {"name": c.params[0], "text": path.read_text(encoding="utf-8")}


@route("PUT", r"/api/admin/templates/([a-z0-9_-]+)", "admin")
def put_template(c):
    c.need("text")
    c.app.renderer.save(c.params[0], c.body["text"])
    with c.app.db.tx() as conn:
        audit(conn, "template.save", "template", c.params[0], None, c.user_id)
    return {"ok": True}


@route("POST", r"/api/admin/templates/([a-z0-9_-]+)/reset", "admin")
def reset_template(c):
    from .config import DEFAULT_TEMPLATES
    source = DEFAULT_TEMPLATES / f"{c.params[0]}.json"
    if not source.exists():
        raise HttpError(404, "There is no built-in version of that template.")
    c.app.renderer.save(c.params[0], source.read_text(encoding="utf-8"))
    with c.app.db.tx() as conn:
        audit(conn, "template.reset", "template", c.params[0], None, c.user_id)
    return {"ok": True}


@route("POST", r"/api/admin/templates/([a-z0-9_-]+)/preview", "admin")
def preview_template(c):
    import io
    renderer = c.app.renderer
    template = renderer.parse(c.body["text"]) if c.body.get("text") else renderer.load(c.params[0])
    image = renderer.render(template, SAMPLE_DATA.get(c.params[0], SAMPLE_DATA["name"]))
    rotation = int(template.get("rotate", 90))
    image = image.rotate(-rotation, expand=True) if rotation else image
    buffer = io.BytesIO()
    image.convert("1").save(buffer, "PNG")
    return Response(buffer.getvalue(), "image/png")


# =========================================================================================
# HTTP plumbing
# =========================================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "ChurchCheckin/" + __version__
    protocol_version = "HTTP/1.1"
    app = None
    networks = []

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def bearer(self) -> str:
        header = self.headers.get("Authorization") or ""
        return header[7:].strip() if header.lower().startswith("bearer ") else ""

    def _allowed(self, ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return False
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return any(address in network for network in self.networks)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        if not self._allowed(self.client_address[0]):
            self._send_json({"error": "Not allowed from this network."}, 403)
            return
        url = urlparse(self.path)
        path = unquote(url.path)
        if not path.startswith("/api/"):
            if method == "GET":
                self._static(path)
            else:
                self._send_json({"error": "Not found"}, 404)
            return
        try:
            self._api(method, path, parse_qs(url.query))
        except HttpError as exc:
            self._send_json({"error": str(exc), "data": exc.data}, exc.status)
        except KNOWN_ERRORS as exc:
            self._send_json({"error": str(exc), "data": getattr(exc, "data", None)}, getattr(exc, "status", 400))
        except (ImportError_, TemplateError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("bad request %s %s: %r", method, path, exc)
            self._send_json({"error": f"Bad request: {exc}"}, 400)
        except Exception:  # noqa: BLE001
            log.error("error in %s %s\n%s", method, path, traceback.format_exc())
            self._send_json({"error": "Something went wrong on the main computer. Try again."}, 500)

    def _api(self, method, path, query):
        for route_method, regex, level, func in ROUTES:
            if route_method != method:
                continue
            match = regex.match(path)
            if not match:
                continue
            raw = b""
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY:  # read(-1) would read until the client hangs up, past MAX_BODY
                self.close_connection = True  # the unread body must not be taken as the next request
                raise HttpError(413 if length > 0 else 400, "File too large." if length > 0 else "Bad request.")
            if length:
                raw = self.rfile.read(length)
            body = {}
            if raw and "json" in (self.headers.get("Content-Type") or ""):
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise HttpError(400, "Expected a JSON object.")
            c = Ctx(self, self.app, match.groups(), query, body, raw)
            self._authorise(c, level)
            result = func(c)
            if isinstance(result, Response):
                self._send(result.body, result.content_type, result.status, result.headers)
            else:
                self._send_json(result)
            return
        raise HttpError(404, "Not found")

    def _authorise(self, c: Ctx, level: str) -> None:
        app = self.app
        c.station = app.auth.station(self.headers.get("X-Station-Token") or "", c.ip)
        c.session = app.auth.session(self.bearer())
        if level == "public":
            return
        if level == "local":
            if not ipaddress.ip_address(c.ip.split("%")[0]).is_loopback:
                raise HttpError(403, "Setup must be done on the main computer itself.")
            return
        if level == "admin":
            if not c.session or c.session["role"] != "admin":
                raise HttpError(401, "Administrator login required.", {"login": "admin"})
            return
        if not c.station:
            raise HttpError(401, "This device is not set up as a check-in station.", {"login": "station"})
        if level == "station":
            return
        if not c.session:
            raise HttpError(401, "Enter the volunteer PIN.", {"login": "pin"})

    def _static(self, path):
        if path in ("/", "/kiosk"):
            path = "/kiosk.html"
        elif path == "/admin":
            path = "/admin.html"
        elif path == "/ca.crt":  # what an iPad installs once to trust the HTTPS address
            ca = self.app.paths.keys / tls.CA_CERT
            if not ca.is_file():
                self._send(b"Not found", "text/plain", 404)
                return
            self._send(ca.read_bytes(), "application/x-x509-ca-cert", 200,
                       {"Content-Disposition": 'attachment; filename="Church-Check-in-CA.crt"'})
            return
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            self._send(b"Not found", "text/plain", 404)
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".webmanifest":
            ctype = "application/manifest+json"
        elif target.suffix in (".woff2", ".svg", ".webp", ".jpg", ".png"):  # Windows takes these from its registry, which can be wrong
            ctype = {".woff2": "font/woff2", ".svg": "image/svg+xml", ".webp": "image/webp", ".jpg": "image/jpeg",
                     ".png": "image/png"}[target.suffix]
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/manifest+json"):
            ctype += "; charset=utf-8"
        self._send(target.read_bytes(), ctype, 200, {"Cache-Control": "no-cache"})

    def _send_json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8",
                   status, {"Cache-Control": "no-store"})

    def _send(self, body: bytes, content_type: str, status=200, headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class TLSServer(Server):
    """The HTTPS listener. The handshake runs on the connection's own thread, so a slow or plain-HTTP client
    can't hold up the others."""

    HANDSHAKE_SECONDS = 10

    def __init__(self, address, handler, context: ssl.SSLContext):
        self.context = context
        super().__init__(address, handler)

    def finish_request(self, request, client_address):
        request.settimeout(self.HANDSHAKE_SECONDS)
        try:
            secure = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as exc:  # e.g. http:// typed for the https port, or an untrusted iPad
            log.debug("TLS handshake from %s failed: %s", client_address[0], exc)
            return
        try:
            secure.settimeout(None)
            super().finish_request(secure, client_address)
        finally:
            self.shutdown_request(secure)


def make_server(app, host: str, port: int, context: ssl.SSLContext | None = None) -> Server:
    networks = [ipaddress.ip_network(n, strict=False) for n in app.config.get("allowed_networks") or []]
    networks += [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
    handler = type("BoundHandler", (Handler,), {"app": app, "networks": networks})
    if context is not None:
        return TLSServer((host, port), handler, context)
    return Server((host, port), handler)

