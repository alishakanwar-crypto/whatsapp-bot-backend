"""IMAP email polling service — monitors info@ppischool.in for teacher homework emails.

When a teacher sends an email containing homework keywords to info@ppischool.in,
this service picks it up and broadcasts the content to all parents of that class
via WhatsApp.
"""

import os
import re
import email
import imaplib
import logging
import asyncio
from email.header import decode_header
from datetime import datetime, timedelta, timezone

from app.services.openai_service import TEACHER_DATA

logger = logging.getLogger(__name__)

# IMAP configuration (same credentials as SMTP — Google Workspace)
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("SMTP_USER", "info@ppischool.in")
IMAP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# Homework-related keywords (same as WhatsApp broadcast, plus email-specific)
_HW_KEYWORDS = [
    "homework", "home work", "hw", "assignment", "classwork", "class work",
    "worksheet", "work sheet", "project", "pending work", "revision",
    "test tomorrow", "exam", "syllabus", "chapter", "exercise",
    "complete", "submit", "bring", "prepare", "practice",
    "circular", "notice", "update for parents",
]

_HW_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _HW_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Grade extraction regex (mirrors the one in webhook.py)
_GRADE_EXTRACT_RE = re.compile(
    r"(?:grade|class|gr|std)\s*(\d{1,2})\s*([a-dA-D])?|"
    r"nursery\s*(\d)?|"
    r"prep\s*(\d)?|"
    r"popsicles",
    re.IGNORECASE,
)

# Track processed email message-IDs to avoid re-processing
_processed_message_ids: set[str] = set()
_MAX_PROCESSED_IDS = 200  # reduced from 500 to limit memory usage


