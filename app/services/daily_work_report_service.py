"""Daily editable work-report template delivery."""

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from app.database import get_db
from app.services import whatsapp_service

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
DAILY_WORK_REPORT_ENABLED = os.getenv("DAILY_WORK_REPORT_ENABLED", "0") == "1"
DAILY_WORK_REPORT_PHONE = os.getenv(
    "DAILY_WORK_REPORT_PHONE",
    "918076455224",
)
DAILY_WORK_REPORT_TEMPLATE = "ppis_daily_work_report"


def _in_ist(now: datetime | None) -> datetime:
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        return current.replace(tzinfo=IST)
    return current.astimezone(IST)


async def _already_accepted(report_date: str) -> bool:
    db = await get_db()
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS daily_work_report_deliveries ("
            "report_date TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        cursor = await db.execute(
            "SELECT status FROM daily_work_report_deliveries "
            "WHERE report_date = ?",
            (report_date,),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row is not None and row["status"] == "accepted"
    finally:
        await db.close()


async def _record_attempt(report_date: str, sent: bool, current: datetime) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO daily_work_report_deliveries "
            "(report_date, status, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(report_date) DO UPDATE SET "
            "status = excluded.status, created_at = excluded.created_at",
            (
                report_date,
                "accepted" if sent else "failed",
                current.strftime("%d-%m-%Y %H:%M:%S IST"),
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def send_daily_work_report(now: datetime | None = None) -> bool:
    if not DAILY_WORK_REPORT_ENABLED:
        logger.info("Daily work report disabled")
        return False

    current = _in_ist(now)
    if current.weekday() == 6:
        logger.info("Daily work report skipped on Sunday")
        return False

    report_date = current.date().isoformat()
    if await _already_accepted(report_date):
        logger.info("Daily work report already sent for %s", report_date)
        return False

    date_line = f"{current.strftime('%A')}, {current.strftime('%d-%m-%Y')}"
    try:
        sent = await whatsapp_service.send_cloud_template_message(
            to=DAILY_WORK_REPORT_PHONE,
            template_name=DAILY_WORK_REPORT_TEMPLATE,
            language_code="en",
            body_params=[date_line],
        )
    except Exception:
        logger.exception(
            "Daily work report send failed for recipient ending %s",
            DAILY_WORK_REPORT_PHONE[-4:],
        )
        sent = False
    await _record_attempt(report_date, sent, current)
    logger.info(
        "Daily work report %s for %s",
        "accepted" if sent else "failed",
        report_date,
    )
    return sent


def _run_sync(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def send_daily_work_report_sync() -> None:
    _run_sync(send_daily_work_report())
