import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.services.whatsapp_service import send_whatsapp_message
from app.services.sheet_refresh_service import refresh_teacher_data_sync, populate_parent_phones_sync, refresh_pi_sheet_full_sync
from app.services.email_polling_service import poll_homework_emails_sync

logger = logging.getLogger(__name__)

# Track consecutive health-monitor failures so we don't spam admin alerts
_health_monitor_alert_sent = False

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


# ---------------------------------------------------------------------------
# Always-Active Health Monitor — runs every 60 seconds
# ---------------------------------------------------------------------------
def _health_monitor_sync() -> None:
    """Check agent connection health and attempt self-recovery.

    Runs every 60 seconds.  If the agent is disconnected for >5 minutes
    it logs a warning.  Admin alerts are handled by the snapshot handler
    itself (after 3 consecutive failures) so this monitor focuses on
    logging and ensuring the health state is accurate.
    """
    global _health_monitor_alert_sent
    try:
        from app.routes.agent_ws import is_agent_connected, get_health_state

        connected = is_agent_connected()
        health = get_health_state()

        if connected:
            if _health_monitor_alert_sent:
                logger.info("Health monitor: agent reconnected — clearing alert flag")
                _health_monitor_alert_sent = False
            # Log periodic health summary (every check when connected)
            logger.debug(
                "Health monitor: agent=CONNECTED  snapshots_served=%d  "
                "snapshots_failed=%d  consecutive_failures=%d  uptime=%.0fs",
                health["total_snapshots_served"],
                health["total_snapshots_failed"],
                health["consecutive_failures"],
                health["uptime_seconds"],
            )
        else:
            logger.warning(
                "Health monitor: agent=DISCONNECTED  consecutive_failures=%d  "
                "last_connected_ago=%.0fs",
                health["consecutive_failures"],
                health["last_connected_seconds_ago"],
            )
            # If disconnected for >5 minutes and we haven't already alerted
            last_ago = health["last_connected_seconds_ago"]
            if last_ago > 300 and not _health_monitor_alert_sent:
                _health_monitor_alert_sent = True
                logger.error(
                    "Health monitor: agent disconnected for >5 minutes "
                    "(%.0fs) — logging critical alert",
                    last_ago,
                )
    except Exception as exc:
        logger.error("Health monitor check failed: %s", exc, exc_info=True)


def _check_openai_credits_sync() -> None:
    """Check OpenAI API health by making a tiny API call.

    If the call fails with a quota error, send a WhatsApp alert to the admin.
    This is more reliable than polling billing endpoints (which require special scopes).
    """
    global _last_low_balance_alert_sent
    from app.services.openai_service import _get_openai_key
    api_key = _get_openai_key()
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


