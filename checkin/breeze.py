"""Optional Breeze-compatible client for roster and attendance synchronisation.

Legacy internal names stay for compatibility with existing local data. The integration is disabled until configured. The key is kept in data/keys/chms-api-key
(outside the database, readable only by this user, included in encrypted backups) and is never sent to the
iPads or shown in full. Every call is a GET with the key in the Api-Key header.
"""

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import clean_text

log = logging.getLogger("checkin.breeze")

KEY_FILE = "breeze-api-key"   # the old Breeze key: only ever deleted now
# Some compatible services expose extra roster and service endpoints.
CHMS_KEY_FILE = "chms-api-key"
# Optional certificate authority file for a self-hosted compatible service using HTTPS.
CHMS_CA_FILE = "chms-ca.pem"
DEFAULT_CHMS_URL = "http://127.0.0.1:8091"
# A compatible service can return stable member IDs when asked. Older services ignore the option and use their usual IDs.
ID_HEADER = "Member ID"


class BreezeError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class BreezeRefused(BreezeError):
    """The church database answered, and refused the call ({"success": false, ...}). `data` is its whole reply,
    e.g. {"erased": true} beside the errors. Anything else (can't reach it, a key problem) is a plain BreezeError."""

    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data if isinstance(data, dict) else {}


def load_key(paths, name: str = KEY_FILE) -> str | None:
    path = paths.keys / name
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_key(paths, key: str | None, name: str = KEY_FILE) -> None:
    path = paths.keys / name
    if not key:
        path.unlink(missing_ok=True)
        return
    path.write_text(key.strip(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def tls_context(extra_ca=None) -> ssl.SSLContext:
    """Windows Python uses the Windows certificate store. Python from python.org on a Mac ships without
    certificates, so fall back to certifi (if installed) or the Mac's own certificate bundle. `extra_ca` is a
    PEM file of one more authority to trust (the church database's)."""
    context = ssl.create_default_context()
    if extra_ca and os.path.exists(extra_ca):
        context.load_verify_locations(str(extra_ca))
    if ssl.get_default_verify_paths().cafile or os.name == "nt":
        return context
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except ImportError:
        for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(bundle):
                context.load_verify_locations(bundle)
                break
    return context


class BreezeClient:
    def __init__(self, subdomain: str, api_key: str, base_url: str | None = None,
                 min_interval: float = 0.5, timeout: float = 30.0, sleep=time.sleep, label: str = "the external service",
                 ca_file=None):
        self.base = (base_url or DEFAULT_CHMS_URL).rstrip("/") + "/api/"
        self.name, self.Name = label, label[:1].upper() + label[1:]   # for messages
        self.key = api_key
        self.min_interval = min_interval
        self.timeout = timeout
        self.sleep = sleep
        self.calls = 0
        self._last = 0.0
        self._tls = tls_context(ca_file)

    @classmethod
    def from_app(cls, db, paths, **kwargs):
        key = load_key(paths, CHMS_KEY_FILE)
        if not key:
            raise BreezeError("The church database isn't connected yet. Add its key in Admin → Sync.", 409)
        return cls("chms", key, base_url=db.get_setting("chms_url") or DEFAULT_CHMS_URL, min_interval=0,
                   ca_file=paths.keys / CHMS_CA_FILE, **kwargs)

    def get(self, path: str, **params):
        query = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                 for k, v in params.items() if v is not None}
        url = self.base + path.lstrip("/") + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Api-Key": self.key, "Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "Church-Check-in"}
        failures = 0
        while True:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout, context=self._tls) as resp:
                    body = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as err:
                err.close()
                if err.code == 429 and failures < 5:
                    failures += 1
                    self._slow_down()
                    continue
                if err.code in (401, 403):
                    raise BreezeError(f"{self.Name} didn't accept the API key. Check it in Admin → Sync.", 401)
                if err.code == 404:
                    raise BreezeError(f"{self.Name} address not found. Check it in Admin → Sync.", 404)
                raise BreezeError(f"{self.Name} replied with an error ({err.code}). Try again later.")
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                if isinstance(getattr(err, "reason", err), ssl.SSLCertVerificationError):
                    raise BreezeError("This computer doesn't trust the church database's certificate yet. Open "
                                      "its address with :8090/ca.crt on this computer and install it, or save "
                                      "it as data\\keys\\chms-ca.pem (see docs/SYNC.md).") from err
                failures += 1
                if failures < 3:
                    self.sleep(2 * failures)
                    continue
                raise BreezeError(f"Can't reach {self.name}. Check that it's running and that this computer "
                                  "is on the church network.") from err
            data = self._decode(body, self.Name)
            if isinstance(data, dict) and data.get("success") is False:
                text = json.dumps(data.get("errors") or data.get("error") or data)[:300]
                if "too many" in text.lower() or "rate" in text.lower():
                    if failures < 5:
                        failures += 1
                        self._slow_down()
                        continue
                if "api key" in text.lower() or "authenticat" in text.lower():
                    raise BreezeError(f"{self.Name} didn't accept the API key. Check it in Admin → Sync.", 401)
                raise BreezeRefused(f"{self.Name} said: {text}", data)
            return data

    def _slow_down(self):
        self.min_interval = min(max(self.min_interval * 2, 3.5), 10.0)
        log.warning("%s asked us to slow down; now one call every %.1f s", self.Name, self.min_interval)
        self.sleep(30)

    @staticmethod
    def _decode(body: str, name: str = "The external service"):
        text = body.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            if text.lower().startswith("<"):
                raise BreezeError(f"{name} sent a web page instead of data. Check the address and API key.", 401)
            raise BreezeError(f"{name} sent something unexpected. Try again later.")

    def read(self, path: str, **params):
        """A read that bypasses Breeze's cache. Breeze keeps each reply for a few minutes per address
        (seen on some services: attendance added or removed a minute earlier didn't show), so every read
        gets a unique, ignored parameter."""
        return self.get(path, **params, _=f"{time.time():.6f}")

    # Calls used by the sync

    def profile_fields(self) -> list[dict]:
        return self.read("profile") or []

    def people(self) -> list[dict]:
        return self.read("people", details=1, limit=0, ids="member") or []

    def events(self, start: str, end: str) -> list[dict]:
        return self.read("events", start=start, end=end, limit=1000) or []

    def services(self) -> list[dict]:
        """The church database's weekly services (compatible service 1.12.0 or later): id, name, weekday, start_time."""
        return self.read("services") or []

    def attendance(self, instance_id) -> list[dict]:
        return self.read("events/attendance/list", instance_id=instance_id, ids="member") or []

    def add_attendance(self, person_id, instance_id) -> str:
        """'added', or 'duplicate' when Breeze already has this person on the event."""
        reply = self.get("events/attendance/add", person_id=person_id, instance_id=instance_id, direction="in")
        return "duplicate" if str(reply).strip().lower() == "duplicate" else "added"

    def add_person(self, first: str, last: str, fields: list[dict] | None = None, add_code: str | None = None):
        """add_code: the newcomer's one-off code, the same on every try. With it the church database (compatible service
        1.9.0 on) sends back the person an earlier try added, so a lost reply never adds them twice. Older
        versions ignore it."""
        return self.get("people/add", first=first, last=last, fields_json=fields or None, ids="member",
                        add_code=add_code)

    def update_person(self, person_id: str, fields: list[dict]):
        """Sends a newcomer check-in added the visitor form answers it kept (compatible service 1.9.0 on). The church
        database fills only its blanks and replies with saved_fields: each answer sent that the person now has,
        whoever entered it. Older versions refuse the call as an unknown endpoint."""
        return self.get("people/update", person_id=person_id, fields_json=fields, ids="member")

    def sunday_report(self, payload: dict):
        """Send the count-only Sunday report to the church database."""
        return self.get("reports/sunday", report_json=payload)


