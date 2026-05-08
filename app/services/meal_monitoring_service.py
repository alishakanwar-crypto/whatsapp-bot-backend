"""
Meal Monitoring Service — automated classroom snapshot during break times.

Captures snapshots from all classroom cameras during:
  - Short Break: 8:50 AM – 9:00 AM IST
  - Lunch Break: 11:20 AM – 11:45 AM IST

Each snapshot is tagged with class, section, date, timestamp and sent to
all parents of that class via WhatsApp with the message:
  "Be Assured, your child has taken meal."

Logs capture time, delivery status, and failed notifications.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Grade-to-camera-location mapping.
# PI sheet grades like "Grade 9A" map to camera location "GRADE 9A".
_GRADE_CAMERA_MAP: dict[str, str] = {}

# Grades with students but no dedicated camera — these are skipped.
_GRADES_WITHOUT_CAMERA = {"Grade 10", "Grade 2C", "Grade 6C", "Grade 7C",
                          "Grade 9", "Grade 11C", "Grade 12C", "Nursery"}


def _pi_grade_to_camera_key(grade: str) -> str | None:
    """Convert PI-sheet grade name to its camera location key.

    Returns None if no camera is mapped for this grade.
    """
    if grade in _GRADES_WITHOUT_CAMERA:
        return None
    g = grade.strip()
    g_upper = g.upper()
    m = re.match(r"GRADE\s*(\d{1,2})\s*([A-D])?", g_upper)
    if m:
        num, sec = m.group(1), (m.group(2) or "")
        return f"GRADE {num}{sec}"
    m = re.match(r"(?:NURSERY|NUR)[\s-]*(\d+)", g_upper)
    if m:
        return f"NUR-{m.group(1)}"
    m = re.match(r"PREP[\s-]*(\d+)", g_upper)
    if m:
        return f"PREP-{m.group(1)}"
    if "POPSICLE" in g_upper:
        return "Popsicles"
    return None


def _format_grade_label(grade: str) -> tuple[str, str]:
    """Return (class_name, section) for tagging.

    Examples:
      'Grade 9A' -> ('Grade 9', 'A')
      'Nursery 1' -> ('Nursery', '1')
      'Popsicles' -> ('Popsicles', '')
    """
    m = re.match(r"(Grade\s*\d{1,2})\s*([A-D])?", grade, re.IGNORECASE)
    if m:
        return m.group(1).strip(), (m.group(2) or "").strip()
    m = re.match(r"(Nursery|Prep)\s*(\d+)", grade, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return grade.strip(), ""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def _get_parents_for_grade(grade: str) -> list[dict]:
    """Return list of {student_name, father_phone, mother_phone} for a grade."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT student_name, father_mobile, mother_mobile "
            "FROM pi_sheet_students WHERE grade = ?",
            (grade,),
        )
        rows = await cursor.fetchall()
        return [
            {"student_name": r[0], "father_phone": r[1] or "", "mother_phone": r[2] or ""}
            for r in rows
        ]
    finally:
        await db.close()


