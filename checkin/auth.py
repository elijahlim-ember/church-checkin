"""Users, PINs, login sessions and station (device) tokens."""

import base64
import datetime as dt
import hashlib
import hmac
import re
import secrets
import threading
import time

from .db import audit
from .util import CODE_ALPHABET, new_token, now, now_iso

PBKDF2_ITERATIONS = 240_000
ADMIN_IDLE_MINUTES = 30
ACCOUNT_LOCK_AFTER = 5  # wrong passwords in a row before an administrator is locked
ACCOUNT_LOCK_MAX = 3600
DEVICE_CODE_LENGTH = 6
DEVICE_CODE_MINUTES = 10  # how long a one-time device code can be used
ONE_TIME_PREFIX = "One-time "


def account_lock_seconds(failures: int) -> int:
    """The 5th wrong password locks for a minute; each one after doubles it, up to an hour."""
    return min(ACCOUNT_LOCK_MAX, 60 * 2 ** (failures - ACCOUNT_LOCK_AFTER))


def _wait_text(seconds: float) -> str:
    seconds = max(1, int(seconds) + 1)
    return f"{seconds} seconds" if seconds < 120 else f"{(seconds + 59) // 60} minutes"


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_secret(secret: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", secret.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """Blocks a client for a while after repeated wrong PINs or passwords. Each block in a row doubles, up to
    max_block (kept short, so a child pressing keys on an iPad can't lock it out for long)."""

    def __init__(self, limit=5, window=300, block=60, max_block=900):
        self.limit, self.window, self.block, self.max_block = limit, window, block, max_block
        self.failures: dict[str, list[float]] = {}
        self.blocked: dict[str, float] = {}
        self.strikes: dict[str, int] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        with self.lock:
            until = self.blocked.get(key, 0)
            if until > time.time():
                raise AuthError(f"Too many wrong attempts. Try again in {_wait_text(until - time.time())}.", 429)

    def fail(self, key: str) -> None:
        with self.lock:
            stamps = [t for t in self.failures.get(key, []) if t > time.time() - self.window] + [time.time()]
            self.failures[key] = stamps
            if len(stamps) >= self.limit:
                strikes = self.strikes[key] = self.strikes.get(key, 0) + 1
                self.blocked[key] = time.time() + min(self.max_block, self.block * 2 ** (strikes - 1))
                self.failures[key] = []

    def ok(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)
            self.strikes.pop(key, None)


class Auth:
    def __init__(self, db, paths):
        self.db = db
        self.pepper = paths.secret("pin-pepper")
        self.limiter = RateLimiter()
        # One-time device codes live only in memory: they last minutes, and a restart simply voids them.
        self.device_codes: dict[str, dict] = {}
        self.device_codes_lock = threading.Lock()

    def pin_lookup(self, pin: str) -> str:
        return hmac.new(self.pepper, pin.encode(), hashlib.sha256).hexdigest()

    # ---- users ------------------------------------------------------------
    def has_admin(self) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM user WHERE role='admin' AND active=1"))

    def validate_secret(self, role: str, secret: str) -> None:
        if role == "volunteer":
            if not re.fullmatch(r"\d{4,8}", secret or ""):
                raise AuthError("Volunteer PIN must be 4 to 8 digits.", 400)
        elif role == "admin":
            if len(secret or "") < 8 or secret.isdigit():
                raise AuthError("Administrator password must be at least 8 characters and not only digits.", 400)
        else:
            raise AuthError("Unknown role.", 400)

    def create_user(self, name: str, role: str, secret: str, actor_id=None) -> int:
        name = (name or "").strip()
        if not name:
            raise AuthError("Name is required.", 400)
        self.validate_secret(role, secret)
        lookup = self.pin_lookup(secret) if role == "volunteer" else None
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM user WHERE name=?", (name,)).fetchone():
                raise AuthError("A user with that name already exists.", 409)
            if lookup and conn.execute("SELECT 1 FROM user WHERE pin_lookup=?", (lookup,)).fetchone():
                raise AuthError("That PIN is already used by another volunteer login. Choose a different PIN.", 409)
            cur = conn.execute(
                "INSERT INTO user(name, role, secret_hash, pin_lookup, created_at) VALUES(?,?,?,?,?)",
                (name, role, hash_secret(secret), lookup, now_iso()))
            audit(conn, "user.create", "user", cur.lastrowid, {"name": name, "role": role}, actor_id)
            return cur.lastrowid

    def update_user(self, user_id: int, secret=None, active=None, actor_id=None) -> None:
        user = self.db.one("SELECT * FROM user WHERE id=?", (user_id,))
        if not user:
            raise AuthError("User not found.", 404)
        with self.db.tx() as conn:
            if secret:
                self.validate_secret(user["role"], secret)
                lookup = self.pin_lookup(secret) if user["role"] == "volunteer" else None
                if lookup and conn.execute("SELECT 1 FROM user WHERE pin_lookup=? AND id<>?", (lookup, user_id)).fetchone():
                    raise AuthError("That PIN is already used by another volunteer login.", 409)
                conn.execute("UPDATE user SET secret_hash=?, pin_lookup=?, failed_attempts=0, locked_until=NULL "
                             "WHERE id=?",
                             (hash_secret(secret), lookup, user_id))
                conn.execute("DELETE FROM auth_session WHERE user_id=?", (user_id,))
                audit(conn, "user.secret_changed", "user", user_id, {"name": user["name"]}, actor_id)
            if active is not None:
                if not active and user["role"] == "admin":
                    admins = conn.execute("SELECT COUNT(*) FROM user WHERE role='admin' AND active=1 AND id<>?",
                                          (user_id,)).fetchone()[0]
                    if admins == 0:
                        raise AuthError("Cannot deactivate the last administrator.", 400)
                conn.execute("UPDATE user SET active=? WHERE id=?", (1 if active else 0, user_id))
                if not active:
                    conn.execute("DELETE FROM auth_session WHERE user_id=?", (user_id,))
                audit(conn, "user.active" if active else "user.deactivate", "user", user_id,
                      {"name": user["name"]}, actor_id)

    # ---- sessions ---------------------------------------------------------
    def _issue(self, user: dict, station_id, hours: float) -> str:
        token = new_token()
        expires = now() + dt.timedelta(hours=hours)
        with self.db.tx() as conn:
            conn.execute("DELETE FROM auth_session WHERE expires_at < ?", (now_iso(),))
            conn.execute(
                "INSERT INTO auth_session(token_hash, user_id, station_id, role, created_at, expires_at, last_seen) "
                "VALUES(?,?,?,?,?,?,?)",
                (token_hash(token), user["id"], station_id, user["role"], now_iso(),
                 expires.isoformat(sep=" "), now_iso()))
            audit(conn, "login", "user", user["id"], {"role": user["role"]}, user["id"], station_id)
        return token

    def login_pin(self, pin: str, station_id, client_key: str) -> tuple[str, dict]:
        self.limiter.check(client_key)
        user = None
        if re.fullmatch(r"\d{4,8}", pin or ""):
            user = self.db.one("SELECT * FROM user WHERE pin_lookup=? AND active=1", (self.pin_lookup(pin),))
        if not user or not verify_secret(pin, user["secret_hash"]):
            self.limiter.fail(client_key)
            raise AuthError("Wrong PIN.")
        self.limiter.ok(client_key)
        hours = float(self.db.get_setting("volunteer_session_hours") or 12)
        return self._issue(user, station_id, hours), user

    def login_admin(self, name: str, password: str, station_id, client_key: str,
                    local: bool = False) -> tuple[str, dict]:
        """local: signing in on the main computer itself, which a lock on the account doesn't stop (the
        per-client limit still applies). Otherwise a locked account can't even try its password."""
        self.limiter.check(client_key)
        user = self.db.one("SELECT * FROM user WHERE name=? AND role='admin' AND active=1", ((name or "").strip(),))
        if user and user["locked_until"] and not local:
            left = (dt.datetime.fromisoformat(user["locked_until"]) - now()).total_seconds()
            if left > 0:
                raise AuthError(f"This administrator is locked after too many wrong passwords. Try again in "
                                f"{_wait_text(left)}, or sign in on the main computer.", 429)
        if not user or not verify_secret(password or "", user["secret_hash"]):
            self.limiter.fail(client_key)
            if user:
                self._account_failed(user)
            raise AuthError("Wrong administrator name or password.")
        self.limiter.ok(client_key)
        if user["failed_attempts"] or user["locked_until"]:
            with self.db.tx() as conn:
                conn.execute("UPDATE user SET failed_attempts=0, locked_until=NULL WHERE id=?", (user["id"],))
        return self._issue(user, station_id, 8), user

    def _account_failed(self, user: dict) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE user SET failed_attempts=failed_attempts+1 WHERE id=?", (user["id"],))
            failures = conn.execute("SELECT failed_attempts FROM user WHERE id=?", (user["id"],)).fetchone()[0]
            if failures >= ACCOUNT_LOCK_AFTER:
                until = (now() + dt.timedelta(seconds=account_lock_seconds(failures))).isoformat(sep=" ")
                conn.execute("UPDATE user SET locked_until=? WHERE id=?", (until, user["id"]))
                audit(conn, "user.locked", "user", user["id"], {"name": user["name"], "failures": failures,
                                                                "until": until}, None)

    def session(self, token: str) -> dict | None:
        if not token:
            return None
        row = self.db.one(
            "SELECT s.*, u.name AS user_name, u.active AS user_active FROM auth_session s "
            "JOIN user u ON u.id = s.user_id WHERE s.token_hash=?", (token_hash(token),))
        if not row or not row["user_active"] or row["expires_at"] < now_iso():
            return None
        if row["role"] == "admin":
            idle_limit = (now() - dt.timedelta(minutes=ADMIN_IDLE_MINUTES)).isoformat(sep=" ")
            if (row["last_seen"] or "") < idle_limit:
                return None
        if (row["last_seen"] or "") < (now() - dt.timedelta(seconds=30)).isoformat(sep=" "):
            with self.db.tx() as conn:
                conn.execute("UPDATE auth_session SET last_seen=? WHERE token_hash=?", (now_iso(), row["token_hash"]))
        return row

    def logout(self, token: str) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM auth_session WHERE token_hash=?", (token_hash(token or ""),))

    # ---- stations (devices) -------------------------------------------------
    def register_station(self, name: str, printer: str | None, fallback: str | None, actor_id) -> tuple[str, dict]:
        name = (name or "").strip()
        if not name:
            raise AuthError("Station name is required.", 400)
        token = new_token()
        with self.db.tx() as conn:
            existing = conn.execute("SELECT id FROM station WHERE name=?", (name,)).fetchone()
            if existing:
                # Re-registering a device under an existing name replaces its token
                # (for example when an iPad's browser data was cleared).
                conn.execute("UPDATE station SET token_hash=?, printer=?, fallback_printer=?, active=1, expires_at=NULL "
                             "WHERE id=?",
                             (token_hash(token), printer, fallback, existing["id"]))
                station_id = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO station(name, printer, fallback_printer, token_hash, created_at) VALUES(?,?,?,?,?)",
                    (name, printer, fallback, token_hash(token), now_iso()))
                station_id = cur.lastrowid
            audit(conn, "station.register", "station", station_id,
                  {"name": name, "printer": printer, "fallback": fallback}, actor_id)
        return token, self.db.one("SELECT * FROM station WHERE id=?", (station_id,))

    def station(self, token: str, ip: str = None) -> dict | None:
        if not token:
            return None
        row = self.db.one("SELECT * FROM station WHERE token_hash=? AND active=1", (token_hash(token),))
        if row and row["expires_at"] and row["expires_at"] <= now_iso():
            with self.db.tx() as conn:
                # Only if the row still holds this token: a join may have just reused it for a new device.
                changed = conn.execute("UPDATE station SET active=0, token_hash=NULL WHERE id=? AND token_hash=?",
                                       (row["id"], row["token_hash"])).rowcount
                if changed:
                    conn.execute("DELETE FROM auth_session WHERE station_id=?", (row["id"],))
                    audit(conn, "station.expired", "station", row["id"], {"name": row["name"]})
            return None
        if row and ((row["last_seen"] or "") < (now() - dt.timedelta(seconds=20)).isoformat(sep=" ")
                    or row["last_ip"] != ip):
            with self.db.tx() as conn:
                conn.execute("UPDATE station SET last_seen=?, last_ip=? WHERE id=?", (now_iso(), ip, row["id"]))
        return row

    # ---- one-time devices -----------------------------------------------------
    def create_device_code(self, station: dict, user_id) -> dict:
        """A volunteer on a station makes a code that lets one more device check people in until the end of
        today. The code works once, for DEVICE_CODE_MINUTES."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(DEVICE_CODE_LENGTH))
        expires = time.time() + DEVICE_CODE_MINUTES * 60
        # The new device lasts to the end of the day the code was made, and never longer than the station that
        # made it (so a one-time device can't make a code just before midnight and roll itself into tomorrow).
        until = now().replace(hour=23, minute=59, second=59, microsecond=0).isoformat(sep=" ")
        if station.get("expires_at"):
            until = min(until, station["expires_at"])
        with self.device_codes_lock:
            self.device_codes = {k: v for k, v in self.device_codes.items() if v["expires"] > time.time()}
            self.device_codes[token_hash(code)] = {"expires": expires, "station_id": station["id"],
                                                   "user_id": user_id, "until": until}
        with self.db.tx() as conn:
            audit(conn, "station.one_time_code", "station", station["id"], {"minutes": DEVICE_CODE_MINUTES},
                  user_id, station["id"])
        return {"code": code, "minutes": DEVICE_CODE_MINUTES,
                "expires_at": (now() + dt.timedelta(minutes=DEVICE_CODE_MINUTES)).isoformat(sep=" ")}

    def join_with_code(self, code: str, client_key: str) -> tuple[str, dict]:
        """Turns this device into a one-time station: "One-time 1", "One-time 2" …, printing where the station that
        made the code prints, and switched off at the end of today."""
        self.limiter.check(client_key)
        code = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
        with self.device_codes_lock:
            invite = self.device_codes.pop(token_hash(code), None) if code else None
        if not invite or invite["expires"] <= time.time():
            self.limiter.fail(client_key)
            raise AuthError("That code is wrong or has expired. Ask for a new one.")
        maker = self.db.one("SELECT * FROM station WHERE id=?", (invite["station_id"],))
        until = invite["until"]
        if not maker or not maker["active"] or until <= now_iso():
            # The desk that made the code has been revoked, or the day it was made for is over.
            self.limiter.fail(client_key)
            raise AuthError("That code is wrong or has expired. Ask for a new one.")
        self.limiter.ok(client_key)
        token = new_token()
        with self.db.tx() as conn:
            taken = {r["name"].lower(): r for r in conn.execute(
                "SELECT * FROM station WHERE name LIKE ?", (ONE_TIME_PREFIX + "%",)).fetchall()}
            stamp = now_iso()

            def in_use(n):
                row = taken.get((ONE_TIME_PREFIX + str(n)).lower())
                return bool(row and row["active"] and (not row["expires_at"] or row["expires_at"] > stamp))

            n = 1
            # Reuse a number whose device has gone (expired or revoked); never take over one still in use.
            while in_use(n):
                n += 1
            name = ONE_TIME_PREFIX + str(n)
            old = taken.get(name.lower())
            values = (token_hash(token), maker.get("printer"), maker.get("fallback_printer"), until)
            if old:
                conn.execute("UPDATE station SET token_hash=?, printer=?, fallback_printer=?, expires_at=?, active=1, "
                             "last_seen=NULL, last_ip=NULL WHERE id=?", values + (old["id"],))
                conn.execute("DELETE FROM auth_session WHERE station_id=?", (old["id"],))
                station_id = old["id"]
            else:
                station_id = conn.execute(
                    "INSERT INTO station(name, token_hash, printer, fallback_printer, expires_at, created_at) "
                    "VALUES(?,?,?,?,?,?)", (name,) + values + (now_iso(),)).lastrowid
            audit(conn, "station.one_time_join", "station", station_id,
                  {"name": name, "until": until, "code_from_station": invite["station_id"]}, invite["user_id"],
                  station_id)
        return token, self.db.one("SELECT * FROM station WHERE id=?", (station_id,))
