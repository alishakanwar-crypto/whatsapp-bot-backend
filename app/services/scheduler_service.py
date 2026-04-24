import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.services.whatsapp_service import send_whatsapp_message
from app.services.sheet_refresh_service import refresh_teacher_data_sync, populate_parent_phones_sync
from app.services.email_polling_service import poll_homework_emails_sync

logger = logging.getLogger(__name__)

# Admin phone number for low-balance alerts
ADMIN_PHONE = "918076455224"
LOW_BALANCE_THRESHOLD = 2.0  # USD
_last_low_balance_alert_sent = False  # Avoid spamming alerts

scheduler = BackgroundScheduler()

# Store scheduled reminders: list of {chat_id, message, job_id}
SCHEDULED_REMINDERS: list[dict] = []

# Default lunch reminder for PPIS BOT group
DEFAULT_REMINDERS = [
    {
        "chat_id": "120363427415804526@g.us",
        "message": (
            "🍽️ *Lunch Reminder* 🍽️\n\n"
            "Dear Students & Staff,\n\n"
            "It's 2:00 PM — time for your lunch break!\n"
            "Please proceed to the dining area in an orderly manner.\n\n"
            "Remember to wash your hands before eating. "
            "Enjoy your nutritious meal! 😊\n\n"
            "— PP International School"
        ),
        "job_id": "lunch_reminder_ppis_bot",
    }
]


def _check_openai_credits_sync() -> None:
    """Check OpenAI API health by making a tiny API call.

    If the call fails with a quota error, send a WhatsApp alert to the admin.
    This is more reliable than polling billing endpoints (which require special scopes).
    """
    global _last_low_balance_alert_sent
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return

    try:
        with httpx.Client(timeout=15.0) as client:
            # Make a minimal chat completion to test if API key has quota
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                },
            )
            if resp.status_code == 200:
                logger.info("OpenAI credit check passed — API is working")
                _last_low_balance_alert_sent = False  # Reset flag
                return

            body = resp.text
            if "insufficient_quota" in body or resp.status_code == 429:
                logger.warning("OpenAI credit check: quota exceeded or rate limited")
                if not _last_low_balance_alert_sent:
                    alert_msg = (
                        "*PPIS Bot — OpenAI Credit Alert*\n\n"
                        "OpenAI credits have run out or are very low. "
                        "The bot is currently using basic fixed replies instead of AI responses.\n\n"
                        "Please top up your OpenAI credits:\n"
                        "https://platform.openai.com/settings/organization/billing/overview\n\n"
                        "Once you add credits, the bot will automatically switch back to AI replies within 5 minutes."
                    )
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(send_whatsapp_message(ADMIN_PHONE, alert_msg))
                        _last_low_balance_alert_sent = True
                        logger.info(f"Low balance alert sent to {ADMIN_PHONE}")
                    finally:
                        loop.close()
            else:
                logger.info(f"OpenAI credit check returned status {resp.status_code}")

    except Exception as e:
        logger.warning(f"Could not check OpenAI credits: {e}")


def _send_reminder_sync(chat_id: str, message: str) -> None:
    """Synchronous wrapper to send a WhatsApp reminder."""
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(send_whatsapp_message(chat_id, message))
        if result:
            logger.info(f"Scheduled reminder sent to {chat_id}")
        else:
            logger.error(f"Failed to send scheduled reminder to {chat_id}")
    except Exception as e:
        logger.error(f"Error sending scheduled reminder: {e}")
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Birthday Wishes
# ---------------------------------------------------------------------------

# Path to the student DOB JSON file (bundled with the app)
_STUDENT_DOB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "static", "student_dobs.json"
)


def _normalize_phone(phone: str) -> str:
    """Normalise a phone string to a single 10-digit number with 91 prefix."""
    if not phone:
        return ""
    # Take only the first number if multiple are listed (separated by / or ,)
    phone = phone.split("/")[0].split(",")[0].strip()
    # Remove non-digit characters
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return ""
    # Ensure 91 prefix
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    elif len(digits) == 12 and digits.startswith("91"):
        pass  # already good
    else:
        # Best-effort: just prepend 91 if short
        if len(digits) < 12:
            digits = "91" + digits
    return digits


