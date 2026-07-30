"""Route duty reminders, leave conflict alerts, and audit reports."""

import asyncio
import email
import imaplib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.database import get_db
from app.services import whatsapp_service

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
ROUTE_DUTY_ENABLED = os.getenv("ROUTE_DUTY_ENABLED", "0") == "1"
HARPREET_PHONE = os.getenv("ROUTE_DUTY_HARPREET_PHONE", "919599488106")
REMINDER_TEMPLATE = os.getenv(
    "ROUTE_DUTY_REMINDER_TEMPLATE", "ppis_route_duty_reminder_v2"
)
ALERT_TEMPLATE = os.getenv(
    "ROUTE_DUTY_ALERT_TEMPLATE", "ppis_route_duty_alert"
)
REPORT_TEMPLATE = os.getenv(
    "ROUTE_DUTY_REPORT_TEMPLATE", "ppis_route_duty_report_v2"
)
REPORT_TIME = "13:30"
LEAVE_IMAP_HOST = os.getenv("LEAVE_IMAP_HOST", "imap.gmail.com")
LEAVE_IMAP_PORT = int(os.getenv("LEAVE_IMAP_PORT", "993"))
LEAVE_IMAP_USER = os.getenv("LEAVE_IMAP_USER", "leave@ppischool.in")
LEAVE_IMAP_PASSWORD = os.getenv("LEAVE_IMAP_PASSWORD", "")
LEAVE_APPROVED_KEYWORDS = tuple(
    keyword.strip()
    for keyword in os.getenv(
        "ROUTE_DUTY_LEAVE_APPROVED_KEYWORDS", "approved,sanctioned,granted"
    ).lower().split(",")
    if keyword.strip()
)

_SCHEDULE_PATH = Path(__file__).parents[1] / "data" / "route_duty_schedule_2026.json"
try:
    with _SCHEDULE_PATH.open(encoding="utf-8") as schedule_file:
        ROUTE_DUTY_SCHEDULE = json.load(schedule_file)
except (OSError, ValueError):
    logger.exception("Unable to load route duty schedule from %s", _SCHEDULE_PATH)
    ROUTE_DUTY_SCHEDULE = []

TEACHER_CANONICAL_NAMES = {
    "Ms Muskan": ["MUSKAN MOTWANI"],
    "Ms Poonam": ["POONAM"],
    "Ms Poonam (Reception)": ["POONAM"],
    "Ms Mansi Gupta": ["MANSI GUPTA"],
    "Ms Mansi (Accounts)": ["MANSI"],
    "Ms Anu": ["ANU BHALLA"],
    "Ms Riya": ["RIYA ARORA"],
    "Ms Kaninka": ["KANINIKA JAIN"],
    "Ms Meenal/Ms Harjeet": ["MEENAL HARJIKA", "HARJEET KAUR"],
    "Ms Aastha": ["AASTHA KHATTAR"],
    "Ms Alisha": ["ALISHA KANWAR"],
    "Ms Charu": ["CHARU CHAUDHARY"],
    "Ms Daman": ["DAMANPREET KAUR"],
    "Ms Deepti": ["DEEPTI SINGH"],
    "Ms Divya": ["DIVYA SHARMA"],
    "Ms Gargi": ["GARGI ARORA"],
    "Ms Geet": ["GEET SACHDEVA"],
    "Ms Geet Sachdeva": ["GEET SACHDEVA"],
    "Ms Harjeet": ["HARJEET KAUR"],
    "Ms Harnoor": ["HARNOOR KAUR"],
    "Ms Lipi": ["LIPI BANSAL"],
    "Ms Mahak": ["MAHAK JAIN"],
    "Ms Mayuri": ["MAYURI TEJWANI"],
    "Ms Mayuri (Accounts)": ["MAYURI TEJWANI"],
    "Ms Meenal": ["MEENAL HARJIKA"],
    "Ms Nashra": ["NASHRA NAIM"],
    "Ms Nikita": ["NIKITA CHAWLA"],
    "Ms Poshika": ["POSHIKA NARULA"],
    "Ms Prity": ["PRITY SHARMA"],
    "Ms Reva": ["REVA RAJPUT"],
    "Ms Shikha": ["SHIKHA SINGH"],
    "Ms Shreya": ["SHREYA SIKKA"],
    "Ms Simrita": ["SIMRITA LAMBA"],
    "Ms Surbhi": ["SURBHI JESWANI"],
    "Ms Tanvi": ["TANVI GOYAL"],
    "Ms Tarleen": ["TARLEEN KAUR"],
    "Ms Twinkle": ["TWINKLE TANDON"],
}
EXTRA_LABELS = {
    "Ms Yamini",
    "Ms Kashish",
    "Ms Kriti",
    "Ms Bhavya",
    "Ms Shefali",
    "Ms Prerna",
}

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}


