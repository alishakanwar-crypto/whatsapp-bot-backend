import asyncio
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

from app.database import DB_PATH, get_db
from app.services import whatsapp_service

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
SHOWCASE_REMINDER_PHONE = os.environ.get(
    "SHOWCASE_REMINDER_PHONE", "918076455224",
)
SHOWCASE_REMINDER_TEMPLATE = os.environ.get(
    "SHOWCASE_REMINDER_TEMPLATE", "ppis_musical_showcase_reminder",
)


class Showcase(NamedTuple):
    event_date: date
    grade_class: str
    theme_occasion: str
    presentation_item: str
    teacher_incharges: str


SHOWCASES = (
    Showcase(date(2026, 7, 20), "Grade I", "Tiger Day", "Symphony of the Stripes", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 7, 31), "Grade I", "Friendship Day", "Harmony of Hearts", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 8, 11), "Grade I", "Independence Day", "Patriotic Group Song - Pyara Pyara Desh", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 8, 27), "Grade I", "Raksha Bandhan", "Tie of Love - Musical Presentation", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 7, 31), "Grade II", "Theme of the Month", "We Are a Family Song", "Ms. Tanvi & Ms. Gargi"),
    Showcase(date(2026, 8, 11), "Grade II", "Independence Day", "Patriotic Group Song - Nanha Munha Raahi Hu", "Ms. Tanvi & Ms. Gargi"),
    Showcase(date(2026, 8, 27), "Grade II", "Raksha Bandhan", "Tie of Love - Musical Presentation", "Ms. Tanvi & Ms. Gargi"),
    Showcase(date(2026, 7, 17), "Grade III", "Nelson Mandela Day", "Oh Nelson Mandela", "Ms. Harnoor & Ms. Shreya"),
    Showcase(date(2026, 7, 30), "Grade III", "Literary Presentation", "Doha Presentation", "Ms. Seema & Ms. Yamini"),
    Showcase(date(2026, 8, 12), "Grade III", "Independence Day", "Hum Bharat Ke Bacche Hain", "Ms. Seema & Ms. Yamini"),
    Showcase(date(2026, 7, 24), "Grade IV", "Literary Presentation", "Doha Presentation", "Ms. Seema"),
    Showcase(date(2026, 8, 11), "Grade IV", "Independence Day", "Hum Bharat Ke Bacche Hain", "Ms. Seema & Ms. Yamini"),
    Showcase(date(2026, 8, 12), "Grade IV", "Independence Theme", "News Showcase", "Ms. Prabhjot & Ms. Daman"),
    Showcase(date(2026, 7, 23), "Grade V", "Poetry - Nature Theme", "All Things Bright and Beautiful", "Ms. Prabhjot"),
    Showcase(date(2026, 8, 12), "Grade V", "Independence Day", "Khoob Ladi Mardani", "Ms. Shikha"),
    Showcase(date(2026, 7, 30), "Grade VI", "Punjabi Folk", "Painti-Akhari Punjabi Song", "Ms. Jasleen"),
    Showcase(date(2026, 7, 28), "Grade VI", "Poetry", "Kavita Prastuti - Hum Bharat ke Bacche hain", "Ms. Shikha"),
    Showcase(date(2026, 8, 7), "Grade VI", "Independence Day", "Mera rang de basanti chola", "Ms. Shikha"),
    Showcase(date(2026, 8, 6), "Grade VI", "Sanskrit", "वर्णमाला गीतं", "Ms. Pooja"),
    Showcase(date(2026, 8, 12), "Grade VI", "Independence Day", "Freedom Fighters Tableau", "Ms. Poonam"),
    Showcase(date(2026, 7, 28), "Grade VII", "Poetry", "Kavita Prastuti", "Ms. Rashmi"),
    Showcase(date(2026, 8, 7), "Grade VII", "Independence Day", "Freedom Fighters Tableau", "Ms. Poonam"),
    Showcase(date(2026, 8, 13), "Grade VII", "Independence Day", "Patriotic Medley", "Ms. Rashmi"),
    Showcase(date(2026, 7, 31), "Grade VIII", "Poetry", "Kavita Prastuti", "Ms. Rashmi"),
    Showcase(date(2026, 8, 7), "Grade VIII", "Independence Day", "Freedom Fighters Tableau", "Ms. Mansi"),
    Showcase(date(2026, 8, 13), "Grade VIII", "Independence Day", "Patriotic Medley", "Ms. Rashmi"),
)


