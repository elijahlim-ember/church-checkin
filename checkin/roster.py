"""Roster import: Excel/CSV reading, column mapping, validation preview and commit.

Sync feeds the church database's people through here as rows shaped like Breeze's old Excel export (the
the local roster can be imported from a CSV or spreadsheet). Two shapes are supported,
plus an optional second file:

* People export (one row per person) with a Family ID and Family Role column.
  Parents carry the mobile numbers; children are matched to them by Family ID.
* Child roster (one row per child) with parent/guardian name and mobile columns
  on the child's own row.
* Optional relationships/households file: Breeze ID -> Family ID (+ role),
  joined onto the people file when it has no family column.

People are upserted by Breeze ID. Nobody is deleted: people missing from a new
file are only deactivated when the administrator explicitly ticks that option.

Only the mapped columns are read. Everything else in the file (addresses, baptism
records, marital status...) is ignored and never stored. "Leave out" rules hide
people such as the deceased, and chosen extra columns can be kept for labels and reports.
"""

import collections
import csv
import datetime as dt
import hashlib
import io
import json
import re

from .db import audit
from .util import (age_on, clean_text, detect_day_first, normalize_name, normalize_phone, normalize_school_year,
                   now_iso, parse_date, phone_is_plausible, search_name, today_iso, truthy)

FIELDS = [
    ("breeze_id", "Breeze ID", ["breeze id", "breezeid", "person id", "id", "member id", "breeze person id"]),
    ("first_name", "First name", ["first name", "firstname", "first", "given name"]),
    ("preferred_name", "Preferred name / nickname", ["nickname", "nick name", "preferred name", "goes by"]),
    ("last_name", "Last name", ["last name", "lastname", "last", "surname", "family name"]),
    # English aliases only: _norm_header drops every non-Latin letter, so a Chinese alias would match any
    # all-Chinese header.
    ("chinese_name", "Chinese name", ["chinese name", "name in chinese", "chinese full name"]),
    ("family_id", "Household / family ID", ["family id", "familyid", "household id", "family", "household"]),
    ("family_role", "Family role", ["family role", "role", "family position", "relationship"]),
    ("guardian_breeze_id", "Parent/guardian Breeze ID", ["parent breeze id", "guardian breeze id", "parent id",
                                                         "guardian id"]),
    ("guardian_name", "Parent/guardian name", ["parent name", "guardian name", "parent guardian name",
                                               "parent", "guardian", "parent guardian"]),
    ("guardian2_name", "Second parent/guardian name", ["parent 2 name", "second parent", "guardian 2 name",
                                                       "parent 2", "guardian 2"]),
    ("mobile", "Mobile number", ["mobile", "mobile phone", "mobile number", "cell", "cell phone", "handphone",
                                 "hp", "parent mobile", "guardian mobile", "phone", "contact number"]),
    ("mobile2", "Second mobile number", ["parent 2 mobile", "guardian 2 mobile", "mobile 2", "second mobile",
                                         "home phone", "home"]),
    ("birthdate", "Date of birth", ["birthdate", "birth date", "birthday", "date of birth", "dob"]),
    ("school_year", "School year / grade", ["grade", "school year", "school level", "level", "year"]),
    ("classroom", "Class / room", ["class", "classroom", "kids class", "sunday school class", "room", "group"]),
    ("sex", "Sex", ["gender", "sex"]),
    ("medical_alert", "Allergy / medical alert flag", ["medical alert", "allergy alert", "alert", "has allergy"]),
    ("medical_note", "Allergy / medical note", ["allergies", "allergy", "medical note", "medical notes",
                                                "medical", "allergy note", "allergies medical"]),
    ("pickup_note", "Authorised pickup note", ["authorised pickup", "authorized pickup", "pickup", "pick up",
                                               "pickup note"]),
    ("status", "Active / status", ["status", "membership status", "active", "archived"]),
]
FIELD_KEYS = [f[0] for f in FIELDS]
REL_FIELDS = [
    ("breeze_id", "Breeze ID", ["breeze id", "breezeid", "person id", "id"]),
    ("family_id", "Household / family ID", ["family id", "familyid", "household id", "family", "household"]),
    ("family_role", "Family role", ["family role", "role", "relationship"]),
]

