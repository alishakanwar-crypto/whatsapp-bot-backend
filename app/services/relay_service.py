"""Core Two-Way Relay Messaging Service.

Handles:
- Parent → Teacher messaging with routing & permissions
- Teacher → Parent messaging (direct + broadcast)
- Message queue with auto-retry
- Delivery status tracking
- Audit logging
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta

from app.database import get_db
from app.services.openai_service import TEACHER_DATA, find_mentioned_teachers

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strict routing grades: Nursery, Prep, Grade 1, Grade 2
# Parents in these grades can ONLY communicate with their class teacher.
# ---------------------------------------------------------------------------
_STRICT_ROUTING_GRADES = {
    "popsicles", "nursery 1", "nursery 2", "nursery 3",
    "prep 1", "prep 2", "prep 3",
    "grade 1a", "grade 1b", "grade 1c",
    "grade 2a", "grade 2b", "grade 2c",
}


def _normalize_phone(phone: str) -> str:
    """Strip to last 10 digits for comparison."""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def _ensure_country_code(phone: str) -> str:
    """Ensure phone has 91 country code prefix."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return "91" + digits
    return digits


def _generate_conversation_id(sender: str, receiver: str) -> str:
    """Generate a deterministic conversation ID for a sender-receiver pair."""
    phones = sorted([_normalize_phone(sender), _normalize_phone(receiver)])
    return f"conv_{phones[0]}_{phones[1]}"


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------

def is_teacher_phone(phone: str) -> dict | None:
    """Check if phone belongs to a teacher. Returns TEACHER_DATA entry or None."""
    normalized = _normalize_phone(phone)
    for entry in TEACHER_DATA:
        t_phone = entry.get("whatsapp", "")
        if not t_phone:
            continue
        if _normalize_phone(t_phone) == normalized:
            return entry
    return None


async def check_parent_teacher_permission(
    parent_phone: str, teacher_phone: str, grade: str
) -> tuple[bool, str]:
    """Check if a parent is allowed to message a specific teacher.

    For strict routing grades (Nursery/Prep/Grade 1-2), parents can only
    message their class teacher. For higher grades, parents can message
    any teacher mentioned by name.

    Returns (allowed, reason).
    """
    grade_lower = grade.lower().strip()

    # Strict routing: only class teacher allowed
    if grade_lower in _STRICT_ROUTING_GRADES:
        db = await get_db()
        try:
            t_norm = _normalize_phone(teacher_phone)
            cursor = await db.execute(
                "SELECT grade FROM teacher_class_permissions "
                "WHERE teacher_phone LIKE ? AND grade = ? AND is_active = 1",
                (f"%{t_norm}%", grade),
            )
            row = await cursor.fetchone()
            if row:
                return True, ""
            return False, (
                f"For {grade}, communication is restricted to the class teacher only. "
                f"Please contact the school office if you need to reach another teacher."
            )
        finally:
            await db.close()

    # Higher grades: flexible routing (any teacher the parent names)
    return True, ""


async def check_teacher_class_permission(
    teacher_phone: str, target_grade: str
) -> tuple[bool, str]:
    """Check if a teacher is allowed to send messages to a specific class.

    Returns (allowed, reason).
    """
    db = await get_db()
    try:
        t_norm = _normalize_phone(teacher_phone)
        cursor = await db.execute(
            "SELECT grade FROM teacher_class_permissions "
            "WHERE teacher_phone LIKE ? AND is_active = 1",
            (f"%{t_norm}%",),
        )
        rows = await cursor.fetchall()
        allowed_grades = {r["grade"] for r in rows}

        if target_grade in allowed_grades:
            return True, ""

        # Admin override: check if teacher has 'admin' permission
        cursor2 = await db.execute(
            "SELECT 1 FROM teacher_class_permissions "
            "WHERE teacher_phone LIKE ? AND permission_type = 'admin' AND is_active = 1",
            (f"%{t_norm}%",),
        )
        if await cursor2.fetchone():
            return True, ""

        return False, (
            f"You are not authorized to send messages to {target_grade}. "
            f"You can only message your assigned class(es): {', '.join(sorted(allowed_grades))}."
        )
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