def due_showcases(today: date) -> dict[date, list[Showcase]]:
    due: dict[date, list[Showcase]] = defaultdict(list)
    for showcase in SHOWCASES:
        days_until = (showcase.event_date - today).days
        if 2 <= days_until <= 3:
            due[showcase.event_date].append(showcase)
    return dict(due)


def format_showcase_details(showcases: list[Showcase]) -> str:
    return "\n".join(
        f"{item.grade_class} — {item.theme_occasion}: {item.presentation_item} "
        f"(Incharges: {item.teacher_incharges})"
        for item in showcases
    )


def _claim_reminder(event_date: date, now: datetime) -> bool:
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(
            "INSERT INTO showcase_reminder_deliveries "
            "(event_date, status, claimed_at) VALUES (?, 'generated', ?) "
            "ON CONFLICT(event_date) DO UPDATE SET "
            "status = 'generated', claimed_at = excluded.claimed_at, "
            "accepted_at = '', status_updated_at = '', wa_message_id = '' "
            "WHERE showcase_reminder_deliveries.status = 'failed'",
            (event_date.isoformat(), now.strftime("%d-%m-%Y %H:%M:%S IST")),
        )
        db.commit()
        return cursor.rowcount == 1


def _finish_reminder(
    event_date: date,
    sent: bool,
    now: datetime,
    wa_message_id: str,
) -> None:
    timestamp = now.strftime("%d-%m-%Y %H:%M:%S IST")
    with sqlite3.connect(DB_PATH) as db:
        if sent:
            db.execute(
                "UPDATE showcase_reminder_deliveries SET "
                "status = 'accepted', accepted_at = ?, "
                "status_updated_at = ?, wa_message_id = ? "
                "WHERE event_date = ?",
                (timestamp, timestamp, wa_message_id, event_date.isoformat()),
            )
        else:
            db.execute(
                "UPDATE showcase_reminder_deliveries SET "
                "status = 'failed', status_updated_at = ? "
                "WHERE event_date = ? AND status = 'generated'",
                (timestamp, event_date.isoformat()),
            )
        db.commit()


async def send_showcase_reminders(now: datetime | None = None) -> int:
    current = now or datetime.now(IST)
    sent_count = 0
    for event_date, showcases in sorted(due_showcases(current.date()).items()):
        if not _claim_reminder(event_date, current):
            continue
        try:
            sent = await whatsapp_service.send_cloud_template_message(
                to=SHOWCASE_REMINDER_PHONE,
                template_name=SHOWCASE_REMINDER_TEMPLATE,
                language_code="en",
                body_params=[
                    event_date.strftime("%d %B %Y"),
                    format_showcase_details(showcases),
                ],
            )
        except Exception:
            logger.exception(
                "Musical showcase reminder errored for %s",
                event_date.isoformat(),
            )
            sent = False
        _finish_reminder(
            event_date,
            sent,
            datetime.now(IST),
            whatsapp_service.last_cloud_template_message_id,
        )
        if sent:
            sent_count += 1
            logger.info(
                "Musical showcase reminder accepted for %s (%d item(s))",
                event_date.isoformat(), len(showcases),
            )
        else:
            logger.error(
                "Musical showcase reminder failed for %s",
                event_date.isoformat(),
            )
    return sent_count


async def record_showcase_delivery_status(
    wa_message_id: str,
    status: str,
    occurred_at: datetime,
) -> bool:
    if not wa_message_id or status not in {"sent", "delivered", "read", "failed"}:
        return False
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE showcase_reminder_deliveries SET "
            "status = ?, status_updated_at = ? WHERE wa_message_id = ?",
            (
                status,
                occurred_at.astimezone(IST).strftime("%d-%m-%Y %H:%M:%S IST"),
                wa_message_id,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1
    finally:
        await db.close()


def send_showcase_reminders_sync() -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(send_showcase_reminders())
    finally:
        loop.close()
