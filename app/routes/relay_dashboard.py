"""Relay Messaging Dashboard API routes.

Provides endpoints for:
- Parent view: sent messages, teacher replies, delivery status
- Teacher view: parent messages, homework submissions, attachments
- Admin view: complete relay logs, failed deliveries, analytics, monitoring
- Permission management
- Message queue management
- Audit log access
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request

from app.database import get_db
from app.services.relay_service import (
    get_relay_messages,
    get_relay_stats,
    get_conversation_thread,
    get_failed_deliveries,
    get_audit_log,
    get_class_communication_report,
    process_message_queue,
    log_audit_event,
    is_teacher_phone,
    send_relay_message_to_parent,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/relay", tags=["relay"])


def _check_relay_auth(request: Request) -> None:
    """Verify X-Agent-Secret header on relay dashboard endpoints.

    Raises 401 when AGENT_SECRET is set and the header is missing/wrong.
    Skips the check when the env var is not configured so local dev
    keeps working without extra setup.
    """
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            raise HTTPException(status_code=401, detail="Unauthorized")


# ── Overview & Stats ─────────────────────────────────────────────────────────

@router.get("/stats")
async def relay_stats(request: Request, grade: str | None = None):
    """Get relay messaging statistics (admin view)."""
    _check_relay_auth(request)
    return await get_relay_stats(grade)


@router.get("/overview")
async def relay_overview(request: Request):
    """High-level relay system overview for the admin dashboard."""
    _check_relay_auth(request)
    stats = await get_relay_stats()
    failed = await get_failed_deliveries(limit=5)

    db = await get_db()
    try:
        # Queue summary
        cursor = await db.execute(
            "SELECT status, COUNT(*) FROM relay_message_queue GROUP BY status"
        )
        queue = {r[0]: r[1] for r in await cursor.fetchall()}

        # Recent activity (last 24h)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM relay_messages "
            "WHERE created_at >= datetime('now', '-24 hours')"
        )
        last_24h = (await cursor.fetchone())[0]

        # Unique active parents (sent a message in last 7 days)
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT sender_phone) FROM relay_messages "
            "WHERE sender_role = 'parent' AND created_at >= datetime('now', '-7 days')"
        )
        active_parents = (await cursor.fetchone())[0]

        # Unique active teachers (sent a message in last 7 days)
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT sender_phone) FROM relay_messages "
            "WHERE sender_role = 'teacher' AND created_at >= datetime('now', '-7 days')"
        )
        active_teachers = (await cursor.fetchone())[0]

        return {
            **stats,
            "last_24h_messages": last_24h,
            "active_parents_7d": active_parents,
            "active_teachers_7d": active_teachers,
            "queue": queue,
            "recent_failures": failed,
        }
    finally:
        await db.close()


# ── Messages (filtered views) ───────────────────────────────────────────────

@router.get("/messages")
async def list_relay_messages(
    phone: str | None = None,
    grade: str | None = None,
    direction: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get relay messages with filters. Works for parent, teacher, and admin views."""
    return await get_relay_messages(
        phone=phone, grade=grade, direction=direction,
        status=status, tag=tag, limit=limit, offset=offset,
    )


