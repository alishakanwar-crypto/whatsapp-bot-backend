"""
Bulk WhatsApp messaging service with permanent safeguards.

Safety features:
1. Bot replies ALWAYS take priority — bulk sending pauses when incoming messages arrive
2. Daily sending cap (configurable, default 500/day)
3. Time-of-day windows — only sends during configured hours
4. Auto-stop on restriction errors — immediately halts if any message returns a ban/restriction error
5. Multi-day spreading — automatically splits large sends across multiple days
6. Random delays (3-8s between messages) and batch pauses (40-50 msg then 1-2 min pause)
7. Shuffled send order and message variations
8. Resumable — tracks progress so it can pick up where it left off
"""

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DAILY_CAP = int(os.getenv("BULK_DAILY_CAP", "500"))
MIN_DELAY = 3        # Minimum seconds between messages
MAX_DELAY = 8        # Maximum seconds between messages
BATCH_SIZE_MIN = 40  # Min messages per batch
BATCH_SIZE_MAX = 50  # Max messages per batch
BATCH_PAUSE_MIN = 60   # 1 minute pause between batches
BATCH_PAUSE_MAX = 120  # 2 minute pause between batches

# IST offset for time-of-day windowing
IST = timezone(timedelta(hours=5, minutes=30))

# Allowed sending windows (IST) — bulk messages only go out during these hours
SEND_WINDOWS = [
    (10, 0, 12, 0),   # 10:00 AM - 12:00 PM IST
    (14, 0, 16, 0),   # 2:00 PM - 4:00 PM IST
]

# Error patterns that indicate WhatsApp restriction/ban
RESTRICTION_PATTERNS = [
    "banned",
    "restricted",
    "blocked",
    "rate limit",
    "too many",
    "spam",
    "temporarily",
    "account restricted",
    "not authorized",
]

# Greeting and closing variations for message uniqueness
GREETING_VARIATIONS = [
    "Namaste! \U0001f64f",
    "Namaste \U0001f64f",
    "Namaste! \U0001f64f\U0001f64f",
    "Namaskar! \U0001f64f",
    "Greetings! \U0001f64f",
]

CLOSING_VARIATIONS = [
    "Warm regards,",
    "With warm regards,",
    "Best regards,",
    "Regards,",
    "Warm wishes,",
]


# ---------------------------------------------------------------------------
# Singleton state — tracks the current bulk send job
# ---------------------------------------------------------------------------
class BulkSendState:
    """Global state for the active bulk send job."""

    def __init__(self):
        self.is_running = False
        self.is_paused = False        # Paused for bot reply priority
        self.should_stop = False       # Hard stop (restriction detected)
        self.daily_sent_count = 0
        self.daily_reset_date: Optional[str] = None
        self.total_sent = 0
        self.total_failed = 0
        self.total_skipped = 0
        self.total_target = 0
        self.current_phone = ""
        self.started_at: Optional[str] = None
        self.last_error = ""
        self.results_file = ""

    def reset(self):
        self.is_running = False
        self.is_paused = False
        self.should_stop = False
        self.daily_sent_count = 0
        self.total_sent = 0
        self.total_failed = 0
        self.total_skipped = 0
        self.total_target = 0
        self.current_phone = ""
        self.started_at = None
        self.last_error = ""

    def to_dict(self) -> dict:
        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "should_stop": self.should_stop,
            "daily_sent_count": self.daily_sent_count,
            "daily_cap": DAILY_CAP,
            "total_sent": self.total_sent,
            "total_failed": self.total_failed,
            "total_skipped": self.total_skipped,
            "total_target": self.total_target,
            "current_phone": self.current_phone,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "results_file": self.results_file,
        }


_state = BulkSendState()


def get_bulk_state() -> BulkSendState:
    return _state


# ---------------------------------------------------------------------------
# Bot-priority: pause/resume bulk sending when bot handles incoming messages
# ---------------------------------------------------------------------------

async def pause_for_bot_reply():
    """Called by the webhook handler when an incoming message is being processed.
    Pauses the bulk sender so the bot reply goes through first."""
    if _state.is_running and not _state.is_paused:
        _state.is_paused = True
        logger.info("BULK SEND: Paused for bot reply priority")


async def resume_after_bot_reply():
    """Called by the webhook handler after the bot reply is sent.
    Resumes the bulk sender."""
    if _state.is_running and _state.is_paused:
        _state.is_paused = False
        logger.info("BULK SEND: Resumed after bot reply")


