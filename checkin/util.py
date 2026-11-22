"""Small shared helpers: time, ids, phone and name normalisation, security codes."""

import datetime as dt
import re
import secrets
import unicodedata
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    SGT = ZoneInfo("Asia/Singapore")
except ZoneInfoNotFoundError:
    SGT = dt.timezone(dt.timedelta(hours=8), "SGT")

# Letters and digits that cannot be confused when read off a sticker in a hurry
# (no 0/O, 1/I/L, 2/Z, 5/S, 8/B, U/V).
CODE_ALPHABET = "ACDEFGHJKMNPQRTWXY34679"


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat(sep=" ")


def today_iso() -> str:
    return dt.date.today().isoformat()


def sg_now() -> dt.datetime:
    return dt.datetime.now(SGT).replace(microsecond=0)


def sg_today_iso() -> str:
    return sg_now().date().isoformat()


def host_sgt_offset_warning() -> str | None:
    offset = dt.datetime.now().astimezone().utcoffset()
    if offset != dt.timedelta(hours=8):
        minutes = int(offset.total_seconds() // 60) if offset is not None else 0
        sign = "+" if minutes >= 0 else "-"
        hours, remainder = divmod(abs(minutes), 60)
        return f"This computer's local time zone is UTC{sign}{hours:02d}:{remainder:02d}; Sunday wrap-up uses Singapore time (UTC+08:00)."
    return None


def new_id() -> str:
    return uuid.uuid4().hex


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_security_code(length: int = 4) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def digits_only(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_phone(value, country_code: str = "65") -> str:
    """Return the local part of a phone number as digits only.

    Handles spaces, hyphens, brackets, '+65', '0065' and '65' prefixes, so
    '+65 9123-4567', '65 91234567' and '9123 4567' all become '91234567'.
    """
    d = digits_only(value)
    if not d:
        return ""
    cc = country_code or ""
    if cc:
        if d.startswith("00" + cc) and len(d) - len(cc) - 2 >= 7:
            d = d[2 + len(cc):]
        elif d.startswith(cc) and len(d) - len(cc) >= 8:
            d = d[len(cc):]
    return d


def phone_is_plausible(digits: str, country_code: str = "65") -> bool:
    if country_code == "65" and len(digits) == 8:
        return digits[0] in "3689"
    # Overseas numbers (for example +61 4xx...) are kept so people can still be found by them.
    return 9 <= len(digits) <= 15


def mask_phone(digits: str) -> str:
    if not digits:
        return ""
    return "•••• " + digits[-4:]


def normalize_name(value) -> str:
    """Lower-case, accent-free, single-spaced version of a name for searching."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")


def search_name(*parts) -> str:
    """A person's search_name: their names (first, preferred, last, Chinese) normalised and joined."""
    return normalize_name(" ".join(str(p) for p in parts if p))


def name_pattern(token: str) -> str:
    """The LIKE pattern for one search word against (' ' || search_name): the start of a word, or anywhere for
    Chinese (and other CJK) text, which has no spaces, so 恩慈 finds 陈恩慈."""
    return f"%{token}%" if CJK_RE.search(token) else f"% {token}%"


ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")


def clean_text(value) -> str:
    """Trim a spreadsheet cell and drop invisible characters (Breeze exports contain zero-width spaces)."""
    return ZERO_WIDTH_RE.sub("", str(value or "")).replace("\u00a0", " ").strip()


def detect_day_first(values) -> bool | None:
    """Look at a column of n/n/yyyy dates: True if day-first (dd/mm), False if month-first, None if unclear."""
    first_big = second_big = False
    for value in values:
        match = re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}", str(value or ""))
        if match:
            first_big |= int(match.group(1)) > 12
            second_big |= int(match.group(2)) > 12
    if first_big and not second_big:
        return True
    if second_big and not first_big:
        return False
    return None


def parse_date(value, day_first: bool | None = None) -> str | None:
    """Parse the date formats Breeze and Excel commonly produce. Returns ISO date or None.

    n/n/yyyy is ambiguous: pass day_first (from detect_day_first on the whole column).
    """
    text = str(value or "").strip()
    if not text:
        return None
    text = text.split("T")[0].split(" ")[0] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else text
    us, uk = ("%m/%d/%Y", "%m/%d/%y"), ("%d/%m/%Y", "%d/%m/%y")
    slash = uk + us if day_first else us + uk
    formats = ("%Y-%m-%d",) + slash + ("%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
                                       "%Y/%m/%d", "%d.%m.%Y")
    for fmt in formats:
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def age_on(birthdate: str | None, on: str | None = None) -> tuple[int, int] | None:
    """Return (years, total_months) of age on the given ISO date."""
    if not birthdate:
        return None
    try:
        born = dt.date.fromisoformat(birthdate)
        ref = dt.date.fromisoformat(on) if on else dt.date.today()
    except ValueError:
        return None
    months = (ref.year - born.year) * 12 + (ref.month - born.month)
    if ref.day < born.day:
        months -= 1
    if months < 0:
        return None
    return months // 12, months


def normalize_school_year(value) -> str:
    """'Primary 1', 'P1', 'p 1' -> 'P1'; 'Kindergarten 2', 'K2' -> 'K2'; 'Nursery' -> 'N1'."""
    text = re.sub(r"\s+", "", str(value or "").strip().upper())
    if not text:
        return ""
    replacements = [
        (r"^PRIMARY(\d)$", r"P\1"), (r"^PRI(\d)$", r"P\1"), (r"^P(\d)$", r"P\1"),
        (r"^KINDERGARTEN(\d)$", r"K\1"), (r"^KINDY(\d)$", r"K\1"), (r"^K(\d)$", r"K\1"),
        (r"^NURSERY(\d)?$", r"N\1"), (r"^SECONDARY(\d)$", r"S\1"), (r"^SEC(\d)$", r"S\1"),
        (r"^GRADE(\d+)$", r"G\1"), (r"^(\d+)(ST|ND|RD|TH)?GRADE$", r"G\1"),
    ]
    for pattern, repl in replacements:
        if re.match(pattern, text):
            out = re.sub(pattern, repl, text)
            return "N1" if out == "N" else out
    return text


def truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "y", "yes", "true", "t", "x", "✓", "active")


def clip(text, length: int) -> str:
    text = str(text or "")
    return text if len(text) <= length else text[: max(0, length - 1)].rstrip() + "…"
