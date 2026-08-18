"""Rebuild the staff birthday poster assets and data file.

Run this whenever new posters are designed or the staff birthday sheet changes.
It reads the name printed on each poster (OCR), the DOB from the staff birthday
Excel sheet and the WhatsApp number from the bot database, then writes
``app/static/birthday_posters/<slug>.jpg`` and ``app/data/staff_birthdays.json``.

Staff whose poster date disagrees with the sheet, who share a WhatsApp number
with a colleague, or who have no poster/number are written with a
``needs_review`` reason so the daily job skips them and alerts the admin
instead of messaging the wrong person.

Requires: pillow, pytesseract (plus the tesseract-ocr binary), openpyxl.

    python scripts/build_staff_birthday_data.py \
        --posters ~/posters --sheet "Staff Birthday Sheet.xlsx" --db /data/app.db
"""

import argparse
import json
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

import openpyxl
import pytesseract
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
POSTER_OUT = REPO_ROOT / "app" / "static" / "birthday_posters"
DATA_OUT = REPO_ROOT / "app" / "data" / "staff_birthdays.json"

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7, "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10,
    "NOV": 11, "DEC": 12,
}
TITLE_WORDS = ("MS", "MR", "MRS", "MISS", "DR")
# Spellings that refer to the same staff member across poster/sheet/database.
ALIASES = {
    "GAUTAM": "GAUTAM LUTHRA",
    "RITIKA DHAMILJA": "RITIKA DHAMIJA",
    "SANTOSH SINGH": "SANTOSH KUMAR SINGH",
    "DAMANPREET": "DAMANPREET KAUR",
    "EMMANUEL ODOLE": "ODOLE OPEYEMI EMMANUEL",
    "POONAM SINGH": "POONAM",
}


def norm(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "")
    name = re.sub(r"[^A-Za-z ]", " ", name).upper()
    cleaned = " ".join(p for p in name.split() if p and p not in TITLE_WORDS)
    return ALIASES.get(cleaned, cleaned)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm(name).lower()).strip("_")


def title_case(name: str) -> str:
    return " ".join(word.capitalize() for word in norm(name).split())


def read_poster(path: Path, name_override: str = "") -> tuple[str, str, str]:
    """OCR a poster and return (title, NAME, MM-DD printed on the artwork)."""
    image = Image.open(path)
    width, height = image.size
    raw_name = name_override or pytesseract.image_to_string(
        image.crop((0, int(height * 0.70), width, int(height * 0.81)))
    )
    raw_date = pytesseract.image_to_string(
        image.crop((int(width * 0.6), int(height * 0.08), width, int(height * 0.18))),
        config="--psm 7",
    )

    upper = raw_name.upper()
    start = max((upper.rfind(word + "."), word) for word in TITLE_WORDS)[0]
    if start >= 0:
        upper = upper[start:]
    title_match = re.match(r"(MS|MR|MRS|MISS|DR)\.", upper)
    title = title_match.group(1).capitalize() + "." if title_match else ""

    date_match = re.search(r"(\d{1,2})\s*-?\s*([A-Z]{3,4})", raw_date.upper().replace("O1", "01"))
    printed = ""
    if date_match:
        month = MONTHS.get(date_match.group(2)[:4]) or MONTHS.get(date_match.group(2)[:3])
        if month:
            printed = f"{month:02d}-{int(date_match.group(1)):02d}"
    return title, norm(upper), printed


def load_sheet(path: Path) -> dict[str, dict[str, str]]:
    worksheet = openpyxl.load_workbook(path)["Sheet1"]
    staff = {}
    for name, department, designation, dob in worksheet.iter_rows(values_only=True):
        if not name or not isinstance(dob, datetime):
            continue
        staff[norm(name)] = {
            "dob": f"{dob.month:02d}-{dob.day:02d}",
            "department": (department or "").strip(),
            "designation": (designation or "").strip(),
        }
    return staff


def load_phones(db_path: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT name, phone FROM trueface_teachers").fetchall()
    phones: dict[str, str] = {}
    owners: dict[str, set[str]] = {}
    for name, phone in rows:
        digits = re.sub(r"\D", "", str(phone or ""))
        if len(digits) != 12:
            continue
        phones[norm(name)] = digits
        owners.setdefault(digits, set()).add(norm(name))
    return phones, owners


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posters", required=True, type=Path)
    parser.add_argument("--sheet", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        help="poster filenames to ignore (e.g. duplicates)",
    )
    parser.add_argument(
        "--name",
        nargs="*",
        default=[],
        metavar="FILE=NAME",
        help='names OCR cannot read, e.g. 34.png="MS. CHARU CHAUDHARY"',
    )
    args = parser.parse_args()
    name_overrides = dict(pair.split("=", 1) for pair in args.name)

    sheet = load_sheet(args.sheet)
    phones, owners = load_phones(args.db)

    if POSTER_OUT.exists():
        shutil.rmtree(POSTER_OUT)
    POSTER_OUT.mkdir(parents=True)

    staff: list[dict[str, str]] = []
    for poster_path in sorted(args.posters.glob("*.png")) + sorted(
        args.posters.glob("*.jpg")
    ):
        if poster_path.name in args.skip:
            continue
        title, name, printed = read_poster(
            poster_path, name_overrides.get(poster_path.name, "")
        )
        if not name:
            print(f"  ! could not read a name on {poster_path.name}; skipped")
            continue

        row = sheet.get(name, {})
        dob = row.get("dob", "")
        review: list[str] = []
        notes: list[str] = []
        if not dob and printed:
            dob = printed
            notes.append("not in the staff sheet; DOB read from the poster")
        elif not dob:
            review.append("no DOB in the staff birthday sheet")
        elif printed and printed != dob:
            review.append(f"poster shows {printed} but the sheet says {dob}")

        phone = phones.get(name, "")
        if not phone:
            review.append("no WhatsApp number on record")
        elif len(owners.get(phone, set())) > 1:
            others = sorted(n for n in owners[phone] if n != name)
            review.append(
                "number is shared with " + ", ".join(title_case(o) for o in others)
            )

        asset = f"{slug(name)}.jpg"
        Image.open(poster_path).convert("RGB").save(
            POSTER_OUT / asset, "JPEG", quality=88, optimize=True
        )
        staff.append(
            {
                "name": title_case(name),
                "display_name": f"{title} {title_case(name)}".strip(),
                "dob": dob,
                "phone": phone,
                "poster": asset,
                "designation": row.get("designation", ""),
                "department": row.get("department", ""),
                "needs_review": "; ".join(review),
                "note": "; ".join(notes),
            }
        )

    postered = {norm(member["name"]) for member in staff}
    for name, row in sheet.items():
        if name in postered:
            continue
        staff.append(
            {
                "name": title_case(name),
                "display_name": title_case(name),
                "dob": row["dob"],
                "phone": phones.get(name, ""),
                "poster": "",
                "designation": row["designation"],
                "department": row["department"],
                "needs_review": "no birthday poster available",
                "note": "",
            }
        )

    staff.sort(key=lambda member: (member["dob"] or "99-99", member["name"]))
    DATA_OUT.write_text(json.dumps(staff, indent=2, ensure_ascii=False) + "\n")

    ready = [m for m in staff if not m["needs_review"]]
    print(f"{len(staff)} staff written to {DATA_OUT}, {len(ready)} ready to auto-send")
    for member in staff:
        if member["needs_review"]:
            print(f"  REVIEW {member['name']:26} {member['dob'] or '  -  '}  {member['needs_review']}")


if __name__ == "__main__":
    main()
