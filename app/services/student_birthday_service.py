"""Student birthday wishes to parents.

Birthdays come from the DOB column of the live PI Sheet (written into
``student_birthdays`` by the daily student sync), and the wish goes out on the
Meta Cloud API as an approved template, because a parent who has not written
to the bot in the last day cannot be sent free text.

Every send is claimed in ``student_birthday_log`` first, so a retry, a second
worker or a restart cannot wish the same child twice on the same day.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.database import get_db
from app.services import whatsapp_service

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

STUDENT_BIRTHDAY_ENABLED = os.getenv("STUDENT_BIRTHDAY_ENABLED", "1") == "1"
WISH_TEMPLATE = os.getenv(
    "STUDENT_BIRTHDAY_TEMPLATE", "ppis_student_birthday_wish"
)
ADMIN_PHONE = os.getenv("STUDENT_BIRTHDAY_ADMIN_PHONE", "918076455224")
# A wish is worth nothing if it arrives at midnight, and worth less if it
# floods one number: parents get it in the morning, one message per number.
SEND_GAP_SECONDS = float(os.getenv("STUDENT_BIRTHDAY_SEND_GAP", "1.5"))

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _valid(year: int, month: int, day: int) -> str:
    """Return the ISO date if it is a plausible school-age birth date."""
    if year < 1990 or year > date.today().year:
        return ""
    try:
        return date(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_dob(raw: str) -> str:
    """Read a PI Sheet date of birth as ``YYYY-MM-DD``, or '' if unreadable.

    The sheet carries at least five shapes, entered by different hands over
    the years: ``24.02.2018``, ``4-Apr-13``, ``11/09/2009``, ``18-09-2008``
    and ``2017-04-01``. Numeric dates are read day first, as the sheet is
    filled in India, unless only month-first can be a real date.
    """
    text = (raw or "").strip()
    # Some cells carry a note beside the date, e.g. "02.04.2016 ( New)".
    text = re.sub(r"\(.*?\)", "", text).strip()
    if not text:
        return ""

    named = re.fullmatch(
        r"(\d{1,2})[-/. ]*([A-Za-z]{3,9})[-/. ]*(\d{2}|\d{4})", text
    )
    if named:
        day = int(named.group(1))
        month = _MONTHS.get(named.group(2)[:4].lower().rstrip("."), 0) or _MONTHS.get(
            named.group(2)[:3].lower(), 0
        )
        year = int(named.group(3))
        if not month:
            return ""
        if year < 100:
            year += 2000 if year <= date.today().year % 100 else 1900
        return _valid(year, month, day)

    parts = re.split(r"[-/.\s]+", text)
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ""

    if len(parts[0]) == 4:
        return _valid(int(parts[0]), int(parts[1]), int(parts[2]))

    first, second = int(parts[0]), int(parts[1])
    year = int(parts[2])
    if year < 100:
        year += 2000 if year <= date.today().year % 100 else 1900
    if first > 12 >= second:
        return _valid(year, second, first)
    if second > 12 >= first:
        return _valid(year, first, second)
    return _valid(year, second, first)


def normalize_phone(phone: str) -> str:
    """One dialable 91XXXXXXXXXX number, or '' when the cell cannot give one."""
    first = str(phone or "").split("/")[0].split(",")[0].split("&")[0]
    digits = re.sub(r"\D", "", first)
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return ""


def display_name(student_name: str) -> str:
    """The child's name as a parent would like to read it."""
    name = " ".join(str(student_name or "").split())
    if name.isupper() or name.islower():
        return name.title()
    return name


def recipients(student: dict) -> list[str]:
    """Both parents' numbers where we have them, without sending twice."""
    out: list[str] = []
    for raw in (student.get("father_phone"), student.get("mother_phone")):
        phone = normalize_phone(raw or "")
        if phone and phone not in out:
            out.append(phone)
    return out


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )


async def birthdays_on(day: date) -> list[dict]:
    """Students whose date of birth falls on this day and month."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT student_name, grade, dob, father_phone, mother_phone "
            "FROM student_birthdays WHERE substr(dob, 6, 5) = ? "
            "ORDER BY grade, student_name",
            (day.strftime("%m-%d"),),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return [
        {
            "student_name": row[0],
            "grade": row[1],
            "dob": row[2],
            "father_phone": row[3],
            "mother_phone": row[4],
        }
        for row in rows
    ]


async def _claim(student: dict, phone: str, wish_date: str, now: datetime) -> bool:
    """Reserve today's wish for this child and number; False if already taken."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO student_birthday_log "
            "(student_name, grade, wish_date, phone, claimed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                student["student_name"],
                student["grade"],
                wish_date,
                phone,
                _timestamp(now),
            ),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


