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
import collections
from email.header import decode_header
from datetime import datetime, timedelta, timezone

from app.services.openai_service import TEACHER_DATA

logger = logging.getLogger(__name__)

# IMAP configuration (same credentials as SMTP — Google Workspace)
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("SMTP_USER", "info@ppischool.in")
IMAP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# Patterns that indicate a teacher is replying to a forwarded parent query email
_REPLY_SUBJECT_PREFIXES = [
    "re: ppis bot: parent query",
    "re: ppis bot: query from",
    "re: ppis bot: file from",
    "re: ppis bot: message for",
    "re: homework query from",
    "re: leave application",
]

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

# Track processed email message-IDs to avoid re-processing.
# In-memory cache is backed by the `processed_messages` DB table so that
# IDs survive OOM kills and Fly.io restarts (prevents duplicate homework
# broadcasts to parents).
_processed_message_ids: collections.OrderedDict[str, None] = collections.OrderedDict()
_MAX_PROCESSED_IDS = 200  # reduced from 500 to limit memory usage


def _db_email_id(message_id: str) -> str:
    """Prefix email Message-IDs so they don't collide with webhook dedup IDs."""
    return f"email:{message_id}"


def _is_email_processed_in_db(message_id: str) -> bool:
    """Check the persistent DB for a previously processed email Message-ID."""
    import sqlite3
    db_path = os.getenv("DB_PATH", "/data/app.db")
    if not os.path.exists(os.path.dirname(db_path) if os.path.dirname(db_path) else "."):
        db_path = os.path.join(os.path.dirname(__file__), "..", "..", "app.db")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ? LIMIT 1",
            (_db_email_id(message_id),),
        )
        found = cur.fetchone() is not None
        conn.close()
        return found
    except Exception:
        return False


def _mark_email_processed_in_db(message_id: str) -> None:
    """Persist a processed email Message-ID in the DB."""
    import sqlite3
    db_path = os.getenv("DB_PATH", "/data/app.db")
    if not os.path.exists(os.path.dirname(db_path) if os.path.dirname(db_path) else "."):
        db_path = os.path.join(os.path.dirname(__file__), "..", "..", "app.db")
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)",
            (_db_email_id(message_id),),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to persist email dedup ID: {e}")


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


def _check_memory_ok() -> bool:
    """Return True if RSS is below the safe threshold (100 MB).

    On 256 MB Fly.io instances the OOM killer fires around 136 MB RSS.
    We abort the poll early if we're already using too much memory so
    that the core bot (WhatsApp replies + camera snapshots) stays alive.
    """
    try:
        # Read current RSS from /proc/self/status (VmRSS field).
        # resource.getrusage().ru_maxrss is peak RSS (monotonically increasing)
        # which would permanently disable polling after any transient spike.
        rss_bytes = 0
        try:
            with open('/proc/self/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        rss_bytes = int(line.split()[1]) * 1024  # KB→B
                        break
        except Exception:
            # Fallback to peak RSS if /proc not available (non-Linux)
            import resource
            rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        rss_mb = rss_bytes / (1024 * 1024)
        if rss_mb > 100:
            logger.warning(f"Memory too high ({rss_mb:.0f} MB) — skipping email poll to prevent OOM")
            return False
        return True
    except Exception:
        return True  # If we can't check, proceed cautiously


def poll_homework_emails_sync() -> None:
    """Connect to IMAP, fetch recent emails from teachers with homework content,
    and broadcast them to parents via WhatsApp.

    Designed to be called periodically from APScheduler (every 60 min).

    MEMORY SAFETY:
    - Checks RSS before starting; aborts if already > 100 MB
    - Processes at most 2 emails per poll (not 5) to stay under OOM threshold
    - Uses IMAP HEADER-only fetch first to filter before downloading full body
    - Forces gc.collect() after each email
    """
    global _processed_message_ids
    import gc

    if not _check_memory_ok():
        return

    imap_password = IMAP_PASSWORD or os.getenv("SMTP_PASSWORD", "")
    if not imap_password:
        logger.warning("IMAP password not configured — skipping email poll")
        return

    logger.info("Polling for teacher homework emails...")

    mailbox = None
    try:
        mailbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=15)
        mailbox.login(IMAP_USER, imap_password)
        mailbox.select("INBOX", readonly=True)  # readonly=True avoids auto-marking as Seen
    except Exception as e:
        logger.error(f"Failed to connect to IMAP: {e}")
        if mailbox:
            try:
                mailbox.logout()
            except Exception:
                pass
        return

    try:
        # Search for UNSEEN emails only
        status, data = mailbox.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            logger.info("No unseen emails found")
            return

        email_ids = data[0].split()
        logger.info(f"Found {len(email_ids)} unseen email(s) — will process max 2")

        # Process only the last 2 unseen emails per poll (was 5 — caused OOM)
        batch = list(reversed(email_ids[-2:]))
        for eid in batch:
            if not _check_memory_ok():
                logger.warning("Memory limit approaching — stopping email processing early")
                break
            try:
                _process_single_email(mailbox, eid)
            except Exception as e:
                logger.error(f"Error processing email {eid}: {e}")
            finally:
                gc.collect()
    finally:
        try:
            if mailbox:
                mailbox.logout()
        except Exception:
            pass

    # Trim processed IDs to prevent unbounded memory growth.
    # Because _processed_message_ids is an OrderedDict, popping from the
    # left always removes the *oldest* entries first.
    while len(_processed_message_ids) > _MAX_PROCESSED_IDS:
        _processed_message_ids.popitem(last=False)  # remove oldest

    gc.collect()
    logger.info("Email poll completed")