async def log_audit_event(
    event_type: str,
    actor_phone: str = "",
    actor_role: str = "",
    target_phone: str = "",
    grade: str = "",
    details: str = "",
    relay_message_id: int | None = None,
) -> None:
    """Log an audit event to relay_audit_log."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO relay_audit_log "
            "(event_type, actor_phone, actor_role, target_phone, grade, "
            "details, relay_message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_type, actor_phone, actor_role, target_phone, grade,
             details, relay_message_id),
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to log audit event: {e}")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Core relay message operations
# ---------------------------------------------------------------------------

async def save_relay_message(
    sender_phone: str,
    sender_role: str,
    receiver_phone: str,
    receiver_role: str,
    direction: str,
    message_text: str,
    message_type: str = "text",
    grade: str = "",
    student_name: str = "",
    delivery_status: str = "pending",
    wa_message_id: str = "",
    email_sent: bool = False,
    tags: str = "",
) -> int:
    """Save a relay message to the database. Returns the message ID."""
    conv_id = _generate_conversation_id(sender_phone, receiver_phone)
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO relay_messages "
            "(conversation_id, sender_phone, sender_role, receiver_phone, "
            "receiver_role, direction, message_text, message_type, grade, "
            "student_name, delivery_status, wa_message_id, email_sent, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conv_id, sender_phone, sender_role, receiver_phone,
             receiver_role, direction, message_text, message_type, grade,
             student_name, delivery_status, wa_message_id,
             1 if email_sent else 0, tags),
        )
        await db.commit()
        msg_id = cursor.lastrowid or 0

        # Audit log
        await log_audit_event(
            event_type=f"relay_{direction}",
            actor_phone=sender_phone,
            actor_role=sender_role,
            target_phone=receiver_phone,
            grade=grade,
            details=f"{message_type}: {message_text[:200]}",
            relay_message_id=msg_id,
        )

        return msg_id
    finally:
        await db.close()


async def update_delivery_status(
    relay_message_id: int,
    status: str,
    wa_message_id: str = "",
) -> None:
    """Update delivery status of a relay message."""
    db = await get_db()
    try:
        updates = ["delivery_status = ?"]
        params: list = [status]

        if wa_message_id:
            updates.append("wa_message_id = ?")
            params.append(wa_message_id)

        if status == "delivered":
            updates.append("delivered_at = CURRENT_TIMESTAMP")
        elif status == "read":
            updates.append("read_at = CURRENT_TIMESTAMP")

        params.append(relay_message_id)
        await db.execute(
            f"UPDATE relay_messages SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Message queue operations
# ---------------------------------------------------------------------------

async def enqueue_message(
    recipient_phone: str,
    recipient_role: str,
    message_text: str,
    media_info: dict | None = None,
    channel: str = "whatsapp",
    priority: int = 5,
    relay_message_id: int | None = None,
) -> int:
    """Add a message to the retry queue. Returns queue entry ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO relay_message_queue "
            "(relay_message_id, recipient_phone, recipient_role, message_text, "
            "media_info, channel, priority, status, next_retry_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', CURRENT_TIMESTAMP)",
            (relay_message_id, recipient_phone, recipient_role, message_text,
             json.dumps(media_info) if media_info else "",
             channel, priority),
        )
        await db.commit()
        return cursor.lastrowid or 0
    finally:
        await db.close()