def _decode_mime_header(raw: str | None) -> str:
    """Decode a MIME-encoded header value to plain text."""
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _extract_text_from_email(msg: email.message.Message) -> str:
    """Extract the plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try HTML
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    # Strip HTML tags for a rough plain-text version
                    return re.sub(r"<[^>]+>", " ", html).strip()
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _match_teacher_by_email(from_addr: str) -> dict | None:
    """Check if the sender email matches a known teacher in TEACHER_DATA."""
    addr = from_addr.lower().strip()
    # Extract email from "Name <email>" format
    m = re.search(r"<([^>]+)>", addr)
    if m:
        addr = m.group(1).lower().strip()
    for entry in TEACHER_DATA:
        teacher_email = (entry.get("email") or "").lower().strip()
        if teacher_email and teacher_email == addr:
            return entry
        # Also check class_email
        class_email = (entry.get("class_email") or "").lower().strip()
        if class_email and class_email == addr:
            return entry
    return None


def _extract_grade_from_text(text: str) -> str:
    """Extract a grade/class from text using the same regex as the webhook."""
    match = _GRADE_EXTRACT_RE.search(text)
    if not match:
        return ""
    if match.group(1):  # grade N [section]
        grade_num = match.group(1)
        section = (match.group(2) or "").upper()
        return f"Grade {grade_num}{section}" if section else f"Grade {grade_num}"
    if match.group(3) is not None:  # nursery N
        return f"Nursery {match.group(3)}" if match.group(3) else "Nursery"
    if match.group(4) is not None:  # prep N
        return f"Prep {match.group(4)}" if match.group(4) else "Prep"
    return "Popsicles"


def poll_homework_emails_sync() -> None:
    """Connect to IMAP, fetch recent emails from teachers with homework content,
    and broadcast them to parents via WhatsApp.

    This is designed to be called periodically from the APScheduler
    (every 10 minutes).
    """
    global _processed_message_ids

    imap_password = IMAP_PASSWORD or os.getenv("SMTP_PASSWORD", "")
    if not imap_password:
        logger.warning("IMAP password not configured — skipping email poll")
        return

    logger.info("Polling for teacher homework emails...")

    try:
        mailbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        mailbox.login(IMAP_USER, imap_password)
        mailbox.select("INBOX")
    except Exception as e:
        logger.error(f"Failed to connect to IMAP: {e}")
        return

    try:
        # Search for emails from the last 24 hours (to avoid reprocessing old mail)
        since_date = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%d-%b-%Y")
        status, data = mailbox.search(None, f'(SINCE "{since_date}")')
        if status != "OK" or not data[0]:
            logger.info("No recent emails found")
            return

        email_ids = data[0].split()
        logger.info(f"Found {len(email_ids)} recent email(s) to scan")

        # Process from newest first (limit to last 15 to avoid OOM)
        for eid in reversed(email_ids[-15:]):
            try:
                _process_single_email(mailbox, eid)
            except Exception as e:
                logger.error(f"Error processing email {eid}: {e}")
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass

    # Trim processed IDs set to prevent unbounded memory growth.
    # Python set does not preserve insertion order, so slicing removes
    # arbitrary items (possibly recent ones), risking duplicate
    # homework broadcasts.  Clearing is safe because the IMAP SINCE
    # date filter will skip old emails on the next poll cycle anyway.
    if len(_processed_message_ids) > _MAX_PROCESSED_IDS:
        _processed_message_ids.clear()

    # Explicit garbage collection to help prevent OOM
    import gc
    gc.collect()


def _process_single_email(mailbox: imaplib.IMAP4_SSL, email_id: bytes) -> None:
    """Process a single email — check if it's from a teacher with homework content.
    
    Preserves the original read/unread status of each email:
    - If the email was unread before we fetched it, we mark it back as unread.
    - If the email was already read, we leave it as read.
    """
    global _processed_message_ids

    # Check if the email was already read BEFORE we fetch it
    # (IMAP FETCH with RFC822 automatically sets the \Seen flag)
    was_already_read = False
    try:
        flag_status, flag_data = mailbox.fetch(email_id, "(FLAGS)")
        if flag_status == "OK" and flag_data[0]:
            flag_str = flag_data[0].decode() if isinstance(flag_data[0], bytes) else str(flag_data[0])
            was_already_read = "\\Seen" in flag_str
    except Exception as e:
        logger.warning(f"Failed to check flags for email {email_id}: {e}")

    status, msg_data = mailbox.fetch(email_id, "(RFC822)")
    if status != "OK" or not msg_data[0]:
        return

    # Restore original read/unread status:
    # FETCH with RFC822 sets \Seen, so if it was unread before, remove the flag
    if not was_already_read:
        try:
            mailbox.store(email_id, "-FLAGS", "\\Seen")
        except Exception as e:
            logger.warning(f"Failed to restore unread status for email {email_id}: {e}")

    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)

    # Get Message-ID to avoid reprocessing
    message_id = msg.get("Message-ID", "")
    if message_id in _processed_message_ids:
        return

    from_addr = _decode_mime_header(msg.get("From", ""))
    subject = _decode_mime_header(msg.get("Subject", ""))
    body = _extract_text_from_email(msg)

    # Check if sender is a known teacher
    teacher_entry = _match_teacher_by_email(from_addr)
    if teacher_entry is None:
        _processed_message_ids.add(message_id)
        return

    # Check for homework keywords in subject or body
    combined_text = f"{subject}\n{body}"
    if not _HW_RE.search(combined_text):
        _processed_message_ids.add(message_id)
        return

    teacher_name = teacher_entry["teacher"].split("/")[0].strip()
    teacher_grade = teacher_entry["grade"]

    logger.info(
        f"Homework email detected from {teacher_name} ({from_addr}): "
        f"Subject: {subject[:80]}"
    )

    # Extract target grade from subject/body; fall back to teacher's own grade
    target_grade = _extract_grade_from_text(subject) or _extract_grade_from_text(body)
    if not target_grade:
        target_grade = teacher_grade

    # Build the message for parents (use subject + body)
    email_content = body.strip()
    if len(email_content) > 1500:
        email_content = email_content[:1500] + "..."

    parent_msg = (
        f"Dear Parent,\n\n"
        f"The following message has been shared by "
        f"{teacher_name} (Class Teacher, {target_grade}) via email:\n\n"
        f"Subject: {subject}\n\n"
        f"{email_content}\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )

    # Broadcast to parents of that grade (run async functions in a new loop)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _broadcast_email_homework(target_grade, teacher_grade, teacher_name, parent_msg)
        )
    except Exception as e:
        logger.error(f"Error broadcasting email homework: {e}")
    finally:
        loop.close()

    _processed_message_ids.add(message_id)


async def _broadcast_email_homework(
    target_grade: str, teacher_grade: str, teacher_name: str, parent_msg: str
) -> None:
    """Look up parents by grade and send the homework message to all of them.

    Uses WhatsApp template messages (required for Cloud API outside 24-hr window).
    """
    from app.routes.webhook import _get_parents_by_grade_fuzzy, _get_parents_by_grade
    from app.services.whatsapp_service import (
        send_whatsapp_message,
        send_cloud_template_message,
        get_whatsapp_provider,
    )

    matched_grade, parent_phones = await _get_parents_by_grade_fuzzy(target_grade)
    if not parent_phones:
        parent_phones = await _get_parents_by_grade(teacher_grade)
        matched_grade = teacher_grade if parent_phones else ""

    if not parent_phones:
        logger.warning(
            f"No parents found for grade {target_grade} (teacher: {teacher_name}) "
            f"— email homework not broadcast"
        )
        return

    # Truncate homework content for template parameter limits
    hw_content = parent_msg.strip()
    if len(hw_content) > 900:
        hw_content = hw_content[:900] + "..."

    sent_count = 0
    fail_count = 0
    for phone in parent_phones:
        recipient = f"91{phone}" if len(phone) == 10 else phone
        if get_whatsapp_provider() == "cloud":
            # Use template message (required outside 24-hr window)
            success = await send_cloud_template_message(
                recipient,
                "ppis_homework_update",
                body_params=[matched_grade, hw_content, teacher_name],
            )
            if not success:
                # Fallback to ppis_class_assignment (UTILITY, APPROVED)
                fallback_text = f"HW from {teacher_name}"
                success = await send_cloud_template_message(
                    recipient,
                    "ppis_class_assignment",
                    body_params=[fallback_text, matched_grade],
                )
        else:
            success = await send_whatsapp_message(recipient, parent_msg)
        if success:
            sent_count += 1
        else:
            fail_count += 1
        await asyncio.sleep(0.5)

    logger.info(
        f"Email homework broadcast from {teacher_name} for {matched_grade}: "
        f"{sent_count} sent, {fail_count} failed out of {len(parent_phones)} parents"
    )
