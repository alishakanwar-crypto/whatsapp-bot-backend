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
SHOWCASE_REMINDER_PHONES = tuple(
    phone.strip()
    for phone in os.environ.get(
        "SHOWCASE_REMINDER_PHONES",
        f"{os.environ.get('SHOWCASE_REMINDER_PHONE', '918076455224')},919599488106",
    ).split(",")
    if phone.strip()
)
SHOWCASE_REMINDER_TEMPLATE = os.environ.get(
    "SHOWCASE_REMINDER_TEMPLATE", "ppis_musical_showcase_reminder",
)
SHOWCASE_REMINDER_LEAD_DAYS = 2
SHOWCASE_SLIDE_LEAD_DAYS = 2
SHOWCASE_SLIDE_TEMPLATE = os.environ.get(
    "SHOWCASE_SLIDE_TEMPLATE", "ppis_showcase_slide_preview",
)
SHOWCASE_SLIDE_BASE_URL = os.environ.get(
    "SHOWCASE_SLIDE_BASE_URL",
    "https://ppis-whatsapp-bot.fly.dev/static/showcase_slides",
)


class Showcase(NamedTuple):
    event_date: date
    grade_class: str
    theme_occasion: str
    presentation_item: str
    teacher_incharges: str


SHOWCASES = (
    # Alisha's final November-December 2026 plan sheet (4 November 2026) is
    # the only source of reminders now: she asked for the earlier sheets'
    # rows to stop being synced, so they are no longer carried here.
    # The two undated Grade VIII speaking rows get reminders once dated.
    Showcase(date(2026, 11, 12), 'Nursery', "Children's Day", 'Dance Showcase', 'Mr. Akshay'),
    Showcase(date(2026, 11, 23), 'Nursery', 'Guru Nanak Jayanti', 'Musical Showcase: Ek Satnam Waheguru', 'Respective CTs'),
    Showcase(date(2026, 11, 25), 'Nursery', 'Speaking Activity', 'Speaking Activity - Fancy Dress Competition (Community Helpers)', 'Respective CTs'),
    Showcase(date(2026, 12, 10), 'Nursery', 'Christmas', 'Christmas Carols', 'Respective CTs'),
    Showcase(date(2026, 12, 22), 'Nursery', 'Little Promises - Big Changes', 'Speaking Competition', 'Respective CTs'),
    Showcase(date(2026, 11, 12), 'Prep', "Children's Day", 'Dance Showcase', 'Mr. Akshay'),
    Showcase(date(2026, 11, 18), 'Prep', 'Speaking Activity', 'Speaking Activity - Fancy Dress Competition (Community Helpers)', 'Respective CTs'),
    Showcase(date(2026, 11, 23), 'Prep', 'Guru Nanak Jayanti', 'Musical Showcase: Ek Satnam Waheguru', 'Respective CTs'),
    Showcase(date(2026, 12, 19), 'Prep', 'Broadway', 'Christmas Carols/Characterwalk', 'Respective CTs'),
    Showcase(date(2026, 12, 21), 'Prep', 'A World Without Water & Air', 'Speaking Competition', 'Respective CTs'),
    Showcase(date(2026, 11, 5), 'Grade I', 'Diwali', 'Musical Showcase: Lighting Up the World', 'Respective CTs'),
    Showcase(date(2026, 11, 13), 'Grade I', "Children's Day", 'Dance Showcase', 'Mr. Akshay'),
    Showcase(date(2026, 11, 16), 'Grade I', 'Hindi Speaking Competition', 'Hindi Poem Recitation Competition (Round 2)', 'Respective CTs'),
    Showcase(date(2026, 11, 19), 'Grade I', 'Guru Nanak Jayanti', 'Parbhat pheri - A walk of Faith', 'Respective CTs'),
    Showcase(date(2026, 11, 20), 'Grade I', 'Guru Nanak Jayanti', 'Visit to Gurudwara', 'Respective CTs'),
    Showcase(date(2026, 12, 19), 'Grade I', 'Broadway', 'The Lion King', 'Ms. Muskan'),
    Showcase(date(2026, 12, 24), 'Grade I', 'My favourite character from Lion King', 'English Speaking Competition Round 1', 'Respective CTs'),
    Showcase(date(2026, 12, 30), 'Grade I', 'My favourite character from Lion King', 'English Speaking Competition Round 2', 'Respective CTs'),
    Showcase(date(2026, 11, 5), 'Grade II', 'Diwali', 'Special Assembly', 'Respective CTs'),
    Showcase(date(2026, 11, 19), 'Grade II', 'Guru Nanak Jayanti', 'Parbhat Feri', 'Respective CTs'),
    Showcase(date(2026, 11, 20), 'Grade II', 'Guru Nanak Jayanti', 'Gurdwara Visit', 'Respective CTs'),
    Showcase(date(2026, 11, 23), 'Grade II', 'Guru Nanak Jayanti', 'Musical Showcase - Shabad Sagar', 'Respective CTs'),
    Showcase(date(2026, 12, 7), 'Grade II', 'Speaking Competition', 'Intra Class Speaking Competition: My Favourite Book Character (Round 1)', 'Respective CTs'),
    Showcase(date(2026, 12, 11), 'Grade II', 'Speaking Competition', 'Intra Class Speaking Competition: My Favourite Book Character (Round 2)', 'Respective CTs'),
    Showcase(date(2026, 12, 19), 'Grade II', 'Broadway', 'The Wizard of Oz', 'Ms. Gargi'),
    Showcase(date(2026, 11, 5), 'Grade III', 'Diwali', 'Musical Showcase - Diwali Mashup Songs', 'CTs of Class 3'),
    Showcase(date(2026, 11, 13), 'Grade III', "Children's Day", 'Dance Showcase', 'Mr. Akshay'),
    Showcase(date(2026, 11, 30), 'Grade III', 'Guru Nanak Jayanti', 'Musical Showcase - Guru Nanak Jayanti', 'CTs of Class 3'),
    Showcase(date(2026, 12, 3), 'Grade III', 'Christmas', 'Christmas Carols', 'Ms. Harnoor'),
    Showcase(date(2026, 12, 4), 'Grade III', 'Speaking Activity - Round 1 - Hindi', 'आशु भाषण प्रतियोगिता', 'Ms. Seema'),
    Showcase(date(2026, 12, 11), 'Grade III', 'Speaking Activity - Round 2 - Hindi', 'आशु भाषण प्रतियोगिता', 'Ms. Seema'),
    Showcase(date(2026, 12, 19), 'Grade III', 'Broadway', 'Charlie and the Chocolate Factory', 'Ms. Harnoor'),
    Showcase(date(2026, 12, 23), 'Grade III', 'Quiz', 'GK Quiz', 'Ms. Jasleen'),
    Showcase(date(2026, 11, 13), 'Grade IV', 'Sports & Fitness', 'German Musical Showcase: Sport Macht Uns Fit (Sport Makes Us Fit)', 'Mr. Tarun'),
    Showcase(date(2026, 12, 8), 'Grade IV', 'Christmas', 'Hindi Poem Presentation - त्योहारों का देश', 'Ms. Seema'),
    Showcase(date(2026, 12, 14), 'Grade IV', 'Speaking Activity - English Round 1', 'Puppet-Powered Tales - Story Narration Using Puppets', 'Ms. Prabhjot'),
    Showcase(date(2026, 12, 21), 'Grade IV', 'Speaking Activity - English Finale', 'Puppet-Powered Tales - Story Narration Using Puppets', 'Ms. Prabhjot'),
    Showcase(date(2026, 12, 23), 'Grade IV', 'Christmas - French', 'Musical Showcase', 'Ms. Twinkle'),
    Showcase(date(2026, 12, 23), 'Grade IV', 'Quiz', 'GK Quiz', 'Ms. Jasleen'),
    Showcase(date(2026, 11, 16), 'Grade V', 'Saint Martin’s Day', 'German - Sankt Martinstag', 'Mr. Tarun'),
    Showcase(date(2026, 12, 6), 'Grade V', 'Christmas', "Let's Be Kind", 'Ms. Prabhjot'),
    Showcase(date(2026, 12, 15), 'Grade V', 'Christmas - French', 'Musical Showcase', 'Ms. Twinkle'),
    Showcase(date(2026, 12, 23), 'Grade V', 'Quiz', 'GK Quiz', 'Ms. Twinkle'),
    Showcase(date(2026, 12, 17), 'Grade V', 'Speaking Activity Hindi - Round 1', 'Doha Vachan', 'Ms. Shikha'),
    Showcase(date(2026, 12, 22), 'Grade V', 'Speaking Activity Hindi - Round 2', 'Doha Vachan', 'Ms. Shikha'),
    Showcase(date(2026, 12, 8), 'Grade VI', 'Christmas - French', 'Musical Showcase - Christmas French Carol', 'Ms. Twinkle'),
    Showcase(date(2026, 12, 10), 'Grade VI', 'Speaking Activity - Round 1 - English', "Debate (Books Offer Knowledge that Digital Entertainment Can't Replace)", 'Ms. Poonam'),
    Showcase(date(2026, 12, 16), 'Grade VI', 'Speaking Activity - Round 2 - English', "Debate (Books Offer Knowledge that Digital Entertainment Can't Replace)", 'Ms. Poonam'),
    Showcase(date(2026, 12, 23), 'Grade VI', 'Quiz', 'GK Quiz', 'Ms. Twinkle'),
    Showcase(date(2026, 12, 16), 'Grade VI', 'Poem Presentation - Hindi', '', 'Ms. Shikha'),
    Showcase(date(2026, 12, 1), 'Grade VII', 'Christmas - German', 'Musical Showcase - Christmas German Carol', 'Mr. Tarun'),
    Showcase(date(2026, 12, 7), 'Grade VII', 'Christmas - French', 'Musical Showcase - Christmas French Carol', 'Ms. Twinkle'),
    Showcase(date(2026, 12, 14), 'Grade VII', 'Speaking Activity - Round 1 - Hindi', 'विद्यालयों में नैतिक शिक्षा (Moral Education) अनिवार्य होनी चाहिए या नहीं', 'Ms. Rashmi'),
    Showcase(date(2026, 12, 16), 'Grade VII', 'Poem Presentation - English', "English Poem Presentation - 'Women: Empowered to Inspire'", 'Ms. Poonam'),
    Showcase(date(2026, 12, 21), 'Grade VII', 'Speaking Activity - Round 2 - Hindi', 'विद्यालयों में नैतिक शिक्षा (Moral Education) अनिवार्य होनी चाहिए या नहीं', 'Ms. Rashmi'),
    Showcase(date(2026, 12, 23), 'Grade VII', 'Quiz', 'GK Quiz', 'Ms. Alisha'),
    Showcase(date(2026, 12, 9), 'Grade VIII', 'Poem Presentation - Hindi', 'कोशिश करने वालों की हार नहीं होती', 'Ms. Rashmi'),
    Showcase(date(2026, 12, 17), 'Grade VIII', 'Christmas', 'Musical Showcase - French Carol', 'Ms. Riya'),
    Showcase(date(2026, 12, 23), 'Grade VIII', 'Quiz', 'GK Quiz', 'Ms. Alisha'),
    Showcase(date(2026, 12, 11), 'Grade IX', 'Christmas', 'Dance Showcase (Dance to be repeated on 24 Dec Special Assembly)', 'Mr. Akshay'),
    Showcase(date(2026, 11, 13), 'Grade X', "Children's Day", 'Special Assembly', 'Ms. Riya'),
    Showcase(date(2026, 12, 24), 'Grade 1 - Grade 8', 'Christmas',
             'Special Assembly — Repeat (Presenting grade to be decided in '
             'line with the musical showcase and Broadway allocations)',
             'Ms. Avneet & Ms. Riya'),
)


