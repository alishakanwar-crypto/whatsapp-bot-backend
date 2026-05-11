"""Dashboard API routes for the PPIS School Command Center web app."""

import logging
import os
from datetime import datetime, timedelta, date
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# ---------------------------------------------------------------------------
# Authentication: dashboard write operations require AGENT_SECRET
# ---------------------------------------------------------------------------
AGENT_SECRET = os.environ.get("AGENT_SECRET", "")


async def verify_dashboard_secret(x_agent_secret: str = Header("")) -> None:
    """Require AGENT_SECRET for write/delete operations on dashboard data."""
    if not AGENT_SECRET:
        return
    if x_agent_secret != AGENT_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Attendance ───────────────────────────────────────────────────────────────

@router.get("/attendance/today")
async def attendance_today():
    """Get today's attendance summary."""
    db = await get_db()
    try:
        # Total present today
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT person_id) FROM attendance_records "
            "WHERE date(logged_at) = date('now', '+5 hours', '+30 minutes')"
        )
        row = await cursor.fetchone()
        present_today = row[0] if row else 0

        # Total registered students
        cursor = await db.execute("SELECT COUNT(DISTINCT person_id) FROM agent_registered_faces")
        row = await cursor.fetchone()
        registered = row[0] if row else 0

        # Total students in PI sheet
        cursor = await db.execute("SELECT COUNT(*) FROM pi_sheet_students")
        row = await cursor.fetchone()
        total_students = row[0] if row else 0

        # Grade-wise breakdown
        cursor = await db.execute(
            "SELECT grade, COUNT(DISTINCT person_id) as count "
            "FROM attendance_records "
            "WHERE date(logged_at) = date('now', '+5 hours', '+30 minutes') "
            "GROUP BY grade ORDER BY grade"
        )
        grades = [{"grade": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # Recent attendance entries
        cursor = await db.execute(
            "SELECT person_id, student_name, grade, camera_label, confidence, "
            "notification_sent, logged_at "
            "FROM attendance_records "
            "WHERE date(logged_at) = date('now', '+5 hours', '+30 minutes') "
            "ORDER BY logged_at DESC LIMIT 500"
        )
        recent = [
            {
                "person_id": r[0], "name": r[1], "grade": r[2],
                "camera": r[3], "confidence": round(r[4] * 100, 1),
                "notified": bool(r[5]), "time": r[6],
            }
            for r in await cursor.fetchall()
        ]

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "present_today": present_today,
            "registered_faces": registered,
            "total_students": total_students,
            "grade_breakdown": grades,
            "recent_entries": recent,
        }
    finally:
        await db.close()


@router.get("/attendance/history")
async def attendance_history(
    days: int = Query(7, ge=1, le=90),
):
    """Get attendance counts per day for the last N days."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT date(logged_at) as day, COUNT(DISTINCT person_id) as count "
            "FROM attendance_records "
            "WHERE logged_at >= datetime('now', ?) "
            "GROUP BY day ORDER BY day",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
        return {"days": [{"date": r[0], "count": r[1]} for r in rows]}
    finally:
        await db.close()


MINIMUM_CONFIDENCE = 0.30  # Backend rejects anything below 30%
ATTENDANCE_WINDOW_START = 7  # 7:00 AM IST
ATTENDANCE_WINDOW_END_HOUR = 9
ATTENDANCE_WINDOW_END_MIN = 30  # 9:30 AM IST


@router.post("/attendance/report")
async def report_attendance(request: Request):
    """Receive attendance records from the campus agent.

    Validates: off-days, holidays, attendance window, confidence floor.
    Logs all rejected records to audit_log for forensics.
    """
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    day_name = now_ist.strftime("%A")

    # Block on Sundays
    if day_name == "Sunday":
        return {"status": "blocked", "reason": "Sunday — school is closed", "inserted": 0}
    # Block on 2nd Saturday only
    if day_name == "Saturday":
        sat_number = (now_ist.day - 1) // 7 + 1
        if sat_number == 2:
            return {"status": "blocked", "reason": "2nd Saturday — school is closed", "inserted": 0}
    # Block on holidays
    today_str = now_ist.strftime("%Y-%m-%d")
    try:
        _hdb = await get_db()
        try:
            _hcur = await _hdb.execute(
                "SELECT reason FROM school_holidays WHERE date = ?", (today_str,))
            _hrow = await _hcur.fetchone()
            if _hrow:
                return {"status": "blocked", "reason": f"Holiday: {_hrow[0]}", "inserted": 0}
        finally:
            await _hdb.close()
    except Exception:
        pass

    # Validate attendance time window (7:00 AM - 9:30 AM IST)
    window_start = now_ist.replace(hour=ATTENDANCE_WINDOW_START, minute=0, second=0, microsecond=0)
    window_end = now_ist.replace(hour=ATTENDANCE_WINDOW_END_HOUR, minute=ATTENDANCE_WINDOW_END_MIN, second=0, microsecond=0)
    if not (window_start <= now_ist <= window_end):
        return {
            "status": "blocked",
            "reason": f"Outside attendance window (7:00-9:30 AM IST, current: {now_ist.strftime('%I:%M %p')})",
            "inserted": 0,
        }

    body = await request.json()
    records = body.get("records", [])
    if not records:
        return {"status": "ok", "inserted": 0}

    from app.services.whatsapp_service import send_cloud_template_message

    db = await get_db()
    try:
        inserted = 0
        updated = 0
        rejected = 0
        backend_notified = 0
        for rec in records:
            person_id = rec.get("person_id", "")
            name = rec.get("name", "")
            grade = rec.get("grade", "")
            camera = rec.get("camera", "")
            confidence = rec.get("confidence", 0)
            notified = 1 if rec.get("notification_sent") else 0
            phones = rec.get("parent_phones", "")
            logged_at = rec.get("logged_at", datetime.now().isoformat())

            # Confidence floor: reject low-confidence matches at backend level
            if confidence < MINIMUM_CONFIDENCE:
                rejected += 1
                logger.warning(
                    f"Attendance rejected: {name} ({person_id}) confidence "
                    f"{confidence:.1%} < {MINIMUM_CONFIDENCE:.0%} minimum"
                )
                await db.execute(
                    "INSERT INTO audit_log (action, table_name, record_id, details) "
                    "VALUES (?, ?, ?, ?)",
                    ("rejected_low_confidence", "attendance_records", person_id,
                     f"confidence={confidence:.4f}, camera={camera}, name={name}"),
                )
                continue

            # If campus agent didn't send notification (missing phone locally),
            # look up phone from backend face DB and send from here
            if not notified and not phones:
                cursor = await db.execute(
                    "SELECT phone FROM registered_faces "
                    "WHERE person_id = ? AND phone IS NOT NULL AND phone != '' "
                    "LIMIT 1",
                    (person_id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    phones = row[0]
                    # Parse time from logged_at
                    try:
                        _ts = datetime.fromisoformat(logged_at)
                        _ist = _ts.astimezone(
                            __import__("datetime").timezone(timedelta(hours=5, minutes=30))
                        )
                        time_str = _ist.strftime("%I:%M %p")
                    except Exception:
                        time_str = "this morning"

                    phone_list = [p.strip() for p in phones.split(",") if p.strip()]
                    for ph in phone_list:
                        digits = "".join(c for c in ph if c.isdigit())
                        if len(digits) == 10:
                            digits = "91" + digits
                        if len(digits) >= 12:
                            ok = await send_cloud_template_message(
                                digits, "ppis_attendance_alert",
                                body_params=[name, time_str],
                            )
                            if ok:
                                logger.info(
                                    f"Backend sent attendance notification for "
                                    f"{name} ({person_id}) to {digits}"
                                )
                                backend_notified += 1
                            else:
                                logger.warning(
                                    f"Backend notification failed for {name} to {digits}"
                                )
                    if phone_list:
                        notified = 1

            # Check if already reported today for this person
            cursor = await db.execute(
                "SELECT id FROM attendance_records "
                "WHERE person_id = ? AND date(logged_at) = date(?)",
                (person_id, logged_at),
            )
            existing = await cursor.fetchone()
            if existing:
                await db.execute(
                    "UPDATE attendance_records SET notification_sent = ?, "
                    "logged_at = ?, confidence = ?, parent_phones = ? "
                    "WHERE id = ?",
                    (notified, logged_at, confidence, phones, existing[0]),
                )
                updated += 1
                continue

            await db.execute(
                "INSERT INTO attendance_records "
                "(person_id, student_name, grade, camera_label, confidence, "
                "notification_sent, parent_phones, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (person_id, name, grade, camera, confidence, notified, phones, logged_at),
            )
            inserted += 1

        await db.commit()
        return {
            "status": "ok",
            "inserted": inserted,
            "updated": updated,
            "rejected": rejected,
            "backend_notified": backend_notified,
        }
    finally:
        await db.close()


@router.delete("/attendance/record/{person_id}",
               dependencies=[Depends(verify_dashboard_secret)])
async def delete_attendance_record(person_id: str):
    """Delete an attendance record by person_id. Requires AGENT_SECRET."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM attendance_records WHERE LOWER(person_id) = LOWER(?)", (person_id,)
        )
        deleted_count = cursor.rowcount
        if deleted_count > 0:
            await db.execute(
                "INSERT INTO audit_log (action, table_name, record_id, details) "
                "VALUES (?, ?, ?, ?)",
                ("delete", "attendance_records", person_id,
                 f"Deleted {deleted_count} record(s)"),
            )
        await db.commit()
        return {"status": "ok", "deleted": deleted_count, "person_id": person_id}
    finally:
        await db.close()