# ---------------------------------------------------------------------------
# Time-of-day check
# ---------------------------------------------------------------------------

def _is_in_send_window() -> bool:
    """Check if current IST time is within an allowed sending window."""
    now_ist = datetime.now(IST)
    for start_h, start_m, end_h, end_m in SEND_WINDOWS:
        start = now_ist.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = now_ist.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if start <= now_ist < end:
            return True
    return False


def _next_window_start() -> datetime:
    """Return the next send window start time (IST)."""
    now_ist = datetime.now(IST)
    for start_h, start_m, end_h, end_m in SEND_WINDOWS:
        start = now_ist.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        if now_ist < start:
            return start
    # Next day's first window
    tomorrow = now_ist + timedelta(days=1)
    start_h, start_m = SEND_WINDOWS[0][0], SEND_WINDOWS[0][1]
    return tomorrow.replace(hour=start_h, minute=start_m, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Daily cap management
# ---------------------------------------------------------------------------

def _check_daily_reset():
    """Reset daily counter if the date has changed (IST)."""
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    if _state.daily_reset_date != today_ist:
        _state.daily_sent_count = 0
        _state.daily_reset_date = today_ist
        logger.info(f"BULK SEND: Daily counter reset for {today_ist}")


def _daily_cap_reached() -> bool:
    """Check if we've hit the daily sending cap."""
    _check_daily_reset()
    return _state.daily_sent_count >= DAILY_CAP


# ---------------------------------------------------------------------------
# Restriction detection
# ---------------------------------------------------------------------------

def _is_restriction_error(status_code: int, response_text: str) -> bool:
    """Check if a send failure indicates a WhatsApp restriction/ban."""
    lower = response_text.lower()
    for pattern in RESTRICTION_PATTERNS:
        if pattern in lower:
            return True
    # 403 or 429 often mean restriction
    if status_code in (403, 429):
        return True
    return False


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def build_personalized_message(record: dict) -> str:
    """Build a personalized welcome message for a parent with child's name."""
    name = record["student_name"]
    grade = record["grade"]
    greeting = random.choice(GREETING_VARIATIONS)
    closing = random.choice(CLOSING_VARIATIONS)

    return (
        f"*PP International School (PPIS) \u2014 WhatsApp Helpdesk* \U0001f3eb\n\n"
        f"Dear Parent of *{name}* ({grade}),\n\n"
        f"{greeting}\n\n"
        f"We are happy to inform you that the *PPIS WhatsApp Bot* is now active on this number.\n\n"
        f"You can reach out to us anytime for:\n"
        f"\U0001f4da Class teacher details\n"
        f"\U0001f68c Transport & bus route information\n"
        f"\U0001f550 School timings & schedule\n"
        f"\U0001f4dd Admission enquiries\n"
        f"\U0001f3eb General school information\n\n"
        f"Simply type your query and get an instant response \u2014 available *24/7*!\n\n"
        f"If the bot cannot answer your query, it will connect you with the relevant school staff.\n\n"
        f"{closing}\n"
        f"PP International School"
    )


def build_generic_message() -> str:
    """Build a generic welcome message for parents whose child couldn't be matched."""
    greeting = random.choice(GREETING_VARIATIONS)
    closing = random.choice(CLOSING_VARIATIONS)

    return (
        f"*PP International School (PPIS) \u2014 WhatsApp Helpdesk* \U0001f3eb\n\n"
        f"Dear Parent,\n\n"
        f"{greeting}\n\n"
        "We are happy to inform you that the *PPIS WhatsApp Bot* is now active on this number.\n\n"
        "You can reach out to us anytime for:\n"
        "\U0001f4da Class teacher details\n"
        "\U0001f68c Transport & bus route information\n"
        "\U0001f550 School timings & schedule\n"
        "\U0001f4dd Admission enquiries\n"
        "\U0001f3eb General school information\n\n"
        "Simply type your query and get an instant response \u2014 available *24/7*!\n\n"
        f"If the bot cannot answer your query, it will connect you with the relevant school staff.\n\n"
        f"{closing}\n"
        f"PP International School"
    )


# ---------------------------------------------------------------------------
# Core send function with restriction detection
# ---------------------------------------------------------------------------

async def _send_single_message(
    client: httpx.AsyncClient, phone: str, message: str,
    api_url: str, api_token: str,
) -> tuple[bool, bool]:
    """Send a single message. Returns (success, is_restriction).
    If is_restriction is True, bulk send should stop immediately."""
    url = f"{api_url}/sendMessage/{api_token}"
    payload = {"chatId": f"{phone}@c.us", "message": message}
    try:
        resp = await client.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            return True, False
        # Check for restriction
        if _is_restriction_error(resp.status_code, resp.text):
            logger.error(f"RESTRICTION DETECTED sending to {phone}: {resp.status_code} - {resp.text}")
            return False, True
        logger.error(f"Failed to send to {phone}: HTTP {resp.status_code} - {resp.text}")
        return False, False
    except Exception as e:
        logger.error(f"Exception sending to {phone}: {e}")
        return False, False


# ---------------------------------------------------------------------------
# Main bulk send coroutine (runs as background task)
# ---------------------------------------------------------------------------

async def run_bulk_send(
    personalized_file: str,
    generic_file: str,
    results_file: str,
    instance_id: str,
    api_token: str,
    api_base_url: str = "https://7107.api.greenapi.com",
    respect_time_windows: bool = True,
    sheet_filter: Optional[str] = None,
):
    """
    Run the bulk send with all safeguards.
    This is designed to be launched as a background asyncio task.
    """
    if _state.is_running:
        logger.warning("Bulk send already running, ignoring duplicate request")
        return

    _state.reset()
    _state.is_running = True
    _state.started_at = datetime.now(IST).isoformat()
    _state.results_file = results_file

    api_url = f"{api_base_url}/waInstance{instance_id}"

    try:
        # Load personalized parent data
        with open(personalized_file) as f:
            personalized = json.load(f)

        # Apply sheet filter if specified (e.g. "Nur 1" to send only to Nursery 1)
        if sheet_filter:
            personalized = [r for r in personalized if r.get("sheet") == sheet_filter]
            logger.info(f"BULK SEND: Sheet filter '{sheet_filter}' applied — {len(personalized)} entries")

        # Load generic (unmatched) parent phones — skip if sheet filter is active
        generic_phones = []
        if not sheet_filter and os.path.exists(generic_file):
            with open(generic_file) as f:
                generic_phones = json.load(f)

        # Build combined send list
        send_list = []
        seen_phones: set[str] = set()
        for record in personalized:
            if record["phone"] not in seen_phones:
                send_list.append({
                    "phone": record["phone"],
                    "student": record["student_name"],
                    "grade": record["grade"],
                    "role": record.get("role", ""),
                    "type": "personalized",
                    "record": record,
                })
                seen_phones.add(record["phone"])

        for phone in generic_phones:
            if phone not in seen_phones:
                send_list.append({
                    "phone": phone,
                    "student": "-",
                    "grade": "-",
                    "role": "-",
                    "type": "generic",
                    "record": None,
                })
                seen_phones.add(phone)

        # SHUFFLE the list for anti-detection
        random.shuffle(send_list)
        _state.total_target = len(send_list)

        # Load previous results to skip already-sent numbers
        sent_phones_done: set[str] = set()
        prev_results: list[dict] = []
        if os.path.exists(results_file):
            with open(results_file) as f:
                prev_results = json.load(f)
                sent_phones_done = {r["phone"] for r in prev_results if r["status"] == "success"}
            _state.total_skipped = len(sent_phones_done)
            logger.info(f"BULK SEND: Skipping {len(sent_phones_done)} already-sent numbers")

        results = list(prev_results)
        batch_count = 0
        current_batch_size = random.randint(BATCH_SIZE_MIN, BATCH_SIZE_MAX)
        consecutive_failures = 0

        logger.info(
            f"BULK SEND: Starting. Target={_state.total_target}, "
            f"Already sent={len(sent_phones_done)}, Daily cap={DAILY_CAP}"
        )

        async with httpx.AsyncClient() as client:
            for entry in send_list:
                # --- Check all safety conditions ---

                # Hard stop (restriction detected or manual stop)
                if _state.should_stop:
                    logger.warning("BULK SEND: Hard stop triggered. Halting.")
                    break

                phone = entry["phone"]
                _state.current_phone = phone

                # Skip already sent
                if phone in sent_phones_done:
                    continue

                # Daily cap check
                if _daily_cap_reached():
                    logger.info(f"BULK SEND: Daily cap of {DAILY_CAP} reached. Pausing until tomorrow.")
                    # Save results
                    _save_results(results, results_file)
                    # Wait until next day's first send window
                    while _daily_cap_reached() and not _state.should_stop:
                        await asyncio.sleep(60)  # Check every minute
                    if _state.should_stop:
                        break

                # Time-of-day window check
                if respect_time_windows and not _is_in_send_window():
                    next_window = _next_window_start()
                    wait_seconds = (next_window - datetime.now(IST)).total_seconds()
                    if wait_seconds > 0:
                        logger.info(
                            f"BULK SEND: Outside send window. "
                            f"Waiting {wait_seconds/60:.0f} min until {next_window.strftime('%H:%M IST')}"
                        )
                        _save_results(results, results_file)
                        # Wait in small chunks so we can check for stop signals
                        while not _is_in_send_window() and not _state.should_stop:
                            await asyncio.sleep(30)
                        if _state.should_stop:
                            break

                # Bot-priority pause: wait while bot is handling an incoming message
                while _state.is_paused and not _state.should_stop:
                    await asyncio.sleep(0.5)
                if _state.should_stop:
                    break

                # Build message at send-time for unique variation
                if entry["type"] == "personalized" and entry["record"]:
                    message = build_personalized_message(entry["record"])
                else:
                    message = build_generic_message()

                # Send
                ok, is_restriction = await _send_single_message(
                    client, phone, message, api_url, api_token
                )

                # Track result
                status = "success" if ok else "failed"
                results.append({
                    "phone": phone,
                    "student": entry["student"],
                    "grade": entry["grade"],
                    "role": entry["role"],
                    "type": entry["type"],
                    "status": status,
                    "timestamp": datetime.now(IST).isoformat(),
                })

                if ok:
                    _state.total_sent += 1
                    _state.daily_sent_count += 1
                    batch_count += 1
                    consecutive_failures = 0
                else:
                    _state.total_failed += 1
                    consecutive_failures += 1

                    if is_restriction:
                        _state.should_stop = True
                        _state.last_error = f"WhatsApp restriction detected at {phone}"
                        logger.error(f"BULK SEND: RESTRICTION DETECTED. Stopping immediately.")
                        break

                    # Auto-stop after 5 consecutive failures (possible issue)
                    if consecutive_failures >= 5:
                        _state.should_stop = True
                        _state.last_error = f"5 consecutive failures. Last phone: {phone}"
                        logger.error("BULK SEND: 5 consecutive failures. Auto-stopping.")
                        break

                # Progress logging every 25 messages
                total_processed = _state.total_sent + _state.total_failed
                if total_processed % 25 == 0 and total_processed > 0:
                    logger.info(
                        f"BULK SEND Progress: {total_processed}/{_state.total_target - _state.total_skipped} | "
                        f"Success: {_state.total_sent} | Failed: {_state.total_failed} | "
                        f"Daily: {_state.daily_sent_count}/{DAILY_CAP}"
                    )
                    _save_results(results, results_file)

                # BATCH PAUSE: after every batch, take a longer break
                if batch_count >= current_batch_size:
                    pause = random.uniform(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
                    logger.info(
                        f"BULK SEND: Batch of {batch_count} done. "
                        f"Pausing {pause:.0f}s ({pause/60:.1f} min)"
                    )
                    _save_results(results, results_file)
                    await asyncio.sleep(pause)
                    batch_count = 0
                    current_batch_size = random.randint(BATCH_SIZE_MIN, BATCH_SIZE_MAX)
                else:
                    # Random delay between messages
                    delay = random.uniform(MIN_DELAY, MAX_DELAY)
                    await asyncio.sleep(delay)

        # Save final results
        _save_results(results, results_file)

        logger.info(
            f"\nBULK SEND COMPLETE\n"
            f"Total target: {_state.total_target}\n"
            f"Sent: {_state.total_sent}\n"
            f"Failed: {_state.total_failed}\n"
            f"Skipped (already sent): {_state.total_skipped}\n"
            f"Stopped early: {_state.should_stop}\n"
            f"Last error: {_state.last_error or 'None'}"
        )

    except Exception as e:
        logger.error(f"BULK SEND: Unexpected error: {e}")
        _state.last_error = str(e)
    finally:
        _state.is_running = False
        _state.current_phone = ""


def _save_results(results: list[dict], results_file: str):
    """Save results to file."""
    try:
        with open(results_file, "w") as f:
            json.dump(results, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save results: {e}")


# ---------------------------------------------------------------------------
# Control functions (called from API routes)
# ---------------------------------------------------------------------------

def stop_bulk_send(reason: str = "Manual stop"):
    """Stop the current bulk send."""
    _state.should_stop = True
    _state.last_error = reason
    logger.info(f"BULK SEND: Stop requested. Reason: {reason}")


def get_status() -> dict:
    """Get current bulk send status."""
    _check_daily_reset()
    return _state.to_dict()