# The reference slide each non-musical showcase projects on the day, hosted
# under /static/showcase_slides. Keyed by date + class + theme because a class
# can present twice on one day (Prep's 19 December carols and speaking round).
# Musical showcases are deliberately absent: Alisha wants slides only for the
# items teachers have to prepare a slide for.
SHOWCASE_SLIDES: dict[tuple[date, str, str], str] = {
    (date(2026, 11, 12), 'Nursery', "Children's Day"): 'slide_r02',
    (date(2026, 11, 25), 'Nursery', 'Speaking Activity'): 'slide_r04',
    (date(2026, 12, 10), 'Nursery', 'Christmas'): 'slide_r05',
    (date(2026, 12, 22), 'Nursery', 'Little Promises - Big Changes'): 'slide_r06',
    (date(2026, 11, 12), 'Prep', "Children's Day"): 'slide_r08',
    (date(2026, 11, 18), 'Prep', 'Speaking Activity'): 'slide_r09',
    (date(2026, 12, 19), 'Prep', 'Broadway'): 'slide_r11',
    (date(2026, 12, 21), 'Prep', 'A World Without Water & Air'): 'slide_r12',
    (date(2026, 11, 13), 'Grade I', "Children's Day"): 'slide_g1_dance',
    (date(2026, 11, 16), 'Grade I', 'Hindi Speaking Competition'): 'slide_r15',
    (date(2026, 12, 19), 'Grade I', 'Broadway'): 'slide_r16',
    (date(2026, 12, 24), 'Grade I', 'My favourite character from Lion King'): 'slide_r17',
    (date(2026, 12, 30), 'Grade I', 'My favourite character from Lion King'): 'slide_r17',
    (date(2026, 11, 5), 'Grade II', 'Diwali'): 'slide_r19',
    (date(2026, 11, 19), 'Grade II', 'Guru Nanak Jayanti'): 'slide_r20',
    (date(2026, 11, 20), 'Grade II', 'Guru Nanak Jayanti'): 'slide_r21',
    (date(2026, 12, 7), 'Grade II', 'Speaking Competition'): 'slide_r23',
    (date(2026, 12, 11), 'Grade II', 'Speaking Competition'): 'slide_r24',
    (date(2026, 12, 19), 'Grade II', 'Broadway'): 'slide_r25',
    (date(2026, 11, 13), 'Grade III', "Children's Day"): 'slide_r28',
    (date(2026, 12, 3), 'Grade III', 'Christmas'): 'slide_r30',
    (date(2026, 12, 4), 'Grade III', 'Speaking Activity - Round 1 - Hindi'): 'slide_r31',
    (date(2026, 12, 11), 'Grade III', 'Speaking Activity - Round 2 - Hindi'): 'slide_r32',
    (date(2026, 12, 19), 'Grade III', 'Broadway'): 'slide_r33',
    (date(2026, 12, 23), 'Grade III', 'Quiz'): 'slide_r34',
    (date(2026, 12, 8), 'Grade IV', 'Christmas'): 'slide_r37',
    (date(2026, 12, 14), 'Grade IV', 'Speaking Activity - English Round 1'): 'slide_r38',
    (date(2026, 12, 21), 'Grade IV', 'Speaking Activity - English Finale'): 'slide_r39',
    (date(2026, 12, 23), 'Grade IV', 'Quiz'): 'slide_r41',
    (date(2026, 11, 16), 'Grade V', 'Saint Martin’s Day'): 'slide_r43',
    (date(2026, 12, 6), 'Grade V', 'Christmas'): 'slide_r44',
    (date(2026, 12, 23), 'Grade V', 'Quiz'): 'slide_r46',
    (date(2026, 12, 17), 'Grade V', 'Speaking Activity Hindi - Round 1'): 'slide_r47',
    (date(2026, 12, 22), 'Grade V', 'Speaking Activity Hindi - Round 2'): 'slide_r48',
    (date(2026, 12, 10), 'Grade VI', 'Speaking Activity - Round 1 - English'): 'slide_r51',
    (date(2026, 12, 16), 'Grade VI', 'Speaking Activity - Round 2 - English'): 'slide_r52',
    (date(2026, 12, 23), 'Grade VI', 'Quiz'): 'slide_r53',
    (date(2026, 12, 16), 'Grade VI', 'Poem Presentation - Hindi'): 'slide_r54',
    (date(2026, 12, 14), 'Grade VII', 'Speaking Activity - Round 1 - Hindi'): 'slide_r58',
    (date(2026, 12, 16), 'Grade VII', 'Poem Presentation - English'): 'slide_r61',
    (date(2026, 12, 21), 'Grade VII', 'Speaking Activity - Round 2 - Hindi'): 'slide_r59',
    (date(2026, 12, 23), 'Grade VII', 'Quiz'): 'slide_r60',
    (date(2026, 12, 9), 'Grade VIII', 'Poem Presentation - Hindi'): 'slide_r63',
    (date(2026, 12, 23), 'Grade VIII', 'Quiz'): 'slide_r65',
    (date(2026, 12, 11), 'Grade IX', 'Christmas'): 'slide_r69',
    (date(2026, 11, 13), 'Grade X', "Children's Day"): 'slide_r71',
}


