"""
Cloud-hosted face registration API.

Stores face images for the Campus Agent's attendance recognition system.
The agent downloads face images on startup, computes encodings locally,
and uses them for real-time recognition.
"""

import base64
import gc
import json
import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, File, Form, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.database import get_db
from app.routes.agent_config import verify_agent_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/face", tags=["face"])


MIN_IMAGE_SIZE = 10_000  # 10 KB — reject tiny/corrupt images
MAX_IMAGE_SIZE = 10_000_000  # 10 MB — reject unreasonably large uploads
MIN_IMAGE_DIMENSION = 100  # pixels — reject images smaller than 100x100


def _validate_image_quality(image_data: bytes) -> dict:
    """Basic image quality validation. Returns {"ok": True} or {"ok": False, "reason": ...}."""
    size = len(image_data)
    if size < MIN_IMAGE_SIZE:
        return {"ok": False, "reason": f"Image too small ({size} bytes < {MIN_IMAGE_SIZE} min)"}
    if size > MAX_IMAGE_SIZE:
        return {"ok": False, "reason": f"Image too large ({size} bytes > {MAX_IMAGE_SIZE} max)"}
    try:
        from PIL import Image as PILImage
        import io
        img = PILImage.open(io.BytesIO(image_data))
        w, h = img.size
        if w < MIN_IMAGE_DIMENSION or h < MIN_IMAGE_DIMENSION:
            return {"ok": False, "reason": f"Image too small ({w}x{h}px, min {MIN_IMAGE_DIMENSION}px)"}
    except ImportError:
        # PIL not available — skip dimension check, accept based on file size only
        return {"ok": True, "width": 0, "height": 0, "size_bytes": size}
    except Exception as e:
        return {"ok": False, "reason": f"Invalid image: {e}"}
    return {"ok": True, "width": w, "height": h, "size_bytes": size}


async def validate_face_in_image(image_data: bytes) -> dict:
    """Validate that an image contains a real human face suitable for recognition.

    Uses OpenAI vision (gpt-4o-mini) to classify the image.
    Returns {"ok": True} if a real face is detected, or
    {"ok": False, "reason": "..."} if rejected.
    """
    import base64 as b64
    from app.services.openai_service import get_client

    ai = get_client()
    if not ai:
        # No OpenAI key — fall back to basic PIL-based heuristic
        return _heuristic_face_check(image_data)

    # Resize image to save tokens (max 512px on longest side)
    try:
        from PIL import Image as PILImage
        import io
        img = PILImage.open(io.BytesIO(image_data))
        max_dim = max(img.size)
        if max_dim > 512:
            ratio = 512 / max_dim
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        small_bytes = buf.getvalue()
    except Exception:
        small_bytes = image_data

    encoded = b64.b64encode(small_bytes).decode("ascii")

    try:
        resp = await ai.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Is this a photo of a real human face suitable for "
                            "face recognition attendance registration? "
                            "Reply ONLY with one of:\n"
                            "FACE_OK - if there is a clear real human face\n"
                            "REJECT_SCREENSHOT - if this is a screenshot of a phone/computer screen\n"
                            "REJECT_DOCUMENT - if this is a document, certificate, or printed paper\n"
                            "REJECT_NO_FACE - if there is no human face visible\n"
                            "REJECT_MULTIPLE - if there are multiple faces (need single person)\n"
                            "REJECT_PHOTO_OF_PHOTO - if this is a photo of a printed photo or screen display\n"
                            "REJECT_OTHER - if it's not suitable for any other reason\n"
                            "Reply with ONLY the code, nothing else."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encoded}",
                            "detail": "low",
                        },
                    },
                ],
            }],
        )
        answer = (resp.choices[0].message.content or "").strip().upper()
        logger.info(f"Face validation AI response: {answer}")

        if "FACE_OK" in answer:
            return {"ok": True}

        reason_map = {
            "REJECT_SCREENSHOT": "This looks like a screenshot, not a real photo. Please send an actual photo of the person's face.",
            "REJECT_DOCUMENT": "This looks like a document or printed paper, not a face photo. Please send a clear photo of the person's face.",
            "REJECT_NO_FACE": "No human face detected in this image. Please send a clear front-facing photo.",
            "REJECT_MULTIPLE": "Multiple faces detected. Please send a photo with only one person's face.",
            "REJECT_PHOTO_OF_PHOTO": "This appears to be a photo of a photo or screen. Please send an original photo taken directly.",
            "REJECT_OTHER": "This image is not suitable for face registration. Please send a clear, front-facing photo of the person.",
        }
        for code, msg in reason_map.items():
            if code in answer:
                return {"ok": False, "reason": msg}

        # Unknown response — accept to avoid blocking legitimate registrations
        return {"ok": True}

    except Exception as e:
        logger.warning(f"Face validation AI error: {e} — falling back to heuristic")
        return _heuristic_face_check(image_data)


