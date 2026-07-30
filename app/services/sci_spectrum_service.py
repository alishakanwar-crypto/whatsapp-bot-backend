"""One-time SCI-Spectrum 2026 visiting-teacher notifications."""

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

EVENT_DATE = date(2026, 8, 1)
IST = ZoneInfo("Asia/Kolkata")
SCI_SPECTRUM_ENABLED = os.getenv("SCI_SPECTRUM_ENABLED", "0") == "1"
TEACHERS_FILE = os.getenv(
    "SCI_SPECTRUM_TEACHERS_FILE", "app/data/sci_spectrum_teachers.json"
)
WELCOME_TEMPLATE = os.getenv(
    "SCI_SPECTRUM_WELCOME_TEMPLATE", "ppis_scispectrum_welcome"
)
THANKYOU_TEMPLATE = os.getenv(
    "SCI_SPECTRUM_THANKYOU_TEMPLATE", "ppis_scispectrum_thankyou"
)
CARD_URL = os.getenv(
    "SCI_SPECTRUM_CARD_URL",
    "https://ppis-whatsapp-bot.fly.dev/static/sci_spectrum_welcome.jpg",
)
DEEPA_PHONE = os.getenv("SCI_SPECTRUM_DEEPI_PHONE", "")


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 10:
        return f"91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return digits


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )


def _teachers_file() -> Path:
    return Path(os.getenv("SCI_SPECTRUM_TEACHERS_FILE", TEACHERS_FILE))


def _load_teachers() -> list[dict[str, str]]:
    path = _teachers_file()
    try:
        with path.open(encoding="utf-8") as teacher_file:
            data = json.load(teacher_file)
    except FileNotFoundError:
        logger.warning("SCI-Spectrum teacher file is missing: %s", path)
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unable to load SCI-Spectrum teacher file %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("SCI-Spectrum teacher file must contain a JSON list: %s", path)
        return []
    teachers = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name") or not item.get("phone"):
            continue
        teachers.append(
            {"name": str(item["name"]), "phone": _normalize_phone(item["phone"])}
        )
    if not teachers:
        logger.warning("SCI-Spectrum teacher file is empty: %s", path)
    return teachers


def _evidence_recipients() -> list[str]:
    configured = os.getenv("SCI_SPECTRUM_EVIDENCE_PHONES", "")
    if configured:
        return [
            phone
            for phone in (_normalize_phone(value) for value in configured.split(","))
            if phone
        ]
    recipients = ["919599488106", "918076455224"]
    deepi = _normalize_phone(os.getenv("SCI_SPECTRUM_DEEPI_PHONE", DEEPA_PHONE))
    if deepi:
        recipients.append(deepi)
    return recipients


async def _record_attempt(
    phase: str,
    recipient: str,
    name: str,
    sent: bool,
    created_at: datetime,
) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO sci_spectrum_deliveries "
            "(phase, recipient, name, status, wa_message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                phase,
                recipient,
                name,
                "accepted" if sent else "failed",
                whatsapp_service.last_cloud_template_message_id if sent else "",
                _timestamp(created_at),
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def _send_phase(phase: str, now: datetime | None = None) -> int:
    if not SCI_SPECTRUM_ENABLED:
        logger.info("SCI-Spectrum %s messages disabled", phase)
        return 0
    teachers = _load_teachers()
    if not teachers:
        logger.warning("SCI-Spectrum %s skipped: no teachers configured", phase)
        return 0
    entries = (
        [(teacher["phone"], teacher["name"]) for teacher in teachers]
        + [(phone, "") for phone in _evidence_recipients()]
        if phase == "thankyou"
        else [(teacher["phone"], teacher["name"]) for teacher in teachers]
    )
    if not entries:
        logger.warning("SCI-Spectrum %s skipped: no recipients configured", phase)
        return 0
    accepted = 0
    current = now or datetime.now(IST)
    for recipient, name in entries:
        try:
            if phase == "welcome":
                sent = await whatsapp_service.send_cloud_template_message(
                    to=recipient,
                    template_name=WELCOME_TEMPLATE,
                    language_code="en",
                    header_image_url=CARD_URL,
                )
            else:
                sent = await whatsapp_service.send_cloud_template_message(
                    to=recipient,
                    template_name=THANKYOU_TEMPLATE,
                    language_code="en",
                )
        except Exception:
            logger.exception(
                "SCI-Spectrum %s send failed for recipient ending %s",
                phase,
                recipient[-4:],
            )
            sent = False
        await _record_attempt(phase, recipient, name, sent, current)
        accepted += int(sent)
    return accepted


async def send_welcome_messages(now: datetime | None = None) -> int:
    return await _send_phase("welcome", now)


async def send_thankyou_messages(now: datetime | None = None) -> int:
    return await _send_phase("thankyou", now)


async def record_sci_spectrum_delivery_status(
    wa_message_id: str, status: str, occurred_at: datetime
) -> bool:
    if not wa_message_id or status not in {"sent", "delivered", "read", "failed"}:
        return False
    db = await get_db()
    try:
        try:
            cursor = await db.execute(
                "UPDATE sci_spectrum_deliveries SET status = ?, status_updated_at = ? "
                "WHERE wa_message_id = ?",
                (
                    status,
                    occurred_at.astimezone(IST).strftime("%d-%m-%Y %H:%M:%S IST"),
                    wa_message_id,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0
        except Exception:
            logger.debug(
                "SCI-Spectrum delivery table is unavailable", exc_info=True,
            )
            return False
    finally:
        await db.close()


def _run_sync(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def send_welcome_messages_sync() -> None:
    _run_sync(send_welcome_messages())


def send_thankyou_messages_sync() -> None:
    _run_sync(send_thankyou_messages())