def _load_student_dob_data_sync() -> None:
    """Load student DOB data from JSON file into the student_birthdays DB table.

    This runs once at startup (or daily) to keep the DB in sync with the JSON.
    """
    import aiosqlite

    dob_path = _STUDENT_DOB_PATH
    if not os.path.exists(dob_path):
        logger.warning(f"Student DOB file not found at {dob_path}")
        return

    try:
        with open(dob_path, "r", encoding="utf-8") as f:
            students = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read student DOB JSON: {e}")
        return

    if not students:
        logger.warning("Student DOB JSON is empty")
        return

    logger.info(f"Loading {len(students)} student DOB records into database...")

    from app.database import DB_PATH

    loop = asyncio.new_event_loop()
    try:
        async def _do_load():
            db = await aiosqlite.connect(DB_PATH)
            try:
                # Clear existing data and reload
                await db.execute("DELETE FROM student_birthdays")
                for s in students:
                    name = s.get("name", "").strip()
                    grade = s.get("grade", "").strip()
                    dob = s.get("dob", "").strip()
                    father_phone = s.get("father_phone", "").strip()
                    mother_phone = s.get("mother_phone", "").strip()
                    if name and dob:
                        await db.execute(
                            "INSERT INTO student_birthdays "
                            "(student_name, grade, dob, father_phone, mother_phone) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (name, grade, dob, father_phone, mother_phone),
                        )
                await db.commit()
                cursor = await db.execute("SELECT COUNT(*) FROM student_birthdays")
                row = await cursor.fetchone()
                logger.info(f"Loaded {row[0]} student birthday records into DB")
            finally:
                await db.close()

        loop.run_until_complete(_do_load())
    except Exception as e:
        logger.error(f"Error loading student DOB data: {e}")
    finally:
        loop.close()


def _send_birthday_wishes_sync() -> None:
    """Check for students whose birthday is today and send wishes to parents.

    Runs daily at midnight IST (18:30 UTC previous day).
    Uses the student_birthdays table which is loaded from the DOB JSON.
    """
    import aiosqlite
    from app.database import DB_PATH

    # Get today's date in IST (UTC+5:30)
    utc_now = datetime.utcnow()
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    today_mm_dd = ist_now.strftime("%m-%d")
    today_str = ist_now.strftime("%Y-%m-%d")

    logger.info(f"Birthday check running for IST date: {ist_now.strftime('%Y-%m-%d')} (MM-DD: {today_mm_dd})")

    loop = asyncio.new_event_loop()
    try:
        async def _do_birthday_check():
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            try:
                # Find students whose DOB matches today's month-day
                # DOB format in DB is YYYY-MM-DD
                cursor = await db.execute(
                    "SELECT * FROM student_birthdays "
                    "WHERE substr(dob, 6) = ? AND (last_wish_sent IS NULL OR last_wish_sent != ?)",
                    (today_mm_dd, today_str),
                )
                birthday_students = await cursor.fetchall()

                if not birthday_students:
                    logger.info(f"No birthdays today ({today_mm_dd})")
                    return

                logger.info(f"Found {len(birthday_students)} birthday(s) today!")

                sent_count = 0
                for student in birthday_students:
                    student_name = student["student_name"]
                    grade = student["grade"]
                    father_phone = _normalize_phone(student["father_phone"])
                    mother_phone = _normalize_phone(student["mother_phone"])

                    wish_msg = (
                        f"Dear Parent,\n\n"
                        f"PP International School wishes *{student_name}* ({grade}) "
                        f"a very Happy Birthday!\n\n"
                        f"May this special day bring joy, laughter, and wonderful "
                        f"memories. We hope the year ahead is filled with success "
                        f"and happiness.\n\n"
                        f"Happy Birthday, {student_name}!\n\n"
                        f"Thank you for your cooperation.\n"
                        f"Warm regards,\nPP International School"
                    )

                    # Send to both parents (deduplicate if same number)
                    sent_phones = set()
                    for phone in [father_phone, mother_phone]:
                        if phone and phone not in sent_phones:
                            result = await send_whatsapp_message(phone, wish_msg)
                            if result:
                                sent_phones.add(phone)
                                sent_count += 1
                                logger.info(
                                    f"Birthday wish sent to {phone} for {student_name}"
                                )
                            else:
                                logger.warning(
                                    f"Failed to send birthday wish to {phone} for {student_name}"
                                )
                            # Small delay between messages
                            await asyncio.sleep(2)

                    # Mark as sent for today
                    await db.execute(
                        "UPDATE student_birthdays SET last_wish_sent = ? WHERE id = ?",
                        (today_str, student["id"]),
                    )

                await db.commit()
                logger.info(f"Birthday wishes sent: {sent_count} messages for {len(birthday_students)} students")

            finally:
                await db.close()

        loop.run_until_complete(_do_birthday_check())
    except Exception as e:
        logger.error(f"Error sending birthday wishes: {e}")
    finally:
        loop.close()


