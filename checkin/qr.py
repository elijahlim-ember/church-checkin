"""Draws a QR code of a short address, so *Add a one-time device* can show a square for whatever address this
computer has today. Plain Python and nothing else: check-in ships no QR library.

Only what a web address needs: byte mode, error correction level M, versions 1 to 5 (up to 84 characters).
`scripts/read_qr.swift` reads a drawn square back with macOS's own readers.
"""

import base64

# version: (blocks, data bytes per block, error-correction bytes per block), all at level M
_VERSIONS = {1: (1, 16, 10), 2: (1, 28, 16), 3: (1, 44, 26), 4: (2, 32, 18), 5: (2, 43, 24)}
QUIET = 4  # light modules around the square, which readers need

_EXP = [0] * 510
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _EXP[_i + 255] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D


def _error_bytes(data, count):
    """Reed-Solomon remainder of `data` for `count` error-correction bytes."""
    generator = [1]
    for i in range(count):
        generator = [(generator[j] if j < len(generator) else 0)
                     ^ (_EXP[_LOG[generator[j - 1]] + i] if j and generator[j - 1] else 0)
                     for j in range(len(generator) + 1)]
    remainder = [0] * count
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        if factor:
            for j in range(count):
                if generator[j + 1]:
                    remainder[j] ^= _EXP[_LOG[generator[j + 1]] + _LOG[factor]]
    return remainder


def _codewords(payload, version):
    blocks, per_block, ec = _VERSIONS[version]
    capacity = blocks * per_block
    bits = "0100" + format(len(payload), "08b") + "".join(format(b, "08b") for b in payload)
    bits += "0" * min(4, capacity * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    data += [(0xEC, 0x11)[i % 2] for i in range(capacity - len(data))]
    parts = [data[i * per_block:(i + 1) * per_block] for i in range(blocks)]
    checks = [_error_bytes(part, ec) for part in parts]
    return [part[i] for i in range(per_block) for part in parts] + [check[i] for i in range(ec) for check in checks]


def _mask(index, x, y):
    return (lambda: (x + y) % 2, lambda: y % 2, lambda: x % 3, lambda: (x + y) % 3,
            lambda: (x // 3 + y // 2) % 2, lambda: x * y % 2 + x * y % 3,
            lambda: (x * y % 2 + x * y % 3) % 2, lambda: ((x + y) % 2 + x * y % 3) % 2)[index]() == 0


def _penalty(rows):
    size = len(rows)
    lines = ["".join("1" if cell else "0" for cell in row) for row in rows]
    lines += ["".join(line[x] for line in lines) for x in range(size)]
    score = 0
    for line in lines:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        score += 40 * (line.count("10111010000") + line.count("00001011101"))
    for y in range(size - 1):
        for x in range(size - 1):
            if rows[y][x] == rows[y][x + 1] == rows[y + 1][x] == rows[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, rows))
    return score + 10 * int(abs(dark * 100 / (size * size) - 50) / 5)


def matrix(text, mask=None):
    """The square as rows of True (dark) and False (light), without the quiet zone."""
    payload = text.encode("utf-8")
    version = next((v for v, (blocks, per_block, _) in _VERSIONS.items() if len(payload) + 2 <= blocks * per_block),
                   None)
    if version is None:
        raise ValueError("Too long for a QR code here.")
    size = 17 + 4 * version
    rows = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            rows[y][x], fixed[y][x] = bool(dark), True

    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # the three corner squares and their light borders
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                put(cx + dx, cy + dy, max(abs(dx), abs(dy)) in (0, 1, 3))
    if version > 1:  # the small square near the fourth corner
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                put(size - 7 + dx, size - 7 + dy, max(abs(dx), abs(dy)) != 1)
    for i in range(8, size - 8):  # the dotted lines between the corners
        put(i, 6, i % 2 == 0)
        put(6, i, i % 2 == 0)

    def put_format(mask_index):
        data = mask_index  # level M is 00, then the mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = ((data << 10) | remainder) ^ 0x5412
        bit = lambda i: (bits >> i) & 1  # noqa: E731
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)

    put_format(0)  # reserves the cells; the real one is written once the mask is chosen
    stream = "".join(format(c, "08b") for c in _codewords(payload, version))
    index = 0
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1
        for step in range(size):
            for x in (right, right - 1):
                y = size - 1 - step if (right + 1) & 2 == 0 else step
                if not fixed[y][x]:
                    rows[y][x] = index < len(stream) and stream[index] == "1"
                    index += 1

    def masked(mask_index):
        nonlocal rows
        plain = rows
        rows = [[cell != (not fixed[y][x] and _mask(mask_index, x, y)) for x, cell in enumerate(row)]
                for y, row in enumerate(plain)]
        put_format(mask_index)
        result, rows = rows, plain
        return result

    if mask is not None:
        return masked(mask)
    return min((masked(i) for i in range(8)), key=_penalty)


def svg(text):
    """The square as an SVG picture, with its light border."""
    rows = matrix(text)
    side = len(rows) + 2 * QUIET
    path = "".join(f"M{x + QUIET} {y + QUIET}h1v1h-1z" for y, row in enumerate(rows) for x, cell in enumerate(row)
                   if cell)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" shape-rendering="crispEdges">'
            f'<rect width="{side}" height="{side}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def data_uri(text):
    """The square as something an <img> can show without another request."""
    return "data:image/svg+xml;base64," + base64.b64encode(svg(text).encode("ascii")).decode("ascii")