def _heuristic_face_check(image_data: bytes) -> dict:
    """Basic heuristic check when OpenAI is unavailable.

    Checks image properties that suggest a screenshot vs a real photo:
    - Screenshots tend to have very specific aspect ratios (9:16, 9:19.5, etc.)
    - Screenshots tend to be PNG format
    - Photos from cameras are typically JPEG
    """
    try:
        from PIL import Image as PILImage
        import io
        img = PILImage.open(io.BytesIO(image_data))
        w, h = img.size
        fmt = (img.format or "").upper()

        # PNG images sent via WhatsApp camera roll are likely screenshots
        if fmt == "PNG" and min(w, h) >= 300:
            aspect = max(w, h) / min(w, h)
            # Common phone screenshot ratios
            if 1.7 < aspect < 2.3:
                return {
                    "ok": False,
                    "reason": "This looks like a screenshot. Please send an actual photo of the person's face.",
                }

        # Check if image is too "flat" in color (solid colors = screenshot/document)
        if img.mode in ("RGB", "RGBA"):
            # Sample center region
            cx, cy = w // 2, h // 2
            crop_size = min(w, h) // 4
            if crop_size > 20:
                center = img.crop((cx - crop_size, cy - crop_size,
                                   cx + crop_size, cy + crop_size))
                colors = center.getcolors(maxcolors=100)
                if colors and len(colors) < 5:
                    return {
                        "ok": False,
                        "reason": "This image doesn't appear to contain a face. Please send a clear photo of the person.",
                    }
    except Exception:
        pass

    # When in doubt, accept (fail-open for heuristic only)
    return {"ok": True}


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

    Validates image quality (size, dimensions) before storing.
    The Campus Agent downloads these images on startup and computes
    face encodings locally for recognition.
    """
    image_data = await image.read()
    if not image_data:
        raise HTTPException(status_code=400, detail="Empty image file")

    quality = _validate_image_quality(image_data)
    if not quality["ok"]:
        raise HTTPException(status_code=400, detail=quality["reason"])

    db = await get_db()
    try:
        # Check for duplicate person_id + angle
        cursor = await db.execute(
            "SELECT id FROM agent_registered_faces "
            "WHERE person_id = ? AND angle = ?",
            (person_id, angle),
        )
        existing = await cursor.fetchone()
        if existing:
            # Update existing registration instead of duplicating
            await db.execute(
                "UPDATE agent_registered_faces SET name = ?, role = ?, "
                "phone = ?, image_data = ?, registered_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (name, role, phone, image_data, existing[0]),
            )
            await db.commit()
            logger.info(f"Updated face: {name} ({person_id}), angle={angle}, id={existing[0]}")
            return {
                "success": True,
                "face_id": existing[0],
                "person_id": person_id,
                "angle": angle,
                "updated": True,
            }

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


@router.get("/manifest", dependencies=[Depends(verify_agent_secret)])
async def face_manifest():
    """Return lightweight metadata for all faces (no image data).

    The agent uses this to determine which faces it's missing locally,
    then downloads only the missing ones via /api/face/image/{face_id}.
    For teachers without a phone, falls back to PI Sheet TEACHER_DATA.
    """
    from app.services.openai_service import TEACHER_DATA

    # Build PI Sheet phone lookup by normalized name
    pi_phone_lookup: dict[str, str] = {}
    for entry in TEACHER_DATA:
        teacher_name = entry.get("teacher", "")
        phone = entry.get("whatsapp", "")
        if not phone:
            continue
        phone_digits = re.sub(r"\D", "", phone)
        if len(phone_digits) == 10:
            phone_digits = "91" + phone_digits
        for name_part in teacher_name.split("/"):
            key = re.sub(r"[^a-z]", "", name_part.lower().strip())
            if key:
                pi_phone_lookup[key] = phone_digits

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, person_id, name, role, phone, angle, registered_at "
            "FROM agent_registered_faces ORDER BY person_id, angle"
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            entry = dict(r)
            # If teacher has no phone, try PI Sheet lookup
            if entry.get("role") == "Teacher" and not entry.get("phone"):
                name_key = re.sub(r"[^a-z]", "", (entry.get("name") or "").lower())
                pi_phone = pi_phone_lookup.get(name_key, "")
                if not pi_phone:
                    # Try first name only
                    first_name = re.sub(r"[^a-z]", "", (entry.get("name") or "").lower().split()[0]) if entry.get("name") else ""
                    pi_phone = pi_phone_lookup.get(first_name, "")
                if pi_phone:
                    entry["phone"] = pi_phone
            results.append(entry)
        return results
    finally:
        await db.close()


@router.get("/images", dependencies=[Depends(verify_agent_secret)])
async def list_face_images():
    """List all face images with metadata (base64-encoded image data).

    Used by the Campus Agent to sync face data on startup.
    Streams results as a JSON array, one row at a time, to avoid
    loading all images into memory at once (prevents OOM on 256MB Fly).
    """
    async def _stream():
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT id, person_id, name, role, phone, angle, image_data, registered_at "
                "FROM agent_registered_faces ORDER BY person_id, angle"
            )
            yield "["
            first = True
            async for r in cursor:
                img_b64 = base64.b64encode(r["image_data"]).decode("ascii")
                entry = {
                    "id": r["id"],
                    "person_id": r["person_id"],
                    "name": r["name"],
                    "role": r["role"],
                    "phone": r["phone"],
                    "angle": r["angle"],
                    "image_base64": img_b64,
                    "registered_at": r["registered_at"],
                }
                chunk = ("," if not first else "") + json.dumps(entry)
                first = False
                yield chunk
                del img_b64, entry, chunk
            yield "]"
        finally:
            gc.collect()
            await db.close()

    return StreamingResponse(_stream(), media_type="application/json")


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


@router.get("/teachers")
async def list_registered_teachers():
    """List all registered teachers (no auth required — public status endpoint)."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT person_id, name, phone,
                   COUNT(*) as photo_count,
                   GROUP_CONCAT(angle) as angles,
                   MIN(registered_at) as registered_at
            FROM agent_registered_faces
            WHERE role = 'Teacher'
            GROUP BY person_id
            ORDER BY name
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.patch("/{person_id}/phone", dependencies=[Depends(verify_agent_secret)])
async def update_phone(person_id: str, phone: str = Form(...)):
    """Update the phone number for all face entries of a person."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE agent_registered_faces SET phone = ? "
            "WHERE person_id = ? COLLATE NOCASE",
            (phone, person_id),
        )
        await db.commit()
        updated = cursor.rowcount
        logger.info(f"Updated phone for {person_id}: {phone} ({updated} rows)")
        return {"updated": updated, "person_id": person_id, "phone": phone}
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


@router.post("/backfill-phones")
async def backfill_phones_from_pi_sheet():
    """Backfill phone numbers for registered faces using PI Sheet data.

    Matches teacher names between agent_registered_faces and TEACHER_DATA,
    then updates empty phone fields with the PI Sheet WhatsApp number.
    """
    from app.services.openai_service import TEACHER_DATA

    def _normalize_name(name: str) -> str:
        return re.sub(r"[^a-z]", "", name.lower().split("/")[0].strip())

    # Build lookup: normalized_name -> phone
    pi_lookup: dict[str, str] = {}
    for entry in TEACHER_DATA:
        teacher_name = entry.get("teacher", "")
        phone = entry.get("whatsapp", "")
        if not phone:
            continue
        phone_digits = re.sub(r"\D", "", phone)
        if len(phone_digits) == 10:
            phone_digits = "91" + phone_digits
        # Index by each name variant (before / after slash)
        for name_part in teacher_name.split("/"):
            key = _normalize_name(name_part)
            if key:
                pi_lookup[key] = phone_digits

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT DISTINCT person_id, name, phone FROM agent_registered_faces "
            "WHERE role = 'Teacher'"
        )
        rows = await cursor.fetchall()

        updated = 0
        details = []
        for row in rows:
            existing_phone = row["phone"] or ""
            person_name = row["name"] or ""
            person_id = row["person_id"] or ""

            # Try to match by name
            name_key = _normalize_name(person_name)
            pi_phone = pi_lookup.get(name_key, "")

            if not pi_phone:
                # Try matching by person_id parts
                pid_parts = person_id.replace("TEACHER_", "").split("_")
                for part in pid_parts:
                    part_key = re.sub(r"[^a-z]", "", part.lower())
                    if part_key in pi_lookup:
                        pi_phone = pi_lookup[part_key]
                        break

            if pi_phone and pi_phone not in existing_phone:
                new_phone = f"{existing_phone},{pi_phone}" if existing_phone else pi_phone
                await db.execute(
                    "UPDATE agent_registered_faces SET phone = ? "
                    "WHERE person_id = ? COLLATE NOCASE",
                    (new_phone, person_id),
                )
                updated += 1
                details.append({
                    "person_id": person_id,
                    "name": person_name,
                    "old_phone": existing_phone,
                    "new_phone": new_phone,
                })

        await db.commit()
        return {"updated": updated, "details": details}
    finally:
        await db.close()
