"""School email addresses for staff, sourced from the PI Sheet.

The PI Sheet class tabs carry a "Class Teacher Email" column, which the sheet
refresh writes into ``openai_service.TEACHER_DATA``. Those addresses are copied
into the ``staff_emails`` table so they survive a sheet outage and can be used
by the birthday wishes (and any other staff notification) without re-reading
the sheet. Names are matched exactly — a partial match would mail the wrong
colleague, because several tabs list two teachers against a single address.
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.database import get_db
from app.services.openai_service import TEACHER_DATA

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
TITLES = ("ms", "mr", "mrs", "miss", "dr", "smt")


def normalize_name(name: str) -> str:
    """Comparable form of a staff name: lowercase words, titles dropped."""
    words = re.sub(r"[^A-Za-z ]", " ", str(name or "")).lower().split()
    return " ".join(w for w in words if len(w) > 1 and w not in TITLES)


def pi_sheet_emails() -> dict[str, str]:
    """Class-teacher email addresses from the PI Sheet, keyed by staff name."""
    emails: dict[str, str] = {}
    for entry in TEACHER_DATA:
        address = str(entry.get("email", "")).strip()
        if "@" not in address:
            continue
        teachers = [t.strip() for t in str(entry.get("teacher", "")).split("/")]
        # A shared address cannot be attributed to either teacher of a pair.
        if len([t for t in teachers if t]) != 1:
            continue
        key = normalize_name(teachers[0])
        if key:
            emails.setdefault(key, address)
    return emails


async def sync_staff_emails() -> int:
    """Save the PI Sheet class-teacher addresses; returns rows written."""
    emails = pi_sheet_emails()
    if not emails:
        logger.warning("STAFF EMAIL: PI Sheet teacher data has no addresses")
        return 0

    stamp = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")
    db = await get_db()
    try:
        for name, address in emails.items():
            await db.execute(
                "INSERT INTO staff_emails (staff_name, email, source, updated_at) "
                "VALUES (?, ?, 'pi_sheet', ?) "
                "ON CONFLICT(staff_name) DO UPDATE SET "
                "email = excluded.email, source = excluded.source, "
                "updated_at = excluded.updated_at "
                "WHERE staff_emails.source = 'pi_sheet'",
                (name, address, stamp),
            )
        await db.commit()
    finally:
        await db.close()
    logger.info("STAFF EMAIL: saved %d addresses from the PI Sheet", len(emails))
    return len(emails)


async def lookup_email(name: str) -> str:
    """Saved school email for a staff member, or '' when unknown."""
    key = normalize_name(name)
    if not key:
        return ""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT email FROM staff_emails WHERE staff_name = ?", (key,)
        )
        row = await cursor.fetchone()
    finally:
        await db.close()
    if row and row[0]:
        return str(row[0])
    # Fall back to the live sheet data if the table has not been synced yet.
    return pi_sheet_emails().get(key, "")


async def save_email(name: str, email: str, source: str = "manual") -> None:
    """Record an address supplied outside the PI Sheet (kept on re-sync)."""
    key = normalize_name(name)
    if not key or "@" not in email:
        return
    stamp = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO staff_emails (staff_name, email, source, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(staff_name) DO UPDATE SET email = excluded.email, "
            "source = excluded.source, updated_at = excluded.updated_at",
            (key, email.strip(), source, stamp),
        )
        await db.commit()
    finally:
        await db.close()
