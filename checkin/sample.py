"""Fake roster for testing and training, in the church database's column layout. No real people."""

import csv
import datetime as dt
import random

SURNAMES = ["Sample", "Example", "Test", "Demo", "Fictional", "Placeholder"]
ADULT_M = ["Alex", "Jordan", "Taylor", "Casey", "Riley", "Morgan", "Avery", "Jamie"]
ADULT_F = ["Alex", "Jordan", "Taylor", "Casey", "Riley", "Morgan", "Avery", "Jamie"]
KIDS = ["Sam", "Kit", "Robin", "Sky", "Ash", "Pip", "Cory", "River", "Quinn", "Lee"]
ALLERGIES = ["Peanuts", "Eggs - mild", "Asthma - inhaler in bag", "Shellfish", "Dairy", "G6PD deficiency"]


def _mobile(rng):
    return rng.choice("89") + "".join(rng.choice("0123456789") for _ in range(7))


def _fmt(rng, mobile):
    style = rng.randint(0, 4)
    return [mobile, f"+65 {mobile[:4]} {mobile[4:]}", f"{mobile[:4]}-{mobile[4:]}", f"65{mobile}",
            f"(+65) {mobile}"][style]


def families(count, seed=7):
    rng = random.Random(seed)
    today = dt.date.today()
    next_id = 30100000
    out = []
    shared_last4 = None
    for index in range(count):
        surname = rng.choice(SURNAMES)
        family_id = 5000 + index
        dad_mobile, mum_mobile = _mobile(rng), _mobile(rng)
        if index == 1:
            shared_last4 = dad_mobile[-4:]
        if index == 2 and shared_last4:
            dad_mobile = dad_mobile[:4] + shared_last4  # two households with the same last four digits
        adults = [
            {"first": rng.choice(ADULT_M), "role": "Head of Household", "mobile": dad_mobile, "sex": "Male"},
            {"first": rng.choice(ADULT_F), "role": "Spouse", "mobile": mum_mobile, "sex": "Female"},
        ]
        if index > 2 and rng.random() < 0.15:
            adults = adults[:1] if rng.random() < 0.5 else adults[1:]
        kids = []
        for _ in range(rng.choice([1, 1, 2, 2, 2, 3, 3, 4])):
            age_days = rng.randint(2 * 365, 13 * 365)
            born = today - dt.timedelta(days=age_days)
            kids.append({"first": rng.choice(KIDS), "born": born, "sex": rng.choice(["Male", "Female"]),
                         "allergy": rng.choice(ALLERGIES) if rng.random() < 0.12 else ""})
        family = {"id": family_id, "surname": surname, "adults": [], "kids": []}
        for adult in adults:
            next_id += rng.randint(1, 40)
            family["adults"].append(dict(adult, breeze_id=str(next_id)))
        for kid in kids:
            next_id += rng.randint(1, 40)
            family["kids"].append(dict(kid, breeze_id=str(next_id)))
        out.append(family)
    return out, rng


def school_year(born):
    today = dt.date.today()
    age = today.year - born.year
    if age <= 3:
        return "Nursery"
    if age <= 6:
        return f"K{min(2, max(1, age - 4))}"
    return f"P{min(6, age - 6)}"


def write_sample(path, count=120, fmt="people", seed=7) -> int:
    data, rng = families(count, seed)
    rows = []
    if fmt == "people":
        header = ["Breeze ID", "First Name", "Nickname", "Last Name", "Family ID", "Family Role", "Gender",
                  "Birthdate", "Grade", "Mobile", "Allergies", "Status"]
        for fam in data:
            for adult in fam["adults"]:
                rows.append([adult["breeze_id"], adult["first"], "", fam["surname"], fam["id"], adult["role"],
                             adult["sex"], "", "", _fmt(rng, adult["mobile"]), "", "Member"])
            for kid in fam["kids"]:
                rows.append([kid["breeze_id"], kid["first"], "", fam["surname"], fam["id"], "Child", kid["sex"],
                             kid["born"].strftime("%m/%d/%Y"), school_year(kid["born"]), "", kid["allergy"],
                             "Member"])
        # Deliberate problems for the validation screen.
        rows.append(rows[3][:])                                              # duplicate Breeze ID
        rows.append(["", "Nobody", "", "NoId", "9999", "Child", "", "", "", "", "", "Member"])  # missing ID
        rows.append(["39999998", "Wrong", "", "Number", "9998", "Head of Household", "", "", "", "12345", "",
                     "Member"])                                              # invalid phone
        rows.append(["39999999", "Lonely", "", "Orphan", "", "Child", "", "05/05/2018", "", "", "", "Member"])
    else:
        header = ["Breeze ID", "First Name", "Last Name", "Birthdate", "Class", "Parent Name", "Parent Breeze ID",
                  "Parent Mobile", "Allergies", "Authorised Pickup"]
        for fam in data:
            parent = fam["adults"][0]
            for kid in fam["kids"]:
                rows.append([kid["breeze_id"], kid["first"], fam["surname"], kid["born"].isoformat(),
                             school_year(kid["born"]), f"{parent['first']} {fam['surname']}", parent["breeze_id"],
                             _fmt(rng, parent["mobile"]), kid["allergy"], ""])
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return len(rows)