def slide_for(showcase: Showcase) -> str:
    """Public URL of a showcase's reference slide thumbnail, if it has one."""
    name = SHOWCASE_SLIDES.get(
        (showcase.event_date, showcase.grade_class, showcase.theme_occasion)
    )
    if not name:
        return ""
    return f"{SHOWCASE_SLIDE_BASE_URL}/{name}_thumb.jpg"


def due_showcase_slides(today: date) -> list[Showcase]:
    """Showcases whose slide goes out, from two days before the event.

    The window stays open until the event day so a slide whose send failed —
    the template not approved yet, or Meta refusing the message — is tried
    again the next day instead of being lost. The delivery claim, not the
    date, is what stops a second copy.
    """
    return [
        showcase
        for showcase in SHOWCASES
        if 0 <= (showcase.event_date - today).days <= SHOWCASE_SLIDE_LEAD_DAYS
        and slide_for(showcase)
    ]


def due_showcases(today: date) -> dict[date, list[Showcase]]:
    due: dict[date, list[Showcase]] = defaultdict(list)
    for showcase in SHOWCASES:
        days_until = (showcase.event_date - today).days
        # Up to the lead time, not exactly on it: when the sheet changes, an
        # event closer than the lead time still gets its one reminder instead of
        # being skipped because its window has passed. Repeats are prevented by
        # the delivery claim, not by the date.
        if 0 <= days_until <= SHOWCASE_REMINDER_LEAD_DAYS:
            due[showcase.event_date].append(showcase)
    return dict(due)