@router.get("/messages/parent/{phone}")
async def parent_messages(
    phone: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Parent view: messages sent by and received by a specific parent."""
    return await get_relay_messages(phone=phone, limit=limit, offset=offset)


@router.get("/messages/teacher/{phone}")
async def teacher_messages(
    phone: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Teacher view: messages sent by and received by a specific teacher."""
    return await get_relay_messages(phone=phone, limit=limit, offset=offset)


@router.get("/messages/grade/{grade}")
async def grade_messages(
    grade: str,
    direction: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Get all relay messages for a specific grade."""
    return await get_relay_messages(
        grade=grade, direction=direction, limit=limit, offset=offset,
    )


# ── Conversation Thread ──────────────────────────────────────────────────────

@router.get("/conversation")
async def conversation_thread(
    phone1: str = Query(..., description="First phone number"),
    phone2: str = Query(..., description="Second phone number"),
    limit: int = Query(50, ge=1, le=200),
):
    """Get the conversation thread between two phones (parent-teacher)."""
    return await get_conversation_thread(phone1, phone2, limit)


# ── Failed Deliveries ────────────────────────────────────────────────────────

@router.get("/failures")
async def list_failures(request: Request, limit: int = Query(50, ge=1, le=200)):
    """Admin view: recently failed message deliveries."""
    _check_relay_auth(request)
    return {"failures": await get_failed_deliveries(limit)}


# ── Audit Log ────────────────────────────────────────────────────────────────

@router.get("/audit")
async def audit_log(
    request: Request,
    event_type: str | None = None,
    actor_phone: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Admin view: audit log of all relay communication events."""
    _check_relay_auth(request)
    return await get_audit_log(
        event_type=event_type, actor_phone=actor_phone,
        limit=limit, offset=offset,
    )


# ── Class Communication Reports ──────────────────────────────────────────────

@router.get("/report/{grade}")
async def class_report(grade: str):
    """Generate a communication report for a specific class/grade."""
    return await get_class_communication_report(grade)


@router.get("/reports/all")
async def all_class_reports():
    """Generate communication reports for all grades."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT DISTINCT grade FROM relay_messages WHERE grade != '' ORDER BY grade"
        )
        grades = [r[0] for r in await cursor.fetchall()]
    finally:
        await db.close()

    reports = []
    for grade in grades:
        report = await get_class_communication_report(grade)
        reports.append(report)
    return {"reports": reports}


# ── Message Queue Management ─────────────────────────────────────────────────

@router.get("/queue")
async def view_queue(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    """Admin view: message queue status."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list = []

        if status:
            conditions.append("status = ?")
            params.append(status)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor = await db.execute(
            f"SELECT id, relay_message_id, recipient_phone, recipient_role, "
            f"message_text, channel, priority, status, attempts, max_attempts, "
            f"last_error, next_retry_at, created_at, processed_at "
            f"FROM relay_message_queue {where} "
            f"ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        )
        rows = await cursor.fetchall()

        # Queue summary
        cursor2 = await db.execute(
            "SELECT status, COUNT(*) FROM relay_message_queue GROUP BY status"
        )
        summary = {r[0]: r[1] for r in await cursor2.fetchall()}

        return {
            "summary": summary,
            "items": [
                {
                    "id": r["id"],
                    "relay_message_id": r["relay_message_id"],
                    "recipient_phone": r["recipient_phone"],
                    "recipient_role": r["recipient_role"],
                    "message_preview": (r["message_text"] or "")[:200],
                    "channel": r["channel"],
                    "priority": r["priority"],
                    "status": r["status"],
                    "attempts": r["attempts"],
                    "max_attempts": r["max_attempts"],
                    "last_error": r["last_error"],
                    "next_retry_at": r["next_retry_at"],
                    "created_at": r["created_at"],
                    "processed_at": r["processed_at"],
                }
                for r in rows
            ],
        }
    finally:
        await db.close()


@router.post("/queue/process")
async def process_queue(request: Request):
    """Admin action: manually trigger queue processing."""
    _check_relay_auth(request)
    result = await process_message_queue()
    await log_audit_event(
        event_type="queue_manual_process",
        details=f"Processed {result['processed']} messages: {result['sent']} sent, {result['failed']} failed",
    )
    return result


@router.post("/queue/{queue_id}/retry")
async def retry_queue_item(request: Request, queue_id: int):
    """Admin action: reset a failed queue item for retry."""
    _check_relay_auth(request)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, status FROM relay_message_queue WHERE id = ?",
            (queue_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"status": "error", "message": "Queue item not found"}

        await db.execute(
            "UPDATE relay_message_queue SET status = 'queued', "
            "attempts = 0, last_error = '', next_retry_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (queue_id,),
        )
        await db.commit()
        return {"status": "ok", "message": f"Queue item {queue_id} reset for retry"}
    finally:
        await db.close()


# ── Teacher Permissions Management ───────────────────────────────────────────

@router.get("/permissions")
async def list_permissions(
    teacher_phone: str | None = None,
    grade: str | None = None,
):
    """Admin view: teacher-class permissions."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list = []

        if teacher_phone:
            conditions.append("teacher_phone LIKE ?")
            params.append(f"%{teacher_phone[-10:]}%")
        if grade:
            conditions.append("grade LIKE ?")
            params.append(f"%{grade}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor = await db.execute(
            f"SELECT id, teacher_phone, teacher_name, grade, permission_type, "
            f"is_active, created_at "
            f"FROM teacher_class_permissions {where} "
            f"ORDER BY grade, teacher_name",
            params,
        )
        rows = await cursor.fetchall()
        return {
            "permissions": [
                {
                    "id": r["id"],
                    "teacher_phone": r["teacher_phone"],
                    "teacher_name": r["teacher_name"],
                    "grade": r["grade"],
                    "permission_type": r["permission_type"],
                    "is_active": bool(r["is_active"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }
    finally:
        await db.close()


@router.post("/permissions")
async def add_permission(request: Request):
    """Admin action: add a teacher-class permission."""
    _check_relay_auth(request)
    body = await request.json()
    teacher_phone = body.get("teacher_phone", "")
    teacher_name = body.get("teacher_name", "")
    grade = body.get("grade", "")
    permission_type = body.get("permission_type", "subject_teacher")

    if not teacher_phone or not grade:
        return {"status": "error", "message": "teacher_phone and grade are required"}

    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO teacher_class_permissions "
            "(teacher_phone, teacher_name, grade, permission_type, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (teacher_phone, teacher_name, grade, permission_type),
        )
        await db.commit()

        await log_audit_event(
            event_type="permission_added",
            actor_role="admin",
            target_phone=teacher_phone,
            grade=grade,
            details=f"Added {permission_type} permission for {teacher_name}",
        )

        return {"status": "ok", "message": f"Permission added for {teacher_name} → {grade}"}
    finally:
        await db.close()


@router.delete("/permissions/{permission_id}")
async def remove_permission(request: Request, permission_id: int):
    """Admin action: deactivate a teacher-class permission."""
    _check_relay_auth(request)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT teacher_name, grade FROM teacher_class_permissions WHERE id = ?",
            (permission_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"status": "error", "message": "Permission not found"}

        await db.execute(
            "UPDATE teacher_class_permissions SET is_active = 0 WHERE id = ?",
            (permission_id,),
        )
        await db.commit()

        await log_audit_event(
            event_type="permission_removed",
            actor_role="admin",
            grade=row["grade"],
            details=f"Deactivated permission for {row['teacher_name']} → {row['grade']}",
        )

        return {"status": "ok", "message": f"Permission deactivated"}
    finally:
        await db.close()


# ── Teacher Direct Messaging ─────────────────────────────────────────────────

@router.post("/send/teacher-to-parents")
async def teacher_send_to_parents(request: Request):
    """Teacher action: send a message to parents of a class.

    Body: {
        "teacher_phone": "...",
        "grade": "Grade 3A",
        "message": "...",
        "student_name": "" (optional, for individual parent)
    }
    """
    body = await request.json()
    teacher_phone = body.get("teacher_phone", "")
    grade = body.get("grade", "")
    message = body.get("message", "")
    student_name = body.get("student_name", "")

    if not teacher_phone or not grade or not message:
        return {"status": "error", "message": "teacher_phone, grade, and message are required"}

    # Verify teacher identity
    teacher_entry = is_teacher_phone(teacher_phone)
    if not teacher_entry:
        return {"status": "error", "message": "Phone number not recognized as a teacher"}

    teacher_name = teacher_entry["teacher"].split("/")[0].strip()

    # Get parent phones for the grade
    db = await get_db()
    try:
        if student_name:
            # Individual parent
            cursor = await db.execute(
                "SELECT father_mobile, mother_mobile FROM pi_sheet_students "
                "WHERE LOWER(student_name) LIKE ? AND grade LIKE ?",
                (f"%{student_name.lower()}%", f"%{grade}%"),
            )
        else:
            # All parents in grade
            cursor = await db.execute(
                "SELECT father_mobile, mother_mobile FROM pi_sheet_students "
                "WHERE grade LIKE ?",
                (f"%{grade}%",),
            )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    import re
    parent_phones: list[str] = []
    for row in rows:
        for col in ("father_mobile", "mother_mobile"):
            raw = (row[col] or "").strip()
            if not raw:
                continue
            digits = re.sub(r"\D", "", raw)
            if len(digits) >= 10:
                parent_phones.append(digits[-10:])

    parent_phones = list(set(parent_phones))

    if not parent_phones:
        return {"status": "error", "message": f"No parent contacts found for {grade}"}

    result = await send_relay_message_to_parent(
        teacher_phone=teacher_phone,
        teacher_name=teacher_name,
        teacher_grade=grade,
        parent_phones=parent_phones,
        message_text=message,
        student_name=student_name,
        is_broadcast=not bool(student_name),
    )

    return {
        "status": "ok",
        "sent": result["sent"],
        "failed": result["failed"],
        "total_parents": len(parent_phones),
    }


# ── Attachment History ────────────────────────────────────────────────────────

@router.get("/attachments")
async def list_attachments(
    grade: str | None = None,
    file_type: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Get attachment history with filters."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list = []

        if grade:
            conditions.append("rm.grade LIKE ?")
            params.append(f"%{grade}%")
        if file_type:
            conditions.append("ra.file_type = ?")
            params.append(file_type)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor = await db.execute(
            f"SELECT ra.id, ra.relay_message_id, ra.file_type, ra.file_name, "
            f"ra.mime_type, ra.file_size, ra.validation_status, ra.created_at, "
            f"rm.sender_phone, rm.sender_role, rm.receiver_phone, rm.grade, "
            f"rm.student_name, rm.direction "
            f"FROM relay_attachments ra "
            f"JOIN relay_messages rm ON rm.id = ra.relay_message_id "
            f"{where} ORDER BY ra.created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()

        return {
            "attachments": [
                {
                    "id": r["id"],
                    "relay_message_id": r["relay_message_id"],
                    "file_type": r["file_type"],
                    "file_name": r["file_name"],
                    "mime_type": r["mime_type"],
                    "file_size": r["file_size"],
                    "validation_status": r["validation_status"],
                    "created_at": r["created_at"],
                    "sender_phone": r["sender_phone"],
                    "sender_role": r["sender_role"],
                    "grade": r["grade"],
                    "student_name": r["student_name"],
                    "direction": r["direction"],
                }
                for r in rows
            ]
        }
    finally:
        await db.close()


# ── Blocked File Types Management ────────────────────────────────────────────

@router.get("/blocked-types")
async def list_blocked_types():
    """Get list of blocked file extensions."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, extension, reason, created_at FROM relay_blocked_file_types "
            "ORDER BY extension"
        )
        rows = await cursor.fetchall()
        return {
            "blocked_types": [
                {"id": r["id"], "extension": r["extension"],
                 "reason": r["reason"], "created_at": r["created_at"]}
                for r in rows
            ]
        }
    finally:
        await db.close()


@router.post("/blocked-types")
async def add_blocked_type(request: Request):
    """Admin action: block a file extension."""
    _check_relay_auth(request)
    body = await request.json()
    ext = body.get("extension", "").lower().strip(".")
    reason = body.get("reason", "Blocked by admin")

    if not ext:
        return {"status": "error", "message": "extension is required"}

    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO relay_blocked_file_types (extension, reason) "
            "VALUES (?, ?)",
            (ext, reason),
        )
        await db.commit()
        return {"status": "ok", "message": f"Extension '.{ext}' blocked"}
    finally:
        await db.close()


@router.delete("/blocked-types/{type_id}")
async def remove_blocked_type(request: Request, type_id: int):
    """Admin action: unblock a file extension."""
    _check_relay_auth(request)
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM relay_blocked_file_types WHERE id = ?",
            (type_id,),
        )
        await db.commit()
        return {"status": "ok", "message": f"Blocked type removed"}
    finally:
        await db.close()


# ── Search ───────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_messages(
    q: str = Query(..., min_length=2, description="Search query"),
    limit: int = Query(50, ge=1, le=200),
):
    """Search relay messages by text content."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, conversation_id, sender_phone, sender_role, "
            "receiver_phone, receiver_role, direction, message_text, "
            "message_type, grade, student_name, delivery_status, tags, "
            "created_at "
            "FROM relay_messages WHERE message_text LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{q}%", limit),
        )
        rows = await cursor.fetchall()
        return {
            "query": q,
            "count": len(rows),
            "messages": [
                {
                    "id": r["id"],
                    "sender_phone": r["sender_phone"],
                    "sender_role": r["sender_role"],
                    "receiver_phone": r["receiver_phone"],
                    "direction": r["direction"],
                    "message_text": r["message_text"][:300],
                    "grade": r["grade"],
                    "student_name": r["student_name"],
                    "delivery_status": r["delivery_status"],
                    "tags": r["tags"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }
    finally:
        await db.close()