def _send_daily_message_report_sync() -> None:
    """Generate and email a daily message history report to alisha.kanwar@ppischool.in.

    The report includes counts and samples of messages sent, received, and failed
    over the past 24 hours.
    """
    import aiosqlite
    from app.database import DB_PATH
    from app.services.email_service import send_email

    utc_now = datetime.utcnow()
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    yesterday_ist = ist_now - timedelta(days=1)
    report_date = yesterday_ist.strftime("%Y-%m-%d")
    # Query window: from yesterday 00:00 IST to today 00:00 IST (in UTC)
    start_utc = (yesterday_ist.replace(hour=0, minute=0, second=0, microsecond=0)
                 - timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = (ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
               - timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

    logger.info(f"Generating daily message report for {report_date} (UTC window: {start_utc} to {end_utc})")

    loop = asyncio.new_event_loop()
    try:
        async def _generate_report():
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            try:
                # Total messages
                cur = await db.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE timestamp >= ? AND timestamp < ?",
                    (start_utc, end_utc),
                )
                total = (await cur.fetchone())["cnt"]

                # Incoming (received from parents/teachers)
                cur = await db.execute(
                    "SELECT COUNT(*) as cnt FROM messages "
                    "WHERE timestamp >= ? AND timestamp < ? AND direction = 'incoming'",
                    (start_utc, end_utc),
                )
                incoming = (await cur.fetchone())["cnt"]

                # Outgoing (sent by bot)
                cur = await db.execute(
                    "SELECT COUNT(*) as cnt FROM messages "
                    "WHERE timestamp >= ? AND timestamp < ? AND direction = 'outgoing'",
                    (start_utc, end_utc),
                )
                outgoing = (await cur.fetchone())["cnt"]

                # Unique senders (parents who messaged)
                cur = await db.execute(
                    "SELECT COUNT(DISTINCT sender) as cnt FROM messages "
                    "WHERE timestamp >= ? AND timestamp < ? AND direction = 'incoming'",
                    (start_utc, end_utc),
                )
                unique_parents = (await cur.fetchone())["cnt"]

                # Forwarded to teachers
                cur = await db.execute(
                    "SELECT COUNT(*) as cnt FROM forwarded_conversations "
                    "WHERE created_at >= ? AND created_at < ?",
                    (start_utc, end_utc),
                )
                forwarded = (await cur.fetchone())["cnt"]

                # Leave applications
                cur = await db.execute(
                    "SELECT COUNT(*) as cnt FROM leave_applications "
                    "WHERE created_at >= ? AND created_at < ?",
                    (start_utc, end_utc),
                )
                leaves = (await cur.fetchone())["cnt"]

                # Sample recent messages (last 20 incoming)
                cur = await db.execute(
                    "SELECT sender, content, timestamp FROM messages "
                    "WHERE timestamp >= ? AND timestamp < ? AND direction = 'incoming' "
                    "ORDER BY timestamp DESC LIMIT 20",
                    (start_utc, end_utc),
                )
                recent_msgs = await cur.fetchall()

                # Build report
                report = (
                    f"PPIS WhatsApp Bot - Daily Message Report\n"
                    f"========================================\n"
                    f"Date: {report_date} (IST)\n\n"
                    f"Summary:\n"
                    f"  Total messages:           {total}\n"
                    f"  Messages received:        {incoming}\n"
                    f"  Messages sent (by bot):   {outgoing}\n"
                    f"  Unique parents:           {unique_parents}\n"
                    f"  Queries forwarded:        {forwarded}\n"
                    f"  Leave applications:       {leaves}\n\n"
                )

                if recent_msgs:
                    report += "Recent Parent Messages (last 20):\n"
                    report += "-" * 50 + "\n"
                    for m in recent_msgs:
                        ts = m["timestamp"] or ""
                        sender_phone = m["sender"] or ""
                        content = (m["content"] or "")[:120]
                        report += f"  [{ts}] {sender_phone}: {content}\n"
                else:
                    report += "No messages received in this period.\n"

                return report

            finally:
                await db.close()

        report_text = loop.run_until_complete(_generate_report())

        if report_text:
            subject = f"PPIS Bot Daily Report - {report_date}"
            success = send_email(
                "alisha.kanwar@ppischool.in",
                subject,
                report_text,
                "PP International School Bot",
            )
            if success:
                logger.info(f"Daily message report sent to alisha.kanwar@ppischool.in for {report_date}")
            else:
                logger.error(f"Failed to send daily message report for {report_date}")
        else:
            logger.warning("Daily report generation returned empty")

    except Exception as e:
        logger.error(f"Error generating/sending daily message report: {e}", exc_info=True)
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

    # Run initial refresh after 60 seconds (was 30s — staggered to reduce memory spikes)
    scheduler.add_job(
        refresh_teacher_data_sync,
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=60),
        id="sheet_refresh_initial",
        replace_existing=True,
    )
    logger.info("Scheduled initial teacher data refresh in 60 seconds")

    # --- Birthday Wishes ---
    # Load student DOB data into DB at startup (after 120s — was 45s, staggered
    # so it doesn't overlap with sheet_refresh_initial which runs at 60s)
    scheduler.add_job(
        _load_student_dob_data_sync,
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=120),
        id="load_student_dob_initial",
        replace_existing=True,
    )
    logger.info("Scheduled initial student DOB data load in 120 seconds")

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

    # --- Daily PI Sheet Full Refresh ---
    # Fetch all grade tabs from PI Sheet every day at 07:30 IST (02:00 UTC).
    # Includes cross-grade dedup: removes promoted students from old grade tabs
    # so homework notifications only go to parents of the correct (current) grade.
    scheduler.add_job(
        refresh_pi_sheet_full_sync,
        trigger=CronTrigger(hour=2, minute=0),  # 07:30 IST = 02:00 UTC
        id="pi_sheet_daily_refresh",
        replace_existing=True,
    )
    logger.info("Scheduled daily PI Sheet full refresh at 07:30 IST (02:00 UTC)")

    # --- Teacher Homework Email Polling ---
    # Poll info@ppischool.in IMAP inbox every 60 minutes (was 30 — caused OOM on 256MB)
    # The poll itself now has memory guards and processes max 2 emails per run.
    scheduler.add_job(
        poll_homework_emails_sync,
        trigger=IntervalTrigger(minutes=60),
        id="email_homework_poll",
        replace_existing=True,
    )
    logger.info("Scheduled teacher homework email polling every 60 minutes")

    # Run initial email poll after 300 seconds (5 min) — let DB seed, sheet refresh,
    # and parent phone population all finish first so baseline memory is stable.
    scheduler.add_job(
        poll_homework_emails_sync,
        trigger="date",
        run_date=datetime.now() + timedelta(seconds=300),
        id="email_homework_poll_initial",
        replace_existing=True,
    )
    logger.info("Scheduled initial email homework poll in 300 seconds (5 min)")

    # --- Daily Message History Report ---
    # DISABLED per user request (Alisha Kanwar, 2026-05-28)
    # Previously sent daily report to alisha.kanwar@ppischool.in at 11:30 PM IST
    # scheduler.add_job(
    #     _send_daily_message_report_sync,
    #     trigger=CronTrigger(hour=18, minute=0, second=0),
    #     id="daily_message_report",
    #     replace_existing=True,
    # )
    logger.info("Daily message report DISABLED per user request")

    # --- Always-Active Health Monitor ---
    # Check agent connection health every 60 seconds.
    # Self-recovery is handled by the WebSocket auto-reconnect in the agent.
    # This monitor logs status and ensures the health state is accurate.
    scheduler.add_job(
        _health_monitor_sync,
        trigger=IntervalTrigger(seconds=60),
        id="health_monitor",
        replace_existing=True,
    )
    logger.info("Scheduled health monitor every 60 seconds")

    # --- Meal Monitoring ---
    # Loads enabled state from settings table. Use the control panel to toggle.
    import sqlite3 as _sqlite3
    _meal_enabled = False
    try:
        _conn = _sqlite3.connect("/data/app.db")
        _row = _conn.execute("SELECT value FROM settings WHERE key='meal_monitoring_enabled'").fetchone()
        _meal_enabled = _row and _row[0] == "1"
        _conn.close()
    except Exception:
        pass

    from app.services.meal_monitoring_service import run_meal_monitoring_sync
    if _meal_enabled:
        scheduler.add_job(
            run_meal_monitoring_sync,
            trigger=CronTrigger(hour=3, minute=20, second=0),
            args=["short_break"],
            id="meal_monitoring_short_break",
            replace_existing=True,
        )
        scheduler.add_job(
            run_meal_monitoring_sync,
            trigger=CronTrigger(hour=5, minute=50, second=0),
            args=["lunch"],
            id="meal_monitoring_lunch",
            replace_existing=True,
        )
        logger.info("Meal monitoring auto-trigger ENABLED (from settings)")
    else:
        logger.info("Meal monitoring auto-trigger DISABLED (toggle from control panel)")

    # --- Teacher Attendance Excel (DVR-based) — DISABLED ---
    # Replaced by TrueFace-based attendance tracking and reporting.
    # from app.services.teacher_attendance_excel import generate_teacher_attendance_excel_sync
    # scheduler.add_job(
    #     generate_teacher_attendance_excel_sync,
    #     trigger=CronTrigger(hour=4, minute=5, second=0),
    #     id="teacher_attendance_excel_daily",
    #     replace_existing=True,
    # )
    # scheduler.add_job(
    #     generate_teacher_attendance_excel_sync,
    #     trigger="date",
    #     run_date=datetime.now() + timedelta(seconds=180),
    #     id="teacher_attendance_excel_initial",
    #     replace_existing=True,
    # )
    logger.info("DVR-based teacher attendance Excel DISABLED — using TrueFace system")

    # --- TrueFace Attendance Reports ---
    # 9:00 AM IST (3:30 UTC) → Arrival report emailed
    from app.routes.trueface import send_arrival_report_sync, send_departure_report_sync
    scheduler.add_job(
        send_arrival_report_sync,
        trigger=CronTrigger(hour=3, minute=30, second=0),
        id="trueface_arrival_report",
        replace_existing=True,
    )
    logger.info("Scheduled TrueFace arrival report at 9:00 AM IST (3:30 UTC)")

    # 4:30 PM IST (11:00 UTC) → Departure report emailed
    scheduler.add_job(
        send_departure_report_sync,
        trigger=CronTrigger(hour=11, minute=0, second=0),
        id="trueface_departure_report",
        replace_existing=True,
    )
    logger.info("Scheduled TrueFace departure report at 4:30 PM IST (11:00 UTC)")

    # --- Gate Reconciliation Reports (every 30 min) ---
    # 6:00 AM - 5:00 PM IST → half-hourly gate head count report
    # IST → UTC: subtract 5h30m (6 AM IST = 00:30 UTC, 5 PM IST = 11:30 UTC)
    from app.routes.gate import send_reconciliation_report_sync
    for ist_minutes in range(6 * 60, 17 * 60 + 1, 30):  # 06:00–17:00 IST every 30 min
        utc_total = (ist_minutes - 330) % (24 * 60)  # subtract 5:30, wrap across midnight
        utc_h, utc_m = divmod(utc_total, 60)
        ist_h, ist_min = divmod(ist_minutes, 60)
        scheduler.add_job(
            send_reconciliation_report_sync,
            trigger=CronTrigger(hour=utc_h, minute=utc_m, second=0),
            id=f"gate_report_{ist_h:02d}{ist_min:02d}",
            replace_existing=True,
        )
    logger.info("Scheduled gate reconciliation reports every 30 min 6:00 AM - 5:00 PM IST")

    # --- Mood & Temperament Reports — PERMANENTLY DISABLED ---
    # Previously ran hourly 7 AM - 12 PM IST. Disabled per user request.
    logger.info("Mood reports DISABLED (permanently)")

    # --- Homework Delivery (Google Docs) ---
    # Check homework docs after each period ends.
    # Times are offset by +3 minutes to give teachers time to update.
    # IST → UTC: subtract 5h30m
    from app.services.homework_delivery_service import (
        run_homework_delivery_sync,
        run_daily_clear_sync,
    )

    # Regular timetable (until 30 Jun 2026):
    #   Period 1: 08:10-08:45  → check 08:48 IST = 03:18 UTC
    #   Period 2: 09:00-09:30  → check 09:33 IST = 04:03 UTC
    #   Period 3: 09:30-10:00  → check 10:03 IST = 04:33 UTC
    #   Period 4: 10:00-10:30  → check 10:33 IST = 05:03 UTC
    #   Period 5: 10:30-11:00  → check 11:03 IST = 05:33 UTC
    #   Period 6: 11:00-11:30  → check 11:33 IST = 06:03 UTC
    _hw_schedule_regular = [
        (1, 3, 18, "Period 1 ends 08:45 → check 08:48 IST"),
        (2, 4, 3,  "Period 2 ends 09:30 → check 09:33 IST"),
        (3, 4, 33, "Period 3 ends 10:00 → check 10:03 IST"),
        (4, 5, 3,  "Period 4 ends 10:30 → check 10:33 IST"),
        (5, 5, 33, "Period 5 ends 11:00 → check 11:03 IST"),
        (6, 6, 3,  "Period 6 ends 11:30 → check 11:33 IST"),
    ]
    _regular_end = datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc)
    for period, h_utc, m_utc, desc in _hw_schedule_regular:
        scheduler.add_job(
            run_homework_delivery_sync,
            trigger=CronTrigger(
                hour=h_utc, minute=m_utc, second=0,
                end_date=_regular_end,
            ),
            args=[period],
            id=f"homework_delivery_period_{period}",
            replace_existing=True,
        )
        logger.info(f"Scheduled homework delivery (regular): {desc}")

    # Regular safety-net at 12:35 PM IST (07:05 UTC)
    scheduler.add_job(
        run_homework_delivery_sync,
        trigger=CronTrigger(
            hour=7, minute=5, second=0,
            end_date=_regular_end,
        ),
        args=[6],
        id="homework_delivery_final_check",
        replace_existing=True,
    )
    logger.info("Scheduled homework delivery (regular): safety check 12:35 IST")

    # Summer timetable (from 1 Jul 2026):
    #   Zero:    07:40-08:10  → check 08:13 IST = 02:43 UTC
    #   First:   08:10-08:50  → check 08:53 IST = 03:23 UTC
    #   Second:  09:00-09:35  → check 09:38 IST = 04:08 UTC
    #   Third:   09:35-10:10  → check 10:13 IST = 04:43 UTC
    #   Fourth:  10:10-10:45  → check 10:48 IST = 05:18 UTC
    #   Fifth:   10:45-11:20  → check 11:23 IST = 05:53 UTC
    #   Sixth:   11:45-12:20  → check 12:23 IST = 06:53 UTC
    #   Seventh: 12:20-12:55  → check 12:58 IST = 07:28 UTC
    #   Eighth:  12:55-01:30  → check 01:33 IST = 08:03 UTC
    _hw_schedule_summer = [
        (0, 2, 43, "Zero ends 08:10 → check 08:13 IST"),
        (1, 3, 23, "First ends 08:50 → check 08:53 IST"),
        (2, 4, 8,  "Second ends 09:35 → check 09:38 IST"),
        (3, 4, 43, "Third ends 10:10 → check 10:13 IST"),
        (4, 5, 18, "Fourth ends 10:45 → check 10:48 IST"),
        (5, 5, 53, "Fifth ends 11:20 → check 11:23 IST"),
        (6, 6, 53, "Sixth ends 12:20 → check 12:23 IST"),
        (7, 7, 28, "Seventh ends 12:55 → check 12:58 IST"),
        (8, 8, 3,  "Eighth ends 01:30 → check 01:33 IST"),
    ]
    _summer_start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    for period, h_utc, m_utc, desc in _hw_schedule_summer:
        scheduler.add_job(
            run_homework_delivery_sync,
            trigger=CronTrigger(
                hour=h_utc, minute=m_utc, second=0,
                start_date=_summer_start,
            ),
            args=[period],
            id=f"homework_delivery_summer_period_{period}",
            replace_existing=True,
        )
        logger.info(f"Scheduled homework delivery (summer): {desc}")

    # Pre-primary safety-net at 11:45 AM IST (06:15 UTC)
    # Popsicles/Nursery/Prep school ends at 11:20 — final catch-all for them.
    scheduler.add_job(
        run_homework_delivery_sync,
        trigger=CronTrigger(
            hour=6, minute=15, second=0,
            start_date=_summer_start,
        ),
        args=[5, "preprimary"],
        id="homework_delivery_summer_preprimary_safety",
        replace_existing=True,
    )
    logger.info("Scheduled homework delivery (summer): pre-primary safety check 11:45 AM IST")

    # Summer safety-net at 01:35 PM IST (08:05 UTC) — Grade 1-12
    scheduler.add_job(
        run_homework_delivery_sync,
        trigger=CronTrigger(
            hour=8, minute=5, second=0,
            start_date=_summer_start,
        ),
        args=[8],
        id="homework_delivery_summer_final_check",
        replace_existing=True,
    )
    logger.info("Scheduled homework delivery (summer): safety check 01:35 PM IST")

    # --- Daily Doc Clear (3:00 PM IST = 9:30 UTC) ---
    # Clears all 34 homework Google Docs at end of school day,
    # restores template instructions, and resets content hashes.
    scheduler.add_job(
        run_daily_clear_sync,
        trigger=CronTrigger(hour=9, minute=30, second=0),
        id="homework_daily_clear",
        replace_existing=True,
    )
    logger.info("Scheduled daily homework doc clear at 3:00 PM IST (9:30 UTC)")

    # One-time Teacher CW/HW Reminder (29 Jun 2026) — already sent, retained
    # for reference only. No job scheduled.

    scheduler.start()
    logger.info("Scheduler started successfully")


