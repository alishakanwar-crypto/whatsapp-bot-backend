import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.database import DB_PATH
from app.services import whatsapp_service

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
GK_OLYMPIAD_REMINDER_PHONES = tuple(
    phone.strip()
    for phone in os.environ.get(
        "GK_OLYMPIAD_REMINDER_PHONES", "918076455224",
    ).split(",")
    if phone.strip()
)
GK_OLYMPIAD_TEMPLATE = os.environ.get(
    "GK_OLYMPIAD_REMINDER_TEMPLATE", "ppis_gk_olympiad_reminder",
)
# Used only while the purpose-written wording is still with Meta: an approved
# announcement carries the same words rather than the reminder going unsent.
GK_OLYMPIAD_FALLBACK_TEMPLATE = os.environ.get(
    "GK_OLYMPIAD_REMINDER_FALLBACK_TEMPLATE", "ppis_school_announcement",
)
GK_OLYMPIAD_DATE = date(2026, 10, 6)
GK_OLYMPIAD_DATE_TEXT = "Monday, 6th October 2026"
GK_OLYMPIAD_STUDY_LINK = "https://shorturl.at/lZOmc"
GK_OLYMPIAD_REMINDER_DATES = (
    date(2026, 9, 25),
    date(2026, 9, 30),
    date(2026, 10, 3),
)
GK_OLYMPIAD_NOTE = (
    "Please remind the participating students that the GK Olympiad is "
    "approaching, so they prepare and revise regularly."
)


def is_reminder_due(today: date) -> bool:
    return today in GK_OLYMPIAD_REMINDER_DATES


def fallback_announcement(today: date) -> str:
    """The same reminder as one line, for the generic announcement wording.

    Meta refuses a template parameter holding a newline, a tab or four
    spaces in a row, so everything the fallback says lives on one line.
    """
    days_left = (GK_OLYMPIAD_DATE - today).days
    return (
        f"Reminder: the GK Olympiad is scheduled on {GK_OLYMPIAD_DATE_TEXT} "
        f"({days_left} day(s) away). {GK_OLYMPIAD_NOTE} "
        f"Study material for practice: {GK_OLYMPIAD_STUDY_LINK}"
    )


def _claim_reminder(reminder_date: date, recipient: str, now: datetime) -> bool:
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(
            "INSERT INTO gk_olympiad_reminder_deliveries "
            "(reminder_date, recipient, status, claimed_at) "
            "VALUES (?, ?, 'generated', ?) "
            "ON CONFLICT(reminder_date, recipient) DO UPDATE SET "
            "status = 'generated', claimed_at = excluded.claimed_at "
            "WHERE gk_olympiad_reminder_deliveries.status = 'failed'",
            (
                reminder_date.isoformat(),
                recipient,
                now.strftime("%d-%m-%Y %H:%M:%S IST"),
            ),
        )
        db.commit()
        return cursor.rowcount == 1


def _finish_reminder(
    reminder_date: date,
    recipient: str,
    sent: bool,
    now: datetime,
) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "UPDATE gk_olympiad_reminder_deliveries SET "
            "status = ?, status_updated_at = ? "
            "WHERE reminder_date = ? AND recipient = ?",
            (
                "accepted" if sent else "failed",
                now.strftime("%d-%m-%Y %H:%M:%S IST"),
                reminder_date.isoformat(),
                recipient,
            ),
        )
        db.commit()


async def _send_one(recipient: str, today: date) -> bool:
    try:
        sent = await whatsapp_service.send_cloud_template_message(
            to=recipient,
            template_name=GK_OLYMPIAD_TEMPLATE,
            language_code="en",
            body_params=[
                GK_OLYMPIAD_DATE_TEXT,
                GK_OLYMPIAD_NOTE,
                GK_OLYMPIAD_STUDY_LINK,
            ],
        )
    except Exception:
        logger.exception("GK Olympiad reminder errored on its own wording")
        sent = False
    if sent or not GK_OLYMPIAD_FALLBACK_TEMPLATE:
        return sent
    try:
        return await whatsapp_service.send_cloud_template_message(
            to=recipient,
            template_name=GK_OLYMPIAD_FALLBACK_TEMPLATE,
            language_code="en",
            body_params=[fallback_announcement(today)],
        )
    except Exception:
        logger.exception("GK Olympiad reminder errored on the announcement wording")
        return False


async def send_gk_olympiad_reminders(now: datetime | None = None) -> int:
    current = now or datetime.now(IST)
    today = current.date()
    if not is_reminder_due(today):
        return 0
    sent_count = 0
    for recipient in GK_OLYMPIAD_REMINDER_PHONES:
        if not _claim_reminder(today, recipient, current):
            continue
        sent = await _send_one(recipient, today)
        _finish_reminder(today, recipient, sent, datetime.now(IST))
        if sent:
            sent_count += 1
            logger.info(
                "GK Olympiad reminder accepted for %s, recipient ending %s",
                today.isoformat(),
                recipient[-4:],
            )
        else:
            logger.error(
                "GK Olympiad reminder failed for %s, recipient ending %s",
                today.isoformat(),
                recipient[-4:],
            )
    return sent_count


def send_gk_olympiad_reminders_sync() -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(send_gk_olympiad_reminders())
    finally:
        loop.close()