def format_showcase_details(showcases: list[Showcase]) -> str:
    # Meta Cloud API rejects template body parameters that contain newlines,
    # tabs, or 4+ consecutive spaces, so multiple items are joined on one line.
    return " | ".join(
        f"{item.grade_class} — {item.theme_occasion}: {item.presentation_item} "
        f"(Incharges: {item.teacher_incharges})"
        for item in showcases
    )


def _claim_reminder(event_date: date, recipient: str, now: datetime) -> bool:
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(
            "INSERT INTO showcase_reminder_deliveries "
            "(event_date, recipient, status, claimed_at) "
            "VALUES (?, ?, 'generated', ?) "
            "ON CONFLICT(event_date, recipient) DO UPDATE SET "
            "status = 'generated', claimed_at = excluded.claimed_at, "
            "accepted_at = '', status_updated_at = '', wa_message_id = '' "
            "WHERE showcase_reminder_deliveries.status = 'failed'",
            (
                event_date.isoformat(),
                recipient,
                now.strftime("%d-%m-%Y %H:%M:%S IST"),
            ),
        )
        db.commit()
        return cursor.rowcount == 1


def _finish_reminder(
    event_date: date,
    recipient: str,
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
                "WHERE event_date = ? AND recipient = ?",
                (
                    timestamp,
                    timestamp,
                    wa_message_id,
                    event_date.isoformat(),
                    recipient,
                ),
            )
        else:
            db.execute(
                "UPDATE showcase_reminder_deliveries SET "
                "status = 'failed', status_updated_at = ? "
                "WHERE event_date = ? AND recipient = ? "
                "AND status = 'generated'",
                (timestamp, event_date.isoformat(), recipient),
            )
        db.commit()