def _send_teacher_cwhw_reminder_sync() -> None:
    """Send CW/HW Google Docs reminder to all class teachers (one-time)."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_send_teacher_cwhw_reminder())
    finally:
        loop.close()


async def _send_teacher_cwhw_reminder() -> None:
    """Send the approved reminder message to all 36 class teachers via WhatsApp."""
    from app.services.whatsapp_service import send_cloud_template_message, _send_cloud_text

    _TEACHER_PHONES = [
        # (Grade, Teacher Name, Phone Numbers)
        ("Popsicles", "Sanya Mehra / Anu", ["9289234655"]),
        ("Nursery 1", "Jasleen Kaur / Simrita", ["9289234654", "8448141634"]),
        ("Nursery 2", "Priyanka Budhiraja / Geet", ["9289234657", "8448141271"]),
        ("Nursery 3", "Nashra / Deepti", ["9289236042", "8448141374"]),
        ("Prep 1", "Meenal Harjika / Suchi", ["9289234656"]),
        ("Prep 2", "Amita Sachdeva / Anjali", ["9289234658"]),
        ("Prep 3", "Mahak Jain / Pooja", ["9289236056"]),
        ("Grade 1A", "Pallavi Kumar", ["9289234652"]),
        ("Grade 1B", "Muskan Motwani", ["9289234660"]),
        ("Grade 2A", "Gargi Arora", ["9289234661"]),
        ("Grade 2B", "Tanvi Goyal", ["9289234662"]),
        ("Grade 3A", "Shreya Sikka", ["9289236072"]),
        ("Grade 3B", "Seema Bakshi", ["9289234664"]),
        ("Grade 3C", "Harnoor Kaur", ["9289234659"]),
        ("Grade 4A", "Prabhjot Kaur", ["9289234663"]),
        ("Grade 4B", "Damanpreet Kaur", ["9289236041"]),
        ("Grade 5A", "Poshika Narula", ["9289236045"]),
        ("Grade 5B", "Aastha Khattar", ["9289234653"]),
        ("Grade 6A", "Kaninika Jain", ["9289234665"]),
        ("Grade 6B", "Shikha Singh", ["9289236043"]),
        ("Grade 7A", "Shyam Manohar", ["9289236049"]),
        ("Grade 7B", "Twinkle Tandon", ["9289236044"]),
        ("Grade 8A", "Tarun Dhall", ["9289236057"]),
        ("Grade 8B", "Rashmi", ["9289236048"]),
        ("Grade 8C", "Nikita Chawla", ["9289236046"]),
        ("Grade 9A", "Mansi Gupta", ["9289236058"]),
        ("Grade 9B", "Vaishali Arora", ["9289236047"]),
        ("Grade 9C", "Harjeet Kaur", ["9289236052"]),
        ("Grade 10A", "Riya Arora", ["9289236050"]),
        ("Grade 10B", "Avneet Kaur", ["9289236051"]),
        ("Grade 11(Science)", "Mridul Pilani", ["9289236055"]),
        ("Grade 11(Commerce)", "Christy Joseph", ["9289236054"]),
        ("Grade 11(Humanities)", "Christy Joseph", ["9289236054"]),
        ("Grade 12(Science)", "Pooja Arora", ["9289236053"]),
        ("Grade 12(Commerce)", "Sucheta Sinha", ["9289236059"]),
        ("Grade 12(Humanities)", "Sucheta Sinha", ["9289236059"]),
    ]

    message = (
        "Dear Teacher,\n\n"
        "This is a reminder from PP International School.\n\n"
        "Starting from 1st July 2026, the Classwork and Homework (CW/HW) "
        "updates for all grades will be shared with parents automatically "
        "through the Google Docs (that were previously shared with you on "
        "your respective email IDs.)\n\n"
        "Kindly ensure that you update the CW/HW in your assigned Google Doc "
        "after each period so that parents receive timely updates.\n\n"
        "Thank you for your cooperation.\n\n"
        "Regards,\nPP International School"
    )

    sent_phones: set[str] = set()
    sent = 0
    failed = 0

    for grade, teacher, phones in _TEACHER_PHONES:
        for phone in phones:
            if phone in sent_phones:
                continue
            sent_phones.add(phone)

            ok = await _send_cloud_text(phone, message)
            if ok:
                sent += 1
                logger.info(
                    f"[TEACHER REMINDER] Sent to {teacher} ({grade}) — {phone}"
                )
            else:
                failed += 1
                logger.warning(
                    f"[TEACHER REMINDER] Failed for {teacher} ({grade}) — {phone}"
                )
            await asyncio.sleep(2)

    logger.info(
        f"=== TEACHER CW/HW REMINDER COMPLETE: {sent} sent, {failed} failed ==="
    )


def stop_scheduler() -> None:
    """Stop the scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")
