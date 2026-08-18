"""Staff birthday wishes: sends each staff member their poster on their birthday.

Posters are pre-designed artwork committed under ``app/static/birthday_posters``
and matched to a staff member in ``app/data/staff_birthdays.json``. Every day at
the configured IST time the poster for that day's birthdays is delivered through
the Meta Cloud API image-header template, once per staff member per year.
"""

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.database import get_db
from app.services import whatsapp_service

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
STAFF_BIRTHDAY_ENABLED = os.getenv("STAFF_BIRTHDAY_ENABLED", "1") == "1"
WISH_TEMPLATE = os.getenv("STAFF_BIRTHDAY_TEMPLATE", "ppis_staff_birthday_wish")
POSTER_BASE_URL = os.getenv(
    "STAFF_BIRTHDAY_POSTER_BASE_URL",
    "https://ppis-whatsapp-bot.fly.dev/static/birthday_posters",
).rstrip("/")
ADMIN_PHONE = os.getenv("STAFF_BIRTHDAY_ADMIN_PHONE", "918076455224")

_DATA_PATH = Path(__file__).parents[1] / "data" / "staff_birthdays.json"
_POSTER_DIR = Path(__file__).parents[1] / "static" / "birthday_posters"


def _data_path() -> Path:
    override = os.getenv("STAFF_BIRTHDAY_FILE", "").strip()
    return Path(override) if override else _DATA_PATH


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 10:
        return f"91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return ""


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).strftime("%d-%m-%Y %H:%M:%S IST")


def load_staff() -> list[dict[str, str]]:
    """Load the staff birthday records, dropping entries without a name."""
    path = _data_path()
    try:
        with path.open(encoding="utf-8") as data_file:
            records = json.load(data_file)
    except FileNotFoundError:
        logger.warning("Staff birthday file is missing: %s", path)
        return []
    except (OSError, ValueError):
        logger.exception("Unable to load staff birthday file %s", path)
        return []
    if not isinstance(records, list):
        logger.warning("Staff birthday file must contain a JSON list: %s", path)
        return []

    staff = []
    for record in records:
        if not isinstance(record, dict) or not str(record.get("name", "")).strip():
            continue
        name = str(record["name"]).strip()
        staff.append(
            {
                "name": name,
                "display_name": str(record.get("display_name") or name).strip(),
                "dob": str(record.get("dob", "")).strip(),
                "phone": _normalize_phone(record.get("phone", "")),
                "poster": str(record.get("poster", "")).strip(),
                "designation": str(record.get("designation", "")).strip(),
                "needs_review": str(record.get("needs_review", "")).strip(),
                "note": str(record.get("note", "")).strip(),
            }
        )
    return staff


def poster_url(staff: dict[str, str]) -> str:
    return f"{POSTER_BASE_URL}/{staff['poster']}" if staff.get("poster") else ""


def poster_exists(staff: dict[str, str]) -> bool:
    return bool(staff.get("poster")) and (_POSTER_DIR / staff["poster"]).is_file()


def birthdays_on(day: date) -> list[dict[str, str]]:
    """Staff whose birthday falls on ``day`` (month and day only)."""
    key = day.strftime("%m-%d")
    return [s for s in load_staff() if s["dob"] == key]


def blocking_reason(staff: dict[str, str]) -> str:
    """Why this staff member cannot be wished automatically, else ''."""
    if staff["needs_review"]:
        return staff["needs_review"]
    if not staff["phone"]:
        return "no WhatsApp number on record"
    if not staff["poster"]:
        return "no birthday poster available"
    if not poster_exists(staff):
        return f"poster file {staff['poster']} is missing from the app"
    return ""


