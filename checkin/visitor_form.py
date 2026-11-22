"""The visitor form's optional answers ("First time here?"), and how they go to the church database.

Only the name and mobile are needed. Check-in keeps the Chinese name (shown and searchable) and gender on the
person. The other answers (email, age group, preferred language, what brings them, whether and how church may
get in touch) are held in person.visitor_details only until the church database confirms it has saved them;
the next sync then wipes them here. A yes to "Can someone from church get in touch?" is consent, dated today.

The codes and field ids match the church database's connector (compatible service connector/shape.py, VISITOR_DETAILS).
"""

import json
import re

from .util import clean_text, today_iso

CHOICES = {
    "age_group": ("under20", "20s30s", "40s50s", "60plus"),
    "preferred_language": ("en", "zh", "both"),
    "visit_reason": ("church", "visiting", "new"),
    "wants_contact": ("yes", "no"),
    "contact_by": ("whatsapp", "call", "email"),
}
SEXES = {"male": "Male", "m": "Male", "female": "Female", "f": "Female"}
CHINESE_NAME_MAX = 40
EMAIL_MAX = 254
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")
BAD_EMAIL = "Enter a valid email address, or leave it blank."
EMAIL_NEEDED = "Enter your email address, or choose another way to get in touch."

# The connector's field ids for people/add. 1004 (Chinese name) is also in its profile: that is how check-in
# knows the connector takes these answers. An older connector without it is sent only the mobile.
CHINESE_NAME_FIELD = "1004"
FIELD_IDS = {"chinese_name": "1004", "email": "1005", "sex": "1006", "age_group": "1007",
             "preferred_language": "1008", "visit_reason": "1009", "wants_contact": "1010", "contact_by": "1011",
             "contact_consent_on": "1012"}
SEX_CODES = {"Male": "M", "Female": "F"}


class FormError(ValueError):
    pass


def chinese_name(value) -> str | None:
    """A Chinese name as kept: trimmed, single-spaced, at most 40 characters."""
    return " ".join(clean_text(value).split())[:CHINESE_NAME_MAX] or None


def sex(value) -> str | None:
    return SEXES.get(clean_text(value).lower())


def read(form: dict) -> dict:
    """The person columns the form's answers fill: chinese_name, sex and visitor_details (JSON, or None when
    nothing else was answered). Answers that aren't one of the choices are left out; a wrong email is refused."""
    email = clean_text(form.get("email")).lower()
    if email and (len(email) > EMAIL_MAX or not EMAIL_RE.match(email)):
        raise FormError(BAD_EMAIL)
    answers = {"email": email} if email else {}
    for name, codes in CHOICES.items():
        value = clean_text(form.get(name)).lower()
        if value in codes:
            answers[name] = value
    if answers.get("wants_contact") == "yes":
        if answers.get("contact_by") == "email" and not email:
            raise FormError(EMAIL_NEEDED)
        answers["contact_consent_on"] = today_iso()
    else:
        answers.pop("contact_by", None)
    return {"chinese_name": chinese_name(form.get("chinese_name")), "sex": sex(form.get("sex")),
            "visitor_details": json.dumps(answers, ensure_ascii=False) if answers else None}


def takes_answers(profile: list[dict]) -> bool:
    """True when the connector's profile has the Chinese name field, so it takes the visitor form's answers."""
    return any(str(f.get("field_id")) == CHINESE_NAME_FIELD for section in profile or []
               for f in section.get("fields") or [])


def connector_fields(person: dict) -> list[dict]:
    """The answers as people/add text fields: the Chinese name, gender and the held answers."""
    values = json.loads(person.get("visitor_details") or "{}")
    values["chinese_name"] = person.get("chinese_name")
    values["sex"] = SEX_CODES.get(person.get("sex") or "")
    return [{"field_id": field_id, "field_type": "text", "response": str(values[name])}
            for name, field_id in FIELD_IDS.items() if values.get(name)]


def confirmed(reply, sent=None) -> bool:
    """True when the connector's reply says it saved every answer that was sent (versions from 4 Nov 2026 reply
    with `saved_fields`, the field ids it accepted). A reply that leaves one out, or lists none, is not a
    confirmation: the connector drops answers it can't use, such as an email its validator refuses, and check-in
    must keep what wasn't saved. `sent` is the fields list that was sent; without it any list counts."""
    items = reply if isinstance(reply, list) else [reply]
    saved = next((i["saved_fields"] for i in items if isinstance(i, dict) and isinstance(i.get("saved_fields"), list)),
                 None)
    if saved is None:
        return False
    if sent is None:
        return True
    answer_ids = set(FIELD_IDS.values())
    wanted = {str(f.get("field_id")) for f in sent if str(f.get("field_id")) in answer_ids}
    return wanted <= {str(field_id) for field_id in saved}


def not_saved(reply, sent) -> list[str]:
    """The names of the answers that were sent but not reported saved (for the sync's problem list)."""
    items = reply if isinstance(reply, list) else [reply]
    saved = {str(x) for i in items if isinstance(i, dict) and isinstance(i.get("saved_fields"), list)
             for x in i["saved_fields"]}
    names = {field_id: name for name, field_id in FIELD_IDS.items()}
    return [names[str(f.get("field_id"))] for f in sent
            if str(f.get("field_id")) in names and str(f.get("field_id")) not in saved]