# ── Chats ────────────────────────────────────────────────────────────────────

@router.get("/chats")
async def get_chats(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    phone: str | None = None,
    direction: str | None = None,
    search: str | None = None,
):
    """Get chat messages with filters."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list = []

        if phone:
            conditions.append("(sender LIKE ? OR receiver LIKE ?)")
            params.extend([f"%{phone}%", f"%{phone}%"])
        if direction:
            conditions.append("direction = ?")
            params.append(direction)
        if search:
            conditions.append("content LIKE ?")
            params.append(f"%{search}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor = await db.execute(
            f"SELECT id, sender, receiver, content, channel, direction, "
            f"datetime(timestamp, '+5 hours', '+30 minutes') "
            f"FROM messages {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()

        # Total count
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM messages {where}", params
        )
        total = (await cursor.fetchone())[0]

        return {
            "total": total,
            "messages": [
                {
                    "id": r[0], "sender": r[1], "receiver": r[2],
                    "content": r[3], "channel": r[4], "direction": r[5],
                    "timestamp": r[6],
                }
                for r in rows
            ],
        }
    finally:
        await db.close()


@router.get("/chats/conversations")
async def get_conversations(limit: int = Query(30, ge=1, le=100)):
    """Get unique conversations (grouped by phone number) with latest message."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT
                CASE WHEN direction = 'incoming' THEN sender ELSE receiver END as phone,
                content as last_message,
                datetime(timestamp, '+5 hours', '+30 minutes') as last_time,
                direction
            FROM messages
            WHERE id IN (
                SELECT MAX(id) FROM messages
                GROUP BY CASE WHEN direction = 'incoming' THEN sender ELSE receiver END
            )
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()

        conversations = []
        for r in rows:
            phone = r[0]
            # Look up parent name from pi_sheet_students
            cur2 = await db.execute(
                "SELECT student_name, grade, father_name, mother_name "
                "FROM pi_sheet_students "
                "WHERE father_mobile LIKE ? OR mother_mobile LIKE ? LIMIT 1",
                (f"%{phone[-10:]}%", f"%{phone[-10:]}%"),
            )
            parent_row = cur2 and await cur2.fetchone()
            label = ""
            if parent_row:
                label = f"{parent_row[0]} ({parent_row[1]})"

            conversations.append({
                "phone": phone,
                "label": label,
                "last_message": r[1][:100] if r[1] else "",
                "last_time": r[2],
                "direction": r[3],
            })

        return {"conversations": conversations}
    finally:
        await db.close()