def start_scheduler() -> None:
    """Start the scheduler with default reminders."""
    for reminder in DEFAULT_REMINDERS:
        # 2:00 PM IST = 8:30 UTC (IST is UTC+5:30)
        trigger = CronTrigger(hour=8, minute=30, second=0)
        scheduler.add_job(
            _send_reminder_sync,
            trigger=trigger,
            args=[reminder["chat_id"], reminder["message"]],
            id=reminder["job_id"],
            replace_existing=True,
        )
        logger.info(
            f"Scheduled reminder '{reminder['job_id']}' "
            f"to {reminder['chat_id']} at 2:00 PM IST (8:30 UTC) daily"
        )
        SCHEDULED_REMINDERS.append(reminder)

    # Schedule OpenAI credit balance check every 12 hours
    scheduler.add_job(
        _check_openai_credits_sync,
        trigger=IntervalTrigger(hours=12),
        id="openai_credit_check",
        replace_existing=True,
    )
    logger.info("Scheduled OpenAI credit balance check every 12 hours")

    # Schedule Google Sheet teacher data refresh every 14 days (fortnightly)
    # Also run once at startup (after 30 seconds delay to let app initialize)
    scheduler.add_job(
        refresh_teacher_data_sync,
        trigger=IntervalTrigger(days=14),
        id="sheet_refresh_teacher_data",
        replace_existing=True,
    )
    logger.info("Scheduled teacher data refresh from Google Sheet every 14 days (fortnightly)")

    # Run initial refresh after 30 seconds
    scheduler.add_job(
        refresh_teacher_data_sync,
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=30),
        id="sheet_refresh_initial",
        replace_existing=True,
    )
    logger.info("Scheduled initial teacher data refresh in 30 seconds")

    # --- Birthday Wishes ---
    # Load student DOB data into DB at startup (after 45 seconds to let DB init)
    scheduler.add_job(
        _load_student_dob_data_sync,
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=45),
        id="load_student_dob_initial",
        replace_existing=True,
    )
    logger.info("Scheduled initial student DOB data load in 45 seconds")

    # Send birthday wishes daily at midnight IST (18:30 UTC previous day)
    scheduler.add_job(
        _send_birthday_wishes_sync,
        trigger=CronTrigger(hour=18, minute=30, second=0),
        id="birthday_wishes_daily",
        replace_existing=True,
    )
    logger.info("Scheduled daily birthday wishes at midnight IST (18:30 UTC)")

    # Also refresh DOB data daily (in case JSON is updated)
    scheduler.add_job(
        _load_student_dob_data_sync,
        trigger=CronTrigger(hour=18, minute=0, second=0),
        id="refresh_student_dob_daily",
        replace_existing=True,
    )
    logger.info("Scheduled daily student DOB data refresh at 18:00 UTC")

    # --- Parent Phone Data Refresh ---
    # Refresh parent phone numbers from personalized_parents.json every 14 days (fortnightly)
    scheduler.add_job(
        populate_parent_phones_sync,
        trigger=IntervalTrigger(days=14),
        id="parent_phones_refresh",
        replace_existing=True,
    )
    logger.info("Scheduled parent phone data refresh every 14 days (fortnightly)")

    # --- Teacher Homework Email Polling ---
    # Poll info@ppischool.in IMAP inbox every 10 minutes for homework emails
    scheduler.add_job(
        poll_homework_emails_sync,
        trigger=IntervalTrigger(minutes=10),
        id="email_homework_poll",
        replace_existing=True,
    )
    logger.info("Scheduled teacher homework email polling every 10 minutes")

    # Run initial email poll after 60 seconds (let other services initialize first)
    scheduler.add_job(
        poll_homework_emails_sync,
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=60),
        id="email_homework_poll_initial",
        replace_existing=True,
    )
    logger.info("Scheduled initial email homework poll in 60 seconds")

    scheduler.start()
    logger.info("Scheduler started successfully")


def stop_scheduler() -> None:
    """Stop the scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")
