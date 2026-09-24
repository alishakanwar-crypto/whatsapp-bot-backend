"""One-off reminders Alisha asks the bot to send her on a given date.

Each reminder is a row of data: the day it fires, what it is about, and the
detail she wants read back. Delivery uses the same claim-as-a-lease discipline
as the GK Olympiad reminder, so a restart at the wrong second neither loses a
reminder day nor sends it twice.
"""

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.database import DB_PATH
from app.services import whatsapp_service

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
INTERNAL_REMINDER_PHONES = tuple(
    phone.strip()
    for phone in os.environ.get(
        "INTERNAL_REMINDER_PHONES", "918076455224",
    ).split(",")
    if phone.strip()
)
INTERNAL_REMINDER_TEMPLATE = os.environ.get(
    "INTERNAL_REMINDER_TEMPLATE", "ppis_internal_reminder_v2",
)
# A claim is a lease, not a tombstone: a process that dies between claiming
# and sending must not silence that reminder for good.
CLAIM_LEASE = timedelta(minutes=10)


@dataclass(frozen=True)
class Reminder:
    key: str
    on: date
    subject: str
    detail: str


REMINDERS: tuple[Reminder, ...] = (
    Reminder(
        key="gk_workshop_teachers",
        on=date(2026, 10, 1),
        subject="the GK workshop for teachers at 2:00 PM",
        detail=(
            "Teachers are to assemble in the school basement at 2:00 PM and "
            "carry their phone, a notepad and a pen."
        ),
    ),
)


def due_reminders(today: date) -> tuple[Reminder, ...]:
    return tuple(reminder for reminder in REMINDERS if reminder.on == today)


def _claim(reminder: Reminder, recipient: str, now: datetime) -> str | None:
    """Take the lease on this reminder and return the claim's token.

    The token is the claim timestamp, which `_finish` must still find in the
    row: a sender whose lease was taken over by a later run must not write its
    own stale result over that run's.
    """
    expiry = (now - CLAIM_LEASE).isoformat()
    token = now.isoformat()
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(
            "INSERT INTO internal_reminder_deliveries "
            "(reminder_key, reminder_date, recipient, status, claimed_at) "
            "VALUES (?, ?, ?, 'generated', ?) "
            "ON CONFLICT(reminder_key, reminder_date, recipient) "
            "DO UPDATE SET "
            "status = 'generated', claimed_at = excluded.claimed_at "
            "WHERE internal_reminder_deliveries.status = 'failed' "
            "   OR (internal_reminder_deliveries.status = 'generated' "
            "       AND (internal_reminder_deliveries.claimed_at < ? "
            "            OR internal_reminder_deliveries.claimed_at "
            "               NOT LIKE '____-__-__T%'))",
            (
                reminder.key,
                reminder.on.isoformat(),
                recipient,
                token,
                expiry,
            ),
        )
        db.commit()
        return token if cursor.rowcount == 1 else None


def _finish(
    reminder: Reminder,
    recipient: str,
    sent: bool,
    now: datetime,
    token: str,
) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "UPDATE internal_reminder_deliveries SET "
            "status = ?, status_updated_at = ? "
            "WHERE reminder_key = ? AND reminder_date = ? AND recipient = ? "
            "  AND status = 'generated' AND claimed_at = ?",
            (
                "accepted" if sent else "failed",
                now.isoformat(),
                reminder.key,
                reminder.on.isoformat(),
                recipient,
                token,
            ),
        )
        db.commit()


async def _send_one(reminder: Reminder, recipient: str) -> bool:
    try:
        return await whatsapp_service.send_cloud_template_message(
            to=recipient,
            template_name=INTERNAL_REMINDER_TEMPLATE,
            language_code="en",
            body_params=[reminder.subject, reminder.detail],
        )
    except Exception:
        logger.exception("Internal reminder %s errored", reminder.key)
        return False


async def send_internal_reminders(now: datetime | None = None) -> int:
    current = now or datetime.now(IST)
    sent_count = 0
    for reminder in due_reminders(current.date()):
        for recipient in INTERNAL_REMINDER_PHONES:
            # Time each lease from its own claim, so a slow send cannot hand
            # the next recipient an already half-spent lease.
            token = _claim(reminder, recipient, now or datetime.now(IST))
            if token is None:
                continue
            sent = await _send_one(reminder, recipient)
            _finish(reminder, recipient, sent, datetime.now(IST), token)
            if sent:
                sent_count += 1
                logger.info(
                    "Internal reminder %s accepted, recipient ending %s",
                    reminder.key,
                    recipient[-4:],
                )
            else:
                logger.error(
                    "Internal reminder %s failed, recipient ending %s",
                    reminder.key,
                    recipient[-4:],
                )
    return sent_count


def send_internal_reminders_sync() -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(send_internal_reminders())
    finally:
        loop.close()
