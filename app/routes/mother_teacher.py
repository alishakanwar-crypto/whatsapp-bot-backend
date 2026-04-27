"""Admin API endpoints for the Mother Teacher routing system.

Provides read-only access to:
- Blocked message logs
- Unauthorized access logs
- Mother Teacher grade/teacher mapping
"""

import logging
from fastapi import APIRouter, Query

from app.database import get_db
from app.services.mother_teacher_service import (
    get_mother_teacher_grades,
    get_class_teacher_for_grade,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mother-teacher", tags=["mother-teacher"])


@router.get("/grades")
async def list_mother_teacher_grades():
    """Return all grades under the Mother Teacher system with their assigned teachers."""
    result = []
    for grade in sorted(get_mother_teacher_grades()):
        entry = get_class_teacher_for_grade(grade)
        teacher_name = entry["teacher"].split("/")[0].strip() if entry else "Unknown"
        teacher_phone = entry.get("whatsapp", "") if entry else ""
        teacher_email = entry.get("email", "") if entry else ""
        result.append({
            "grade": grade,
            "class_teacher": teacher_name,
            "teacher_phone": teacher_phone,
            "teacher_email": teacher_email,
        })
    return result


@router.get("/blocked-messages")
async def list_blocked_messages(
    grade: str | None = Query(None, description="Filter by child grade"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Return recent blocked message attempts (admin only)."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list[str | int] = []

        if grade:
            conditions.append("child_grade = ?")
            params.append(grade)

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT id, sender_phone, child_grade, target_teacher_name,
                   target_teacher_phone, message_snippet, reason, created_at
            FROM mother_teacher_blocked_messages
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        return [
            {
                "id": row[0],
                "sender_phone": row[1],
                "child_grade": row[2],
                "target_teacher_name": row[3],
                "target_teacher_phone": row[4],
                "message_snippet": row[5],
                "reason": row[6],
                "created_at": str(row[7]),
            }
            for row in rows
        ]
    finally:
        await db.close()


@router.get("/access-logs")
async def list_access_logs(
    grade: str | None = Query(None, description="Filter by child grade"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Return unauthorized access attempt logs (admin only)."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list[str | int] = []

        if grade:
            conditions.append("child_grade = ?")
            params.append(grade)

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT id, accessor_phone, accessor_role, attempted_resource,
                   child_grade, reason, created_at
            FROM mother_teacher_access_logs
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        return [
            {
                "id": row[0],
                "accessor_phone": row[1],
                "accessor_role": row[2],
                "attempted_resource": row[3],
                "child_grade": row[4],
                "reason": row[5],
                "created_at": str(row[6]),
            }
            for row in rows
        ]
    finally:
        await db.close()
