"""One-time SCI-Spectrum 2026 visiting-teacher notifications."""

import asyncio
import csv
import io
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

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
SHEET_CSV_URL = os.getenv("SCI_SPECTRUM_SHEET_CSV_URL", "")


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 10:
        return f"91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return ""


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )


def _teachers_file() -> Path:
    return Path(os.getenv("SCI_SPECTRUM_TEACHERS_FILE", TEACHERS_FILE))


def _load_json_teachers() -> list[dict[str, str]]:
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
    teachers = [
        teacher
        for teacher in teachers
        if len(teacher["phone"]) == 12 and teacher["phone"].startswith("91")
    ]
    if not teachers:
        logger.warning("SCI-Spectrum teacher file is empty: %s", path)
    return teachers


async def _fetch_sheet_teachers() -> list[dict[str, str]]:
    url = os.getenv("SCI_SPECTRUM_SHEET_CSV_URL", SHEET_CSV_URL).strip()
    if not url:
        return []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=20.0)
            response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text))
        headers = [header or "" for header in (reader.fieldnames or [])]
        name_column = next(
            (header for header in headers if "name" in header.lower()), None
        )
        phone_column = next(
            (
                header
                for header in headers
                if any(
                    token in header.lower()
                    for token in ("number", "phone", "whatsapp")
                )
            ),
            None,
        )
        if not name_column or not phone_column:
            logger.warning("SCI-Spectrum sheet is missing name/phone columns")
            return []
        teachers = []
        for row in reader:
            name = str(row.get(name_column, "") or "").strip()
            phone = _normalize_phone(row.get(phone_column, ""))
            if (
                name
                and len(phone) == 12
                and phone.startswith("91")
            ):
                teachers.append({"name": name, "phone": phone})
        return teachers
    except Exception as exc:
        logger.warning("Unable to fetch SCI-Spectrum teacher sheet: %s", exc)
        return []


async def _load_teachers() -> list[dict[str, str]]:
    if os.getenv("SCI_SPECTRUM_SHEET_CSV_URL", SHEET_CSV_URL).strip():
        return await _fetch_sheet_teachers()
    return _load_json_teachers()


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


async def _claim_welcome(
    phone: str, name: str, now: datetime
) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO sci_spectrum_welcomed "
            "(phone, name, welcomed_at) VALUES (?, ?, ?)",
            (phone, name, _timestamp(now)),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


async def _release_welcome(phone: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM sci_spectrum_welcomed WHERE phone = ?", (phone,)
        )
        await db.commit()
    finally:
        await db.close()


async def poll_and_send_welcomes(now: datetime | None = None) -> int:
    if not SCI_SPECTRUM_ENABLED:
        logger.info("SCI-Spectrum welcome polling disabled")
        return 0
    current = now or datetime.now(IST)
    if current.astimezone(IST).date() != EVENT_DATE:
        logger.info("SCI-Spectrum welcome polling skipped outside event date")
        return 0
    teachers = await _load_teachers()
    if not teachers:
        logger.warning("SCI-Spectrum welcome polling skipped: no teachers configured")
        return 0
    accepted = 0
    for teacher in teachers:
        recipient = teacher["phone"]
        name = teacher["name"]
        if not await _claim_welcome(recipient, name, current):
            continue
        try:
            sent = await whatsapp_service.send_cloud_template_message(
                to=recipient,
                template_name=WELCOME_TEMPLATE,
                language_code="en",
                header_image_url=CARD_URL,
            )
        except Exception:
            logger.exception(
                "SCI-Spectrum welcome send failed for recipient ending %s",
                recipient[-4:],
            )
            sent = False
        await _record_attempt("welcome", recipient, name, sent, current)
        if not sent:
            await _release_welcome(recipient)
        accepted += int(sent)
    return accepted


async def send_welcome_messages(now: datetime | None = None) -> int:
    return await poll_and_send_welcomes(now)


async def send_thankyou_messages(now: datetime | None = None) -> int:
    if not SCI_SPECTRUM_ENABLED:
        logger.info("SCI-Spectrum thankyou messages disabled")
        return 0
    teachers = await _load_teachers()
    if not teachers:
        logger.warning("SCI-Spectrum thankyou skipped: no teachers configured")
        return 0
    entries = (
        [(teacher["phone"], teacher["name"]) for teacher in teachers]
        + [(phone, "") for phone in _evidence_recipients()]
    )
    current = now or datetime.now(IST)
    accepted = 0
    for recipient, name in entries:
        try:
            sent = await whatsapp_service.send_cloud_template_message(
                to=recipient,
                template_name=THANKYOU_TEMPLATE,
                language_code="en",
            )
        except Exception:
            logger.exception(
                "SCI-Spectrum thankyou send failed for recipient ending %s",
                recipient[-4:],
            )
            sent = False
        await _record_attempt("thankyou", recipient, name, sent, current)
        accepted += int(sent)
    return accepted


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


def poll_and_send_welcomes_sync() -> None:
    _run_sync(poll_and_send_welcomes())


def send_thankyou_messages_sync() -> None:
    _run_sync(send_thankyou_messages())
