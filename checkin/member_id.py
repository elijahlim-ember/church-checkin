"""Member IDs, the church database's own ID for every person and family, e.g. MEMB-7K3M-Q9XY.

Check-in never makes them (the church database does); it only checks and tidies what is typed or sent, so an
administrator can type one however they like and a typo is refused rather than linked to the wrong person.

The identifier uses Crockford's base 32 (no I, L, O or U), seven characters and a
check character chosen so that the weighted sum in GF(32) is zero. tests/test_member_id.py checks both programs
agree on the same IDs.
"""

import re

PREFIX = "MEMB"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LENGTH = 7  # random characters, before the check character
_VALUE = {symbol: index for index, symbol in enumerate(ALPHABET)}
_LOOKALIKES = str.maketrans("ILO", "110")
_POLY = 0b100101  # x^5 + x^2 + 1


def times(a: int, b: int) -> int:
    """Multiply two elements of GF(32)."""
    result = 0
    while b:
        if b & 1:
            result ^= a
        b >>= 1
        a <<= 1
        if a & 0b100000:
            a ^= _POLY
    return result


def _powers(count: int) -> list[int]:
    value, result = 1, []
    for _ in range(count):
        value = times(value, 2)
        result.append(value)
    return result


WEIGHTS = _powers(LENGTH)  # α^1 … α^7; the check character's weight is 1


def _checksum(symbols: str) -> int:
    total = 0
    for weight, symbol in zip(WEIGHTS + [1], symbols):
        total ^= times(weight, _VALUE[symbol])
    return total


def _show(symbols: str) -> str:
    return f"{PREFIX}-{symbols[:4]}-{symbols[4:]}"


def make(random_part: str) -> str:
    """The full ID for seven characters (used by the tests)."""
    random_part = random_part.upper()
    if len(random_part) != LENGTH or any(c not in _VALUE for c in random_part):
        raise ValueError("An Member ID needs seven characters from its alphabet.")
    return _show(random_part + ALPHABET[_checksum(random_part)])


def parse(text, *, bare: bool = False) -> str | None:
    """The ID in its standard form, or None if the text isn't a valid Member ID.
    With bare=True the MEMB prefix may be left out, but then the ID must contain a letter, so a phone
    number or an old numeric ID is never mistaken for one."""
    compact = re.sub(r"[\s\-]", "", str(text or "")).upper()
    prefixed = compact.startswith(PREFIX)
    if prefixed:
        compact = compact[len(PREFIX):]
    elif not bare:
        return None
    compact = compact.translate(_LOOKALIKES)
    if not prefixed and compact.isdigit():
        return None
    if len(compact) != LENGTH + 1 or any(c not in _VALUE for c in compact) or _checksum(compact):
        return None
    return _show(compact)


def looks_like_one(text) -> bool:
    """Starts with MEMB, so it was meant as a member ID and a typo should be refused, not stored."""
    return re.sub(r"[\s\-]", "", str(text or "")).upper().startswith(PREFIX)
