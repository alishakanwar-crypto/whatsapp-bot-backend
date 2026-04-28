"""
Cloud-hosted face registration API.

Stores face images for the Campus Agent's attendance recognition system.
The agent downloads face images on startup, computes encodings locally,
and uses them for real-time recognition.
"""

import base64
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, File, Form, UploadFile
from fastapi.responses import Response

from app.database import get_db
from app.routes.agent_config import verify_agent_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/face", tags=["face"])


@router.post("/register", dependencies=[Depends(verify_agent_secret)])
async def register_face(
    person_id: str = Form(...),
    name: str = Form(...),
    role: str = Form(""),
    phone: str = Form(""),
    angle: str = Form("front"),
    image: UploadFile = File(...),
):
    """Register a face image in the cloud database.

    The Campus Agent downloads these images on startup and computes
    face encodings locally for recognition.
    """
    image_data = await image.read()
    if not image_data:
        raise HTTPException(status_code=400, detail="Empty image file")

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO agent_registered_faces "
            "(person_id, name, role, phone, angle, image_data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, name, role, phone, angle, image_data),
        )
        await db.commit()
        face_id = cursor.lastrowid
        logger.info(f"Registered face: {name} ({person_id}), angle={angle}, id={face_id}")
        return {
            "success": True,
            "face_id": face_id,
            "person_id": person_id,
            "angle": angle,
        }
    finally:
        await db.close()


@router.get("/registered", dependencies=[Depends(verify_agent_secret)])
async def list_registered():
    """List all registered persons (without image data)."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT person_id, name, role, phone,
                   COUNT(*) as face_count,
                   GROUP_CONCAT(angle) as angles,
                   MIN(registered_at) as registered_at
            FROM agent_registered_faces
            GROUP BY person_id
            ORDER BY registered_at DESC
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.get("/images", dependencies=[Depends(verify_agent_secret)])
async def list_face_images():
    """List all face images with metadata (base64-encoded image data).

    Used by the Campus Agent to sync face data on startup.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, person_id, name, role, phone, angle, image_data, registered_at "
            "FROM agent_registered_faces ORDER BY person_id, angle"
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "id": r["id"],
                "person_id": r["person_id"],
                "name": r["name"],
                "role": r["role"],
                "phone": r["phone"],
                "angle": r["angle"],
                "image_base64": base64.b64encode(r["image_data"]).decode("ascii"),
                "registered_at": r["registered_at"],
            })
        return results
    finally:
        await db.close()


@router.get("/image/{face_id}", dependencies=[Depends(verify_agent_secret)])
async def get_face_image(face_id: int):
    """Download a single face image by ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT image_data FROM agent_registered_faces WHERE id = ?",
            (face_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Face not found")
        return Response(content=row["image_data"], media_type="image/jpeg")
    finally:
        await db.close()


@router.delete("/entry/{face_id}", dependencies=[Depends(verify_agent_secret)])
async def delete_face_entry(face_id: int):
    """Delete a single face entry by ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM agent_registered_faces WHERE id = ?", (face_id,),
        )
        await db.commit()
        deleted = cursor.rowcount
        logger.info(f"Deleted face entry id={face_id}, rows={deleted}")
        return {"deleted": deleted, "face_id": face_id}
    finally:
        await db.close()


@router.delete("/{person_id}", dependencies=[Depends(verify_agent_secret)])
async def delete_person(person_id: str):
    """Delete all face images for a person."""
    db = await get_db()
    try:
        # Retrieve the original person_id from DB before deleting
        # (LowercaseURLMiddleware lowercases the path param, so we need
        # the DB value to return the correct casing to the client)
        cursor = await db.execute(
            "SELECT person_id FROM agent_registered_faces "
            "WHERE person_id = ? COLLATE NOCASE LIMIT 1",
            (person_id,),
        )
        row = await cursor.fetchone()
        original_person_id = row["person_id"] if row else person_id

        cursor = await db.execute(
            "DELETE FROM agent_registered_faces WHERE person_id = ? COLLATE NOCASE",
            (person_id,),
        )
        await db.commit()
        deleted = cursor.rowcount
        logger.info(f"Deleted {deleted} face(s) for {original_person_id}")
        return {"deleted": deleted, "person_id": original_person_id}
    finally:
        await db.close()
