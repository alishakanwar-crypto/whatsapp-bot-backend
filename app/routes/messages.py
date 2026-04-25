import logging
import re
from fastapi import APIRouter, Query

from app.database import get_db
from app.models.schemas import MessageResponse
from app.services.mother_teacher_service import (
    get_mother_teacher_grades,
    is_mother_teacher_grade,
    is_teacher_phone_assigned_for_grade,
    log_unauthorized_access,
)
from app.services.openai_service import TEACHER_DATA

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/messages", tags=["messages"])

# Admin panel numbers that bypass Mother Teacher access control
_ADMIN_PANEL_NUMBERS: set[str] = {
    "9971166562",   # Mr. Rahul Gupta
    "9910034550",   # Ms. Purnima Gupta
    "9599488106",   # Ms. Harpreet Kaur
    "8076455224",   # Ms. Alisha Ahuja
}


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def _is_admin(phone: str) -> bool:
    return _normalize_phone(phone) in _ADMIN_PANEL_NUMBERS


def _requester_teaches_mt_grade(phone: str) -> bool:
    """Return True if *phone* belongs to a teacher whose grade is a Mother Teacher grade."""
    norm = _normalize_phone(phone)
    for entry in TEACHER_DATA:
        t_phone = entry.get("whatsapp", "")
        if not t_phone:
            continue
        if _normalize_phone(t_phone) == norm:
            return is_mother_teacher_grade(entry.get("grade", ""))
    return False


async def _get_parent_mt_grades(parent_phone: str) -> list[str]:
    """Return Mother Teacher grades linked to *parent_phone* via pi_sheet_students."""
    last10 = _normalize_phone(parent_phone)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT grade FROM pi_sheet_students "
            "WHERE father_mobile LIKE ? OR mother_mobile LIKE ?",
            (f"%{last10}%", f"%{last10}%"),
        )
        rows = await cursor.fetchall()
        mt_grades = get_mother_teacher_grades()
        return [row[0] for row in rows if row[0] in mt_grades]
    finally:
        await db.close()


@router.get("/", response_model=list[MessageResponse])
async def list_messages(
    phone_number: str | None = Query(None, description="Filter by phone number"),
    channel: str | None = Query(None, description="Filter by channel (whatsapp/sms)"),
    direction: str | None = Query(None, description="Filter by direction (incoming/outgoing)"),
    requester_phone: str | None = Query(
        None,
        description="Phone of the requesting teacher/admin (for access control)",
    ),
    limit: int = Query(50, ge=1, le=500, description="Number of messages to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """Get message history with optional filters.

    Access control for Mother Teacher grades (Nursery to Class 2):
    - Admin panel users can view all messages.
    - Non-admin teachers can only view messages of their own assigned class.
    - If *requester_phone* is provided and the queried *phone_number* belongs
      to a parent of a Mother Teacher grade, the request is blocked unless the
      requester is the assigned class teacher or an admin.
    """
    # --- Mother Teacher access control ---
    # Only restrict access when the requester is a teacher whose own grade is
    # an MT grade but is NOT the assigned class teacher.  Non-MT-grade teachers
    # are allowed through (the parent may also have children in their grade).
    if phone_number and requester_phone and not _is_admin(requester_phone):
        if _requester_teaches_mt_grade(requester_phone):
            parent_mt_grades = await _get_parent_mt_grades(phone_number)
            if parent_mt_grades:
                requester_authorized = any(
                    is_teacher_phone_assigned_for_grade(requester_phone, g)
                    for g in parent_mt_grades
                )
                if not requester_authorized:
                    await log_unauthorized_access(
                        accessor_phone=requester_phone,
                        accessor_role="teacher",
                        attempted_resource=f"messages for {phone_number}",
                        child_grade=parent_mt_grades[0],
                        reason="non_class_teacher_viewing_chat",
                    )
                    logger.warning(
                        "Mother Teacher access denied: %s tried to view messages of %s (grade %s)",
                        requester_phone, phone_number, parent_mt_grades[0],
                    )
                    return []

    db = await get_db()
    try:
        conditions: list[str] = []
        params: list[str | int] = []

        if phone_number:
            conditions.append("(sender = ? OR receiver = ?)")
            params.extend([phone_number, phone_number])
        if channel:
            conditions.append("channel = ?")
            params.append(channel)
        if direction:
            conditions.append("direction = ?")
            params.append(direction)

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT id, sender, receiver, content, channel, direction, timestamp
            FROM messages
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        return [
            MessageResponse(
                id=row[0],
                sender=row[1],
                receiver=row[2],
                content=row[3],
                channel=row[4],
                direction=row[5],
                timestamp=str(row[6]),
            )
            for row in rows
        ]
    finally:
        await db.close()