STRUCTURAL_TYPES = {"name", "paragraph", "file", "family", "phone", "email", "address"}
PHONE_HEADERS = {"mobile": "Mobile", "home": "Home", "work": "Work"}


def _display(value) -> str:
    """A profile answer as the text Breeze's Excel export shows: choices by their name, lists joined."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "")
    if isinstance(value, list):
        return "; ".join(t for t in (_display(v) for v in value) if t)
    return str(value)


def people_table(profile: list[dict], people: list[dict]) -> tuple[list[str], list[dict]]:
    """Breeze's API people list as rows with the same column headings as Breeze's Excel people export,
    so the import's saved column choices and leave-out rules apply unchanged."""
    fields = [f for section in profile for f in section.get("fields") or []
              if f.get("field_type") not in STRUCTURAL_TYPES and f.get("field_type") != "birthdate"]
    headers = [ID_HEADER, "First Name", "Last Name", "Nickname", "Middle Name", "Family", "Family Role",
               "Mobile", "Home", "Work", "Birthdate"]
    headers += [f["name"].strip() for f in fields if f.get("name") and f["name"].strip() not in headers]
    phone_ids = [str(f["field_id"]) for section in profile for f in section.get("fields") or []
                 if f.get("field_type") == "phone"]
    rows = []
    for person in people:
        details = person.get("details") or {}
        pid = str(person.get("id") or "")
        row = {ID_HEADER: pid,
               "First Name": person.get("force_first_name") or person.get("first_name") or "",
               "Last Name": person.get("last_name") or "",
               "Nickname": person.get("nick_name") or "",
               "Middle Name": person.get("middle_name") or "",
               "Birthdate": _display(details.get("birthdate"))}
        own = next((m for m in person.get("family") or [] if str(m.get("person_id")) == pid), None)
        if own:
            row["Family"] = str(own.get("family_id") or "")
            row["Family Role"] = own.get("role_name") or ""
        for field_id in phone_ids:
            for phone in details.get(field_id) or []:
                header = PHONE_HEADERS.get((phone.get("phone_type") or "").lower())
                if header and phone.get("phone_number") and not row.get(header):
                    row[header] = phone["phone_number"]
        for field in fields:
            name = field["name"].strip()
            if name not in row:
                row[name] = _display(details.get(str(field["field_id"])))
        rows.append({h: clean_text(row.get(h, "")) for h in headers})
    return headers, rows


def legacy_ids(people: list[dict]) -> tuple[dict, dict] | None:
    """Old ID -> Member ID for people and for families, when the church database sent Member IDs with the old IDs
    beside them (legacy_id, legacy_family_id). None when it sent only one kind of ID (an older version)."""
    if not any(p.get("legacy_id") for p in people):
        return None
    persons, families = {}, {}
    for person in people:
        pid, old = str(person.get("id") or ""), str(person.get("legacy_id") or "")
        if pid and old:
            persons[old] = pid
        own = next((m for m in person.get("family") or [] if str(m.get("person_id")) == pid), None)
        if own and own.get("legacy_family_id") and own.get("family_id"):
            families[str(own["legacy_family_id"])] = str(own["family_id"])
    return persons, families