# ── Notifications ────────────────────────────────────────────────────────────

@router.get("/notifications/stats")
async def notification_stats():
    """Get notification delivery statistics."""
    db = await get_db()
    try:
        # Count messages by direction today
        cursor = await db.execute(
            "SELECT direction, COUNT(*) FROM messages "
            "WHERE date(timestamp) = date('now', '+5 hours', '+30 minutes') GROUP BY direction"
        )
        msg_counts = {r[0]: r[1] for r in await cursor.fetchall()}

        # Total messages
        cursor = await db.execute("SELECT COUNT(*) FROM messages")
        total_messages = (await cursor.fetchone())[0]

        # Today's attendance notifications
        cursor = await db.execute(
            "SELECT COUNT(*) FROM attendance_records "
            "WHERE date(logged_at) = date('now', '+5 hours', '+30 minutes') AND notification_sent = 1"
        )
        notified_today = (await cursor.fetchone())[0]

        return {
            "today_incoming": msg_counts.get("incoming", 0),
            "today_outgoing": msg_counts.get("outgoing", 0),
            "total_messages": total_messages,
            "attendance_notified_today": notified_today,
        }
    finally:
        await db.close()


# ── Students ─────────────────────────────────────────────────────────────────

@router.get("/students")
async def get_students(
    grade: str | None = None,
    search: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get student directory from PI sheet."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list = []

        if grade:
            conditions.append("grade LIKE ?")
            params.append(f"%{grade}%")
        if search:
            conditions.append("(student_name LIKE ? OR father_name LIKE ? OR mother_name LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor = await db.execute(
            f"SELECT id, student_name, grade, father_name, mother_name, "
            f"father_mobile, mother_mobile, address, transport "
            f"FROM pi_sheet_students {where} ORDER BY grade, student_name "
            f"LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()

        cursor = await db.execute(
            f"SELECT COUNT(*) FROM pi_sheet_students {where}", params
        )
        total = (await cursor.fetchone())[0]

        return {
            "total": total,
            "students": [
                {
                    "id": r[0], "name": r[1], "grade": r[2],
                    "father_name": r[3], "mother_name": r[4],
                    "father_phone": r[5], "mother_phone": r[6],
                    "address": r[7], "transport": r[8],
                }
                for r in rows
            ],
        }
    finally:
        await db.close()


@router.post("/students/refresh")
async def refresh_students():
    """Re-import all students from the PI Sheet (all grade tabs).

    Fetches every grade tab, excludes withdrawn students, deduplicates,
    and replaces the pi_sheet_students table.
    """
    from app.services.sheet_refresh_service import fetch_all_pi_sheet_tabs

    result = await fetch_all_pi_sheet_tabs()
    if result:
        db = await get_db()
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM pi_sheet_students")
            total = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT COUNT(*) FROM pi_sheet_students "
                "WHERE (father_mobile != '' AND father_mobile IS NOT NULL) "
                "OR (mother_mobile != '' AND mother_mobile IS NOT NULL)"
            )
            with_phones = (await cursor.fetchone())[0]
            return {
                "status": "ok",
                "total_students": total,
                "with_phones": with_phones,
                "missing_phones": total - with_phones,
            }
        finally:
            await db.close()
    return {"status": "error", "message": "Failed to refresh PI Sheet data"}


@router.delete("/students/{student_id}")
async def delete_student(student_id: int):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT student_name, grade FROM pi_sheet_students WHERE id = ?",
            (student_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"status": "error", "message": "Student not found"}
        await db.execute("DELETE FROM pi_sheet_students WHERE id = ?", (student_id,))
        await db.commit()
        return {"status": "ok", "deleted": {"id": student_id, "name": row[0], "grade": row[1]}}
    finally:
        await db.close()


@router.get("/students/grades")
async def get_grades():
    """Get list of all grades with student counts."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT grade, COUNT(*) as count FROM pi_sheet_students "
            "GROUP BY grade ORDER BY grade"
        )
        return {"grades": [{"grade": r[0], "count": r[1]} for r in await cursor.fetchall()]}
    finally:
        await db.close()


# ── Face Registration ────────────────────────────────────────────────────────

@router.get("/faces")
async def get_registered_faces():
    """Get all registered face entries."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT person_id, name, role, phone, angle, registered_at "
            "FROM agent_registered_faces ORDER BY registered_at DESC"
        )
        rows = await cursor.fetchall()

        # Group by person
        persons: dict = {}
        for r in rows:
            pid = r[0]
            if pid not in persons:
                persons[pid] = {
                    "person_id": pid, "name": r[1], "role": r[2],
                    "phone": r[3], "registered_at": r[5], "face_count": 0,
                }
            persons[pid]["face_count"] += 1

        return {"registered": list(persons.values()), "total": len(persons)}
    finally:
        await db.close()