async def send_showcase_reminders(now: datetime | None = None) -> int:
    current = now or datetime.now(IST)
    sent_count = 0
    for event_date, showcases in sorted(due_showcases(current.date()).items()):
        details = format_showcase_details(showcases)
        for recipient in SHOWCASE_REMINDER_PHONES:
            if not _claim_reminder(event_date, recipient, current):
                continue
            try:
                sent = await whatsapp_service.send_cloud_template_message(
                    to=recipient,
                    template_name=SHOWCASE_REMINDER_TEMPLATE,
                    language_code="en",
                    body_params=[event_date.strftime("%d %B %Y"), details],
                )
            except Exception:
                logger.exception(
                    "Musical showcase reminder errored for %s, recipient ending %s",
                    event_date.isoformat(),
                    recipient[-4:],
                )
                sent = False
            _finish_reminder(
                event_date,
                recipient,
                sent,
                datetime.now(IST),
                whatsapp_service.last_cloud_template_message_id,
            )
            if sent:
                sent_count += 1
                logger.info(
                    "Musical showcase reminder accepted for %s, recipient ending "
                    "%s (%d item(s))",
                    event_date.isoformat(),
                    recipient[-4:],
                    len(showcases),
                )
            else:
                logger.error(
                    "Musical showcase reminder failed for %s, recipient ending %s",
                    event_date.isoformat(),
                    recipient[-4:],
                )
    return sent_count


