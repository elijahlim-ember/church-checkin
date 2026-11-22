"""Label templates (JSON files in data/templates) rendered to bitmaps with Pillow.

A template is a list of elements positioned in millimetres on the label as you
read it (text running along the tape). Fields such as {name} or {code} are
filled from the check-in. See templates/README.md for the format.
"""

import json
import os
import re
import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .printing.raster import MEDIA, mm_to_dots

FONT_CANDIDATES = {
    "bold": [
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ],
    "regular": [
        "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ],
    # For Simplified Chinese labels later.
    "cjk": ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc"],
}

FIELD_RE = re.compile(r"\{([a-z_0-9]+)\}")

SAMPLE_DATA = {
    "name": {
        "name": "Alex", "first_name": "Alex", "last_name": "Sample", "full_name": "Alex Sample",
        "class": "Sample Group", "room": "Room 1", "age": "Age 7", "school_year": "P1",
        "alert": "ALLERGY / MEDICAL", "alert_note": "Peanuts - EpiPen in bag", "visitor": "",
        "code": "", "date": "Sun 25 Oct 2026", "date_short": "25 Oct", "time": "09:24",
        "service": "Sample Sunday Service", "church": "Your Church",
    },
    "guardian": {
        "children": "Alex, Jordan, Sam", "count": "3", "household": "Sample", "pickup_note": "",
        "code": "K7XP", "date": "Sun 25 Oct 2026", "date_short": "25 Oct", "time": "09:24",
        "service": "Sample Children’s Service", "church": "Your Church",
    },
    "test": {"printer": "TEST-PRINTER", "number": "1", "date": "Sun 25 Oct 2026", "time": "09:24",
             "church": "Your Church", "media": "62mm continuous"},
}


class TemplateError(Exception):
    pass


class LabelRenderer:
    def __init__(self, template_dir: Path, font_dir: Path | None = None):
        self.template_dir = Path(template_dir)
        self.font_dir = font_dir or (self.template_dir / "fonts")
        self._cache: dict[str, tuple[float, dict]] = {}
        self._fonts: dict[tuple, ImageFont.FreeTypeFont] = {}
        self._lock = threading.Lock()

    # ---- templates ------------------------------------------------------------
    def names(self) -> list[str]:
        return sorted(p.stem for p in self.template_dir.glob("*.json"))

    def path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", name or ""):
            raise TemplateError("Template names may only use lower-case letters, digits, - and _.")
        return self.template_dir / f"{name}.json"

    def load(self, name: str) -> dict:
        path = self.path(name)
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise TemplateError(f"Label template '{name}' is missing.") from exc
        with self._lock:
            cached = self._cache.get(name)
            if cached and cached[0] == mtime:
                return cached[1]
        template = self.parse(path.read_text(encoding="utf-8"))
        with self._lock:
            self._cache[name] = (mtime, template)
        return template

    def parse(self, text: str) -> dict:
        try:
            template = json.loads(text)
        except ValueError as exc:
            raise TemplateError(f"Template is not valid JSON: {exc}") from exc
        if not isinstance(template, dict) or not isinstance(template.get("elements"), list):
            raise TemplateError("Template must be an object with an 'elements' list.")
        if template.get("media", "62") not in MEDIA:
            raise TemplateError(f"Unknown media '{template.get('media')}'. Use one of: {', '.join(MEDIA)}")
        for index, element in enumerate(template["elements"]):
            if element.get("type") not in ("text", "box", "line"):
                raise TemplateError(f"Element {index + 1}: type must be text, box or line.")
        return template

    def save(self, name: str, text: str) -> None:
        template = self.parse(text)
        self.render(template, SAMPLE_DATA.get(name, SAMPLE_DATA["name"]))  # must render cleanly
        tmp = self.path(name).with_suffix(".tmp")
        tmp.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path(name))

    # ---- fonts --------------------------------------------------------------------
    def font(self, template: dict, element: dict, size_px: int) -> ImageFont.FreeTypeFont:
        weight = "bold" if element.get("bold") else "regular"
        custom = element.get("font") or template.get("font_bold" if weight == "bold" else "font")
        key = (custom, weight, size_px)
        with self._lock:
            if key in self._fonts:
                return self._fonts[key]
        candidates = []
        if custom:
            candidates += [str(self.font_dir / custom), custom]
        candidates += FONT_CANDIDATES[weight] + FONT_CANDIDATES["regular"]
        font = None
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                try:
                    font = ImageFont.truetype(candidate, size_px)
                    break
                except OSError:
                    continue
        if font is None:
            font = ImageFont.load_default(size=size_px)
        with self._lock:
            self._fonts[key] = font
        return font

    # ---- rendering --------------------------------------------------------------------
    def render_named(self, name: str, data: dict) -> Image.Image:
        return self.render(self.load(name), data)

    def render(self, template: dict, data: dict) -> Image.Image:
        """Return a grayscale image sized for the printer: width = printable dots across the tape."""
        media = MEDIA[template.get("media", "62")]
        across = media.printable_dots
        if media.die_cut:
            along = media.length_dots
        else:
            from .printing.raster import continuous_lines
            along = continuous_lines(float(template.get("length_mm", 90)), media)
        canvas = Image.new("L", (along, across), 255)  # as read: text runs along the tape
        draw = ImageDraw.Draw(canvas)
        def filled(field):
            return bool(str(data.get(field, "") or "").strip())

        def as_list(value):
            return [value] if isinstance(value, str) else list(value or [])

        for element in template["elements"]:
            # "if": field or [fields] that must all have a value; "unless": fields that must all be empty.
            if not all(filled(f) for f in as_list(element.get("if"))):
                continue
            if any(filled(f) for f in as_list(element.get("unless"))):
                continue
            kind = element["type"]
            if kind == "box":
                self._box(draw, element)
            elif kind == "line":
                self._line(draw, element)
            else:
                self._text(draw, template, element, data)
        rotation = int(template.get("rotate", 90))
        return canvas.rotate(rotation, expand=True) if rotation else canvas

    @staticmethod
    def _px(value) -> int:
        return mm_to_dots(float(value or 0))

    def _box(self, draw, el) -> None:
        x, y = self._px(el.get("x")), self._px(el.get("y"))
        w, h = self._px(el.get("width")), self._px(el.get("height"))
        radius = self._px(el.get("radius", 0))
        fill = 0 if el.get("fill") else None
        outline = 0 if not el.get("fill") or el.get("outline") else None
        width = max(1, self._px(el.get("thickness", 0.4)))
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill, outline=outline, width=width)

    def _line(self, draw, el) -> None:
        points = [self._px(el.get(k)) for k in ("x1", "y1", "x2", "y2")]
        draw.line(points, fill=0, width=max(1, self._px(el.get("thickness", 0.4))))

    def _text(self, draw, template, el, data) -> None:
        text = FIELD_RE.sub(lambda m: str(data.get(m.group(1), "") or ""), str(el.get("value", "")))
        if el.get("upper"):
            text = text.upper()
        text = text.strip()
        if not text:
            return
        x, y = self._px(el.get("x")), self._px(el.get("y"))
        w = self._px(el.get("width", 50))
        h = self._px(el.get("height", 10))
        size = self._px(el.get("size", 5))
        min_size = max(8, int(size * float(el.get("min_scale", 0.45))))
        max_lines = int(el.get("max_lines", 1))
        lines, font = self._fit(template, el, text, w, h, size, min_size, max_lines)
        if el.get("invert"):
            draw.rounded_rectangle([x, y, x + w, y + h], radius=self._px(el.get("radius", 1)), fill=0)
        color = 255 if el.get("invert") else 0
        line_h = int(font.size * float(el.get("line_spacing", 1.12)))
        block_h = line_h * (len(lines) - 1) + self._ink_height(font)
        valign = el.get("valign", "middle")
        if valign == "top":
            top = y
        elif valign == "bottom":
            top = y + h - block_h
        else:
            top = y + (h - block_h) // 2
        pad = self._px(el.get("padding", 1.2 if el.get("invert") else 0))
        for index, line in enumerate(lines):
            line_w = draw.textlength(line, font=font)
            align = el.get("align", "left")
            if align == "center":
                left = x + (w - line_w) / 2
            elif align == "right":
                left = x + w - line_w - pad
            else:
                left = x + pad
            ascent_offset = font.getbbox("Hg")[1]
            draw.text((left, top + index * line_h - ascent_offset), line, font=font, fill=color)

    def _ink_height(self, font) -> int:
        box = font.getbbox("Hg")
        return box[3] - box[1]

    def _fit(self, template, el, text, w, h, size, min_size, max_lines):
        pad = self._px(el.get("padding", 1.2 if el.get("invert") else 0)) * 2
        size_px = size
        while True:
            font = self.font(template, el, size_px)
            lines = self._wrap(text, font, w - pad, max_lines)
            line_h = int(font.size * float(el.get("line_spacing", 1.12)))
            height = line_h * (len(lines) - 1) + self._ink_height(font)
            widest = max(font.getlength(line) for line in lines)
            fits = widest <= w - pad and height <= h
            if fits or size_px <= min_size or not el.get("fit", True):
                if not fits:
                    lines = [self._ellipsize(line, font, w - pad) for line in lines]
                return lines, font
            size_px = max(min_size, int(size_px * 0.92))

    @staticmethod
    def _wrap(text, font, width, max_lines) -> list[str]:
        if max_lines <= 1:
            return [text]
        words, lines, current = text.split(), [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if font.getlength(trial) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) > max_lines:
            lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
        return lines

    @staticmethod
    def _ellipsize(line, font, width) -> str:
        if font.getlength(line) <= width:
            return line
        while line and font.getlength(line + "…") > width:
            line = line[:-1]
        return line.rstrip() + "…"
