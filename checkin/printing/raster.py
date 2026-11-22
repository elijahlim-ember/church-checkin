"""Brother QL raster command encoding and status decoding (QL-800/810W/820NWB series).

Follows Brother's "QL-800/810W/820NWB Raster Command Reference". The printer
must be in its default Raster command mode, with P-touch Editor Lite switched
off (Editor Lite makes the printer appear as a USB drive instead of a printer).
"""

from dataclasses import dataclass

HEAD_DOTS = 720            # print head width at 300 dpi
BYTES_PER_LINE = HEAD_DOTS // 8
DPI = 300


@dataclass(frozen=True)
class Media:
    key: str
    width_mm: int
    length_mm: int          # 0 for continuous rolls
    printable_dots: int     # across the tape
    length_dots: int        # along the tape for die-cut labels, 0 for continuous
    right_margin_dots: int  # offset of the printable area from the head edge
    die_cut: bool
    feed_margin: int        # dots fed before/after continuous labels
    description: str


MEDIA = {m.key: m for m in [
    Media("62", 62, 0, 696, 0, 12, False, 35, "DK-22205 / DK-22251 62mm continuous"),
    Media("29", 29, 0, 306, 0, 6, False, 35, "DK-22210 29mm continuous"),
    Media("38", 38, 0, 413, 0, 12, False, 35, "DK-22225 38mm continuous"),
    Media("50", 50, 0, 554, 0, 12, False, 35, "DK-22223 50mm continuous"),
    Media("54", 54, 0, 590, 0, 0, False, 35, "DK-N55224 54mm continuous"),
    Media("62x29", 62, 29, 696, 271, 12, True, 0, "DK-11209 62x29mm die-cut"),
    Media("62x100", 62, 100, 696, 1109, 12, True, 0, "DK-11202 62x100mm die-cut"),
    Media("29x90", 29, 90, 306, 991, 6, True, 0, "DK-11201 29x90mm die-cut"),
    Media("39x90", 38, 90, 413, 991, 12, True, 0, "DK-11208 38x90mm die-cut"),
]}

MEDIA_TYPE_CONTINUOUS = 0x0A
MEDIA_TYPE_DIE_CUT = 0x0B

MODEL_CODES = {0x38: "QL-800", 0x39: "QL-810W", 0x41: "QL-820NWB", 0x35: "QL-700", 0x36: "QL-710W", 0x37: "QL-720NW"}

ERROR1 = {0x01: "No media", 0x02: "End of media", 0x04: "Cutter jam", 0x10: "Printer in use",
          0x20: "Printer turned off", 0x40: "High-voltage adapter", 0x80: "Fan motor error"}
ERROR2 = {0x01: "Replace media", 0x02: "Expansion buffer full", 0x04: "Communication error",
          0x08: "Communication buffer full", 0x10: "Cover open", 0x20: "Overheating",
          0x40: "Media cannot be fed", 0x80: "System error"}
STATUS_TYPES = {0x00: "reply", 0x01: "printing_completed", 0x02: "error", 0x04: "turned_off",
                0x05: "notification", 0x06: "phase_change"}

STATUS_REQUEST = b"\x00" * 400 + b"\x1b\x40" + b"\x1b\x69\x53"


def mm_to_dots(mm: float) -> int:
    return int(round(mm * DPI / 25.4))


def continuous_lines(length_mm: float, media: Media) -> int:
    """Raster lines for a continuous label of the given total length (margins included)."""
    lines = mm_to_dots(length_mm) - 2 * media.feed_margin
    return max(150, lines)