def _now_string(value: datetime | None = None) -> str:
    return (value or datetime.now(IST)).astimezone(IST).strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 10:
        return f"91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return ""


def _normalize_name(name: str) -> str:
    value = re.sub(r"\([^)]*\)", "", name or "")
    value = re.sub(r"^\s*ms\.?\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value)
    return " ".join(value.upper().split())


def _route_number(route: str) -> str:
    return re.sub(r"\D", "", route or "") or route


def _canonical_names(label: str) -> list[str]:
    if label in TEACHER_CANONICAL_NAMES:
        return TEACHER_CANONICAL_NAMES[label]
    if label in EXTRA_LABELS:
        return []
    return [_normalize_name(label)]


async def _trueface_phone(canonical_name: str) -> str:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT name, phone FROM trueface_teachers")
        rows = await cursor.fetchall()
    finally:
        await db.close()
    matches = [
        row["phone"]
        for row in rows
        if _normalize_name(row["name"]) == _normalize_name(canonical_name)
        and _normalize_phone(row["phone"])
    ]
    if len(matches) == 1:
        return _normalize_phone(matches[0])
    return ""


async def _resolve_recipients(label: str) -> list[str]:
    """Resolve a schedule teacher label without guessing ambiguous contacts."""
    canonical_names = _canonical_names(label)
    db = await get_db()
    try:
        recipients: list[str] = []
        if not canonical_names:
            cursor = await db.execute(
                "SELECT phone FROM route_duty_teachers WHERE label = ?", (label,)
            )
            for row in await cursor.fetchall():
                phone = _normalize_phone(row["phone"])
                if phone and phone not in recipients:
                    recipients.append(phone)
            if recipients:
                return recipients
        for canonical_name in canonical_names:
            cursor = await db.execute(
                "SELECT phone FROM route_duty_teachers "
                "WHERE label = ? OR canonical_name = ?",
                (label, canonical_name),
            )
            overrides = await cursor.fetchall()
            phones = [
                _normalize_phone(row["phone"])
                for row in overrides
                if _normalize_phone(row["phone"])
            ]
            if not phones:
                cursor = await db.execute(
                    "SELECT name, phone FROM trueface_teachers"
                )
                rows = await cursor.fetchall()
                matches = [
                    _normalize_phone(row["phone"])
                    for row in rows
                    if _normalize_name(row["name"])
                    == _normalize_name(canonical_name)
                    and _normalize_phone(row["phone"])
                ]
                if len(matches) == 1:
                    phones = matches
            for phone in phones:
                if phone and phone not in recipients:
                    recipients.append(phone)
        if not recipients:
            logger.warning("Route duty teacher label unresolved: %s", label)
        return recipients
    finally:
        await db.close()


async def schedule_gaps() -> list[str]:
    labels = sorted({duty["teacher_pdf"] for duty in ROUTE_DUTY_SCHEDULE})
    gaps = []
    for label in labels:
        if not await _resolve_recipients(label):
            gaps.append(label)
    return gaps


async def _is_working_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT 1 FROM school_holidays WHERE date = ?", (day.isoformat(),)
        )
        return await cursor.fetchone() is None
    finally:
        await db.close()


async def _next_working_day(from_date: date) -> date:
    candidate = from_date + timedelta(days=1)
    while not await _is_working_day(candidate):
        candidate += timedelta(days=1)
    return candidate


