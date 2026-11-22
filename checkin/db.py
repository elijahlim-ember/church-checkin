"""SQLite storage: schema, migrations, a thread-safe connection wrapper and settings."""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

from .util import now_iso

SCHEMA_V1 = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE app_setting(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT
);

CREATE TABLE classroom(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  room TEXT,
  min_age_months INTEGER,
  max_age_months INTEGER,
  school_years TEXT,
  breeze_event_name TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE household(
  id INTEGER PRIMARY KEY,
  family_key TEXT UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'breeze',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE person(
  id INTEGER PRIMARY KEY,
  breeze_id TEXT UNIQUE,
  visitor_id TEXT UNIQUE,
  local_key TEXT UNIQUE,
  first_name TEXT NOT NULL,
  preferred_name TEXT,
  last_name TEXT NOT NULL DEFAULT '',
  search_name TEXT NOT NULL DEFAULT '',
  is_child INTEGER NOT NULL DEFAULT 0,
  birthdate TEXT,
  school_year TEXT,
  sex TEXT,
  classroom_id INTEGER REFERENCES classroom(id),
  class_source TEXT,
  medical_alert INTEGER NOT NULL DEFAULT 0,
  medical_note TEXT,
  pickup_note TEXT,
  notes TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT 'breeze',
  import_hash TEXT,
  last_import_id INTEGER,
  anonymised INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX ix_person_search ON person(search_name);

CREATE TABLE household_member(
  household_id INTEGER NOT NULL REFERENCES household(id),
  person_id INTEGER NOT NULL REFERENCES person(id),
  role TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(household_id, person_id)
);
CREATE INDEX ix_member_person ON household_member(person_id);

CREATE TABLE phone(
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES person(id),
  digits TEXT NOT NULL,
  last4 TEXT NOT NULL,
  kind TEXT,
  UNIQUE(person_id, digits)
);
CREATE INDEX ix_phone_last4 ON phone(last4);
CREATE INDEX ix_phone_digits ON phone(digits);

CREATE TABLE service(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  weekday INTEGER,
  start_time TEXT,
  breeze_event_name TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE session(
  id INTEGER PRIMARY KEY,
  service_id INTEGER REFERENCES service(id),
  date TEXT NOT NULL,
  name TEXT NOT NULL,
  start_time TEXT,
  breeze_event_name TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  opened_at TEXT,
  closed_at TEXT,
  UNIQUE(service_id, date)
);

CREATE TABLE user(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  role TEXT NOT NULL,
  secret_hash TEXT NOT NULL,
  pin_lookup TEXT UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  created_at TEXT
);

CREATE TABLE printer(
  name TEXT PRIMARY KEY COLLATE NOCASE,
  backend TEXT NOT NULL,
  address TEXT,
  media TEXT NOT NULL DEFAULT '62',
  enabled INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT
);

CREATE TABLE station(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  printer TEXT,
  fallback_printer TEXT,
  token_hash TEXT UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  last_seen TEXT,
  last_ip TEXT,
  created_at TEXT
);

CREATE TABLE auth_session(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES user(id),
  station_id INTEGER REFERENCES station(id),
  role TEXT NOT NULL,
  created_at TEXT,
  expires_at TEXT,
  last_seen TEXT
);

CREATE TABLE checkin_txn(
  id TEXT PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES session(id),
  household_id INTEGER REFERENCES household(id),
  station_id INTEGER REFERENCES station(id),
  user_id INTEGER REFERENCES user(id),
  security_code TEXT NOT NULL,
  code_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'committed',
  created_at TEXT NOT NULL,
  undone_at TEXT,
  undone_by INTEGER,
  undo_reason TEXT,
  reprint_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_txn_session ON checkin_txn(session_id, created_at);
CREATE INDEX ix_txn_code ON checkin_txn(code_date, security_code);

CREATE TABLE attendance(
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES session(id),
  person_id INTEGER NOT NULL REFERENCES person(id),
  txn_id TEXT NOT NULL REFERENCES checkin_txn(id),
  classroom_id INTEGER REFERENCES classroom(id),
  class_name TEXT,
  status TEXT NOT NULL DEFAULT 'in',
  checked_in_at TEXT NOT NULL,
  checked_out_at TEXT,
  checkout_user INTEGER,
  checkout_station INTEGER,
  undone_at TEXT
);
CREATE UNIQUE INDEX ux_attendance_once ON attendance(session_id, person_id) WHERE status <> 'undone';
CREATE INDEX ix_attendance_txn ON attendance(txn_id);

CREATE TABLE print_job(
  id TEXT PRIMARY KEY,
  txn_id TEXT REFERENCES checkin_txn(id),
  kind TEXT NOT NULL,
  station_id INTEGER,
  requested_printer TEXT,
  printer TEXT,
  payload TEXT NOT NULL,
  label_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  error TEXT,
  route_note TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  print_ms INTEGER
);
CREATE INDEX ix_job_status ON print_job(status, created_at);
CREATE INDEX ix_job_txn ON print_job(txn_id);

CREATE TABLE audit_log(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  user_id INTEGER,
  station_id INTEGER,
  action TEXT NOT NULL,
  entity TEXT,
  entity_id TEXT,
  details TEXT
);
CREATE INDEX ix_audit_ts ON audit_log(ts);

CREATE TABLE import_batch(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  user_id INTEGER,
  filename TEXT,
  sha256 TEXT,
  mapping TEXT,
  totals TEXT,
  warnings TEXT,
  status TEXT NOT NULL,
  committed_at TEXT
);

CREATE TABLE export_batch(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  user_id INTEGER,
  kind TEXT NOT NULL,
  params TEXT,
  filename TEXT,
  row_count INTEGER
);
"""

# Version 2: extra columns kept from the roster (JSON, e.g. {"x_primary_congregation": "Sample Group"}).
SCHEMA_V2 = """
ALTER TABLE person ADD COLUMN extra TEXT;
"""

# Version 3: sync remembers which check-ins were sent to which event (first Breeze, now the church database).
SCHEMA_V3 = """
ALTER TABLE attendance ADD COLUMN breeze_sent_at TEXT;
ALTER TABLE attendance ADD COLUMN breeze_instance TEXT;
"""

# Version 4: Member IDs. When the church database first sends them, sync moves each person and household to
# their Member ID and keeps the old ID here (never shown), so a rollback of the church database can move back.
SCHEMA_V4 = """
ALTER TABLE person ADD COLUMN legacy_id TEXT;
ALTER TABLE household ADD COLUMN legacy_key TEXT;
"""

# Version 5: the visitor form's Chinese name (kept, and read from the church database for members), and its
# other answers as JSON (email, age group, language, what brings them, contact). Those are wiped once the church
# database confirms it has them.
SCHEMA_V5 = """
ALTER TABLE person ADD COLUMN chinese_name TEXT;
ALTER TABLE person ADD COLUMN visitor_details TEXT;
"""

# Version 6: one-time devices. A phone or spare iPad joined with a volunteer's one-time code is a station that
# stops working at expires_at (the end of that day). Permanent stations leave it empty.
SCHEMA_V6 = """
ALTER TABLE station ADD COLUMN expires_at TEXT;
"""

# Version 7: newcomers are added to the church database once, and their family links aren't lost.
# person.add_code is the one-off code sent with a newcomer's add, the same on every try, so a retry after a lost
# reply gets the same person back (compatible service 1.9.0 on). Empty once the add is confirmed; 'erased' when the church
# database says it erased that person since, so check-in never adds them again.
# family_link keeps each newcomer linked or added by a sync until their family is set up in the church database
# (families/add or families/create), so a sync that stops halfway sends it next time. A household's links stay
# while anyone in it is still waiting to be added, so one added at a later sync joins the family too; joined marks
# those already in it.
SCHEMA_V7 = """
ALTER TABLE person ADD COLUMN add_code TEXT;
CREATE TABLE family_link(
  person_id INTEGER PRIMARY KEY REFERENCES person(id),
  household_id INTEGER NOT NULL,
  breeze_id TEXT NOT NULL,
  added INTEGER NOT NULL,
  joined INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""

# Schema v8: each service remembers the church database's own number for the service it sends attendance to
# (picked in Admin, or learned the first time the names match), so a later rename or typo on either side can't
# stop a day's attendance.
SCHEMA_V8 = """
ALTER TABLE service ADD COLUMN chms_service_id TEXT;
"""

SCHEMA_V9 = """
CREATE TABLE IF NOT EXISTS day_report(
  date TEXT PRIMARY KEY,
  built_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  late INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT,
  delivery_error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try TEXT
);
"""

# Each new version of the app appends its schema changes here. They run once, in order, at start-up,
# after an automatic encrypted backup, so existing data is upgraded in place.
MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6, SCHEMA_V7, SCHEMA_V8, SCHEMA_V9]
SCHEMA_VERSION = len(MIGRATIONS)


def pending_migrations(conn: sqlite3.Connection) -> int:
    return SCHEMA_VERSION - current_version(conn)


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> None:
    version = current_version(conn)
    for index in range(version, SCHEMA_VERSION):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript() would commit implicitly, so run statements one by one.
            for statement in _split_sql(MIGRATIONS[index]):
                conn.execute(statement)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(index + 1),))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _split_sql(script: str):
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                yield buffer.strip()
            buffer = ""
    if buffer.strip():
        yield buffer.strip()


class Database:
    """One shared connection, serialised by a re-entrant lock.

    Every request is short (a few milliseconds), so serialising is simpler and
    safer than juggling per-thread connections, and it makes each check-in a
    single atomic transaction.
    """

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.change_seq = 0
        self._depth = 0
        self.conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def migrate(self) -> None:
        with self.lock:
            migrate(self.conn)

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()

    def reopen(self) -> None:
        with self.lock:
            self.conn = self._connect()
            migrate(self.conn)

    @contextmanager
    def tx(self):
        with self.lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.conn
                finally:
                    self._depth -= 1
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    def bump(self) -> None:
        """Tell polling screens that attendance changed so they refresh."""
        self.change_seq += 1

    def all(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params=()):
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str, default=None):
        if key in ("checkin_mode", "checkin_everyone"):
            return self.settings()[key]
        row = self.one("SELECT value FROM app_setting WHERE key=?", (key,))
        if row is None:
            return SETTING_DEFAULTS.get(key, default) if default is None else default
        return json.loads(row["value"])

    def set_setting(self, key: str, value) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO app_setting(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), now_iso()),
            )

    def settings(self) -> dict:
        stored = {row["key"]: json.loads(row["value"]) for row in self.all("SELECT key, value FROM app_setting")}
        values = dict(SETTING_DEFAULTS, **stored)
        if values.get("checkin_mode") not in CHECKIN_MODES or "checkin_mode" not in stored:
            # Before 1.6 this was the on/off "Whole family checks in"; off meant children only.
            values["checkin_mode"] = "children" if stored.get("checkin_everyone") is False else "individual"
        values["checkin_everyone"] = values["checkin_mode"] != "children"
        return values