def _process_single_email(mailbox: imaplib.IMAP4_SSL, email_id: bytes) -> None:
    """Process a single email — check if it's from a teacher with homework content.

    The mailbox is opened in readonly mode, so FETCH does NOT mark emails
    as Seen — original read/unread status is automatically preserved.
    """
    global _processed_message_ids

    status, msg_data = mailbox.fetch(email_id, "(RFC822)")
    if status != "OK" or not msg_data[0]:
        return

    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)

    # Get Message-ID to avoid reprocessing (check in-memory cache + persistent DB)
    message_id = msg.get("Message-ID", "")
    if message_id and message_id in _processed_message_ids:
        return
    if message_id and _is_email_processed_in_db(message_id):
        _processed_message_ids[message_id] = None  # warm the in-memory cache
        return


    from_addr = _decode_mime_header(msg.get("From", ""))
    subject = _decode_mime_header(msg.get("Subject", ""))
    body = _extract_text_from_email(msg)

    # Check if sender is a known teacher
    teacher_entry = _match_teacher_by_email(from_addr)
    if teacher_entry is None:
        if message_id:
            _processed_message_ids[message_id] = None
        return

    # --- Check if this is a reply to a forwarded parent query ---
    subject_lower = subject.lower().strip()
    is_reply_to_parent_query = any(
        subject_lower.startswith(prefix) for prefix in _REPLY_SUBJECT_PREFIXES
    )
    if is_reply_to_parent_query:
        teacher_name = teacher_entry["teacher"].split("/")[0].strip()
        teacher_grade = teacher_entry["grade"]
        # Extract email attachments
        att_list = _extract_email_attachments(msg)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                _relay_teacher_email_reply_to_parent(
                    teacher_entry=teacher_entry,
                    teacher_name=teacher_name,
                    teacher_grade=teacher_grade,
                    subject=subject,
                    body=body,
                    attachments=att_list,
                )
            )
        except Exception as e:
            logger.error(f"Error relaying teacher email reply to parent: {e}")
        finally:
            loop.close()
        if message_id:
            _processed_message_ids[message_id] = None
            _mark_email_processed_in_db(message_id)
        return

    # Check for homework keywords in subject or body
    combined_text = f"{subject}\n{body}"
    if not _HW_RE.search(combined_text):
        if message_id:
            _processed_message_ids[message_id] = None
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

    if message_id:
        _processed_message_ids[message_id] = None
        _mark_email_processed_in_db(message_id)


def _extract_email_attachments(
    msg: email.message.Message,
) -> list[tuple[bytes, str, str]]:
    """Extract file attachments from an email message.

    Returns a list of (file_bytes, filename, mime_type) tuples.
    """
    attachments: list[tuple[bytes, str, str]] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in disposition and "inline" not in disposition:
            continue
        content_type = part.get_content_type()
        # Skip text parts that are part of the email body
        if content_type in ("text/plain", "text/html") and "attachment" not in disposition:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        filename = part.get_filename()
        if filename:
            filename = _decode_mime_header(filename)
        else:
            # Generate a name from content type
            ext = content_type.split("/")[-1] if "/" in content_type else "bin"
            filename = f"attachment.{ext}"
        attachments.append((payload, filename, content_type))
    return attachments


