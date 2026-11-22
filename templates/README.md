# Label templates

The app copies these files into `data/templates/` the first time it runs. Edit the copies
there, or use **Admin → Labels**, which checks a template and shows a preview before saving.
You don't need to rebuild anything.

| File | Used for |
|------|----------|
| `name.json` | One name label per person checked in (adults and children) |
| `guardian.json` | Parent pickup tag. Only used if **pickup codes** are switched on in Settings |
| `test.json` | The **Test Print** button and the 100-label printer test |

## Format

```json
{
  "media": "62",          // label roll: 62, 29, 38, 50, 54 (continuous) or 62x29, 62x100, 29x90, 39x90 (die-cut)
  "length_mm": 90,        // continuous rolls only: total label length including the 3 mm feed margins
  "rotate": 90,           // 90 = text runs along the tape (landscape). 270 flips it. 0 = portrait.
  "elements": [ ... ]
}
```

All positions are millimetres from the top-left of the label **as you read it**. On a 62 mm roll
the printable height is 58.9 mm. On a continuous label the printable length is
`length_mm - 6`, for example 84 mm on a 90 mm label, or 69 mm on the default 75 mm name label.

Element types:

* `text`: `value` (with `{field}` placeholders), `x`, `y`, `width`, `height`, `size` (font size in mm),
  `bold`, `align` (`left`/`center`/`right`), `valign` (`top`/`middle`/`bottom`), `max_lines`,
  `invert` (white text on a black box), `upper`, `fit` (default `true`: shrinks the text until it fits),
  `min_scale` (smallest shrink, default `0.45` of `size`).
* `box`: `x`, `y`, `width`, `height`, `fill`, `thickness`, `radius`.
* `line`: `x1`, `y1`, `x2`, `y2`, `thickness`.

Any element can have `"if"` (only drawn when the field has a value) and/or `"unless"` (only drawn when it
is empty). Either can be one field or a list: `"unless": ["last_name", "code"]` means "only when both are
empty". The default name label uses this to print the name larger and centred when there's no last name.

## Fields

Name label: `name` (preferred name, or first name), `first_name`, `last_name`, `full_name`, `class` and `room`
(children with a class only), `age`, `school_year`, `alert` (shows "ALLERGY / MEDICAL" when flagged), `alert_note`
(a short note, only if "print alert note" is on), `visitor`, `code` (empty unless pickup codes are on), `date`,
`date_short`, `time`, `service`, `church`.

Pickup tag: `children`, `count`, `household`, `pickup_note`, `code`, `date`, `date_short`, `time`,
`service`, `church`.

Test label: `printer`, `number`, `media`, `date`, `time`, `church`.

Extra roster columns ticked in the import screen are available too, as `x_` plus the column name in
lower case with `_` for spaces: *Chinese Name* → `{x_chinese_name}`, *Primary Congregation* →
`{x_primary_congregation}`.

Phone numbers and full medical details are never available to labels.

Fonts: Arial (on Windows) is used by default. To use another font, put the `.ttf` file in
`data/templates/fonts/` and add `"font": "MyFont.ttf"` (or `"font_bold"`) to the template or to a single
element. For Chinese text later, use `"font": "C:/Windows/Fonts/msyh.ttc"`.