@router.delete("/faces/{person_id}",
               dependencies=[Depends(verify_dashboard_secret)])
async def dashboard_delete_face(person_id: str):
    """Delete a face entry. Requires AGENT_SECRET."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT person_id FROM agent_registered_faces "
            "WHERE person_id = ? COLLATE NOCASE LIMIT 1",
            (person_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"status": "error", "message": "Person not found"}
        original_pid = row[0]
        cursor = await db.execute(
            "DELETE FROM agent_registered_faces WHERE person_id = ? COLLATE NOCASE",
            (person_id,),
        )
        await db.commit()
        deleted = cursor.rowcount
        await db.execute(
            "INSERT INTO audit_log (action, table_name, record_id, details) "
            "VALUES (?, ?, ?, ?)",
            ("delete", "agent_registered_faces", original_pid,
             f"Deleted {deleted} face(s)"),
        )
        logger.info(f"Dashboard: deleted {deleted} face(s) for {original_pid}")
        return {"status": "ok", "deleted": deleted, "person_id": original_pid}
    finally:
        await db.close()


@router.patch("/faces/{person_id}/phone",
              dependencies=[Depends(verify_dashboard_secret)])
async def dashboard_update_face_phone(person_id: str, request: Request):
    """Update the phone number for a face entry. Requires AGENT_SECRET."""
    body = await request.json()
    phone = body.get("phone", "")
    if not phone:
        return {"status": "error", "message": "Missing phone"}
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE agent_registered_faces SET phone = ? "
            "WHERE person_id = ? COLLATE NOCASE",
            (phone, person_id),
        )
        await db.commit()
        updated = cursor.rowcount
        logger.info(f"Dashboard: updated phone for {person_id}: {phone} ({updated} rows)")
        return {"status": "ok", "updated": updated, "person_id": person_id, "phone": phone}
    finally:
        await db.close()


# ── Cameras ──────────────────────────────────────────────────────────────────

@router.get("/cameras")
async def get_cameras():
    """Get camera mapping."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT location, dvr_index, channel, description, cam_type "
            "FROM agent_camera_mapping ORDER BY location"
        )
        rows = await cursor.fetchall()
        return {
            "total": len(rows),
            "cameras": [
                {
                    "location": r[0], "dvr_index": r[1], "channel": r[2],
                    "description": r[3],
                    "cam_type": r[4] if r[4] else f"DVR {r[1] + 1}",
                }
                for r in rows
            ],
        }
    finally:
        await db.close()