INACTIVE_WORDS = {"inactive", "archived", "deceased", "moved", "moved away", "left", "former", "no", "false",
                  "0", "n", "no longer attends"}
# Membership/status values that usually mean "don't show this person at check-in" (offered as leave-out
# rules). "Transfer out" on its own is deliberately absent: it is often a past event, not a current status.
LEAVE_OUT_VALUES = {"dceas", "deceased", "removed", "tf out", "transferred out", "no longer attends",
                    "excommunicated"}
EMPTY_NOTES = {"", "-", "na", "n a", "nil", "none", "no", "nkda", "no known allergies", "nka"}
GUARDIAN_ROLES = ("head", "spouse", "adult", "parent", "guardian", "father", "mother", "husband", "wife")


class ImportError_(Exception):
    pass


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def decode_csv(raw: bytes) -> tuple[str, list[str]]:
    try:
        return raw.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), [
            "The file was not UTF-8, so it was read as Windows-1252. In Excel use 'Save as > CSV UTF-8' next time."]


def clean_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return clean_text(value)


def _unique_headers(raw_headers) -> list[str]:
    headers, seen = [], {}
    for header in raw_headers:
        header = clean_cell(header)
        if header and header in seen:
            seen[header] += 1
            header = f"{header} ({seen[header]})"
        elif header:
            seen[header] = 1
        headers.append(header)
    return headers


def read_xlsx(raw: bytes) -> tuple[list[str], list[dict]]:
    """Read the worksheet with the most rows from an Excel file (Breeze's normal export)."""
    try:
        import openpyxl
    except ImportError as exc:
        raise ImportError_("Reading Excel files needs the 'openpyxl' package (pip install openpyxl), "
                           "or save the file as CSV UTF-8.") from exc
    try:
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - corrupt or password-protected workbook
        raise ImportError_(f"Could not open the Excel file: {exc}") from exc
    best = []
    for sheet in book.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if len(rows) > len(best):
            best = rows
    book.close()
    header_index = next((i for i, r in enumerate(best[:20]) if sum(1 for v in r if clean_cell(v)) >= 2), None)
    if header_index is None:
        raise ImportError_("The spreadsheet has no heading row.")
    headers = _unique_headers(best[header_index])
    out = []
    for values in best[header_index + 1:]:
        cells = [clean_cell(v) for v in values]
        if not any(cells):
            continue
        out.append({headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers)) if headers[i]})
    return [h for h in headers if h], out


def load_table(raw: bytes, filename: str = "") -> tuple[list[str], list[dict], list[str]]:
    """Read an uploaded roster file (.xlsx or .csv). Returns headers, rows, notes."""
    if filename.lower().endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
        headers, rows = read_xlsx(raw)
        return headers, rows, []
    if filename.lower().endswith(".xls"):
        raise ImportError_("Old .xls files aren't supported. In Excel use Save As → .xlsx or CSV UTF-8.")
    text, notes = decode_csv(raw)
    headers, rows = read_csv(text)
    return headers, rows, notes


def read_csv(text: str) -> tuple[list[str], list[dict]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        raise ImportError_("The file is empty.")
    headers = _unique_headers(rows[0])
    if not any(headers):
        raise ImportError_("The first row must contain column headings.")
    out = []
    for values in rows[1:]:
        cells = [clean_text(v) for v in values]
        if not any(cells):
            continue
        out.append({headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers)) if headers[i]})
    return [h for h in headers if h], out


