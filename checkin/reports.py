"""Operational reports, retention and anonymisation."""

import csv
import datetime as dt
import io
import re

from .db import audit
from .util import mask_phone, now_iso, today_iso


class ReportError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# Excel also runs the full-width signs that Chinese and Japanese keyboards type (＝ ＋ － ＠).
FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _plain(value):
    """Text that Excel would run as a formula (a visitor's name typed at the kiosk, say) stays text."""
    return "'" + value if isinstance(value, str) and value.startswith(FORMULA_START) else value


def to_csv(columns: list[str], rows: list[dict], for_excel: bool = False) -> str:
    """for_excel: reports people open in Excel, so text that looks like a formula stays text."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _plain(v) for k, v in row.items()} if for_excel else row)
    return out.getvalue()


def sessions_for(db, date: str | None = None, session_ids=None) -> list[dict]:
    if session_ids:
        ids = [int(s) for s in session_ids]
        return db.all(f"SELECT * FROM session WHERE id IN ({','.join('?' * len(ids))}) ORDER BY date, start_time",
                      ids)
    return db.all("SELECT * FROM session WHERE date=? ORDER BY start_time, id", (date or today_iso(),))


# ---- operational reports -------------------------------------------------------------------
REPORTS = {
    "present": "Checked in now (not yet collected), by class",
    "totals": "Totals by service and class",
    "attendance": "Full attendance for the day",
    "visitors": "Visitors",
    "medical": "Allergy / medical alerts (checked in today)",
    "print_issues": "Print failures and reprints",
    "audit": "Audit trail",
}


def report(db, kind: str, date: str | None = None, session_id: int | None = None) -> dict:
    date = date or today_iso()
    if not DATE_RE.fullmatch(date):
        raise ReportError("Choose a date as YYYY-MM-DD.")
    sessions = sessions_for(db, date, [session_id] if session_id else None)
    ids = [s["id"] for s in sessions] or [-1]
    marks = ",".join("?" * len(ids))
    if kind == "present":
        cols = ["Class", "Name", "Last name", "Code", "Checked in", "Alert", "Service"]
        rows = db.all(
            "SELECT a.class_name AS 'Class', COALESCE(p.preferred_name, p.first_name) AS 'Name', "
            "p.last_name AS 'Last name', t.security_code AS 'Code', substr(a.checked_in_at, 12, 5) AS 'Checked in', "
            "CASE WHEN p.medical_alert THEN 'YES' ELSE '' END AS 'Alert', s.name AS 'Service' "
            "FROM attendance a JOIN person p ON p.id=a.person_id JOIN checkin_txn t ON t.id=a.txn_id "
            f"JOIN session s ON s.id=a.session_id WHERE a.session_id IN ({marks}) AND a.status='in' "
            "ORDER BY a.class_name, p.first_name", ids)
    elif kind == "totals":
        cols = ["Service", "Class", "Checked in", "Collected", "Still here", "Visitors"]
        rows = db.all(
            "SELECT s.name AS 'Service', COALESCE(a.class_name, 'Adults') AS 'Class', COUNT(*) AS 'Checked in', "
            "SUM(a.status='out') AS 'Collected', SUM(a.status='in') AS 'Still here', "
            "SUM(p.source='visitor') AS 'Visitors' FROM attendance a JOIN session s ON s.id=a.session_id "
            f"JOIN person p ON p.id=a.person_id WHERE a.session_id IN ({marks}) AND a.status<>'undone' "
            "GROUP BY s.id, COALESCE(a.class_name, 'Adults') ORDER BY s.start_time, a.class_name IS NULL, a.class_name", ids)
        if rows:
            total = {c: sum(int(r[c] or 0) for r in rows) for c in cols[2:]}
            rows.append(dict(total, **{"Service": "TOTAL", "Class": ""}))
    elif kind == "attendance":
        cols = ["Date", "Service", "Class", "Member ID", "Visitor ID", "First name", "Last name", "Checked in",
                "Checked out", "Code", "Station", "Status"]
        rows = db.all(
            "SELECT s.date AS 'Date', s.name AS 'Service', a.class_name AS 'Class', p.breeze_id AS 'Member ID', "
            "p.visitor_id AS 'Visitor ID', p.first_name AS 'First name', p.last_name AS 'Last name', "
            "substr(a.checked_in_at, 12, 5) AS 'Checked in', substr(a.checked_out_at, 12, 5) AS 'Checked out', "
            "t.security_code AS 'Code', st.name AS 'Station', a.status AS 'Status' "
            "FROM attendance a JOIN person p ON p.id=a.person_id JOIN session s ON s.id=a.session_id "
            "JOIN checkin_txn t ON t.id=a.txn_id LEFT JOIN station st ON st.id=t.station_id "
            f"WHERE a.session_id IN ({marks}) ORDER BY s.start_time, a.class_name, p.first_name", ids)
    elif kind == "visitors":
        cols = ["Visitor ID", "Name", "Last name", "Class", "Parent/guardian", "Mobile", "Registered",
                "Member ID", "Notes"]
        rows = db.all(
            "SELECT p.visitor_id AS 'Visitor ID', p.first_name AS 'Name', p.last_name AS 'Last name', "
            "a.class_name AS 'Class', "
            "(SELECT g.first_name || ' ' || g.last_name FROM household_member gm JOIN person g ON g.id=gm.person_id "
            " WHERE gm.household_id=m.household_id AND g.is_child=0 LIMIT 1) AS 'Parent/guardian', "
            "(SELECT ph.digits FROM household_member gm JOIN phone ph ON ph.person_id=gm.person_id "
            " WHERE gm.household_id=m.household_id LIMIT 1) AS 'Mobile', "
            "substr(p.created_at, 1, 10) AS 'Registered', p.breeze_id AS 'Member ID', "
            "(SELECT g.notes FROM household_member gm JOIN person g ON g.id=gm.person_id "
            " WHERE gm.household_id=m.household_id AND g.is_child=0 LIMIT 1) AS 'Notes' "
            "FROM attendance a JOIN person p ON p.id=a.person_id JOIN household_member m ON m.person_id=p.id "
            f"WHERE a.session_id IN ({marks}) AND a.status<>'undone' AND p.source='visitor' "
            "GROUP BY p.id ORDER BY p.last_name, p.first_name", ids)
    elif kind == "medical":
        cols = ["Class", "Name", "Last name", "Alert note", "Status"]
        rows = db.all(
            "SELECT a.class_name AS 'Class', COALESCE(p.preferred_name, p.first_name) AS 'Name', "
            "p.last_name AS 'Last name', COALESCE(p.medical_note, '(flagged, no note)') AS 'Alert note', "
            "a.status AS 'Status' FROM attendance a JOIN person p ON p.id=a.person_id "
            f"WHERE a.session_id IN ({marks}) AND a.status<>'undone' AND p.medical_alert=1 "
            "ORDER BY a.class_name, p.first_name", ids)
    elif kind == "print_issues":
        cols = ["Time", "Kind", "Printer", "Status", "Problem", "Attempts", "Code", "Family", "Labels"]
        rows = db.all(
            "SELECT substr(j.created_at, 12, 8) AS 'Time', j.kind AS 'Kind', j.printer AS 'Printer', "
            "j.status AS 'Status', COALESCE(j.error, j.route_note, '') AS 'Problem', j.attempts AS 'Attempts', "
            "t.security_code AS 'Code', h.name AS 'Family', j.label_count AS 'Labels' FROM print_job j "
            "LEFT JOIN checkin_txn t ON t.id=j.txn_id LEFT JOIN household h ON h.id=t.household_id "
            "WHERE substr(j.created_at, 1, 10)=? AND (j.status IN ('failed','unknown') OR j.kind='reprint' "
            "OR j.attempts>1 OR j.route_note IS NOT NULL) ORDER BY j.created_at", (date,))
    elif kind == "audit":
        cols = ["Time", "User", "Station", "Action", "Item", "Details"]
        rows = db.all(
            "SELECT l.ts AS 'Time', u.name AS 'User', st.name AS 'Station', l.action AS 'Action', "
            "COALESCE(l.entity, '') || ' ' || COALESCE(l.entity_id, '') AS 'Item', l.details AS 'Details' "
            "FROM audit_log l LEFT JOIN user u ON u.id=l.user_id LEFT JOIN station st ON st.id=l.station_id "
            "WHERE substr(l.ts, 1, 10)=? ORDER BY l.id DESC", (date,))
    else:
        raise ReportError("Unknown report.", 404)
    return {"kind": kind, "title": REPORTS[kind], "date": date, "columns": cols, "rows": rows,
            "sessions": [{"id": s["id"], "name": s["name"], "status": s["status"]} for s in sessions]}


# ---- people admin, retention ---------------------------------------------------------------------
def anonymise_person(db, person_id: int, user_id=None, reason: str = "") -> None:
    """Remove identifying details but keep anonymous attendance counts."""
    with db.tx() as conn:
        person = conn.execute("SELECT * FROM person WHERE id=?", (person_id,)).fetchone()
        if not person:
            raise ReportError("Person not found.", 404)
        conn.execute(
            "UPDATE person SET first_name='Removed', preferred_name=NULL, last_name='', search_name='', "
            "breeze_id=NULL, legacy_id=NULL, visitor_id=NULL, local_key=NULL, birthdate=NULL, school_year=NULL, sex=NULL, "
            "medical_alert=0, medical_note=NULL, pickup_note=NULL, notes=NULL, chinese_name=NULL, visitor_details=NULL, "
            "active=0, anonymised=1, "
            "import_hash=NULL, updated_at=? WHERE id=?", (now_iso(), person_id))
        conn.execute("DELETE FROM phone WHERE person_id=?", (person_id,))
        conn.execute("UPDATE household_member SET active=0 WHERE person_id=?", (person_id,))
        conn.execute("UPDATE print_job SET payload='[]' WHERE txn_id IN (SELECT txn_id FROM attendance WHERE person_id=?)",
                     (person_id,))
        audit(conn, "person.anonymise", "person", person_id, {"reason": reason}, user_id)
    db.bump()


def run_retention(db, user_id=None) -> dict:
    settings = db.settings()
    today = dt.date.today()
    visitor_cutoff = (today - dt.timedelta(days=30 * int(settings.get("retention_visitor_months") or 24))).isoformat()
    audit_cutoff = (today - dt.timedelta(days=30 * int(settings.get("retention_audit_months") or 36))).isoformat()
    payload_cutoff = (today - dt.timedelta(days=int(settings.get("retention_print_payload_days") or 30))).isoformat()
    # One transaction for finding the old visitors and anonymising them, so a check-in or a sync landing in
    # between can't have someone who has just come back, or just been linked, wiped.
    with db.tx() as conn:
        stale = conn.execute(
            "SELECT p.id FROM person p WHERE p.source='visitor' AND p.anonymised=0 AND p.breeze_id IS NULL "
            "AND COALESCE((SELECT MAX(a.checked_in_at) FROM attendance a WHERE a.person_id=p.id), p.created_at) < ? "
            "AND NOT EXISTS (SELECT 1 FROM household_member m JOIN household_member k ON k.household_id=m.household_id "
            "  JOIN attendance a2 ON a2.person_id=k.person_id WHERE m.person_id=p.id AND a2.checked_in_at >= ?)",
            (visitor_cutoff, visitor_cutoff)).fetchall()
        for row in stale:
            anonymise_person(db, row["id"], user_id, "retention: visitor inactive")
    with db.tx() as conn:
        audits = conn.execute("DELETE FROM audit_log WHERE ts < ?", (audit_cutoff,)).rowcount
        payloads = conn.execute("UPDATE print_job SET payload='[]' WHERE created_at < ? AND payload<>'[]'",
                                (payload_cutoff,)).rowcount
        sessions = conn.execute("DELETE FROM auth_session WHERE expires_at < ?", (now_iso(),)).rowcount
        result = {"visitors_anonymised": len(stale), "audit_entries_deleted": audits,
                  "print_payloads_cleared": payloads, "expired_logins_removed": sessions}
        audit(conn, "retention.run", None, None, result, user_id)
    return result


def people_search(db, query: str, limit: int = 50) -> list[dict]:
    """Admin → People: by name (Chinese name too), Member ID (typed any way), old ID, VIS- id, or part of a phone
    number (4 digits or more)."""
    from . import member_id
    from .util import name_pattern, normalize_name
    tokens = normalize_name(query).split()
    if not tokens:
        rows = db.all("SELECT * FROM person WHERE anonymised=0 ORDER BY updated_at DESC LIMIT ?", (limit,))
    else:
        clauses = " AND ".join("(' ' || search_name) LIKE ?" for _ in tokens)
        digits = re.sub(r"\D", "", query) if re.fullmatch(r"[\d\s()+-]+", query.strip()) else ""
        if len(digits) > 8:   # +65 9123 4567 finds 91234567
            digits = digits[-8:]
        by_phone = "id IN (SELECT person_id FROM phone WHERE digits LIKE ?)" if len(digits) >= 4 else "0"
        rows = db.all(f"SELECT * FROM person WHERE anonymised=0 AND ({clauses} OR breeze_id=? OR legacy_id=? OR visitor_id=? "
                      f"OR {by_phone}) ORDER BY last_name, first_name LIMIT ?",
                      (*[name_pattern(t) for t in tokens], member_id.parse(query, bare=True) or query.strip(), query.strip(),
                       query.strip().upper(),
                       *([f"%{digits}%"] if len(digits) >= 4 else []), limit))
    out = []
    for row in rows:
        phones = db.all("SELECT digits FROM phone WHERE person_id=? ORDER BY id", (row["id"],))
        household = db.one("SELECT h.id, h.name FROM household_member m JOIN household h ON h.id=m.household_id "
                           "WHERE m.person_id=? AND m.active=1 LIMIT 1", (row["id"],))
        row["phones"] = [mask_phone(p["digits"]) for p in phones]
        row["mobiles"] = [p["digits"] for p in phones]   # admin only, like the person's own page
        row["household"] = household
        out.append(row)
    return out