async def _claim_wish(staff: dict[str, str], wish_date: str, now: datetime) -> bool:
    """Reserve today's wish for this staff member; False if already claimed."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO staff_birthday_log "
            "(staff_name, wish_date, phone, claimed_at) VALUES (?, ?, ?, ?)",
            (staff["name"], wish_date, staff["phone"], _timestamp(now)),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


async def _finish_wish(
    staff: dict[str, str],
    wish_date: str,
    sent: bool,
    now: datetime,
    message_id: str,
) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE staff_birthday_log SET status = ?, wa_message_id = ?, "
            "status_updated_at = ? WHERE staff_name = ? AND wish_date = ?",
            (
                "sent" if sent else "failed",
                message_id if sent else "",
                _timestamp(now),
                staff["name"],
                wish_date,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def _release_wish(staff: dict[str, str], wish_date: str) -> None:
    """Drop a claim so a later run can retry (used for failed sends)."""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM staff_birthday_log WHERE staff_name = ? AND wish_date = ?",
            (staff["name"], wish_date),
        )
        await db.commit()
    finally:
        await db.close()


async def _notify_admin(wish_date: str, skipped: list[dict[str, str]]) -> None:
    if not skipped or not ADMIN_PHONE:
        return
    lines = [
        f"Birthday wishes could not be sent automatically today ({wish_date}):",
        "",
    ]
    lines += [f"- {item['name']}: {item['reason']}" for item in skipped]
    lines.append("")
    lines.append("Please wish them manually or share the missing details.")
    try:
        await whatsapp_service.send_cloud_text(ADMIN_PHONE, "\n".join(lines))
    except Exception:
        logger.exception("Unable to alert admin about skipped birthday wishes")


async def send_birthday_wishes(
    now: datetime | None = None, dry_run: bool = False
) -> dict:
    """Send today's staff birthday posters. Returns a per-staff summary."""
    current = (now or datetime.now(IST)).astimezone(IST)
    today = current.date()
    wish_date = today.strftime("%Y-%m-%d")
    summary: dict = {
        "date": wish_date,
        "template": WISH_TEMPLATE,
        "dry_run": dry_run,
        "sent": [],
        "failed": [],
        "skipped": [],
        "already_sent": [],
    }

    if not STAFF_BIRTHDAY_ENABLED and not dry_run:
        logger.info("Staff birthday wishes disabled")
        summary["disabled"] = True
        return summary

    for staff in birthdays_on(today):
        entry = {
            "name": staff["name"],
            "display_name": staff["display_name"],
            "phone": staff["phone"],
            "poster_url": poster_url(staff),
        }
        reason = blocking_reason(staff)
        if reason:
            summary["skipped"].append({**entry, "reason": reason})
            logger.warning(
                "Skipping birthday wish for %s: %s", staff["name"], reason
            )
            continue

        if dry_run:
            summary["sent"].append(entry)
            continue

        if not await _claim_wish(staff, wish_date, current):
            summary["already_sent"].append(entry)
            continue

        try:
            sent = await whatsapp_service.send_cloud_template_message(
                to=staff["phone"],
                template_name=WISH_TEMPLATE,
                language_code="en",
                body_params=[staff["display_name"]],
                header_image_url=poster_url(staff),
            )
        except Exception:
            logger.exception("Birthday wish failed for %s", staff["name"])
            sent = False

        if sent:
            await _finish_wish(
                staff,
                wish_date,
                True,
                datetime.now(IST),
                whatsapp_service.last_cloud_template_message_id,
            )
            summary["sent"].append(entry)
            logger.info(
                "Birthday wish sent to %s (%s)", staff["name"], staff["phone"][-4:]
            )
        else:
            await _release_wish(staff, wish_date)
            summary["failed"].append(entry)

    if not dry_run:
        await _notify_admin(wish_date, summary["skipped"] + [
            {**item, "reason": "WhatsApp send failed"} for item in summary["failed"]
        ])

    logger.info(
        "Staff birthday run %s: sent=%d failed=%d skipped=%d already=%d",
        wish_date,
        len(summary["sent"]),
        len(summary["failed"]),
        len(summary["skipped"]),
        len(summary["already_sent"]),
    )
    return summary


def send_birthday_wishes_sync() -> None:
    """Scheduler entrypoint: run the daily birthday send in a fresh loop."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(send_birthday_wishes())
    except Exception:
        logger.exception("Staff birthday run failed")
    finally:
        loop.close()


def upcoming(days: int = 30, now: datetime | None = None) -> list[dict[str, str]]:
    """Staff birthdays in the next ``days`` days, in calendar order."""
    current = (now or datetime.now(IST)).astimezone(IST).date()
    staff = load_staff()
    result = []
    for offset in range(days + 1):
        day = date.fromordinal(current.toordinal() + offset)
        key = day.strftime("%m-%d")
        for member in staff:
            if member["dob"] == key:
                result.append(
                    {
                        **member,
                        "on": day.strftime("%d-%m-%Y"),
                        "blocking_reason": blocking_reason(member),
                    }
                )
    return result