async def _get_all_classroom_grades() -> list[str]:
    """Return all distinct grades that have a mapped camera."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT DISTINCT grade FROM pi_sheet_students ORDER BY grade"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows if _pi_grade_to_camera_key(r[0]) is not None]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

async def _log_meal_snapshot(grade: str, camera_key: str, break_type: str,
                             capture_time: str, parents_sent: int,
                             parents_failed: int, status: str) -> None:
    """Log a meal monitoring snapshot to the database."""
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO meal_monitoring_logs "
            "(grade, camera_key, break_type, capture_time, parents_sent, "
            "parents_failed, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (grade, camera_key, break_type, capture_time, parents_sent,
             parents_failed, status),
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to log meal snapshot: {e}")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Core meal monitoring logic
# ---------------------------------------------------------------------------

async def run_meal_monitoring(break_type: str = "lunch",
                              target_grade: str | None = None,
                              target_phones: list[str] | None = None) -> dict:
    """Capture snapshots from classroom cameras and send to parents.

    Args:
        break_type: "short_break" or "lunch"
        target_grade: If set, only process this specific grade (e.g. "Grade 3C")
        target_phones: If set, only send to these phone numbers (for testing)

    Returns summary dict with sent/failed counts.
    """
    from app.routes.agent_ws import request_snapshot, is_agent_connected
    from app.services.whatsapp_service import (
        upload_base64_image_cloud,
        send_cloud_media,
        send_cloud_template_message,
    )

    now_ist = datetime.now(IST)
    date_str = now_ist.strftime("%d-%m-%Y")
    time_str = now_ist.strftime("%I:%M %p")
    break_label = "Short Break" if break_type == "short_break" else "Lunch Break"

    test_mode = bool(target_grade or target_phones)
    logger.info(f"=== MEAL MONITORING START: {break_label} at {time_str} IST {'(TEST MODE)' if test_mode else ''} ===")

    if not is_agent_connected():
        logger.error("Meal monitoring: agent not connected, aborting")
        return {"status": "error", "error": "agent_not_connected", "sent": 0, "failed": 0}

    if target_grade:
        grades = [target_grade]
    else:
        grades = await _get_all_classroom_grades()
    logger.info(f"Meal monitoring: {len(grades)} grades to capture")

    total_sent = 0
    total_failed = 0
    total_skipped = 0
    results: list[dict] = []

    for grade in grades:
        camera_key = _pi_grade_to_camera_key(grade)
        if not camera_key:
            continue

        class_name, section = _format_grade_label(grade)
        capture_ts = datetime.now(IST).strftime("%I:%M:%S %p")

        # Capture snapshot from this classroom camera
        try:
            result = await request_snapshot(camera_key, timeout=30.0)
        except Exception as e:
            logger.error(f"Meal monitoring: snapshot failed for {grade} ({camera_key}): {e}")
            await _log_meal_snapshot(grade, camera_key, break_type, capture_ts, 0, 0, "capture_failed")
            total_skipped += 1
            continue

        if not result.get("success") or not result.get("images"):
            logger.warning(f"Meal monitoring: no image for {grade} ({camera_key}): {result.get('error', 'no images')}")
            await _log_meal_snapshot(grade, camera_key, break_type, capture_ts, 0, 0, "no_image")
            total_skipped += 1
            continue

        # Use the first image from the snapshot
        img_data = result["images"][0]
        img_b64 = img_data.get("image_base64", "")
        if not img_b64:
            logger.warning(f"Meal monitoring: empty image for {grade}")
            total_skipped += 1
            continue

        # Upload the image to Meta Cloud API once (reuse media_id for all parents)
        try:
            media_id = await upload_base64_image_cloud(img_b64)
        except Exception as e:
            logger.error(f"Meal monitoring: image upload failed for {grade}: {e}")
            await _log_meal_snapshot(grade, camera_key, break_type, capture_ts, 0, 0, "upload_failed")
            total_skipped += 1
            continue

        if not media_id:
            logger.error(f"Meal monitoring: upload returned no media_id for {grade}")
            await _log_meal_snapshot(grade, camera_key, break_type, capture_ts, 0, 0, "upload_failed")
            total_skipped += 1
            continue

        # Build caption and template params
        section_tag = f" - Section {section}" if section else ""
        grade_label = f"{class_name}{section_tag}"
        caption = (
            f"📸 *{grade_label}*\n"
            f"📅 Date: {date_str} | 🕐 Time: {capture_ts}\n"
            f"🍽️ {break_label}\n\n"
            f"Be Assured, your child has taken meal."
        )

        # Get all parents of this grade
        parents = await _get_parents_for_grade(grade)
        if not parents:
            logger.warning(f"Meal monitoring: no parents found for {grade}")
            await _log_meal_snapshot(grade, camera_key, break_type, capture_ts, 0, 0, "no_parents")
            continue

        # Send to all unique parent phone numbers (both father and mother)
        sent_phones: set[str] = set()
        grade_sent = 0
        grade_failed = 0

        # Normalize target_phones for matching
        _target_set: set[str] | None = None
        if target_phones:
            _target_set = set()
            for tp in target_phones:
                d = "".join(c for c in tp if c.isdigit())
                if len(d) == 10:
                    d = "91" + d
                _target_set.add(d)

        for parent in parents:
            for phone in [parent["father_phone"], parent["mother_phone"]]:
                if not phone or len(phone) < 10:
                    continue
                # Normalize
                digits = "".join(c for c in phone if c.isdigit())
                if len(digits) == 10:
                    digits = "91" + digits
                if digits in sent_phones:
                    continue  # Avoid duplicate
                # Filter by target phones if specified
                if _target_set and digits not in _target_set:
                    continue
                sent_phones.add(digits)

                try:
                    # NONE of our templates have header components, so media
                    # MUST always be sent as a separate message after the
                    # template (which opens a conversation window).
                    tmpl_ok = await send_cloud_template_message(
                        digits,
                        "ppis_meal_update",
                        body_params=[grade_label, date_str, capture_ts],
                    )

                    if not tmpl_ok:
                        # Fallback: use ppis_class_assignment template
                        tmpl_ok = await send_cloud_template_message(
                            digits,
                            "ppis_class_assignment",
                            body_params=[
                                f"Meal update: {grade_label}",
                                f"{date_str} {capture_ts}",
                            ],
                        )

                    if tmpl_ok and media_id:
                        # Send image separately after template opens window
                        await asyncio.sleep(2)
                        img_ok = await send_cloud_media(
                            digits, "image", media_id=media_id, caption=caption,
                        )
                        if not img_ok:
                            logger.warning(f"Meal monitoring: image send retry for {digits}")
                            await asyncio.sleep(3)
                            img_ok = await send_cloud_media(
                                digits, "image", media_id=media_id, caption=caption,
                            )
                        if not img_ok:
                            logger.error(f"Meal monitoring: image send FAILED for {digits} after retry")

                    if tmpl_ok:
                        grade_sent += 1
                    else:
                        grade_failed += 1
                except Exception as e:
                    logger.error(f"Meal monitoring: send failed to {digits}: {e}")
                    grade_failed += 1

            # Rate limit: small delay every 20 messages
            if (grade_sent + grade_failed) % 20 == 0 and (grade_sent + grade_failed) > 0:
                await asyncio.sleep(0.5)

        total_sent += grade_sent
        total_failed += grade_failed

        await _log_meal_snapshot(
            grade, camera_key, break_type, capture_ts,
            grade_sent, grade_failed,
            "ok" if grade_failed == 0 else "partial",
        )

        logger.info(
            f"Meal monitoring: {grade} — {grade_sent} sent, {grade_failed} failed "
            f"(of {len(sent_phones)} unique phones)"
        )

        results.append({
            "grade": grade,
            "camera": camera_key,
            "sent": grade_sent,
            "failed": grade_failed,
            "unique_phones": len(sent_phones),
        })

        # Small delay between grades to avoid overloading the agent
        await asyncio.sleep(1)

    summary = {
        "status": "ok",
        "break_type": break_type,
        "break_label": break_label,
        "date": date_str,
        "time": time_str,
        "grades_processed": len(results),
        "grades_skipped": total_skipped,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "details": results,
    }

    logger.info(
        f"=== MEAL MONITORING COMPLETE: {break_label} — "
        f"{total_sent} sent, {total_failed} failed, "
        f"{total_skipped} grades skipped ==="
    )

    return summary


# ---------------------------------------------------------------------------
# Sync wrapper for APScheduler
# ---------------------------------------------------------------------------

def run_meal_monitoring_sync(break_type: str = "lunch") -> None:
    """Synchronous wrapper called by APScheduler."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run_meal_monitoring(break_type))
    except Exception as e:
        logger.error(f"Meal monitoring sync error: {e}", exc_info=True)
    finally:
        loop.close()