async def get_queued_messages(limit: int = 50) -> list[dict]:
    """Get messages ready to be retried."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, relay_message_id, recipient_phone, recipient_role, "
            "message_text, media_info, channel, priority, attempts, max_attempts, "
            "last_error "
            "FROM relay_message_queue "
            "WHERE status = 'queued' AND attempts < max_attempts "
            "AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP) "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "relay_message_id": r["relay_message_id"],
                "recipient_phone": r["recipient_phone"],
                "recipient_role": r["recipient_role"],
                "message_text": r["message_text"],
                "media_info": json.loads(r["media_info"]) if r["media_info"] else None,
                "channel": r["channel"],
                "priority": r["priority"],
                "attempts": r["attempts"],
                "max_attempts": r["max_attempts"],
                "last_error": r["last_error"],
            }
            for r in rows
        ]
    finally:
        await db.close()


async def mark_queue_processed(queue_id: int, success: bool, error: str = "") -> None:
    """Mark a queue entry as processed or failed."""
    db = await get_db()
    try:
        if success:
            await db.execute(
                "UPDATE relay_message_queue SET status = 'sent', "
                "processed_at = CURRENT_TIMESTAMP, attempts = attempts + 1 "
                "WHERE id = ?",
                (queue_id,),
            )
        else:
            # Exponential backoff: 30s, 60s, 120s, ...
            await db.execute(
                "UPDATE relay_message_queue SET "
                "attempts = attempts + 1, last_error = ?, "
                "next_retry_at = datetime('now', '+' || (30 * (1 << attempts)) || ' seconds'), "
                "status = CASE WHEN attempts + 1 >= max_attempts THEN 'failed' ELSE 'queued' END "
                "WHERE id = ?",
                (error, queue_id),
            )
        await db.commit()
    finally:
        await db.close()


async def process_message_queue() -> dict:
    """Process pending messages in the queue. Returns summary."""
    from app.services.whatsapp_service import send_whatsapp_message

    queued = await get_queued_messages()
    sent = 0
    failed = 0

    for item in queued:
        try:
            success = await send_whatsapp_message(
                item["recipient_phone"], item["message_text"]
            )
            await mark_queue_processed(item["id"], success)
            if success:
                sent += 1
                if item["relay_message_id"]:
                    await update_delivery_status(
                        item["relay_message_id"], "delivered"
                    )
            else:
                failed += 1
        except Exception as e:
            await mark_queue_processed(item["id"], False, str(e))
            failed += 1

    return {"processed": len(queued), "sent": sent, "failed": failed}


# ---------------------------------------------------------------------------
# Smart features: auto-tagging
# ---------------------------------------------------------------------------

_HOMEWORK_KEYWORDS = re.compile(
    r"\b(?:homework|home\s*work|hw|assignment|classwork|class\s*work|"
    r"worksheet|work\s*sheet|exercise|revision|practice|submit|"
    r"गृहकार्य|होमवर्क)\b",
    re.IGNORECASE,
)

_NOTICE_KEYWORDS = re.compile(
    r"\b(?:circular|notice|announcement|update|reminder|important|urgent|"
    r"ptm|parent\s*teacher\s*meet|सूचना|नोटिस)\b",
    re.IGNORECASE,
)

_EXAM_KEYWORDS = re.compile(
    r"\b(?:exam|test|quiz|assessment|syllabus|परीक्षा|टेस्ट)\b",
    re.IGNORECASE,
)


def auto_tag_message(message_text: str, has_attachment: bool = False) -> str:
    """Auto-detect and return comma-separated tags for a message."""
    tags: list[str] = []
    if _HOMEWORK_KEYWORDS.search(message_text):
        tags.append("homework")
    if _NOTICE_KEYWORDS.search(message_text):
        tags.append("notice")
    if _EXAM_KEYWORDS.search(message_text):
        tags.append("exam")
    if has_attachment:
        tags.append("attachment")
    return ",".join(tags)


# ---------------------------------------------------------------------------
# Relay message sending (the actual send logic)
# ---------------------------------------------------------------------------

async def send_relay_message_to_parent(
    teacher_phone: str,
    teacher_name: str,
    teacher_grade: str,
    parent_phones: list[str],
    message_text: str,
    media_info: dict | None = None,
    student_name: str = "",
    is_broadcast: bool = False,
) -> dict:
    """Send a message from a teacher to parent(s).

    Returns {"sent": N, "failed": N, "relay_message_ids": [...]}.
    """
    from app.services.whatsapp_service import (
        send_whatsapp_message,
        forward_cloud_media_to_recipient,
    )
    from app.services.email_service import send_email_async

    sent = 0
    failed = 0
    relay_ids: list[int] = []
    tags = auto_tag_message(message_text, media_info is not None)

    for phone in parent_phones:
        recipient = _ensure_country_code(phone)

        # Save relay message record
        msg_id = await save_relay_message(
            sender_phone=teacher_phone,
            sender_role="teacher",
            receiver_phone=phone,
            receiver_role="parent",
            direction="teacher_to_parent",
            message_text=message_text[:1000],
            message_type="broadcast" if is_broadcast else "direct",
            grade=teacher_grade,
            student_name=student_name,
            tags=tags,
        )
        relay_ids.append(msg_id)

        # Build parent-facing message
        parent_msg = (
            f"Message from {teacher_name} ({teacher_grade}):\n\n"
            f"{message_text}\n\n"
            f"_Reply to this message to respond to the teacher._\n\n"
            f"Warm regards,\nPP International School"
        )

        # Try direct WhatsApp
        wa_ok = await send_whatsapp_message(recipient, parent_msg)
        if not wa_ok:
            logger.warning(f"Direct msg to parent {recipient} failed (24-hr window likely closed)")

        # Send media if present
        if wa_ok and media_info and media_info.get("cloud_media_id"):
            await asyncio.sleep(3)
            try:
                await forward_cloud_media_to_recipient(
                    media_info, recipient,
                    caption=f"From {teacher_name} ({teacher_grade})",
                )
            except Exception as e:
                logger.error(f"Media forward to parent {recipient} failed: {e}")

        if wa_ok:
            sent += 1
            await update_delivery_status(msg_id, "delivered")
        else:
            failed += 1
            await update_delivery_status(msg_id, "failed")
            # Enqueue for retry
            await enqueue_message(
                recipient_phone=recipient,
                recipient_role="parent",
                message_text=parent_msg,
                media_info=media_info,
                relay_message_id=msg_id,
            )

        # Rate limit: 0.5s between messages
        if len(parent_phones) > 1:
            await asyncio.sleep(0.5)

    return {"sent": sent, "failed": failed, "relay_message_ids": relay_ids}


async def send_relay_message_to_teacher(
    parent_phone: str,
    parent_label: str,
    teacher_entry: dict,
    message_text: str,
    media_info: dict | None = None,
    grade: str = "",
    student_name: str = "",
) -> dict:
    """Send a message from a parent to a teacher.

    Returns {"success": bool, "relay_message_id": int, "method": str}.
    """
    from app.services.whatsapp_service import (
        send_whatsapp_message,
        forward_cloud_media_to_recipient,
    )
    from app.services.email_service import send_email_async

    teacher_phone = teacher_entry.get("whatsapp", "")
    teacher_email = teacher_entry.get("email", "")
    teacher_name = teacher_entry["teacher"].split("/")[0].strip()
    teacher_grade = teacher_entry["grade"]
    tags = auto_tag_message(message_text, media_info is not None)

    # Save relay message
    msg_id = await save_relay_message(
        sender_phone=parent_phone,
        sender_role="parent",
        receiver_phone=teacher_phone,
        receiver_role="teacher",
        direction="parent_to_teacher",
        message_text=message_text[:1000],
        message_type="attachment" if media_info else "text",
        grade=grade or teacher_grade,
        student_name=student_name,
        tags=tags,
    )

    wa_ok = False
    email_ok = False
    methods: list[str] = []

    # WhatsApp delivery
    if teacher_phone:
        chat_id = teacher_phone if "@" in teacher_phone else f"{teacher_phone}@c.us"
        recipient = _ensure_country_code(teacher_phone)

        query_msg = (
            f"\U0001f4e9 *Query from {parent_label}:*\n\n"
            f"\"{message_text[:500]}\"\n\n"
            f"_Reply to this message \u2014 your response will be forwarded to the parent._"
        )

        wa_ok = await send_whatsapp_message(chat_id, query_msg)
        if not wa_ok:
            logger.warning(f"Direct msg to teacher {recipient} failed (24-hr window likely closed)")

        if wa_ok and media_info:
            await asyncio.sleep(3)
            try:
                await forward_cloud_media_to_recipient(
                    media_info, chat_id,
                    caption=media_info.get("caption", ""),
                )
            except Exception as e:
                logger.error(f"Media forward to teacher failed: {e}")

        if wa_ok:
            methods.append("WhatsApp")

    # Email delivery (always as backup)
    if teacher_email:
        from app.routes.webhook import _download_media_bytes, _make_email_attachments
        _dl_bytes, _dl_mime = await _download_media_bytes(media_info)
        email_atts = _make_email_attachments(media_info, _dl_bytes, _dl_mime)

        email_body = (
            f"Dear {teacher_name},\n\n"
            f"{parent_label} has sent the following query via the PPIS Bot:\n\n"
            f"\"{message_text[:500]}\"\n\n"
            f"Kindly reply to this email and your response will be "
            f"forwarded back to the parent.\n\n"
            f"Regards,\nPPIS Bot"
        )
        email_ok = await send_email_async(
            teacher_email,
            f"PPIS Bot: Query from {parent_label}",
            email_body,
            attachments=email_atts or None,
        )
        if email_ok:
            methods.append("email")

    success = wa_ok or email_ok
    status = "delivered" if success else "failed"
    await update_delivery_status(msg_id, status)

    if not success:
        await enqueue_message(
            recipient_phone=teacher_phone,
            recipient_role="teacher",
            message_text=message_text,
            media_info=media_info,
            relay_message_id=msg_id,
        )

    await log_audit_event(
        event_type="relay_parent_to_teacher",
        actor_phone=parent_phone,
        actor_role="parent",
        target_phone=teacher_phone,
        grade=grade,
        details=f"{'OK' if success else 'FAILED'} via {', '.join(methods) or 'none'}",
        relay_message_id=msg_id,
    )

    return {
        "success": success,
        "relay_message_id": msg_id,
        "method": " & ".join(methods),
    }


# ---------------------------------------------------------------------------
# Lookup helpers for the dashboard
# ---------------------------------------------------------------------------

async def get_relay_messages(
    phone: str | None = None,
    grade: str | None = None,
    direction: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Query relay messages with filters."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list = []

        if phone:
            conditions.append("(sender_phone LIKE ? OR receiver_phone LIKE ?)")
            params.extend([f"%{_normalize_phone(phone)}%"] * 2)
        if grade:
            conditions.append("grade LIKE ?")
            params.append(f"%{grade}%")
        if direction:
            conditions.append("direction = ?")
            params.append(direction)
        if status:
            conditions.append("delivery_status = ?")
            params.append(status)
        if tag:
            conditions.append("tags LIKE ?")
            params.append(f"%{tag}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor = await db.execute(
            f"SELECT id, conversation_id, sender_phone, sender_role, "
            f"receiver_phone, receiver_role, direction, message_text, "
            f"message_type, grade, student_name, delivery_status, "
            f"wa_message_id, email_sent, retry_count, tags, "
            f"created_at, delivered_at, read_at "
            f"FROM relay_messages {where} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()

        # Total count
        cursor2 = await db.execute(
            f"SELECT COUNT(*) FROM relay_messages {where}", params
        )
        total = (await cursor2.fetchone())[0]

        messages = []
        for r in rows:
            msg = {
                "id": r["id"],
                "conversation_id": r["conversation_id"],
                "sender_phone": r["sender_phone"],
                "sender_role": r["sender_role"],
                "receiver_phone": r["receiver_phone"],
                "receiver_role": r["receiver_role"],
                "direction": r["direction"],
                "message_text": r["message_text"],
                "message_type": r["message_type"],
                "grade": r["grade"],
                "student_name": r["student_name"],
                "delivery_status": r["delivery_status"],
                "email_sent": bool(r["email_sent"]),
                "tags": r["tags"],
                "created_at": r["created_at"],
                "delivered_at": r["delivered_at"],
                "read_at": r["read_at"],
            }
            # Fetch attachments
            att_cursor = await db.execute(
                "SELECT id, file_type, file_name, mime_type, file_size "
                "FROM relay_attachments WHERE relay_message_id = ?",
                (r["id"],),
            )
            att_rows = await att_cursor.fetchall()
            msg["attachments"] = [
                {
                    "id": a["id"],
                    "file_type": a["file_type"],
                    "file_name": a["file_name"],
                    "mime_type": a["mime_type"],
                    "file_size": a["file_size"],
                }
                for a in att_rows
            ]
            messages.append(msg)

        return {"total": total, "messages": messages}
    finally:
        await db.close()


async def get_relay_stats(grade: str | None = None) -> dict:
    """Get relay messaging statistics."""
    db = await get_db()
    try:
        grade_filter = ""
        params: list = []
        if grade:
            grade_filter = "WHERE grade LIKE ?"
            params.append(f"%{grade}%")

        # Total messages
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM relay_messages {grade_filter}", params
        )
        total = (await cursor.fetchone())[0]

        # By direction
        cursor = await db.execute(
            f"SELECT direction, COUNT(*) FROM relay_messages {grade_filter} "
            f"GROUP BY direction", params
        )
        by_direction = {r[0]: r[1] for r in await cursor.fetchall()}

        # By status
        cursor = await db.execute(
            f"SELECT delivery_status, COUNT(*) FROM relay_messages {grade_filter} "
            f"GROUP BY delivery_status", params
        )
        by_status = {r[0]: r[1] for r in await cursor.fetchall()}

        # Today's messages
        today_filter = grade_filter.replace("WHERE", "AND") if grade_filter else ""
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM relay_messages "
            f"WHERE date(created_at) = date('now', '+5 hours', '+30 minutes') "
            f"{today_filter}",
            params,
        )
        today = (await cursor.fetchone())[0]

        # Messages with attachments
        cursor = await db.execute(
            f"SELECT COUNT(DISTINCT rm.id) FROM relay_messages rm "
            f"JOIN relay_attachments ra ON ra.relay_message_id = rm.id "
            f"{grade_filter.replace('WHERE', 'WHERE rm.' if grade_filter else '')}",
            params,
        )
        with_attachments = (await cursor.fetchone())[0]

        # Queue status
        cursor = await db.execute(
            "SELECT status, COUNT(*) FROM relay_message_queue GROUP BY status"
        )
        queue_status = {r[0]: r[1] for r in await cursor.fetchall()}

        # By grade (top 10)
        cursor = await db.execute(
            "SELECT grade, COUNT(*) as cnt FROM relay_messages "
            "WHERE grade != '' GROUP BY grade ORDER BY cnt DESC LIMIT 10"
        )
        by_grade = [{"grade": r[0], "count": r[1]} for r in await cursor.fetchall()]

        return {
            "total_messages": total,
            "today_messages": today,
            "by_direction": by_direction,
            "by_status": by_status,
            "with_attachments": with_attachments,
            "queue": queue_status,
            "by_grade": by_grade,
        }
    finally:
        await db.close()


async def get_conversation_thread(
    phone1: str, phone2: str, limit: int = 50
) -> list[dict]:
    """Get the conversation thread between two phones."""
    n1 = _normalize_phone(phone1)
    n2 = _normalize_phone(phone2)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, sender_phone, sender_role, receiver_phone, receiver_role, "
            "direction, message_text, message_type, delivery_status, tags, "
            "created_at, delivered_at, read_at "
            "FROM relay_messages "
            "WHERE (sender_phone LIKE ? AND receiver_phone LIKE ?) "
            "OR (sender_phone LIKE ? AND receiver_phone LIKE ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (f"%{n1}%", f"%{n2}%", f"%{n2}%", f"%{n1}%", limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "sender_phone": r["sender_phone"],
                "sender_role": r["sender_role"],
                "message_text": r["message_text"],
                "message_type": r["message_type"],
                "delivery_status": r["delivery_status"],
                "tags": r["tags"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        await db.close()


async def get_failed_deliveries(limit: int = 50) -> list[dict]:
    """Get recently failed deliveries for admin monitoring."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, sender_phone, sender_role, receiver_phone, "
            "receiver_role, direction, message_text, grade, "
            "delivery_status, retry_count, created_at "
            "FROM relay_messages WHERE delivery_status = 'failed' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "sender_phone": r["sender_phone"],
                "sender_role": r["sender_role"],
                "receiver_phone": r["receiver_phone"],
                "direction": r["direction"],
                "message_text": r["message_text"][:200],
                "grade": r["grade"],
                "retry_count": r["retry_count"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        await db.close()


async def get_audit_log(
    event_type: str | None = None,
    actor_phone: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Get audit log entries with filters."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if actor_phone:
            conditions.append("actor_phone LIKE ?")
            params.append(f"%{_normalize_phone(actor_phone)}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor = await db.execute(
            f"SELECT id, event_type, actor_phone, actor_role, target_phone, "
            f"grade, details, relay_message_id, created_at "
            f"FROM relay_audit_log {where} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()

        cursor2 = await db.execute(
            f"SELECT COUNT(*) FROM relay_audit_log {where}", params
        )
        total = (await cursor2.fetchone())[0]

        return {
            "total": total,
            "entries": [
                {
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "actor_phone": r["actor_phone"],
                    "actor_role": r["actor_role"],
                    "target_phone": r["target_phone"],
                    "grade": r["grade"],
                    "details": r["details"],
                    "relay_message_id": r["relay_message_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }
    finally:
        await db.close()


async def get_class_communication_report(grade: str) -> dict:
    """Generate a communication report for a specific class."""
    db = await get_db()
    try:
        # Total messages for this grade
        cursor = await db.execute(
            "SELECT COUNT(*) FROM relay_messages WHERE grade = ?", (grade,)
        )
        total = (await cursor.fetchone())[0]

        # Parent → Teacher count
        cursor = await db.execute(
            "SELECT COUNT(*) FROM relay_messages "
            "WHERE grade = ? AND direction = 'parent_to_teacher'",
            (grade,),
        )
        p2t = (await cursor.fetchone())[0]

        # Teacher → Parent count
        cursor = await db.execute(
            "SELECT COUNT(*) FROM relay_messages "
            "WHERE grade = ? AND direction = 'teacher_to_parent'",
            (grade,),
        )
        t2p = (await cursor.fetchone())[0]

        # Failed deliveries
        cursor = await db.execute(
            "SELECT COUNT(*) FROM relay_messages "
            "WHERE grade = ? AND delivery_status = 'failed'",
            (grade,),
        )
        failed = (await cursor.fetchone())[0]

        # Messages by tag
        cursor = await db.execute(
            "SELECT tags, COUNT(*) FROM relay_messages "
            "WHERE grade = ? AND tags != '' GROUP BY tags",
            (grade,),
        )
        by_tag: dict[str, int] = {}
        for r in await cursor.fetchall():
            for tag in r[0].split(","):
                tag = tag.strip()
                if tag:
                    by_tag[tag] = by_tag.get(tag, 0) + r[1]

        # Messages per day (last 7 days)
        cursor = await db.execute(
            "SELECT date(created_at) as day, COUNT(*) "
            "FROM relay_messages WHERE grade = ? "
            "AND created_at >= datetime('now', '-7 days') "
            "GROUP BY day ORDER BY day",
            (grade,),
        )
        daily = [{"date": r[0], "count": r[1]} for r in await cursor.fetchall()]

        return {
            "grade": grade,
            "total_messages": total,
            "parent_to_teacher": p2t,
            "teacher_to_parent": t2p,
            "failed_deliveries": failed,
            "by_tag": by_tag,
            "daily_trend": daily,
        }
    finally:
        await db.close()