async def duties_for_date(day: date) -> list[dict]:
    duties = [
        {
            "date": item["date"],
            "route": item["route"],
            "teacher_label": item["teacher_pdf"],
            "report_time": item.get("report_time", REPORT_TIME),
        }
        for item in ROUTE_DUTY_SCHEDULE
        if item.get("date") == day.isoformat()
    ]
    return sorted(duties, key=lambda duty: int(re.search(r"\d+", duty["route"]).group()))


async def _claim_reminder(duty: dict, recipient: str, now: datetime) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO route_duty_reminders "
            "(duty_date, route, teacher_label, recipient, claimed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                duty["date"],
                duty["route"],
                duty["teacher_label"],
                recipient,
                _now_string(now),
            ),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


async def _finish_reminder(
    duty: dict, recipient: str, sent: bool, now: datetime, message_id: str
) -> None:
    db = await get_db()
    try:
        timestamp = _now_string(now)
        await db.execute(
            "UPDATE route_duty_reminders SET status = ?, accepted_at = ?, "
            "status_updated_at = ?, wa_message_id = ? "
            "WHERE duty_date = ? AND route = ? AND teacher_label = ? "
            "AND recipient = ?",
            (
                "accepted" if sent else "failed",
                timestamp if sent else "",
                timestamp,
                message_id if sent else "",
                duty["date"],
                duty["route"],
                duty["teacher_label"],
                recipient,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def send_duty_reminders(now: datetime | None = None) -> int:
    if not ROUTE_DUTY_ENABLED:
        logger.info("Route duty reminders disabled")
        return 0
    current = now or datetime.now(IST)
    target = await _next_working_day(current.astimezone(IST).date())
    sent_count = 0
    for duty in await duties_for_date(target):
        recipients = await _resolve_recipients(duty["teacher_label"])
        if not recipients:
            await _record_unresolved_reminder(duty, current)
            continue
        for recipient in recipients:
            if not await _claim_reminder(duty, recipient, current):
                continue
            try:
                sent = await whatsapp_service.send_cloud_template_message(
                    to=recipient,
                    template_name=REMINDER_TEMPLATE,
                    language_code="en",
                    body_params=[
                        target.strftime("%d/%m/%Y"),
                        _route_number(duty["route"]),
                    ],
                )
            except Exception:
                logger.exception("Route duty reminder failed for %s", recipient[-4:])
                sent = False
            await _finish_reminder(
                duty,
                recipient,
                sent,
                datetime.now(IST),
                whatsapp_service.last_cloud_template_message_id,
            )
            sent_count += int(sent)
    return sent_count


async def _record_unresolved_reminder(duty: dict, now: datetime) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO route_duty_reminders "
            "(duty_date, route, teacher_label, recipient, status, claimed_at) "
            "VALUES (?, ?, ?, '', 'unresolved', ?)",
            (
                duty["date"],
                duty["route"],
                duty["teacher_label"],
                _now_string(now),
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def _conflicts_for_date(day: date) -> dict[str, dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT route, teacher_label, teacher_name FROM route_duty_leave_conflicts "
            "WHERE leave_date = ?",
            (day.isoformat(),),
        )
        return {row["route"]: dict(row) for row in await cursor.fetchall()}
    finally:
        await db.close()


def _format_duty_lines(duties: list[dict], conflicts: dict[str, dict]) -> str:
    lines = []
    for duty in duties:
        line = (
            f"{duty['route']} — {duty['teacher_label']} "
            f"({duty['report_time']})"
        )
        conflict = conflicts.get(duty["route"])
        if conflict:
            line += (
                f" [CONFLICT: {conflict['teacher_name']} on leave; "
                "replacement pending]"
            )
        lines.append(line)
    return "; ".join(lines)


async def _claim_report(report_type: str, period_key: str, now: datetime) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO route_duty_report_log "
            "(report_type, period_key, recipient, claimed_at) VALUES (?, ?, ?, ?)",
            (report_type, period_key, _normalize_phone(HARPREET_PHONE), _now_string(now)),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


async def _finish_report(
    report_type: str, period_key: str, sent: bool, now: datetime
) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE route_duty_report_log SET status = ?, wa_message_id = ?, "
            "status_updated_at = ? WHERE report_type = ? AND period_key = ? "
            "AND recipient = ?",
            (
                "accepted" if sent else "failed",
                whatsapp_service.last_cloud_template_message_id if sent else "",
                _now_string(now),
                report_type,
                period_key,
                _normalize_phone(HARPREET_PHONE),
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def send_harpreet_daily_report(now: datetime | None = None) -> bool:
    if not ROUTE_DUTY_ENABLED:
        logger.info("Route duty daily report disabled")
        return False
    current = now or datetime.now(IST)
    target = await _next_working_day(current.astimezone(IST).date())
    period_key = target.isoformat()
    if not await _claim_report("daily", period_key, current):
        return False
    duties = await duties_for_date(target)
    body = _format_duty_lines(duties, await _conflicts_for_date(target))
    try:
        sent = await whatsapp_service.send_cloud_template_message(
            to=_normalize_phone(HARPREET_PHONE),
            template_name=REPORT_TEMPLATE,
            language_code="en",
            body_params=[target.strftime("%d/%m/%Y"), body or "No route duties scheduled"],
        )
    except Exception:
        logger.exception("Route duty daily report failed")
        sent = False
    await _finish_report("daily", period_key, sent, datetime.now(IST))
    return sent


_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?\b")
_DATE_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)|"
    r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)


def _parse_leave_dates(text: str, reference_year: int = 2026) -> list[date]:
    found: set[date] = set()
    for match in _DATE_NUMERIC_RE.finditer(text):
        day, month, year = match.groups()
        try:
            found.add(date(int(year or reference_year), int(month), int(day)))
        except ValueError:
            continue
    for match in _DATE_MONTH_RE.finditer(text):
        day_text, month_text, month_first, day_first = match.groups()
        day = day_text or day_first
        month = month_text or month_first
        month_number = _MONTHS.get(month.lower())
        if not month_number:
            continue
        try:
            found.add(date(reference_year, month_number, int(day)))
        except ValueError:
            continue
    return sorted(found)


_extract_leave_dates = _parse_leave_dates


def _decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    from email.header import decode_header

    parts = decode_header(raw)
    decoded = []
    for value, charset in parts:
        if isinstance(value, bytes):
            decoded.append(value.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(value)
    return " ".join(decoded)


def _extract_text_from_email(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != "text/plain":
                continue
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            payload = part.get_payload(decode=True)
            if payload:
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = message.get_payload(decode=True)
        if payload:
            return payload.decode(
                message.get_content_charset() or "utf-8", errors="replace"
            )
    return ""


async def _processed(message_id: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?",
            (f"leave:{message_id}",),
        )
        return await cursor.fetchone() is not None
    finally:
        await db.close()


async def _mark_processed(message_id: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)",
            (f"leave:{message_id}",),
        )
        await db.commit()
    finally:
        await db.close()


async def _schedule_teacher_matches(text: str) -> list[dict]:
    normalized_text = _normalize_name(text)
    matches = []
    for item in ROUTE_DUTY_SCHEDULE:
        label = item["teacher_pdf"]
        aliases = [label, *_canonical_names(label)]
        if any(
            _normalize_name(alias) and _normalize_name(alias) in normalized_text
            for alias in aliases
        ):
            matches.append(
                {
                    "date": item["date"],
                    "route": item["route"],
                    "teacher_label": item["teacher_pdf"],
                    "report_time": item.get("report_time", REPORT_TIME),
                }
            )
    return matches


async def record_missed_duty(
    teacher_name: str, duty_date: date | str, route: str, reason: str = "Leave"
) -> bool:
    day = duty_date.isoformat() if isinstance(duty_date, date) else duty_date
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO route_duty_missed "
            "(teacher_name, duty_date, route, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (teacher_name, day, route, reason, _now_string()),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


async def _create_leave_conflict(
    duty: dict, source_message_id: str, now: datetime
) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO route_duty_leave_conflicts "
            "(teacher_label, teacher_name, leave_date, route, source_message_id, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                duty["teacher_label"],
                duty["teacher_label"],
                duty["date"],
                duty["route"],
                source_message_id,
                _now_string(now),
            ),
        )
        await db.commit()
        created = cursor.rowcount == 1
    finally:
        await db.close()
    if created:
        await record_missed_duty(duty["teacher_label"], duty["date"], duty["route"])
    return created


async def poll_leave_mailbox(now: datetime | None = None) -> int:
    if not ROUTE_DUTY_ENABLED:
        logger.info("Route duty leave polling disabled")
        return 0
    password = LEAVE_IMAP_PASSWORD
    if not password and LEAVE_IMAP_USER == os.getenv("SMTP_USER", ""):
        password = os.getenv("SMTP_PASSWORD", "")
    if not password:
        logger.warning("Route duty leave mailbox password is not configured")
        return 0
    mailbox = None
    processed_count = 0
    current = now or datetime.now(IST)
    try:
        mailbox = imaplib.IMAP4_SSL(LEAVE_IMAP_HOST, LEAVE_IMAP_PORT)
        mailbox.login(LEAVE_IMAP_USER, password)
        mailbox.select("INBOX", readonly=True)
        status, data = mailbox.search(None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return 0
        for email_id in data[0].split():
            status, fetched = mailbox.fetch(email_id, "(RFC822)")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            message = email.message_from_bytes(fetched[0][1])
            message_id = _decode_mime_header(message.get("Message-ID")) or email_id.decode()
            if await _processed(message_id):
                continue
            subject = _decode_mime_header(message.get("Subject"))
            text = f"{subject}\n{_extract_text_from_email(message)}"
            lower = text.lower()
            try:
                if any(keyword in lower for keyword in LEAVE_APPROVED_KEYWORDS):
                    dates = _parse_leave_dates(text, current.year)
                    matches = await _schedule_teacher_matches(text)
                    for leave_date in dates:
                        for duty in matches:
                            if duty["date"] != leave_date.isoformat():
                                continue
                            if not await _create_leave_conflict(duty, message_id, current):
                                continue
                            sent = await whatsapp_service.send_cloud_template_message(
                                to=_normalize_phone(HARPREET_PHONE),
                                template_name=ALERT_TEMPLATE,
                                language_code="en",
                                body_params=[
                                    duty["teacher_label"],
                                    leave_date.strftime("%d/%m/%Y"),
                                    _route_number(duty["route"]),
                                ],
                            )
                            db = await get_db()
                            try:
                                await db.execute(
                                    "UPDATE route_duty_leave_conflicts SET alerted_at = ?, "
                                    "alert_status = ? WHERE teacher_label = ? AND "
                                    "leave_date = ? AND route = ?",
                                    (
                                        _now_string(),
                                        "accepted" if sent else "failed",
                                        duty["teacher_label"],
                                        duty["date"],
                                        duty["route"],
                                    ),
                                )
                                await db.commit()
                            finally:
                                await db.close()
                processed_count += 1
            finally:
                await _mark_processed(message_id)
    except Exception:
        logger.exception("Route duty leave mailbox polling failed")
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except Exception:
                logger.debug("Route duty IMAP logout failed", exc_info=True)
    return processed_count


async def frequent_missed(threshold: int = 2) -> list[dict]:
    cutoff = (datetime.now(IST).date() - timedelta(days=30)).isoformat()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT teacher_name, COUNT(*) AS missed_count FROM route_duty_missed "
            "WHERE duty_date >= ? GROUP BY teacher_name HAVING COUNT(*) >= ? "
            "ORDER BY missed_count DESC, teacher_name",
            (cutoff, threshold),
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def _send_period_report(
    report_type: str, period_key: str, start: date, end: date, now: datetime
) -> bool:
    if not await _claim_report(report_type, period_key, now):
        return False
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT teacher_name, duty_date, route, compensation_status, comp_date "
            "FROM route_duty_missed WHERE duty_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        missed = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT teacher_name, leave_date, route FROM route_duty_leave_conflicts "
            "WHERE leave_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        conflicts = await cursor.fetchall()
    finally:
        await db.close()
    duty_lines = []
    cursor_day = start
    while cursor_day <= end:
        duties = await duties_for_date(cursor_day)
        if duties:
            duty_lines.append(
                _format_duty_lines(duties, await _conflicts_for_date(cursor_day))
            )
        cursor_day += timedelta(days=1)
    frequent = await frequent_missed()
    body_parts = [
        f"Period: {start.strftime('%d/%m/%Y')} to {end.strftime('%d/%m/%Y')}",
        f"Duties: {'; '.join(duty_lines) if duty_lines else 'None'}",
        f"Missed duties: {len(missed)}",
        f"Leave conflicts: {len(conflicts)}",
    ]
    if missed:
        body_parts.append(
            "Missed: "
            + "; ".join(
                f"{row['teacher_name']} {row['duty_date']} {row['route']} "
                f"({row['compensation_status']}"
                f"{', ' + row['comp_date'] if row['comp_date'] else ''})"
                for row in missed
            )
        )
    if frequent:
        body_parts.append(
            "Frequent missed: "
            + "; ".join(
                f"{row['teacher_name']} ({row['missed_count']})" for row in frequent
            )
        )
    body = "; ".join(body_parts)
    try:
        sent = await whatsapp_service.send_cloud_template_message(
            to=_normalize_phone(HARPREET_PHONE),
            template_name=REPORT_TEMPLATE,
            language_code="en",
            body_params=[period_key, body],
        )
    except Exception:
        logger.exception("Route duty %s report failed", report_type)
        sent = False
    await _finish_report(report_type, period_key, sent, datetime.now(IST))
    return sent


async def send_weekly_report(now: datetime | None = None) -> bool:
    if not ROUTE_DUTY_ENABLED:
        logger.info("Route duty weekly report disabled")
        return False
    current = now or datetime.now(IST)
    end = current.astimezone(IST).date()
    start = end - timedelta(days=6)
    return await _send_period_report(
        "weekly", f"{end.isocalendar().year}-W{end.isocalendar().week:02d}",
        start, end, current,
    )


async def send_monthly_report(now: datetime | None = None) -> bool:
    if not ROUTE_DUTY_ENABLED:
        logger.info("Route duty monthly report disabled")
        return False
    current = now or datetime.now(IST)
    current_date = current.astimezone(IST).date()
    last_day = current_date.replace(day=1) - timedelta(days=1)
    start = last_day.replace(day=1)
    return await _send_period_report(
        "monthly", last_day.strftime("%Y-%m"), start, last_day, current
    )


async def record_route_duty_delivery_status(
    wa_message_id: str, status: str, occurred_at: datetime
) -> bool:
    if not wa_message_id or status not in {"sent", "delivered", "read", "failed"}:
        return False
    timestamp = occurred_at.astimezone(IST).strftime("%d-%m-%Y %H:%M:%S IST")
    db = await get_db()
    try:
        updated = False
        try:
            cursor = await db.execute(
                "UPDATE route_duty_reminders SET status = ?, status_updated_at = ? "
                "WHERE wa_message_id = ?",
                (status, timestamp, wa_message_id),
            )
            updated = cursor.rowcount > 0
            cursor = await db.execute(
                "UPDATE route_duty_report_log SET status = ?, status_updated_at = ? "
                "WHERE wa_message_id = ?",
                (status, timestamp, wa_message_id),
            )
            await db.commit()
            return updated or cursor.rowcount > 0
        except Exception:
            logger.debug("Route duty delivery tables are unavailable", exc_info=True)
            return False
    finally:
        await db.close()


async def mark_reminder_acknowledged(
    recipient: str, now: datetime | None = None
) -> bool:
    current = now or datetime.now(IST)
    target = (
        await _next_working_day(current.astimezone(IST).date())
    ).isoformat()
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE route_duty_reminders SET acknowledged_at = ? "
            "WHERE rowid = (SELECT rowid FROM route_duty_reminders "
            "WHERE recipient = ? AND duty_date = ? AND acknowledged_at = '' "
            "ORDER BY rowid DESC LIMIT 1)",
            (_now_string(current), _normalize_phone(recipient), target),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


def _run_sync(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def send_duty_reminders_sync() -> None:
    _run_sync(send_duty_reminders())


def send_harpreet_daily_report_sync() -> None:
    _run_sync(send_harpreet_daily_report())


def poll_leave_mailbox_sync() -> None:
    _run_sync(poll_leave_mailbox())


def send_weekly_report_sync() -> None:
    _run_sync(send_weekly_report())


def send_monthly_report_sync() -> None:
    _run_sync(send_monthly_report())