def audit(conn, action: str, entity: str = None, entity_id=None, details=None, user_id=None, station_id=None):
    conn.execute(
        "INSERT INTO audit_log(ts, user_id, station_id, action, entity, entity_id, details) VALUES(?,?,?,?,?,?,?)",
        (now_iso(), user_id, station_id, action, entity, None if entity_id is None else str(entity_id),
         json.dumps(details, ensure_ascii=False) if details is not None else None),
    )


# individual: everyone can check in and only the person who searched starts ticked.
# family: everyone can check in and the whole family starts ticked. children: children only.
CHECKIN_MODES = ("individual", "family", "children")

SETTING_DEFAULTS = {
    "church_name": os.environ.get("ORG_NAME", "Your Church"),
    "country_code": "65",
    "child_max_age": 12,
    "checkin_mode": "individual",
    "pickup_codes": False,
    "medical_alerts": False,
    "chinese_button": False,   # the kiosk's 中文 button: there just in case, off until the church wants it
    "breeze_date_format": "auto",
    "preselect_children": True,
    "guardian_labels_per_checkin": 1,
    "child_labels_per_child": 1,
    "print_alert_note": True,
    "success_return_seconds": 3,
    "idle_return_seconds": 45,
    "volunteer_session_hours": 12,
    "failover": "auto",
    "breeze_event_mode": "session",
    "breeze_include_time": False,
    "breeze_anonymous_visitors": False,
    "backup_keep": 20,
    "retention_visitor_months": 24,
    "retention_audit_months": 36,
    "retention_print_payload_days": 30,
    "print_confirm": "per_label",
    "print_compression": False,
    "print_high_quality": False,
    "allow_volunteer_visitors": True,
}