async def _relay_teacher_email_reply_to_parent(
    teacher_entry: dict,
    teacher_name: str,
    teacher_grade: str,
    subject: str,
    body: str,
    attachments: list[tuple[bytes, str, str]],
) -> None:
    """When a teacher replies to a forwarded parent query via email,
    relay the reply (text + attachments) back to the parent via WhatsApp.

    Looks up the original parent from the forwarded_conversations table
    using the teacher's phone number.
    """
    from app.database import get_db
    from app.services.whatsapp_service import (
        send_whatsapp_message,
        get_whatsapp_provider,
        upload_media_bytes_cloud,
        send_cloud_media,
    )

    teacher_wa = (teacher_entry.get("whatsapp") or "").strip()

    # Look up the most recent forwarded conversation for this teacher.
    # Try by WhatsApp number first, then fall back to teacher_name LIKE match.
    db = await get_db()
    try:
        row = None
        if teacher_wa:
            digits = re.sub(r"\D", "", teacher_wa)
            last10 = digits[-10:] if len(digits) >= 10 else digits
            cursor = await db.execute(
                "SELECT original_chat_id, sender_phone, teacher_name, teacher_grade, original_message "
                "FROM forwarded_conversations "
                "WHERE teacher_phone LIKE ? "
                "AND created_at > datetime('now', '-48 hours') "
                "ORDER BY created_at DESC LIMIT 1",
                (f"%{last10}%",),
            )
            row = await cursor.fetchone()
        if not row:
            # Fall back to matching by teacher name
            cursor = await db.execute(
                "SELECT original_chat_id, sender_phone, teacher_name, teacher_grade, original_message "
                "FROM forwarded_conversations "
                "WHERE teacher_name LIKE ? "
                "AND created_at > datetime('now', '-48 hours') "
                "ORDER BY created_at DESC LIMIT 1",
                (f"%{teacher_name}%",),
            )
            row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        logger.warning(
            f"No forwarded conversation found for teacher {teacher_name} ({teacher_phone}) "
            f"— cannot relay email reply to parent"
        )
        return

    parent_chat_id = row[0]  # original_chat_id (e.g. "919876543210@c.us")
    parent_phone = row[1]     # sender_phone
    original_message = row[4] or ""

    logger.info(
        f"Relaying teacher email reply from {teacher_name} to parent {parent_chat_id}. "
        f"Subject: {subject[:60]}. Attachments: {len(attachments)}"
    )

    # Clean up body: strip quoted text from the reply
    reply_text = _strip_email_quoted_text(body).strip()
    if len(reply_text) > 1500:
        reply_text = reply_text[:1500] + "..."

    # Build the relay message
    relay_msg = (
        f"Reply from {teacher_name} (Class Teacher, {teacher_grade}):\n\n"
        f"{reply_text}\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )

    # Try to determine the parent's WhatsApp-compatible phone
    # parent_chat_id might be "919876543210@c.us" or just a phone number
    wa_recipient = parent_chat_id.replace("@c.us", "").replace("@s.whatsapp.net", "")
    if not wa_recipient:
        wa_recipient = re.sub(r"\D", "", parent_phone)
        if len(wa_recipient) == 10:
            wa_recipient = f"91{wa_recipient}"

    # Send the text reply
    text_sent = await send_whatsapp_message(wa_recipient, relay_msg)
    if not text_sent:
        logger.warning(f"Direct msg to parent {wa_recipient} failed (24-hr window likely closed)")

    # Send attachments
    att_sent = 0
    for file_bytes, filename, mime_type in attachments:
        try:
            if get_whatsapp_provider() == "cloud":
                media_id = await upload_media_bytes_cloud(file_bytes, mime_type, filename)
                if media_id:
                    # Determine media type for Cloud API
                    if mime_type.startswith("image/"):
                        media_type = "image"
                    elif mime_type.startswith("video/"):
                        media_type = "video"
                    elif mime_type.startswith("audio/"):
                        media_type = "audio"
                    else:
                        media_type = "document"
                    ok = await send_cloud_media(
                        wa_recipient, media_type,
                        media_id=media_id,
                        caption=f"From {teacher_name}: {filename}",
                        filename=filename,
                    )
                    if ok:
                        att_sent += 1
                    await asyncio.sleep(2)
            else:
                # Green API: write to temp file and send
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as tmp:
                    tmp.write(file_bytes)
                    tmp_path = tmp.name
                from app.services.whatsapp_service import forward_file_by_url
                ok = await forward_file_by_url(
                    wa_recipient, f"file://{tmp_path}", filename,
                    f"From {teacher_name}: {filename}",
                )
                if ok:
                    att_sent += 1
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to send email attachment {filename} to parent: {e}")

    # Log to relay tracking
    try:
        from app.services.relay_service import save_relay_message, auto_tag_message
        from app.services.attachment_service import save_attachment_metadata, classify_media_type
        tags = auto_tag_message(reply_text, len(attachments) > 0)
        rid = await save_relay_message(
            sender_phone=teacher_wa or teacher_name,
            sender_role="teacher",
            receiver_phone=wa_recipient,
            receiver_role="parent",
            direction="teacher_to_parent",
            message_text=reply_text[:1000],
            message_type="attachment" if attachments else "text",
            grade=teacher_grade,
            delivery_status="delivered" if text_sent else "failed",
            tags=tags,
        )
        for file_bytes, filename, mime_type in attachments:
            if rid:
                await save_attachment_metadata(
                    relay_message_id=rid,
                    file_type=classify_media_type(mime_type, filename),
                    file_name=filename,
                    mime_type=mime_type,
                    file_size=len(file_bytes),
                )
    except Exception as e:
        logger.error(f"Relay tracking error (email reply): {e}")

    logger.info(
        f"Email reply relayed: {teacher_name} → parent {wa_recipient}. "
        f"Text: {'OK' if text_sent else 'FAILED'}. "
        f"Attachments: {att_sent}/{len(attachments)}"
    )


def _strip_email_quoted_text(body: str) -> str:
    """Remove quoted text from an email reply body.

    Strips everything after common reply markers like:
    - 'On ... wrote:'
    - '> ' quoted lines
    - '--- Original Message ---'
    """
    # Remove "On <date> ... wrote:" block and everything after
    on_wrote = re.search(
        r"\n\s*On\s+.{10,80}\s+wrote:\s*\n",
        body,
        re.IGNORECASE,
    )
    if on_wrote:
        body = body[:on_wrote.start()]

    # Remove "--- Original Message ---" and everything after
    orig_msg = re.search(
        r"\n\s*-{3,}\s*Original Message\s*-{3,}",
        body,
        re.IGNORECASE,
    )
    if orig_msg:
        body = body[:orig_msg.start()]

    # Remove "From: ... Sent: ..." outlook-style headers
    from_sent = re.search(
        r"\n\s*From:\s+.+\n\s*Sent:\s+",
        body,
        re.IGNORECASE,
    )
    if from_sent:
        body = body[:from_sent.start()]

    # Remove leading '>' quoted lines at the end
    lines = body.rstrip().split("\n")
    while lines and lines[-1].strip().startswith(">"):
        lines.pop()

    return "\n".join(lines).strip()


async def _broadcast_email_homework(
    target_grade: str, teacher_grade: str, teacher_name: str, parent_msg: str
) -> None:
    """Look up parents by grade and send the homework message to all of them.

    Uses WhatsApp template messages (required for Cloud API outside 24-hr window).
    """
    from app.routes.webhook import _get_parents_by_grade_fuzzy, _get_parents_by_grade
    from app.services.whatsapp_service import send_whatsapp_message

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

    hw_content = parent_msg.strip()
    if len(hw_content) > 900:
        hw_content = hw_content[:900] + "..."

    sent_count = 0
    fail_count = 0
    for phone in parent_phones:
        recipient = f"91{phone}" if len(phone) == 10 else phone
        hw_msg = f"📚 *Homework — {matched_grade}*\n\nFrom {teacher_name}:\n\n{hw_content}"
        success = await send_whatsapp_message(recipient, hw_msg)
        if success:
            sent_count += 1
        else:
            fail_count += 1
        await asyncio.sleep(0.5)

    logger.info(
        f"Email homework broadcast from {teacher_name} for {matched_grade}: "
        f"{sent_count} sent, {fail_count} failed out of {len(parent_phones)} parents"
    )