async def _finish(
    student: dict, phone: str, wish_date: str, message_id: str, now: datetime
) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE student_birthday_log SET status = ?, wa_message_id = ?, "
            "status_updated_at = ? WHERE student_name = ? AND grade = ? "
            "AND wish_date = ? AND phone = ?",
            (
                "sent",
                message_id,
                _timestamp(now),
                student["student_name"],
                student["grade"],
                wish_date,
                phone,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def _release(student: dict, phone: str, wish_date: str) -> None:
    """Drop a claim so a later run can try this number again."""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM student_birthday_log WHERE student_name = ? "
            "AND grade = ? AND wish_date = ? AND phone = ?",
            (student["student_name"], student["grade"], wish_date, phone),
        )
        await db.commit()
    finally:
        await db.close()


async def _notify_admin(wish_date: str, summary: dict) -> None:
    """Tell the office only about the children the bot could not wish."""
    no_number = summary["no_number"]
    failed = summary["failed"]
    if not (no_number or failed) or not ADMIN_PHONE:
        return
    lines = [f"Birthday wishes needing attention today ({wish_date}):", ""]
    for item in no_number:
        lines.append(f"- {item['name']} ({item['grade']}): no parent number on file")
    for item in failed:
        lines.append(
            f"- {item['name']} ({item['grade']}): WhatsApp refused the wish"
        )
    lines += ["", "Please wish them by hand or correct the PI Sheet."]
    try:
        await whatsapp_service.send_cloud_text(ADMIN_PHONE, "\n".join(lines))
    except Exception:
        logger.exception("Unable to report unsent student birthday wishes")


async def send_birthday_wishes(
    now: datetime | None = None, dry_run: bool = False
) -> dict:
    """Wish every child whose birthday is today, to both parents' numbers."""
    current = (now or datetime.now(IST)).astimezone(IST)
    today = current.date()
    wish_date = today.strftime("%Y-%m-%d")
    summary: dict = {
        "date": wish_date,
        "template": WISH_TEMPLATE,
        "dry_run": dry_run,
        "students": 0,
        "sent": [],
        "failed": [],
        "no_number": [],
        "already_sent": [],
    }

    if not STUDENT_BIRTHDAY_ENABLED and not dry_run:
        logger.info("Student birthday wishes disabled")
        summary["disabled"] = True
        return summary

    students = await birthdays_on(today)
    summary["students"] = len(students)

    for student in students:
        name = display_name(student["student_name"])
        grade = student["grade"] or ""
        entry = {"name": name, "grade": grade}
        phones = recipients(student)
        if not phones:
            summary["no_number"].append(entry)
            logger.warning(
                "No parent number for %s (%s), birthday wish not sent", name, grade
            )
            continue

        for phone in phones:
            if dry_run:
                summary["sent"].append({**entry, "phone": phone})
                continue

            if not await _claim(student, phone, wish_date, current):
                summary["already_sent"].append({**entry, "phone": phone})
                continue

            try:
                sent = await whatsapp_service.send_cloud_template_message(
                    to=phone,
                    template_name=WISH_TEMPLATE,
                    language_code="en",
                    body_params=[name, grade],
                )
            except Exception:
                logger.exception("Birthday wish to %s failed for %s", phone, name)
                sent = False

            if sent:
                await _finish(
                    student,
                    phone,
                    wish_date,
                    whatsapp_service.last_cloud_template_message_id,
                    datetime.now(IST),
                )
                summary["sent"].append({**entry, "phone": phone})
                logger.info("Birthday wish sent for %s to %s", name, phone[-4:])
            else:
                await _release(student, phone, wish_date)
                if not any(f["name"] == name for f in summary["failed"]):
                    summary["failed"].append(entry)

            if SEND_GAP_SECONDS > 0:
                await asyncio.sleep(SEND_GAP_SECONDS)

    if not dry_run:
        await _notify_admin(wish_date, summary)

    logger.info(
        "Student birthday run %s: %d children, sent=%d failed=%d "
        "no_number=%d already=%d",
        wish_date,
        summary["students"],
        len(summary["sent"]),
        len(summary["failed"]),
        len(summary["no_number"]),
        len(summary["already_sent"]),
    )
    return summary


def send_birthday_wishes_sync() -> None:
    """Scheduler entrypoint: run today's student wishes in a fresh loop."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(send_birthday_wishes())
    except Exception:
        logger.exception("Student birthday run failed")
    finally:
        loop.close()
