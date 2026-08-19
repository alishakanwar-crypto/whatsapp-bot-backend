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
from app.services.email_service import send_email_async
from app.services.staff_email_service import lookup_email

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
STAFF_BIRTHDAY_ENABLED = os.getenv("STAFF_BIRTHDAY_ENABLED", "1") == "1"
WISH_TEMPLATE = os.getenv("STAFF_BIRTHDAY_TEMPLATE", "ppis_staff_birthday_wish")
POSTER_BASE_URL = os.getenv(
    "STAFF_BIRTHDAY_POSTER_BASE_URL",
    "https://ppis-whatsapp-bot.fly.dev/static/birthday_posters",
).rstrip("/")
ADMIN_PHONE = os.getenv("STAFF_BIRTHDAY_ADMIN_PHONE", "918076455224")
# Principal Ma'am receives the same mail as proof that the wish went out.
PRINCIPAL_EMAIL = os.getenv(
    "STAFF_BIRTHDAY_PRINCIPAL_EMAIL", "deepi.bector@ppischool.in"
)
EMAIL_SUBJECT = "Happy Birthday from Team PPIS!"
EMAIL_BODY = (
    "Dear {name},\n\n"
    "Wishing you many happy returns of the day. May the coming year be full "
    "of peace, health, happiness and prosperity.\n\n"
    "Best wishes,\nTeam PPIS"
)
PRINCIPAL_BODY = (
    "Respected Ma'am,\n\n"
    "The birthday wish below was sent today on WhatsApp and email.\n\n"
    "Staff member: {name}\n"
    "School email: {email}\n"
    "WhatsApp: {phone}\n"
    "Sent at: {stamp}\n\n"
    "The poster shared with them is attached.\n\n"
    "Regards,\nPPIS Bot"
)

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
                "email": str(record.get("email", "")).strip(),
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


async def resolve_email(staff: dict[str, str]) -> str:
    """School email for this staff member: record first, then saved PI Sheet."""
    if staff.get("email"):
        return staff["email"]
    try:
        return await lookup_email(staff["name"])
    except Exception:
        logger.exception("Unable to look up school email for %s", staff["name"])
        return ""


async def _claim_email(staff: dict[str, str], wish_date: str, address: str) -> bool:
    """Reserve today's birthday email; False when it already went out.

    The email is claimed separately from the WhatsApp wish so that a retry of a
    failed WhatsApp template does not mail the staff member a second time.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO staff_birthday_email_log "
            "(staff_name, wish_date, email) VALUES (?, ?, ?)",
            (staff["name"], wish_date, address),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


async def _finish_email(
    staff: dict[str, str], wish_date: str, address: str, sent: bool
) -> None:
    db = await get_db()
    try:
        if sent:
            await db.execute(
                "UPDATE staff_birthday_email_log SET status = 'sent', email = ?, "
                "sent_at = ? WHERE staff_name = ? AND wish_date = ?",
                (address, _timestamp(), staff["name"], wish_date),
            )
        else:
            # Drop the claim so the next run can try the email again.
            await db.execute(
                "DELETE FROM staff_birthday_email_log "
                "WHERE staff_name = ? AND wish_date = ?",
                (staff["name"], wish_date),
            )
        await db.commit()
    finally:
        await db.close()


async def _deliver_email(
    staff: dict[str, str], wish_date: str, address: str
) -> str:
    """Email today's poster once; returns 'sent', 'failed' or 'no address'."""
    if not address:
        return "no address"
    if not await _claim_email(staff, wish_date, address):
        # Already mailed today (a WhatsApp retry must not mail them again).
        return "sent"
    emailed = await _email_wish(staff, address)
    await _finish_email(staff, wish_date, address, emailed)
    if not emailed:
        logger.warning(
            "Birthday email to %s (%s) did not go out", staff["name"], address
        )
    return "sent" if emailed else "failed"


async def _email_wish(staff: dict[str, str], address: str) -> bool:
    """Email the poster to the staff member; False if it could not be sent."""
    poster = _POSTER_DIR / staff["poster"]
    try:
        attachments = [(poster.name, poster.read_bytes())]
    except OSError:
        logger.exception("Unable to read poster %s for email", poster)
        return False
    try:
        sent = await send_email_async(
            address,
            EMAIL_SUBJECT,
            EMAIL_BODY.format(name=staff["display_name"]),
            sender_name="PP International School",
            attachments=attachments,
        )
    except Exception:
        logger.exception("Birthday email failed for %s", staff["name"])
        return False

    if PRINCIPAL_EMAIL and PRINCIPAL_EMAIL != address:
        try:
            await send_email_async(
                PRINCIPAL_EMAIL,
                f"{EMAIL_SUBJECT} — {staff['display_name']}",
                PRINCIPAL_BODY.format(
                    name=staff["display_name"],
                    email=address,
                    phone=staff["phone"],
                    stamp=_timestamp(),
                ),
                sender_name="PP International School",
                attachments=attachments,
            )
        except Exception:
            logger.exception(
                "Birthday proof copy to the principal failed for %s", staff["name"]
            )
    return sent


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
    email: str = "",
    email_status: str = "",
) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE staff_birthday_log SET status = ?, wa_message_id = ?, "
            "email = ?, email_status = ?, "
            "status_updated_at = ? WHERE staff_name = ? AND wish_date = ?",
            (
                "sent" if sent else "failed",
                message_id if sent else "",
                email,
                email_status,
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
        address = await resolve_email(staff)
        entry = {
            "name": staff["name"],
            "display_name": staff["display_name"],
            "phone": staff["phone"],
            "email": address,
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

        email_status = await _deliver_email(staff, wish_date, address)
        entry["emailed"] = email_status == "sent"

        if sent:
            await _finish_wish(
                staff,
                wish_date,
                True,
                datetime.now(IST),
                whatsapp_service.last_cloud_template_message_id,
                email=address,
                email_status=email_status,
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