# ── Overview ─────────────────────────────────────────────────────────────────

@router.get("/overview")
async def dashboard_overview():
    """Get a full overview of the system for the dashboard home page."""
    db = await get_db()
    try:
        # Students
        cursor = await db.execute("SELECT COUNT(*) FROM pi_sheet_students")
        total_students = (await cursor.fetchone())[0]

        # Registered faces
        cursor = await db.execute("SELECT COUNT(DISTINCT person_id) FROM agent_registered_faces")
        registered_faces = (await cursor.fetchone())[0]

        # Today's attendance
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT person_id) FROM attendance_records "
            "WHERE date(logged_at) = date('now', '+5 hours', '+30 minutes')"
        )
        present_today = (await cursor.fetchone())[0]

        # Today's messages
        cursor = await db.execute(
            "SELECT COUNT(*) FROM messages WHERE date(timestamp) = date('now', '+5 hours', '+30 minutes')"
        )
        messages_today = (await cursor.fetchone())[0]

        # Total messages
        cursor = await db.execute("SELECT COUNT(*) FROM messages")
        total_messages = (await cursor.fetchone())[0]

        # Cameras
        cursor = await db.execute("SELECT COUNT(*) FROM agent_camera_mapping")
        total_cameras = (await cursor.fetchone())[0]

        # Recent activity (last 10 messages) — convert to IST
        cursor = await db.execute(
            "SELECT sender, content, direction, "
            "datetime(timestamp, '+5 hours', '+30 minutes') "
            "FROM messages ORDER BY timestamp DESC LIMIT 10"
        )
        recent_messages = [
            {"sender": r[0], "content": r[1][:80], "direction": r[2], "time": r[3]}
            for r in await cursor.fetchall()
        ]

        return {
            "total_students": total_students,
            "registered_faces": registered_faces,
            "present_today": present_today,
            "messages_today": messages_today,
            "total_messages": total_messages,
            "total_cameras": total_cameras,
            "recent_activity": recent_messages,
        }
    finally:
        await db.close()


# ── Teacher Attendance Excel ─────────────────────────────────────────────────

@router.get("/teacher-attendance-excel")
async def download_teacher_attendance_excel(month: str | None = None):
    """Download the teacher attendance Excel workbook.

    Optional query param `month` in YYYY-MM format regenerates for that month.
    Without it, regenerates for the current month and returns the file.
    """
    from app.services.teacher_attendance_excel import (
        generate_teacher_attendance_excel,
        EXCEL_PATH,
    )

    try:
        if month:
            parts = month.split("-")
            target = date(int(parts[0]), int(parts[1]), 1)
        else:
            from app.services.teacher_attendance_excel import IST
            target = datetime.now(IST).date()

        await generate_teacher_attendance_excel(target)
    except Exception as e:
        logger.error(f"Teacher attendance Excel generation failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

    if not Path(str(EXCEL_PATH)).exists():
        return {"status": "error", "message": "Excel file not found"}

    filename = f"PPIS_Teacher_Attendance_{target.strftime('%B_%Y')}.xlsx"
    return FileResponse(
        path=str(EXCEL_PATH),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
