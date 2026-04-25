import json
import logging
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.database import get_db
from app.auth import require_admin
from app.models.schemas import SettingsUpdate, SettingsResponse

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_admin)],
)


@router.get("/", response_model=SettingsResponse)
async def get_settings():
    """Get current bot settings."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = 'system_prompt'"
        )
        row = await cursor.fetchone()
        system_prompt = row[0] if row else "You are a helpful AI assistant."
        return SettingsResponse(system_prompt=system_prompt)
    finally:
        await db.close()


@router.put("/", response_model=SettingsResponse)
async def update_settings(settings: SettingsUpdate):
    """Update bot settings."""
    db = await get_db()
    try:
        if settings.system_prompt is not None:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('system_prompt', ?)",
                (settings.system_prompt,),
            )
            await db.commit()

        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = 'system_prompt'"
        )
        row = await cursor.fetchone()
        system_prompt = row[0] if row else "You are a helpful AI assistant."
        return SettingsResponse(system_prompt=system_prompt)
    finally:
        await db.close()


# ---- PI Sheet student data endpoints ----

class PISheetStudent(BaseModel):
    student: str
    grade: str
    father: str = ""
    mother: str = ""
    father_mobile: str = ""
    mother_mobile: str = ""
    address: str = ""
    transport: str = ""


class PISheetUpload(BaseModel):
    students: list[PISheetStudent]


@router.post("/pi-sheet")
async def upload_pi_sheet(data: PISheetUpload):
    """Upload PI Sheet student data (replaces existing data)."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM pi_sheet_students")
        for s in data.students:
            await db.execute(
                "INSERT INTO pi_sheet_students (student_name, grade, father_name, mother_name, father_mobile, mother_mobile, address, transport) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (s.student, s.grade, s.father, s.mother, s.father_mobile, s.mother_mobile, s.address, s.transport),
            )
        await db.commit()
        return {"status": "ok", "count": len(data.students)}
    finally:
        await db.close()


@router.get("/pi-sheet")
async def get_pi_sheet_stats():
    """Get PI Sheet student data stats."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM pi_sheet_students")
        row = await cursor.fetchone()
        count = row[0] if row else 0
        cursor2 = await db.execute("SELECT DISTINCT grade FROM pi_sheet_students ORDER BY grade")
        grades = [r[0] async for r in cursor2]
        return {"count": count, "grades": grades}
    finally:
        await db.close()


@router.get("/pi-sheet/lookup")
async def lookup_student(q: str = ""):
    """Look up a student or parent by name or phone number."""
    db = await get_db()
    try:
        q_lower = q.strip().lower()
        digits = "".join(c for c in q if c.isdigit())
        results = []
        if len(digits) >= 10:
            bare = digits[-10:]
            cursor = await db.execute(
                "SELECT * FROM pi_sheet_students WHERE father_mobile LIKE ? OR mother_mobile LIKE ?",
                (f"%{bare}%", f"%{bare}%"),
            )
            results = [dict(r) for r in await cursor.fetchall()]

        if not results:
            cursor = await db.execute(
                "SELECT * FROM pi_sheet_students WHERE LOWER(student_name) LIKE ? OR LOWER(father_name) LIKE ? OR LOWER(mother_name) LIKE ?",
                (f"%{q_lower}%", f"%{q_lower}%", f"%{q_lower}%"),
            )
            results = [dict(r) for r in await cursor.fetchall()]

        return {"results": results[:20], "total": len(results)}
    finally:
        await db.close()