def packbits(data: bytes) -> bytes:
    """TIFF PackBits, as accepted by the QL 'M 02' compression mode."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        run = 1
        while i + run < n and run < 128 and data[i + run] == data[i]:
            run += 1
        if run >= 2:
            out.append((257 - run) & 0xFF)
            out.append(data[i])
            i += run
            continue
        start = i
        i += 1
        while i < n and i - start < 128:
            if i + 1 < n and data[i] == data[i + 1]:
                break
            i += 1
        out.append(i - start - 1)
        out.extend(data[start:i])
    return bytes(out)


def unpackbits(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        header = data[i]
        i += 1
        if header < 128:
            out.extend(data[i:i + header + 1])
            i += header + 1
        elif header > 128:
            out.extend(bytes([data[i]]) * (257 - header))
            i += 1
    return bytes(out)


def image_to_lines(image, media: Media) -> list[bytes]:
    """Turn a Pillow image (width = printable dots across the tape) into 90-byte raster lines.

    Black pixels become 1 bits. The image is placed at the media offset and
    mirrored, because the head prints each line from the opposite end.
    """
    from PIL import Image, ImageOps

    if image.width != media.printable_dots:
        raise ValueError(f"Label image is {image.width} dots wide; {media.key} media needs {media.printable_dots}.")
    gray = image.convert("L")
    canvas = Image.new("L", (HEAD_DOTS, gray.height), 255)
    canvas.paste(gray, (HEAD_DOTS - gray.width - media.right_margin_dots, 0))
    canvas = canvas.transpose(Image.FLIP_LEFT_RIGHT)
    bits = ImageOps.invert(canvas).point(lambda v: 255 if v >= 128 else 0).convert("1")
    raw = bits.tobytes()
    return [raw[row * BYTES_PER_LINE:(row + 1) * BYTES_PER_LINE] for row in range(bits.height)]


def build_job(pages: list, media: Media, cut_each: bool = True, compress: bool = False,
              high_quality: bool = False) -> bytes:
    """Encode one or more label images into a single raster print job.

    Each page is auto-cut; the job ends with a cut. All pages travel in one
    write so the printer starts on the first label while later ones arrive.
    """
    if not pages:
        raise ValueError("Nothing to print.")
    data = bytearray(b"\x00" * 400)          # invalidate
    data += b"\x1b\x40"                       # initialise
    for index, image in enumerate(pages):
        lines = image_to_lines(image, media)
        data += b"\x1b\x69\x61\x01"           # switch to raster mode
        # Valid-field flags: recovery on, media type, width and length given, optional quality priority.
        flags = 0x80 | 0x02 | 0x04 | 0x08 | (0x40 if high_quality else 0)
        data += b"\x1b\x69\x7a" + bytes([
            flags,
            MEDIA_TYPE_DIE_CUT if media.die_cut else MEDIA_TYPE_CONTINUOUS,
            media.width_mm,
            media.length_mm if media.die_cut else 0,
        ]) + len(lines).to_bytes(4, "little") + bytes([0 if index == 0 else 1, 0])
        data += b"\x1b\x69\x4d" + bytes([0x40 if cut_each else 0x00])    # auto cut
        data += b"\x1b\x69\x41\x01"                                       # cut every 1 label
        data += b"\x1b\x69\x4b" + bytes([0x08])                           # cut at end
        data += b"\x1b\x69\x64" + (media.feed_margin if not media.die_cut else 0).to_bytes(2, "little")
        data += b"\x4d" + bytes([0x02 if compress else 0x00])
        for line in lines:
            if compress:
                packed = packbits(line)
                data += b"\x67\x00" + bytes([len(packed)]) + packed
            else:
                data += b"\x67\x00" + bytes([BYTES_PER_LINE]) + line
        data += b"\x1a" if index == len(pages) - 1 else b"\x0c"
    return bytes(data)


@dataclass
class PrinterStatus:
    ok: bool
    state: str                 # ready | no_media | cover_open | cutter_jam | busy | error | offline | media_mismatch
    message: str
    errors: list
    media_width: int = 0
    media_length: int = 0
    media_type: str = ""
    model: str = ""
    status_type: str = ""
    phase: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def parse_status(packet: bytes) -> PrinterStatus | None:
    if len(packet) < 32 or packet[0] != 0x80 or packet[1] != 0x20:
        return None
    err1, err2 = packet[8], packet[9]
    errors = [text for bit, text in ERROR1.items() if err1 & bit and bit != 0x10]
    errors += [text for bit, text in ERROR2.items() if err2 & bit]
    media_kind = {0x0A: "continuous", 0x4A: "continuous", 0x0B: "die-cut", 0x4B: "die-cut"}.get(packet[11], "")
    status_type = STATUS_TYPES.get(packet[18], f"0x{packet[18]:02x}")
    phase = "printing" if packet[19] == 0x01 else "ready"
    state, message = "ready", "Ready"
    if errors:
        if any(e in errors for e in ("No media", "End of media", "Replace media", "Media cannot be fed")):
            state, message = "no_media", "Out of labels"
        elif "Cover open" in errors:
            state, message = "cover_open", "Cover open"
        elif "Cutter jam" in errors:
            state, message = "cutter_jam", "Cutter jam"
        elif "Printer turned off" in errors:
            state, message = "offline", "Printer turned off"
        else:
            state, message = "error", "Printer error: " + ", ".join(errors)
    elif packet[11] == 0x00 or packet[10] == 0:
        state, message = "no_media", "No label roll"
        errors = ["No media"]
    elif phase == "printing":
        state, message = "busy", "Printing"
    return PrinterStatus(
        ok=state in ("ready", "busy"), state=state, message=message, errors=errors,
        media_width=packet[10], media_length=packet[17], media_type=media_kind,
        model=MODEL_CODES.get(packet[4], f"0x{packet[4]:02x}"), status_type=status_type, phase=phase)


def check_media(status: PrinterStatus, media: Media) -> PrinterStatus:
    """Flag a loaded roll that does not match the configured media."""
    if not status.ok or not status.media_width:
        return status
    wrong_width = status.media_width != media.width_mm
    wrong_kind = status.media_type and status.media_type != ("die-cut" if media.die_cut else "continuous")
    wrong_length = media.die_cut and status.media_length and status.media_length != media.length_mm
    if wrong_width or wrong_kind or wrong_length:
        loaded = f"{status.media_width}mm" + (f" x {status.media_length}mm" if status.media_length else "")
        loaded += f" {status.media_type}" if status.media_type else ""
        status.ok = False
        status.state = "media_mismatch"
        status.message = f"Wrong label roll: {loaded} loaded, expected {media.description}"
    return status


def take_packets(data: bytes) -> tuple[list[bytes], bytes]:
    """Split a read buffer into 32-byte status packets (resynchronising on 0x80 0x20).

    Returns the complete packets and the unconsumed tail.
    """
    packets, i = [], 0
    while i + 32 <= len(data):
        if data[i] == 0x80 and data[i + 1] == 0x20:
            packets.append(data[i:i + 32])
            i += 32
        else:
            i += 1
    return packets, data[i:]


def decode_job(data: bytes) -> list:
    """Parse a raster job back into label images (strict). Used by the preview printer and tests,
    so every label shown in preview went through exactly the bytes a real printer receives."""
    from PIL import Image

    pages, lines = [], []
    expected = None
    compressed = False
    i, n = 0, len(data)
    while i < n:
        byte = data[i]
        if byte == 0x00:
            i += 1
        elif data[i:i + 2] == b"\x1b\x40":
            i += 2
        elif data[i:i + 3] == b"\x1b\x69\x53":
            i += 3
        elif data[i:i + 3] == b"\x1b\x69\x61":
            if data[i + 3] != 0x01:
                raise ValueError("Job does not switch the printer to raster mode")
            i += 4
        elif data[i:i + 3] == b"\x1b\x69\x7a":
            expected = int.from_bytes(data[i + 7:i + 11], "little")
            i += 13
        elif data[i:i + 3] in (b"\x1b\x69\x4d", b"\x1b\x69\x41", b"\x1b\x69\x4b"):
            i += 4
        elif data[i:i + 3] == b"\x1b\x69\x64":
            i += 5
        elif byte == 0x4d:
            compressed = data[i + 1] == 0x02
            i += 2
        elif byte == 0x67:
            length = data[i + 2]
            payload = data[i + 3:i + 3 + length]
            line = unpackbits(payload) if compressed else payload
            if len(line) != BYTES_PER_LINE:
                raise ValueError(f"Raster line has {len(line)} bytes")
            lines.append(line)
            i += 3 + length
        elif byte == 0x5a:
            lines.append(bytes(BYTES_PER_LINE))
            i += 1
        elif byte in (0x0c, 0x1a):
            if expected is not None and expected != len(lines):
                raise ValueError(f"Page declared {expected} lines but sent {len(lines)}")
            image = Image.frombytes("1", (HEAD_DOTS, len(lines)), b"".join(lines))
            image = image.transpose(Image.FLIP_LEFT_RIGHT)
            from PIL import ImageOps
            pages.append(ImageOps.invert(image.convert("L")))
            lines, expected = [], None
            i += 1
            if byte == 0x1a:
                break
        else:
            raise ValueError(f"Unexpected byte 0x{byte:02x} at offset {i}")
    return pages