def suggest_mapping(headers: list[str], fields=FIELDS) -> dict:
    """Guess which column feeds which field: exact alias match first, then partial."""
    mapping, used = {}, set()
    normalised = {h: _norm_header(h) for h in headers if h}
    for key, _, aliases in fields:
        alias_norms = [_norm_header(a) for a in aliases]
        for alias in alias_norms:
            match = next((h for h, n in normalised.items() if n == alias and h not in used), None)
            if match:
                mapping[key] = match
                used.add(match)
                break
    for key, _, aliases in fields:
        if key in mapping:
            continue
        # Partial matches only for distinctive aliases, and never on long description-style headings.
        alias_norms = [_norm_header(a) for a in aliases if len(_norm_header(a)) >= 6]
        match = next((h for h, n in normalised.items()
                      if h not in used and len(h) <= 40 and any(a in n for a in alias_norms)), None)
        if match:
            mapping[key] = match
            used.add(match)
    return mapping


def extra_key(header: str) -> str:
    """Template/report field name for an extra column, e.g. 'Primary Congregation' -> x_primary_congregation."""
    return "x_" + (re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_") or "field")


def suggest_rules(headers: list[str], rows: list[dict]) -> list[dict]:
    """Propose leave-out rules: a 'date of decease' style column, and status values like DCEAS or REMOVED."""
    rules = []
    for header in headers:
        if re.search(r"deceas|death|died", header, re.I) and not re.search(r"month|year|since", header, re.I):
            if any(row.get(header) for row in rows):
                rules.append({"column": header, "op": "any", "values": []})
    for header in headers:
        values = {row.get(header) for row in rows if row.get(header)}
        if not values or len(values) > 40:
            continue
        hits = sorted(v for v in values if normalize_name(v) in LEAVE_OUT_VALUES)
        if hits:
            rules.append({"column": header, "op": "in", "values": hits})
    return rules


def column_value_options(headers: list[str], rows: list[dict], limit: int = 40) -> dict:
    """Distinct values of short category-like columns, offered when writing leave-out rules."""
    options = {}
    for header in headers:
        values = collections.Counter(row.get(header) for row in rows if row.get(header))
        if values and len(values) <= limit and not re.search(r"name|phone|mobile|email|address|birth", header, re.I):
            options[header] = [v for v, _ in values.most_common()]
    return options


def default_choices(db, headers: list[str], rows: list[dict]) -> tuple[dict, list, list]:
    """Column mapping, leave-out rules and extra columns for a new file: last week's choices where the
    columns still exist, otherwise sensible guesses."""
    saved = db.get_setting("import_mapping")
    if saved is not None:
        mapping = {k: v for k, v in saved.items() if v in headers}
    else:
        # First import: only suggest the fields check-in actually uses, so nothing more is stored.
        settings = db.settings()
        kids = not settings.get("checkin_everyone") or bool(db.scalar("SELECT COUNT(*) FROM classroom"))
        unused = {"sex"}
        if not kids:
            unused |= {"birthdate", "school_year", "classroom"}
        if not settings.get("medical_alerts"):
            unused |= {"medical_alert", "medical_note"}
        if not settings.get("pickup_codes"):
            unused |= {"pickup_note"}
        mapping = {k: v for k, v in suggest_mapping(headers).items() if k not in unused}
    saved_rules = db.get_setting("import_rules")
    if saved_rules is None:
        rules = suggest_rules(headers, rows)
    else:
        rules = [r for r in saved_rules if r.get("column") in headers]
    extra = [h for h in (db.get_setting("import_extra") or []) if h in headers]
    return mapping, rules, extra


def remember_choices(db, mapping: dict, rules: list, extra: list, analysis) -> None:
    db.set_setting("import_mapping", mapping)
    db.set_setting("import_rules", rules)
    db.set_setting("import_extra", extra)
    if analysis.day_first is not None:
        db.set_setting("roster_day_first", analysis.day_first)


def _rule_matches(rule: dict, row: dict) -> bool:
    value = (row.get(rule.get("column")) or "").strip()
    if rule.get("op") == "any":
        return bool(value)
    wanted = {normalize_name(v) for v in rule.get("values") or []}
    return bool(value) and normalize_name(value) in wanted


def _is_inactive(value: str, header: str) -> bool:
    if not value:
        return False
    if "archiv" in _norm_header(header):
        return truthy(value)
    return normalize_name(value) in INACTIVE_WORDS


def _note(value: str) -> str:
    text = (value or "").strip()
    return "" if normalize_name(text) in EMPTY_NOTES else text


def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _hash(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


class Analysis:
    """Result of reading a file against the current database (nothing written yet)."""

    def __init__(self):
        self.people = {}        # key -> person record
        self.households = {}    # family_key -> {"name":..., "members": {person_key: role}}
        self.classes_new = set()
        self.warnings = {
            "file": [], "duplicate_ids": [], "missing_ids": [], "missing_first_name": [], "missing_last_name": [],
            "invalid_phones": [], "children_no_household": [], "children_no_class": [], "family_names_as_ids": [],
        }
        self.totals = {}
        self.deactivate = []
        self.left_out = collections.Counter()
        self.day_first = None


def analyse(db, table, mapping: dict, rel_table=None, rel_mapping: dict = None, rules: list | None = None,
            extra: list | None = None) -> Analysis:
    """table / rel_table: (headers, rows) from load_table, or CSV text.

    rules: leave-out rules [{"column", "op": "any"|"in", "values"}]; matching people are imported as inactive.
    extra: column names to keep for labels/reports (stored as x_... fields).
    """
    settings = db.settings()
    country = settings.get("country_code") or "65"
    max_age = int(settings.get("child_max_age") or 12)
    result = Analysis()
    headers, rows = read_csv(table) if isinstance(table, str) else table[:2]
    rules = [r for r in (rules or []) if r.get("column") in headers]
    extra = [h for h in (extra or []) if h in headers]
    mapping = {k: v for k, v in (mapping or {}).items() if v and v in headers and k in FIELD_KEYS}
    if "first_name" not in mapping:
        raise ImportError_("Map the 'First name' column before continuing.")
    if "breeze_id" not in mapping:
        raise ImportError_("Map the 'Breeze ID' column before continuing.")

    def get(row, key):
        col = mapping.get(key)
        return (row.get(col) or "").strip() if col else ""

    # n/n/yyyy dates are ambiguous: work out the order from the file itself (any date column), which
    # also tells the Breeze export which order this Breeze account uses.
    date_columns = [h for h in headers if re.search(r"date|birth", h, re.I)]
    file_day_first = detect_day_first(row.get(h) for row in rows for h in date_columns)
    day_first = file_day_first
    if "birthdate" in mapping:
        day_first = detect_day_first(row.get(mapping["birthdate"]) for row in rows)
        if day_first is None:
            day_first = file_day_first if file_day_first is not None else \
                bool(settings.get("roster_day_first", country == "65"))
    result.day_first = day_first

    relations = {}
    if rel_table:
        rel_headers, rel_rows = read_csv(rel_table) if isinstance(rel_table, str) else rel_table[:2]
        rel_map = {k: v for k, v in (rel_mapping or suggest_mapping(rel_headers, REL_FIELDS)).items()
                   if v in rel_headers}
        if "breeze_id" not in rel_map or "family_id" not in rel_map:
            raise ImportError_("The relationships file needs a Breeze ID column and a Family ID column.")
        for rel in rel_rows:
            bid = (rel.get(rel_map["breeze_id"]) or "").strip()
            if bid:
                relations[bid] = ((rel.get(rel_map["family_id"]) or "").strip(),
                                  (rel.get(rel_map.get("family_role", ""), "") or "").strip())

    child_row_format = "guardian_name" in mapping or "guardian_breeze_id" in mapping
    existing_classes = {normalize_name(r["name"]): r["name"] for r in db.all("SELECT name FROM classroom")}
    family_values = []
    seen_ids = {}

    for index, row in enumerate(rows, start=2):
        bid = re.sub(r"\.0$", "", get(row, "breeze_id"))
        first = get(row, "first_name")
        last = get(row, "last_name")
        label = f"row {index}: {first} {last}".strip()
        if not first:
            result.warnings["missing_first_name"].append(f"row {index}")
            continue
        if not bid:
            result.warnings["missing_ids"].append(label)
            continue
        if bid in seen_ids:
            result.warnings["duplicate_ids"].append(f"{bid} ({label}; first seen on row {seen_ids[bid]})")
            continue
        seen_ids[bid] = index
        if not last:
            result.warnings["missing_last_name"].append(label)
        left_out = next((r for r in rules if _rule_matches(r, row)), None)
        if left_out:
            result.left_out[f"{left_out['column']}: {row.get(left_out['column'])}"
                            if left_out.get("op") == "in" else f"{left_out['column']} filled in"] += 1

        family_id = get(row, "family_id")
        role_text = normalize_name(get(row, "family_role"))
        if bid in relations:
            family_id = family_id or relations[bid][0]
            role_text = role_text or normalize_name(relations[bid][1])
        birthdate = parse_date(get(row, "birthdate"), day_first)
        age = age_on(birthdate, today_iso())
        if "child" in role_text:
            is_child = True
        elif any(word in role_text for word in GUARDIAN_ROLES):
            is_child = False
        elif child_row_format:
            is_child = True
        else:
            is_child = bool(age and age[0] < 18)

        phones = []
        for key in ("mobile", "mobile2"):
            raw = get(row, key)
            if not raw:
                continue
            for part in re.split(r"[/,;]| or ", raw):
                digits = normalize_phone(part, country)
                if not digits:
                    continue
                if not phone_is_plausible(digits, country):
                    result.warnings["invalid_phones"].append(f"{label}: '{part.strip()}'")
                    continue
                if digits not in phones:
                    phones.append(digits)

        guardian_bid = get(row, "guardian_breeze_id")
        if family_id:
            family_key = "F:" + family_id
            family_values.append(family_id)
        elif guardian_bid:
            family_key = "G:" + guardian_bid
        elif child_row_format and phones:
            family_key = "P:" + phones[0]
        elif not is_child and phones:
            family_key = "P:" + phones[0]
        else:
            family_key = "S:" + bid
            if is_child:
                result.warnings["children_no_household"].append(label)

        class_name = get(row, "classroom")
        if class_name:
            norm = normalize_name(class_name)
            if norm in existing_classes:
                class_name = existing_classes[norm]
            else:
                result.classes_new.add(class_name)
                existing_classes[norm] = class_name

        note = _note(get(row, "medical_note"))
        alert = truthy(get(row, "medical_alert")) if "medical_alert" in mapping else bool(note)
        status_col = mapping.get("status", "")
        record = {
            "breeze_id": bid,
            "first_name": first,
            "last_name": last,
            "is_child": 1 if is_child else 0,
            "active": 0 if (left_out or _is_inactive(get(row, "status"), status_col)) else 1,
            "family_key": family_key,
            "role": "child" if is_child else "guardian",
            "phones": sorted(phones),
        }
        optional = {
            "preferred_name": get(row, "preferred_name") or None,
            "chinese_name": " ".join(get(row, "chinese_name").split())[:40] or None,
            "birthdate": birthdate,
            "school_year": normalize_school_year(get(row, "school_year")) or None,
            "sex": get(row, "sex") or None,
            "classroom": class_name or None,
            "medical_alert": 1 if (alert or note) else 0,
            "medical_note": note or None,
            "pickup_note": get(row, "pickup_note") or None,
        }
        field_for = {"medical_alert": ("medical_alert", "medical_note")}
        for key, value in optional.items():
            sources = field_for.get(key, (key,))
            if any(src in mapping for src in sources):
                record[key] = value
        if extra:
            record["extra"] = {extra_key(h): row.get(h, "") for h in extra if row.get(h, "")}
        person_key = "B:" + bid
        result.people[person_key] = record
        household = result.households.setdefault(family_key, {"name": "", "members": {}})
        household["members"][person_key] = record["role"]
        if not household["name"] and not is_child and last:
            household["name"] = last

        if is_child and child_row_format:
            # Guardians named on the child's row become guardian records of the same household.
            names = [get(row, "guardian_name"), get(row, "guardian2_name")]
            for position, gname in enumerate(names):
                if not gname:
                    continue
                gfirst, glast = _split_name(gname)
                if guardian_bid and position == 0:
                    gkey = "B:" + guardian_bid
                else:
                    gkey = "L:" + family_key + ":" + normalize_name(gname)
                guardian = result.people.get(gkey)
                if guardian is None:
                    guardian = {
                        "breeze_id": guardian_bid if gkey.startswith("B:") else None,
                        "local_key": gkey if gkey.startswith("L:") else None,
                        "first_name": gfirst, "last_name": glast, "is_child": 0, "active": 1,
                        "family_key": family_key, "role": "guardian", "phones": [],
                    }
                    result.people[gkey] = guardian
                gphones = phones[:1] if position == 0 else phones[1:2]
                for digits in gphones:
                    if digits not in guardian["phones"]:
                        guardian["phones"] = sorted(guardian["phones"] + [digits])
                household["members"].setdefault(gkey, "guardian")
                if not household["name"]:
                    household["name"] = glast or last
            if names[0] or names[1]:
                record["phones"] = []
        if not household["name"]:
            household["name"] = last

    # Families without a shared surname are named after their members, e.g. "Alex Sample & Jordan Example".
    for household in result.households.values():
        if household["name"]:
            continue
        members = [result.people[k] for k in household["members"] if k in result.people]
        members.sort(key=lambda p: (p["is_child"], not p["active"]))
        names = [p.get("preferred_name") or p["first_name"] for p in members if p["active"]] or \
                [p.get("preferred_name") or p["first_name"] for p in members]
        household["name"] = " & ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
    missing_last = result.warnings["missing_last_name"]
    if len(missing_last) > max(20, len(rows) * 0.2):
        result.warnings["missing_last_name"] = []
        result.warnings["file"].append(
            f"{len(missing_last)} people have no last name in this file. That's fine: labels use the "
            "preferred name (Nickname) or First Name.")

    if family_values:
        non_numeric = sum(1 for v in family_values if not re.fullmatch(r"[\w-]*\d[\w-]*", v))
        if non_numeric > len(family_values) / 2:
            result.warnings["family_names_as_ids"].append(
                "The family column looks like family names rather than IDs. Families sharing a name "
                "would be merged into one household. Use a Family ID column if possible.")

    # Children whose class can't be decided from a class column or a class rule
    # (only relevant once the church uses children's classes).
    classrooms = db.all("SELECT * FROM classroom WHERE active=1")
    uses_classes = bool(classrooms) or "classroom" in mapping
    existing_people = {}
    for row in db.all("SELECT id, breeze_id, local_key, import_hash, active, classroom_id, source FROM person"):
        if row["breeze_id"]:
            existing_people["B:" + row["breeze_id"]] = row
        if row["local_key"]:
            existing_people[row["local_key"]] = row
    for key, person in result.people.items():
        if not uses_classes or not person["is_child"] or not person["active"]:
            continue
        has_class = bool(person.get("classroom"))
        if not has_class and existing_people.get(key, {}).get("classroom_id") and "classroom" not in mapping:
            has_class = True
        if not has_class:
            has_class = match_class_rule(classrooms, person.get("birthdate"), person.get("school_year"),
                                         today_iso()) is not None
        age = age_on(person.get("birthdate"), today_iso())
        if not has_class and (age is None or age[0] <= max_age):
            result.warnings["children_no_class"].append(f"{person['first_name']} {person['last_name']}")

    new = updated = unchanged = skipped = 0
    for key, person in result.people.items():
        person["import_hash"] = _hash({k: v for k, v in person.items() if k != "import_hash"})
        current = existing_people.get(key)
        if current is None and not person["active"]:
            # Never stored: someone left out (deceased, transferred...) who isn't in the database yet.
            skipped += 1
            person["_state"] = "skipped"
        elif current is None:
            new += 1
            person["_state"] = "new"
        elif current["import_hash"] == person["import_hash"]:
            unchanged += 1
            person["_state"] = "unchanged"
        else:
            updated += 1
            person["_state"] = "updated"
    for key, row in existing_people.items():
        if row["source"] == "breeze" and row["active"] and key not in result.people:
            result.deactivate.append(row["id"])
    result.deactivate = sorted(set(result.deactivate))

    existing_households = {r["family_key"] for r in db.all("SELECT family_key FROM household")}
    result.totals = {
        "rows": len(rows),
        "people_in_file": len(result.people),
        "children": sum(1 for p in result.people.values() if p["is_child"]),
        "guardians": sum(1 for p in result.people.values() if not p["is_child"]),
        "people_new": new,
        "people_updated": updated,
        "people_unchanged": unchanged,
        "not_stored": skipped,
        "households": len(result.households),
        "households_new": sum(1 for k in result.households if k not in existing_households),
        "classes_new": len(result.classes_new),
        "would_deactivate": len(result.deactivate),
        "inactive_in_file": sum(1 for p in result.people.values() if not p["active"]),
        "active_in_file": sum(1 for p in result.people.values() if p["active"]),
        "left_out": sum(result.left_out.values()),
    }
    return result


def match_class_rule(classrooms: list[dict], birthdate, school_year, on_date):
    """Pick a class for a child from configured rules: school year first, then age range."""
    year = normalize_school_year(school_year) if school_year else ""
    if year:
        for room in classrooms:
            years = [normalize_school_year(y) for y in (room.get("school_years") or "").split(",") if y.strip()]
            if year in years:
                return room
    age = age_on(birthdate, on_date)
    if age is not None:
        months = age[1]
        for room in sorted(classrooms, key=lambda r: (r.get("sort_order") or 0, r["id"])):
            lo, hi = room.get("min_age_months"), room.get("max_age_months")
            if lo is None and hi is None:
                continue
            if (lo is None or months >= lo) and (hi is None or months <= hi):
                return room
    return None


def summary(analysis: Analysis, limit: int = 40) -> dict:
    warnings = {k: {"count": len(v), "items": v[:limit]} for k, v in analysis.warnings.items()}
    samples = [
        {k: v for k, v in p.items() if not k.startswith("_") and k != "import_hash"}
        for p in list(analysis.people.values())[:6]
    ]
    return {"totals": analysis.totals, "warnings": warnings, "samples": samples,
            "classes_new": sorted(analysis.classes_new), "left_out": dict(analysis.left_out.most_common()),
            "day_first": analysis.day_first}


def commit(db, analysis: Analysis, batch_id: int, deactivate_missing: bool, user_id=None) -> dict:
    ts = now_iso()
    with db.tx() as conn:
        class_ids = {normalize_name(r["name"]): r["id"] for r in conn.execute("SELECT id, name FROM classroom")}
        for name in sorted(analysis.classes_new):
            if normalize_name(name) not in class_ids:
                cur = conn.execute("INSERT INTO classroom(name, breeze_event_name) VALUES(?, NULL)", (name,))
                class_ids[normalize_name(name)] = cur.lastrowid

        stored = {k for k, p in analysis.people.items() if p.get("_state") != "skipped"}
        household_ids = {}
        for family_key, household in analysis.households.items():
            if not any(member in stored for member in household["members"]):
                continue
            row = conn.execute("SELECT id FROM household WHERE family_key=?", (family_key,)).fetchone()
            if row:
                conn.execute("UPDATE household SET name=?, active=1, updated_at=? WHERE id=?",
                             (household["name"], ts, row["id"]))
                household_ids[family_key] = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO household(family_key, name, source, active, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?)", (family_key, household["name"], "breeze", 1, ts, ts))
                household_ids[family_key] = cur.lastrowid

        person_ids = {}
        for key, p in analysis.people.items():
            if key not in stored:
                continue
            if key.startswith("B:"):
                row = conn.execute("SELECT id, class_source, chinese_name FROM person WHERE breeze_id=?",
                                   (p["breeze_id"],)).fetchone()
            else:
                row = conn.execute("SELECT id, class_source, chinese_name FROM person WHERE local_key=?",
                                   (key,)).fetchone()
            # A roster without a Chinese name column leaves the one check-in has (from the visitor form).
            chinese = p["chinese_name"] if "chinese_name" in p else (row["chinese_name"] if row else None)
            values = {
                "first_name": p["first_name"], "last_name": p["last_name"], "is_child": p["is_child"],
                "active": p["active"], "import_hash": p["import_hash"], "last_import_id": batch_id,
                "search_name": search_name(p["first_name"], p.get("preferred_name"), p["last_name"], chinese),
                "updated_at": ts,
            }
            for field in ("preferred_name", "chinese_name", "birthdate", "school_year", "sex", "medical_alert",
                          "medical_note", "pickup_note"):
                if field in p:
                    values[field] = p[field]
            if "extra" in p:
                values["extra"] = json.dumps(p["extra"], ensure_ascii=False) if p["extra"] else None
            if "classroom" in p:
                if p["classroom"]:
                    values["classroom_id"] = class_ids.get(normalize_name(p["classroom"]))
                    values["class_source"] = "import"
                elif row and row["class_source"] == "import":
                    values["classroom_id"] = None
                    values["class_source"] = None
            if row:
                assignments = ", ".join(f"{k}=?" for k in values)
                conn.execute(f"UPDATE person SET {assignments} WHERE id=?", (*values.values(), row["id"]))
                person_id = row["id"]
            else:
                values.update({"breeze_id": p.get("breeze_id"), "local_key": p.get("local_key"),
                               "source": "breeze", "created_at": ts})
                cols = ", ".join(values)
                marks = ", ".join("?" for _ in values)
                cur = conn.execute(f"INSERT INTO person({cols}) VALUES({marks})", tuple(values.values()))
                person_id = cur.lastrowid
            person_ids[key] = person_id
            conn.execute("DELETE FROM phone WHERE person_id=?", (person_id,))
            for digits in p["phones"] if p["active"] else []:  # inactive people keep no phone numbers
                conn.execute("INSERT OR IGNORE INTO phone(person_id, digits, last4, kind) VALUES(?,?,?,?)",
                             (person_id, digits, digits[-4:], "mobile"))

        imported = list(person_ids.values())
        for chunk in range(0, len(imported), 500):
            part = imported[chunk:chunk + 500]
            conn.execute(f"DELETE FROM household_member WHERE person_id IN ({','.join('?' * len(part))})", part)
        for family_key, household in analysis.households.items():
            for person_key, role in household["members"].items():
                if person_key in person_ids:
                    conn.execute(
                        "INSERT OR REPLACE INTO household_member(household_id, person_id, role, active) "
                        "VALUES(?,?,?,1)", (household_ids[family_key], person_ids[person_key], role))

        deactivated = 0
        if deactivate_missing and analysis.deactivate:
            for chunk in range(0, len(analysis.deactivate), 500):
                part = analysis.deactivate[chunk:chunk + 500]
                cur = conn.execute(
                    f"UPDATE person SET active=0, updated_at=? WHERE id IN ({','.join('?' * len(part))})",
                    (ts, *part))
                deactivated += cur.rowcount
        conn.execute(
            "UPDATE household SET active=0 WHERE source='breeze' AND id NOT IN ("
            "SELECT m.household_id FROM household_member m JOIN person p ON p.id=m.person_id WHERE p.active=1)")

        totals = dict(analysis.totals, deactivated=deactivated)
        conn.execute("UPDATE import_batch SET status='committed', committed_at=?, totals=? WHERE id=?",
                     (ts, json.dumps(totals), batch_id))
        audit(conn, "roster.import_commit", "import_batch", batch_id, totals, user_id)
    db.bump()
    return totals
