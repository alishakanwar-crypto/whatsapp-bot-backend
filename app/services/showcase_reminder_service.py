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
SHOWCASE_REMINDER_LEAD_DAYS = 3


class Showcase(NamedTuple):
    event_date: date
    grade_class: str
    theme_occasion: str
    presentation_item: str
    teacher_incharges: str


SHOWCASES = (
    Showcase(date(2026, 7, 16), "Nursery", "Animals", "Old Mac Donald had a farm", "Respective CTs"),
    Showcase(date(2026, 8, 3), "Nursery", "Friendship Day", "The more we get together", "Respective CTs"),
    Showcase(date(2026, 8, 12), "Nursery", "World Elephant Day", "Elephant Rally", "Respective CTs"),
    Showcase(date(2026, 8, 13), "Nursery", "Independence Day", "Patriotic Group Song - Nanha Munha Raahi Hu", "Respective CTs"),
    Showcase(date(2026, 9, 3), "Nursery", "Janmashtami", "Shri Krishna Govind Hare Murari / Chanting - Hare Krishna Hare Rama", "Respective CTs"),
    Showcase(date(2026, 9, 11), "Nursery", "Grandparents Day", "Daadu Daadi Nana Naani", "Respective CTs"),
    Showcase(date(2026, 7, 16), "Prep", "Animals", "Old Mac Donald had a farm", "Respective CTs"),
    Showcase(date(2026, 8, 3), "Prep", "Friendship Day", "The more we get together", "Respective CTs"),
    Showcase(date(2026, 8, 12), "Prep", "World Elephant Day", "Elephant Rally", "Respective CTs"),
    Showcase(date(2026, 8, 13), "Prep", "Independence Day", "National Symbols Rhyme / Saare jahan se acha", "Respective CTs"),
    Showcase(date(2026, 9, 3), "Prep", "Janmashtami", "Shri Krishna Govind Hare Murari / Chanting - Hare Krishna Hare Rama", "Respective CTs"),
    Showcase(date(2026, 9, 11), "Prep", "Grandparents Day", "S se Super", "Respective CTs"),
    Showcase(date(2026, 7, 20), "Grade I", "Tiger Day", "Symphony of the Stripes", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 7, 31), "Grade I", "Friendship Day", "Harmony of Hearts", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 8, 11), "Grade I", "Independence Day", "Patriotic Group Song - Nanha Munha Raahi Hu", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 9, 2), "Grade I", "Janmashtami", "Prayer - Achutam keshavam", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 8, 27), "Grade I", "Raksha Bandhan", "Tie of Love - Musical Presentation", "Ms. Pallavi & Ms. Muskaan"),
    Showcase(date(2026, 7, 31), "Grade II", "Theme of the Month", "We Are a Family Song", "Ms. Tanvi & Ms. Gargi"),
    Showcase(date(2026, 8, 11), "Grade II", "Independence Day", "Patriotic Group Song - Nanha Munha Raahi Hu", "Ms. Tanvi & Ms. Gargi"),
    Showcase(date(2026, 9, 2), "Grade II", "Janmashtami", "Prayer - Achutam keshavam", "Ms. Tanvi & Ms. Gargi"),
    Showcase(date(2026, 8, 27), "Grade II", "Raksha Bandhan", "Tie of Love - Musical Presentation", "Ms. Tanvi & Ms. Gargi"),
    Showcase(date(2026, 7, 17), "Grade III", "Nelson Mandela Day", "Oh Nelson Mandela", "Ms. Harnoor & Ms. Shreya"),
    Showcase(date(2026, 7, 30), "Grade III", "Literary Presentation", "Doha Presentation", "Ms. Seema & Ms. Yamini"),
    Showcase(date(2026, 8, 12), "Grade III", "Independence Day", "News Showcase", "Ms. Shreya & Ms. Harnoor"),
    Showcase(date(2026, 9, 2), "Grade III", "Janmashtami", "Prayer - Itni shakti", "Music Team"),
    Showcase(date(2026, 8, 7), "Grade III", "Independence Day", "Hum Bharat Ke Bacche Hain", "Ms. Seema & Ms. Yamini"),
    Showcase(date(2026, 7, 24), "Grade IV", "Literary Presentation", "Doha Presentation", "Ms. Seema"),
    Showcase(date(2026, 8, 11), "Grade IV", "Independence Day", "Hum Bharat Ke Bacche Hain", "Ms. Seema & Ms. Yamini"),
    Showcase(date(2026, 8, 27), "Grade IV", "Janmashtami", "Prayer - Krishna Teri murli", "Music Team"),
    Showcase(date(2026, 7, 23), "Grade V", "Poetry - Nature Theme", "All Things Bright and Beautiful", "Ms. Prabhjot"),
    Showcase(date(2026, 7, 28), "Grade V", "Poetry", "Kavita Prastuti - Hum Bharat ke Bachhe hain", "Ms. Shikha"),
    Showcase(date(2026, 8, 26), "Grade V", "Janmashtami", "Prayer - Krishna Teri murli", "Music Team"),
    Showcase(date(2026, 8, 12), "Grade V", "Independence Day", "Khoob Ladi Mardani", "Ms. Shikha"),
    Showcase(date(2026, 8, 3), "Grade VI", "Punjabi Folk", "Painti-Akhari Punjabi Song", "Ms. Jasleen"),
    Showcase(date(2026, 8, 7), "Grade VI", "Independence Day", "Mera rang de basanti chola", "Ms. Shikha"),
    Showcase(date(2026, 8, 12), "Grade VI", "Independence Day", "Freedom Fighters Tableau", "Ms. Poonam"),
    Showcase(date(2026, 8, 7), "Grade VII", "Independence Day", "Freedom Fighters Tableau", "Ms. Poonam"),
    Showcase(date(2026, 8, 13), "Grade VII", "Independence Day", "Patriotic Medley", "Ms. Rashmi"),
    Showcase(date(2026, 7, 31), "Grade VIII", "Poetry", "Kavita Prastuti", "Ms. Rashmi"),
    Showcase(date(2026, 8, 7), "Grade VIII", "Independence Day", "Freedom Fighters Tableau", "Ms. Mansi"),
    Showcase(date(2026, 8, 13), "Grade VIII", "Independence Day", "Patriotic Medley", "Music Team"),
    # Nursery to Grade II sheet, September to December 2026.
    Showcase(date(2026, 9, 22), "Nursery", "Ganesh Chaturthi", "Oh! My Friend Ganesha", "Respective CTs"),
    Showcase(date(2026, 9, 30), "Nursery", "Letterland", "Letter Y Song", "Respective CTs"),
    Showcase(date(2026, 10, 1), "Nursery", "Gandhi Jayanti", "Raghupati Raghav", "Respective CTs"),
    Showcase(date(2026, 10, 15), "Nursery", "Dusshehra", "RamKatha", "Respective CTs"),
    Showcase(date(2026, 9, 22), "Prep", "Ganesh Chaturthi", "Oh! My Friend Ganesha", "Respective CTs"),
    Showcase(date(2026, 10, 1), "Prep", "Gandhi Jayanti", "Gandhi Ji k bache - Rhyme", "Respective CTs"),
    Showcase(date(2026, 10, 10), "Prep", "Navratre", "Showcase for Parents - Theatre, Music & Dance", "Respective CTs"),
    Showcase(date(2026, 10, 16), "Prep", "Dusshehra", "Ram Katha", "Respective CTs"),
    Showcase(date(2026, 10, 1), "Grade I", "Swatch Bharat", "Cleanliness Drive", "Respective CTs"),
    Showcase(date(2026, 10, 16), "Grade I", "Gandhi Jayanti", "Raghupati Raghav", "Respective CTs"),
    Showcase(date(2026, 10, 1), "Grade II", "Gandhi Jayanti", "Special Assembly", "Respective CTs"),
    Showcase(date(2026, 10, 15), "Grade II", "Dusshehra", "Hey Ram! Prayer", "Respective CTs"),
    # Grade III rows of the same sheet, October to December 2026.
    Showcase(date(2026, 10, 13), "Grade III", "Gandhi Jayanti", "Raghupati Raghav Raja Ram + Ram Aayenge", "Respective CTs"),
    # Alisha's revisions of 28 October 2026: Grade III's Guru Nanak showcase
    # became a Diwali one on 30 October, and Grades I and III each gained a
    # November showcase.
    Showcase(date(2026, 10, 30), "Grade III", "Diwali", "Ram Aenge", "Respective CTs"),
    # Alisha's live November-December 2026 showcase plan sheet, re-synced on
    # 3 November 2026: every row of that sheet whose date she has fixed.
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
    Showcase(date(2026, 12, 19), 'Prep', 'A World Without Water & Air', 'Speaking Competition', 'Respective CTs'),
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
    Showcase(date(2026, 12, 24), 'Grade 1 - Grade 8', 'Christmas', 'Special Assembly — Repeat (Presenting grade to be decided in line with the musical showcase and Broadway allocations)', 'Ms. Avneet & Ms. Riya'),
)


def due_showcases(today: date) -> dict[date, list[Showcase]]:
    due: dict[date, list[Showcase]] = defaultdict(list)
    for showcase in SHOWCASES:
        days_until = (showcase.event_date - today).days
        if days_until == SHOWCASE_REMINDER_LEAD_DAYS:
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