def _claim_slide(slide_key: str, recipient: str, now: datetime) -> bool:
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(
            "INSERT INTO showcase_slide_deliveries "
            "(slide_key, recipient, status, claimed_at) "
            "VALUES (?, ?, 'generated', ?) "
            "ON CONFLICT(slide_key, recipient) DO UPDATE SET "
            "status = 'generated', claimed_at = excluded.claimed_at "
            # A claim left 'generated' belongs to a send that died before it
            # could finish, so it is retried rather than skipped forever.
            "WHERE showcase_slide_deliveries.status IN ('failed', 'generated')",
            (slide_key, recipient, now.strftime("%d-%m-%Y %H:%M:%S IST")),
        )
        db.commit()
        return cursor.rowcount == 1


def _finish_slide(
    slide_key: str, recipient: str, sent: bool, now: datetime,
) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "UPDATE showcase_slide_deliveries SET "
            "status = ?, status_updated_at = ? "
            "WHERE slide_key = ? AND recipient = ?",
            (
                "accepted" if sent else "failed",
                now.strftime("%d-%m-%Y %H:%M:%S IST"),
                slide_key,
                recipient,
            ),
        )
        db.commit()


async def send_showcase_slides(now: datetime | None = None) -> int:
    """Send each non-musical showcase's reference slide two days ahead.

    The teachers need the slide in hand before the day, so this is a separate
    send from the text reminder, which goes out the same day: one message per
    showcase, carrying
    that showcase's own slide as the template's image header.
    """
    current = now or datetime.now(IST)
    sent_count = 0
    for showcase in due_showcase_slides(current.date()):
        slide_key = (
            f"{showcase.event_date.isoformat()}|{showcase.grade_class}"
            f"|{showcase.theme_occasion}"
        )
        item = showcase.presentation_item or showcase.theme_occasion
        for recipient in SHOWCASE_REMINDER_PHONES:
            if not _claim_slide(slide_key, recipient, current):
                continue
            try:
                sent = await whatsapp_service.send_cloud_template_message(
                    to=recipient,
                    template_name=SHOWCASE_SLIDE_TEMPLATE,
                    language_code="en",
                    body_params=[
                        showcase.grade_class,
                        item,
                        showcase.event_date.strftime("%d %B %Y"),
                    ],
                    header_image_url=slide_for(showcase),
                )
            except Exception:
                logger.exception(
                    "Showcase slide errored for %s, recipient ending %s",
                    slide_key,
                    recipient[-4:],
                )
                sent = False
            _finish_slide(slide_key, recipient, sent, datetime.now(IST))
            if sent:
                sent_count += 1
                logger.info(
                    "Showcase slide accepted for %s, recipient ending %s",
                    slide_key,
                    recipient[-4:],
                )
            else:
                logger.error(
                    "Showcase slide failed for %s, recipient ending %s",
                    slide_key,
                    recipient[-4:],
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


def send_showcase_slides_sync() -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(send_showcase_slides())
    finally:
        loop.close()
