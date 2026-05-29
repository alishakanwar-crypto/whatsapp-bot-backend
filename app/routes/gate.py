"""
Gate Head Count Reconciliation & Entry Monitoring System
========================================================
AI-powered school headcount reconciliation comparing detections across:
  - Entry Gate DVR Cameras
  - TrueFace 3000 Facial Recognition System
  - Reception Camera
  - Additional Internal Cameras

Classifies: Teachers, Visitors, Unknown Persons
Reconciliation categories:
  ✓ FULLY VERIFIED — detected on both TrueFace + DVR cameras
  ⚠ ENTRY ONLY (DVR Only) — seen on DVR but not marked on TrueFace
  ⚠ TRUEFACE ONLY — recognized by TrueFace but no DVR sighting
  — ABSENT — not detected on any system

Endpoints:
    POST /api/gate/entry             — receive gate entry events
    POST /api/gate/teacher-sighting  — receive DVR teacher face sightings
    POST /api/gate/visitor-sighting  — receive DVR visitor (unknown) sightings
    GET  /api/gate/status            — today's running totals
    GET  /api/gate/reconciliation/{date} — full reconciliation data

Scheduled:
    Hourly 7 AM – 5 PM IST — reconciliation report emailed

All timestamps use IST (Asia/Kolkata, UTC+05:30).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

logger = logging.getLogger("app.gate")

IST = timezone(timedelta(hours=5, minutes=30))
router = APIRouter()

REPORT_RECIPIENTS = os.environ.get(
    "GATE_REPORT_EMAIL",
    "alisha.kanwar@ppischool.in",
)

CHAIRMAN_PHONE = os.environ.get("TRUEFACE_CHAIRMAN_PHONE", "919971166562")
UNKNOWN_ALERT_PHONE = os.environ.get("UNKNOWN_ALERT_PHONE", "918796105084")
UNKNOWN_ALERT_TEMPLATE = "unknown_person_detected"

# Dedup: max one unknown person alert per camera per 60-second window
_unknown_alert_last: dict[str, float] = {}
_UNKNOWN_ALERT_COOLDOWN = 60  # seconds

# Cameras at actual entry/exit points (for accurate visitor counting)
_ENTRY_CAMERA_KEYWORDS = ("ENTRY GATE", "DISPERSAL")

# Deduplication: entries within this window from same/nearby cameras = same person
_DEDUP_WINDOW_SECONDS = 120  # 2 minutes


def _deduplicate_gate_entries(entries: list[dict]) -> list[dict]:
    """Collapse raw gate detections into estimated unique crossings.

    Same-camera re-detections within the time window are dropped.
    Cross-camera detections with matching attire within the window
    are treated as the same person.
    """
    if not entries:
        return []

    sorted_entries = sorted(entries, key=lambda e: e.get("timestamp", ""))
    unique: list[dict] = []

    for entry in sorted_entries:
        ts_str = entry.get("timestamp", "")
        attire = entry.get("attire_color", "unknown")
        direction = entry.get("direction", "IN")

        is_dup = False
        for prev in reversed(unique):
            prev_ts = prev.get("timestamp", "")
            try:
                t1 = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(prev_ts, "%Y-%m-%d %H:%M:%S")
                diff = abs((t1 - t2).total_seconds())
            except (ValueError, TypeError):
                break
            if diff > _DEDUP_WINDOW_SECONDS:
                break
            if prev.get("direction") != direction:
                continue
            # Same camera within window → definitely same person
            if prev.get("camera") == entry.get("camera"):
                is_dup = True
                break
            # Different camera but same attire within window → likely same person
            if attire and attire != "unknown" and prev.get("attire_color", "unknown") == attire:
                is_dup = True
                break

        if not is_dup:
            unique.append(entry)

    return unique


# ============================================================
# Database helpers
# ============================================================

async def _get_db():
    from app.database import get_db
    return await get_db()


async def _store_gate_entries(db, entries: list[dict]) -> int:
    """Store gate entry events in the database. Returns count stored."""
    count = 0
    for entry in entries:
        ts = entry.get("timestamp", "")
        date_part = ts.split(" ")[0] if " " in ts else datetime.now(IST).strftime("%Y-%m-%d")
        camera = entry.get("camera", "")
        direction = entry.get("direction", "IN")
        attire_color = entry.get("attire_color", "unknown")
        person_crop = entry.get("person_crop", "")

        await db.execute(
            "INSERT INTO gate_entries (date, timestamp, camera, direction, attire_color, person_crop) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date_part, ts, camera, direction, attire_color, person_crop),
        )
        count += 1
    await db.commit()
    return count


async def _get_gate_entries(db, date: str, direction: str | None = None) -> list[dict]:
    """Get all gate entries for a date, optionally filtered by direction."""
    if direction:
        cur = await db.execute(
            "SELECT id, date, timestamp, camera, direction, attire_color, reconciled, matched_pin, notes "
            "FROM gate_entries WHERE date = ? AND direction = ? ORDER BY timestamp",
            (date, direction),
        )
    else:
        cur = await db.execute(
            "SELECT id, date, timestamp, camera, direction, attire_color, reconciled, matched_pin, notes "
            "FROM gate_entries WHERE date = ? ORDER BY timestamp",
            (date,),
        )
    rows = await cur.fetchall()
    return [
        {
            "id": r[0], "date": r[1], "timestamp": r[2], "camera": r[3],
            "direction": r[4], "attire_color": r[5], "reconciled": bool(r[6]),
            "matched_pin": r[7], "notes": r[8],
        }
        for r in rows
    ]


async def _get_gate_entries_with_crops(db, date: str) -> list[dict]:
    """Get gate entries including person_crop images for snapshot gallery."""
    cur = await db.execute(
        "SELECT id, timestamp, camera, direction, person_crop "
        "FROM gate_entries WHERE date = ? AND direction = 'IN' "
        "AND person_crop != '' ORDER BY timestamp DESC LIMIT 16",
        (date,),
    )
    return [
        {"id": r[0], "timestamp": r[1], "camera": r[2],
         "direction": r[3], "person_crop": r[4]}
        for r in await cur.fetchall()
    ]


async def _get_trueface_attendance(db, date: str) -> list[dict]:
    """Get all TrueFace attendance records for a date."""
    cur = await db.execute(
        "SELECT pin, name, arrival_time, departure_time "
        "FROM trueface_attendance WHERE date = ? ORDER BY arrival_time",
        (date,),
    )
    return [
        {"pin": r[0], "name": r[1], "arrival_time": r[2], "departure_time": r[3]}
        for r in await cur.fetchall()
    ]


async def _get_total_teachers(db) -> int:
    """Get total registered teachers count (all teachers, not just those with phones)."""
    cur = await db.execute("SELECT COUNT(*) FROM trueface_teachers")
    row = await cur.fetchone()
    return row[0] if row else 0


async def _get_all_teachers(db) -> list[dict]:
    """Get all registered teachers (including those without phone numbers)."""
    cur = await db.execute(
        "SELECT pin, name, phone FROM trueface_teachers ORDER BY name"
    )
    return [{"pin": r[0], "name": r[1], "phone": r[2] or ""} for r in await cur.fetchall()]


async def _get_contact_categories(db) -> dict[str, str]:
    """Get category for each contact by PIN from trueface_contacts.
    Returns dict mapping UPPERCASED name -> category."""
    cur = await db.execute(
        "SELECT name, category FROM trueface_contacts ORDER BY name"
    )
    result: dict[str, str] = {}
    for r in await cur.fetchall():
        name = (r[0] or "").upper().strip()
        category = (r[1] or "staff").lower().strip()
        if name:
            result[name] = category
    return result


async def _store_teacher_sightings(db, sightings: list[dict]) -> int:
    """Store DVR teacher sightings in the database."""
    count = 0
    for s in sightings:
        ts = s.get("timestamp", "")
        date_part = s.get("date", "")
        if not date_part and " " in ts:
            date_part = ts.split(" ")[0]
        if not date_part:
            date_part = datetime.now(IST).strftime("%Y-%m-%d")

        outfit_color = s.get("outfit_color", "")
        outfit_desc = s.get("outfit_description", "")
        outfit_colors = json.dumps(s.get("outfit_colors", []))

        await db.execute(
            "INSERT INTO teacher_dvr_sightings "
            "(date, timestamp, person_id, name, camera, confidence, "
            "outfit_color, outfit_description, outfit_colors_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_part, ts, s.get("person_id", ""), s.get("name", ""),
             s.get("camera", ""), s.get("confidence", 0.0),
             outfit_color, outfit_desc, outfit_colors),
        )
        count += 1
    await db.commit()
    return count


async def _get_teacher_sightings(db, date: str) -> list[dict]:
    """Get all DVR teacher sightings for a date."""
    cur = await db.execute(
        "SELECT person_id, name, camera, timestamp, confidence, "
        "outfit_color, outfit_description, outfit_colors_json "
        "FROM teacher_dvr_sightings WHERE date = ? ORDER BY name, timestamp",
        (date,),
    )
    results = []
    for r in await cur.fetchall():
        try:
            outfit_colors = json.loads(r[7]) if r[7] else []
        except (json.JSONDecodeError, TypeError):
            outfit_colors = []
        results.append({
            "person_id": r[0], "name": r[1], "camera": r[2],
            "timestamp": r[3], "confidence": r[4],
            "outfit_color": r[5] or "",
            "outfit_description": r[6] or "",
            "outfit_colors": outfit_colors,
        })
    return results


def _classify_visitor(camera: str, timestamp: str) -> str:
    """Classify an unknown visitor based on camera location and time.

    Categories:
      - Parent: Entry Gate during arrival (6:30-9:00) or dismissal (13:00-15:00)
      - Vendor: Entry Gate outside school hours or Basement cameras
      - Visitor: Default for Reception or other cameras
    """
    cam_upper = camera.upper()
    hour = -1
    try:
        time_part = timestamp.split(" ")[1] if " " in timestamp else timestamp
        hour = int(time_part.split(":")[0])
    except (IndexError, ValueError):
        pass

    is_entry = any(k in cam_upper for k in ("ENTRY", "DISPERSAL"))
    is_basement = "BASEMENT" in cam_upper

    if is_basement:
        return "Vendor"
    if is_entry:
        if 6 <= hour <= 8 or 13 <= hour <= 15:
            return "Parent"
        if hour < 6 or hour >= 17:
            return "Vendor"
    return "Visitor"


async def _store_visitor_sightings(db, visitors: list[dict]) -> int:
    """Store DVR visitor (unknown face) sightings in the database."""
    count = 0
    for v in visitors:
        ts = v.get("timestamp", "")
        date_part = v.get("date", "")
        if not date_part and " " in ts:
            date_part = ts.split(" ")[0]
        if not date_part:
            date_part = datetime.now(IST).strftime("%Y-%m-%d")

        cam = v.get("camera", "")
        classification = _classify_visitor(cam, ts)
        snapshot = v.get("snapshot", "")

        await db.execute(
            "INSERT INTO visitor_dvr_sightings "
            "(date, timestamp, camera, classification, snapshot) "
            "VALUES (?, ?, ?, ?, ?)",
            (date_part, ts, cam, classification, snapshot),
        )
        count += 1
    await db.commit()
    return count


async def _get_visitor_sightings(db, date: str) -> list[dict]:
    """Get all DVR visitor sightings for a date."""
    cur = await db.execute(
        "SELECT id, timestamp, camera, classification, snapshot "
        "FROM visitor_dvr_sightings "
        "WHERE date = ? ORDER BY timestamp",
        (date,),
    )
    return [
        {
            "id": r[0], "timestamp": r[1], "camera": r[2],
            "classification": r[3] or "Visitor",
            "snapshot": r[4] or "",
        }
        for r in await cur.fetchall()
    ]


async def _store_vehicle_entries(db, entries: list[dict]) -> int:
    """Store vehicle entry events in the database. Returns count stored."""
    count = 0
    for entry in entries:
        ts = entry.get("timestamp", "")
        date_part = ts.split(" ")[0] if " " in ts else datetime.now(IST).strftime("%Y-%m-%d")
        camera = entry.get("camera", "")
        direction = entry.get("direction", "IN")
        vehicle_type = entry.get("vehicle_type", "car")

        snapshot = entry.get("snapshot", "")

        await db.execute(
            "INSERT INTO vehicle_entries (date, timestamp, camera, direction, vehicle_type, snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date_part, ts, camera, direction, vehicle_type, snapshot),
        )
        count += 1
    await db.commit()
    return count


async def _get_vehicle_entries(db, date: str) -> list[dict]:
    """Get all vehicle entries for a date."""
    cur = await db.execute(
        "SELECT id, timestamp, camera, direction, vehicle_type, snapshot "
        "FROM vehicle_entries WHERE date = ? ORDER BY timestamp",
        (date,),
    )
    return [
        {"id": r[0], "timestamp": r[1], "camera": r[2],
         "direction": r[3], "vehicle_type": r[4], "snapshot": r[5] if len(r) > 5 else ""}
        for r in await cur.fetchall()
    ]


# ============================================================
# Reconciliation Logic
# ============================================================

async def _reconcile(db, date: str) -> dict:
    """Perform head count reconciliation for a given date.

    Architecture:
      CATEGORY 1 — VERIFIED STAFF (registered in TrueFace/face DB)
        Teachers, Admin, Security, Support — each tracked individually.
        Source of truth: TrueFace attendance + DVR face sightings.

      CATEGORY 2 — UNKNOWN / UNREGISTERED PEOPLE
        Parents, visitors, vendors, delivery agents, etc.
        Tagged as UNKNOWN-001, UNKNOWN-002, etc.
        Tracked with snapshots, timestamps, camera source.

      OCCUPANCY — unique people currently INSIDE (not raw detections).
        Staff with arrival but no departure = INSIDE.
        Unknown entries without matching exits = INSIDE.
    """
    from collections import Counter

    gate_in_all = await _get_gate_entries(db, date, direction="IN")
    gate_out_all = await _get_gate_entries(db, date, direction="OUT")
    trueface = await _get_trueface_attendance(db, date)
    all_teachers = await _get_all_teachers(db)
    total_registered = len(all_teachers)
    dvr_sightings = await _get_teacher_sightings(db, date)
    visitor_sightings = await _get_visitor_sightings(db, date)
    vehicle_entries = await _get_vehicle_entries(db, date)
    contact_categories = await _get_contact_categories(db)
    gate_entries_with_crops = await _get_gate_entries_with_crops(db, date)

    # Only count entry/exit cameras for gate totals (not internal cameras)
    gate_in_entry = [e for e in gate_in_all if any(k in e["camera"].upper() for k in _ENTRY_CAMERA_KEYWORDS)]
    gate_out_entry = [e for e in gate_out_all if any(k in e["camera"].upper() for k in _ENTRY_CAMERA_KEYWORDS)]
    raw_gate_in = len(gate_in_entry)
    raw_gate_out = len(gate_out_entry)

    # Deduplicate: collapse same-person detections across cameras/scans
    gate_in = _deduplicate_gate_entries(gate_in_entry)
    gate_out = _deduplicate_gate_entries(gate_out_entry)
    unique_gate_in = len(gate_in)
    unique_gate_out = len(gate_out)

    # ── CATEGORY 1: VERIFIED STAFF ──
    # Name aliases: DVR name → TrueFace canonical name (for people registered
    # under different names on different systems)
    _NAME_ALIASES: dict[str, str] = {
        "ALISHA AHUJA": "ALISHA KANWAR",
    }

    def _canonical(name: str) -> str:
        """Return canonical (TrueFace) name, resolving aliases."""
        upper = name.upper().strip()
        return _NAME_ALIASES.get(upper, upper)

    # Build TrueFace lookup by name
    tf_by_name: dict[str, dict] = {}
    for t in trueface:
        tf_by_name[_canonical(t["name"])] = t

    # Build DVR sightings grouped by person
    dvr_by_person: dict[str, list[dict]] = {}
    dvr_names: dict[str, str] = {}
    dvr_outfits: dict[str, list[dict]] = {}
    for s in dvr_sightings:
        key = _canonical(s["name"])
        dvr_by_person.setdefault(key, []).append(s)
        dvr_names[key] = s["name"]
        if s.get("outfit_color") and s["outfit_color"] != "unknown":
            dvr_outfits.setdefault(key, []).append({
                "color": s["outfit_color"],
                "description": s.get("outfit_description", ""),
                "colors": s.get("outfit_colors", []),
                "camera": s.get("camera", ""),
                "timestamp": s.get("timestamp", ""),
            })

    # All unique registered staff names (using canonical names)
    all_names: set[str] = set()
    for t in all_teachers:
        all_names.add(_canonical(t["name"]))
    for key in tf_by_name:
        all_names.add(key)
    for key in dvr_by_person:
        all_names.add(key)

    # Per-person detail with proper category separation
    staff_detail = []
    for name_upper in sorted(all_names):
        tf = tf_by_name.get(name_upper)
        dvr_list = dvr_by_person.get(name_upper, [])

        dvr_cameras = []
        dvr_times = []
        dvr_sighting_details = []
        for s in dvr_list:
            cam = s["camera"]
            ts = s["timestamp"]
            time_part = ts.split(" ")[1] if " " in ts else ts
            if cam not in dvr_cameras:
                dvr_cameras.append(cam)
            dvr_times.append(time_part)
            dvr_sighting_details.append({
                "camera": cam, "time": time_part,
                "confidence": s.get("confidence", 0),
                "outfit_color": s.get("outfit_color", ""),
                "outfit_description": s.get("outfit_description", ""),
            })

        display_name = dvr_names.get(name_upper, tf["name"] if tf else name_upper.title())

        outfit_observations = dvr_outfits.get(name_upper, [])
        outfit_summary = outfit_observations[-1].get("description", "unknown") if outfit_observations else "-"

        raw_category = contact_categories.get(name_upper, "staff")
        category = _normalize_category(raw_category, name=display_name)

        # Determine presence status
        is_present = tf is not None or len(dvr_list) > 0
        has_departed = tf is not None and tf.get("departure_time") is not None
        if is_present and not has_departed:
            occupancy_status = "INSIDE"
        elif is_present and has_departed:
            occupancy_status = "EXITED"
        else:
            occupancy_status = "ABSENT"

        # Time trail
        time_trail: list[dict] = []
        if tf and tf.get("arrival_time"):
            time_trail.append({"time": tf["arrival_time"], "source": "TrueFace 3000", "event": "Arrival"})
        for det in dvr_sighting_details:
            time_trail.append({"time": det["time"], "source": det["camera"], "event": "DVR Sighting"})
        if tf and tf.get("departure_time"):
            time_trail.append({"time": tf["departure_time"], "source": "TrueFace 3000", "event": "Departure"})
        time_trail.sort(key=lambda x: x["time"])

        staff_detail.append({
            "name": display_name,
            "category": category,
            "raw_category": raw_category,
            "trueface_present": tf is not None,
            "trueface_arrival": tf["arrival_time"] if tf else None,
            "trueface_departure": tf["departure_time"] if tf else None,
            "trueface_pin": tf["pin"] if tf else None,
            "dvr_seen": len(dvr_list) > 0,
            "dvr_sighting_count": len(dvr_list),
            "dvr_cameras": dvr_cameras,
            "dvr_times": dvr_times,
            "dvr_first_seen": dvr_times[0] if dvr_times else None,
            "dvr_last_seen": dvr_times[-1] if dvr_times else None,
            "dvr_sighting_details": dvr_sighting_details,
            "outfit_summary": outfit_summary,
            "outfit_observations": outfit_observations,
            "time_trail": time_trail,
            "occupancy_status": occupancy_status,
            "status": _reconciliation_status(tf is not None, len(dvr_list) > 0),
        })

    # Staff presence counts
    staff_present = [s for s in staff_detail if s["occupancy_status"] in ("INSIDE", "EXITED")]
    staff_inside = [s for s in staff_detail if s["occupancy_status"] == "INSIDE"]
    staff_exited = [s for s in staff_detail if s["occupancy_status"] == "EXITED"]
    staff_absent = [s for s in staff_detail if s["occupancy_status"] == "ABSENT"]

    # Category breakdown for staff INSIDE
    staff_category_inside: Counter = Counter()
    for s in staff_inside:
        staff_category_inside[s["category"]] += 1

    # Category breakdown for all present staff
    staff_category_present: Counter = Counter()
    for s in staff_present:
        staff_category_present[s["category"]] += 1

    # Side-by-side (kept for compatibility)
    side_by_side = {
        "both_present": [t for t in staff_detail if t["trueface_present"] and t["dvr_seen"]],
        "trueface_only": [t for t in staff_detail if t["trueface_present"] and not t["dvr_seen"]],
        "dvr_only": [t for t in staff_detail if not t["trueface_present"] and t["dvr_seen"]],
        "neither": [t for t in staff_detail if not t["trueface_present"] and not t["dvr_seen"]],
    }

    # ── CATEGORY 2: UNKNOWN / UNREGISTERED PEOPLE ──
    # Build unknown person list from visitor sightings + unmatched gate entries
    unknown_persons: list[dict] = []
    for idx, v in enumerate(visitor_sightings, 1):
        unknown_id = f"UNKNOWN-{idx:03d}"
        ts = v.get("timestamp", "")
        time_part = ts.split(" ")[1] if " " in ts else ts
        classification = v.get("classification", "Visitor")
        unknown_persons.append({
            "id": unknown_id,
            "timestamp": ts,
            "time": time_part,
            "camera": v.get("camera", "Unknown"),
            "direction": "IN",
            "status": "INSIDE",
            "classification": classification,
            "person_crop": v.get("snapshot", ""),
        })

    # Add crops from gate entries that are unmatched
    for g in gate_entries_with_crops:
        if g.get("person_crop"):
            idx = len(unknown_persons) + 1
            ts = g.get("timestamp", "")
            time_part = ts.split(" ")[1] if " " in ts else ts
            unknown_persons.append({
                "id": f"UNKNOWN-{idx:03d}",
                "timestamp": ts,
                "time": time_part,
                "camera": g.get("camera", "Unknown"),
                "direction": g.get("direction", "IN"),
                "status": "INSIDE" if g.get("direction") == "IN" else "EXITED",
                "person_crop": g.get("person_crop", ""),
            })

    # Count unknown entries/exits from the actual unknown person list
    unknown_in_list = [u for u in unknown_persons if u["direction"] == "IN"]
    unknown_out_list = [u for u in unknown_persons if u["direction"] == "OUT"]
    estimated_unknown_in = len(unknown_in_list)
    estimated_unknown_out = len(unknown_out_list)
    estimated_unknown_inside = max(0, estimated_unknown_in - estimated_unknown_out)
    # Also keep the gate-based estimate as a fallback reference
    unique_staff_detected = len(staff_present)
    gate_based_unknown = max(0, unique_gate_in - unique_staff_detected)

    # Classification counts
    classification_counts: dict[str, int] = {}
    for u in unknown_persons:
        cls = u.get("classification", "Visitor")
        classification_counts[cls] = classification_counts.get(cls, 0) + 1

    # Visitor/unknown grouped by camera
    visitor_by_camera: dict[str, list[dict]] = {}
    for v in visitor_sightings:
        cam = v.get("camera", "Unknown")
        visitor_by_camera.setdefault(cam, []).append(v)

    # ── VEHICLES ──
    vehicles_in = [v for v in vehicle_entries if v["direction"] == "IN"]
    vehicles_out = [v for v in vehicle_entries if v["direction"] == "OUT"]
    vehicle_type_counts = dict(Counter(v["vehicle_type"] for v in vehicles_in))

    # ── OCCUPANCY SUMMARY ──
    total_staff_inside = len(staff_inside)
    total_unknown_inside = estimated_unknown_inside
    total_inside = total_staff_inside + total_unknown_inside
    total_staff_exited = len(staff_exited)
    total_unknown_exited = estimated_unknown_out

    # ── ALERTS ──
    alerts: list[dict] = []
    for t in side_by_side["trueface_only"]:
        alerts.append({
            "type": "ATTENDANCE_WITHOUT_ENTRY",
            "severity": "medium",
            "person": t["name"],
            "category": t.get("category", "staff"),
            "detail": f"TrueFace at {t.get('trueface_arrival', '?')} but not seen on any DVR camera.",
        })
    for t in side_by_side["dvr_only"]:
        alerts.append({
            "type": "ENTRY_WITHOUT_ATTENDANCE",
            "severity": "high",
            "person": t["name"],
            "category": t.get("category", "staff"),
            "detail": f"Seen on DVR ({', '.join(t.get('dvr_cameras', []))}) at {t.get('dvr_first_seen', '?')} but NOT marked on TrueFace.",
        })
    if total_unknown_inside > 0:
        alerts.append({
            "type": "UNKNOWN_PERSONS_INSIDE",
            "severity": "medium",
            "person": "",
            "category": "unknown",
            "detail": f"{total_unknown_inside} unregistered/unknown person(s) estimated inside campus.",
        })
    # Flag student detections during no-school period (likely false positives)
    student_detections = [s for s in staff_detail
                         if s["category"] == "Students" and s["occupancy_status"] != "ABSENT"]
    if student_detections:
        names = ", ".join(s["name"] for s in student_detections)
        alerts.append({
            "type": "STUDENT_DETECTED",
            "severity": "low",
            "person": names,
            "category": "Students",
            "detail": (f"{len(student_detections)} student(s) detected on DVR: {names}. "
                       "May be false positive if school is not in session."),
        })

    # ── GATE vs FACE DISCREPANCY ──
    # Compare YOLO head count with identified faces to find unreconciled entries
    total_identified = unique_staff_detected + estimated_unknown_in
    unreconciled_count = max(0, unique_gate_in - total_identified)

    # Group gate entries by hourly time window for discrepancy breakdown
    hourly_gate: dict[str, int] = {}
    hourly_identified: dict[str, int] = {}
    for g in gate_in:
        ts = g.get("timestamp", "")
        hour_key = ts[11:13] + ":00" if len(ts) > 13 else "unknown"
        hourly_gate[hour_key] = hourly_gate.get(hour_key, 0) + 1
    # Staff sightings by hour
    for s in staff_detail:
        if s.get("dvr_first_seen"):
            hour_key = s["dvr_first_seen"][:2] + ":00" if len(s["dvr_first_seen"]) >= 2 else "unknown"
            hourly_identified[hour_key] = hourly_identified.get(hour_key, 0) + 1
    # Visitor sightings by hour
    for v in visitor_sightings:
        ts = v.get("timestamp", "")
        hour_key = ts[11:13] + ":00" if len(ts) > 13 else "unknown"
        hourly_identified[hour_key] = hourly_identified.get(hour_key, 0) + 1

    all_hours = sorted(set(list(hourly_gate.keys()) + list(hourly_identified.keys())))
    hourly_discrepancy = []
    for h in all_hours:
        if h == "unknown":
            continue
        gate_count = hourly_gate.get(h, 0)
        id_count = hourly_identified.get(h, 0)
        gap = max(0, gate_count - id_count)
        hourly_discrepancy.append({
            "hour": h,
            "gate_entries": gate_count,
            "identified": id_count,
            "unreconciled": gap,
        })

    # Unmatched gate entries: IN entries that don't correspond to any identified person
    # Include attire color and camera info for visual tracking
    unmatched_entries = []
    for g in gate_in:
        if not g.get("reconciled") and not g.get("matched_pin"):
            ts = g.get("timestamp", "")
            time_part = ts.split(" ")[1] if " " in ts else ts
            unmatched_entries.append({
                "timestamp": ts,
                "time": time_part,
                "camera": g.get("camera", "Unknown"),
                "attire_color": g.get("attire_color", ""),
            })

    discrepancy = {
        "gate_head_count": unique_gate_in,
        "faces_identified": total_identified,
        "staff_identified": unique_staff_detected,
        "visitors_identified": estimated_unknown_in,
        "unreconciled": unreconciled_count,
        "reconciliation_rate": round(
            (total_identified / unique_gate_in * 100) if unique_gate_in > 0 else 100, 1
        ),
        "hourly_breakdown": hourly_discrepancy,
        "unmatched_entries": unmatched_entries[:50],
    }

    if unreconciled_count > 0:
        alerts.append({
            "type": "GATE_FACE_DISCREPANCY",
            "severity": "high" if unreconciled_count > 5 else "medium",
            "person": "",
            "category": "unknown",
            "detail": (
                f"Gate counted {unique_gate_in} people IN, but only "
                f"{total_identified} identified ({unique_staff_detected} staff + "
                f"{estimated_unknown_in} visitors). "
                f"{unreconciled_count} unreconciled entries."
            ),
        })

    # Update daily summary
    await db.execute(
        "INSERT OR REPLACE INTO gate_daily_summary "
        "(date, total_in, total_out, trueface_matched, unreconciled) "
        "VALUES (?, ?, ?, ?, ?)",
        (date, raw_gate_in, raw_gate_out, len(trueface), unreconciled_count),
    )
    await db.commit()

    return {
        "date": date,
        # Raw gate detections (for reference — NOT unique people)
        "raw_gate_in": raw_gate_in,
        "raw_gate_out": raw_gate_out,
        # Deduplicated counts (estimated unique crossings)
        "unique_gate_in": unique_gate_in,
        "unique_gate_out": unique_gate_out,
        # Legacy keys for backward compat
        "total_gate_in": unique_gate_in,
        "total_gate_out": unique_gate_out,
        # Verified staff
        "total_registered": total_registered,
        "trueface_identified": len(trueface),
        "dvr_sighted": len([s for s in staff_detail if s["dvr_seen"]]),
        "staff_detail": staff_detail,
        "teacher_detail": staff_detail,  # backward compat
        "side_by_side": side_by_side,
        # Staff occupancy
        "staff_inside": total_staff_inside,
        "staff_exited": total_staff_exited,
        "staff_absent": len(staff_absent),
        "staff_category_inside": dict(staff_category_inside),
        "staff_category_present": dict(staff_category_present),
        "category_counts": dict(staff_category_present),  # backward compat
        # Unknown / unregistered
        "unknown_persons": unknown_persons,
        "estimated_unknown_in": estimated_unknown_in,
        "estimated_unknown_out": estimated_unknown_out,
        "estimated_unknown_inside": estimated_unknown_inside,
        "visitor_count": estimated_unknown_in,  # backward compat
        "visitor_sightings": visitor_sightings,
        "visitor_by_camera": visitor_by_camera,
        "classification_counts": classification_counts,
        # Occupancy totals
        "total_inside": total_inside,
        "total_exited": total_staff_exited + total_unknown_exited,
        # Vehicles
        "vehicles_in": len(vehicles_in),
        "vehicles_out": len(vehicles_out),
        "vehicle_types": vehicle_type_counts,
        "vehicle_entries": vehicle_entries,
        # Snapshots
        "gate_entries_with_crops": gate_entries_with_crops,
        # Alerts
        "alerts": alerts,
        # Gate vs Face discrepancy
        "discrepancy": discrepancy,
        # Legacy
        "total_registered_teachers": total_registered,
        "unreconciled_count": unreconciled_count,
        "timing_trail": [],
    }


# Grade/section pattern: digits followed by optional section letter
# Matches names like "garv jain 9 - B", "student 10-A", "name 1 - C"
_STUDENT_GRADE_RE = re.compile(
    r"\b(?:nursery|prep|[kK][gG]|[1-9]|1[0-2])\s*[-–]\s*[A-Za-z]\b"
)


def _is_student_name(name: str) -> bool:
    """Detect if a name contains a grade/section pattern (e.g. '9 - B')."""
    return bool(_STUDENT_GRADE_RE.search(name))


def _normalize_category(raw: str, name: str = "") -> str:
    """Normalize raw TrueFace category into display group.

    Also detects students by name pattern (grade-section in name).
    """
    # Student detection by name pattern (overrides all other categories)
    if name and _is_student_name(name):
        return "Students"

    raw = (raw or "staff").lower().strip()
    if raw == "student":
        return "Students"
    teacher_keywords = ("teacher", "eng-teacher", "comp. teacher", "art teacher",
                        "music teacher", "sports teacher", "dance teacher",
                        "theatre teacher", "trainee teacher", "lib. teacher",
                        "taekwondo tea.", "principal")
    admin_keywords = ("admin", "admin it", "account", "est. manager",
                      "lab. incharge", "max nurse")
    if any(kw == raw for kw in teacher_keywords):
        return "Teachers"
    if any(kw == raw for kw in admin_keywords):
        return "Admin / Office Staff"
    if raw in ("advocate",):
        return "Advocates / Legal"
    if raw in ("staff",):
        return "Staff"
    if raw in ("web designers",):
        return "IT / Design"
    return raw.title()


def _reconciliation_status(trueface_present: bool, dvr_seen: bool) -> str:
    """Return a human-readable reconciliation status."""
    if trueface_present and dvr_seen:
        return "✓ FULLY VERIFIED"
    if trueface_present and not dvr_seen:
        return "⚠ TRUEFACE ONLY"
    if not trueface_present and dvr_seen:
        return "⚠ ENTRY ONLY (DVR)"
    return "— ABSENT"


# ============================================================
# Unknown Person WhatsApp Alert
# ============================================================

def _generate_placeholder_image() -> str:
    """Generate a small placeholder PNG as base64 for alerts without a snapshot."""
    import struct
    import zlib

    width, height = 200, 200
    raw = b""
    for y in range(height):
        raw += b"\x00"  # filter byte
        for x in range(width):
            raw += b"\xcc\xcc\xcc"  # light gray
    compressed = zlib.compress(raw)

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")
    return base64.b64encode(png).decode()


async def _send_unknown_person_alert(entry: dict):
    """Send WhatsApp template alert with snapshot for an unrecognized person.

    Uses the 'unknown_person_detected' template (IMAGE header + body params)
    so alerts work outside the 24-hour conversation window.
    Template requires an IMAGE header — uses placeholder if no crop available.
    Deduplicates by camera to avoid spam (one alert per camera per 60s).
    """
    import time
    camera = entry.get("camera", "Unknown")
    now = time.time()

    cache_key = camera
    last_sent = _unknown_alert_last.get(cache_key, 0)
    if now - last_sent < _UNKNOWN_ALERT_COOLDOWN:
        return

    _unknown_alert_last[cache_key] = now

    ts = entry.get("timestamp", "")
    direction = entry.get("direction", "IN")
    person_crop = entry.get("person_crop", "")

    try:
        from app.services.whatsapp_service import (
            upload_base64_image_cloud,
            send_cloud_template_message,
        )

        image_data = person_crop if person_crop else _generate_placeholder_image()
        header_image_id = await upload_base64_image_cloud(image_data)
        if not header_image_id:
            logger.warning("[GATE] Failed to upload image for alert (camera=%s)", camera)
            return

        sent = await send_cloud_template_message(
            to=UNKNOWN_ALERT_PHONE,
            template_name=UNKNOWN_ALERT_TEMPLATE,
            language_code="en",
            body_params=[camera, ts, direction],
            header_image_id=header_image_id,
        )

        logger.info("[GATE] Unknown person alert → %s: %s (camera=%s, image=%s)",
                    UNKNOWN_ALERT_PHONE, "OK" if sent else "FAILED", camera,
                    "yes" if person_crop else "placeholder")
    except Exception as e:
        logger.error("[GATE] Unknown person alert failed: %s (camera=%s)", e, camera)


# ============================================================
# Endpoints
# ============================================================

@router.post("/api/gate/entry")
async def receive_gate_entries(request: Request):
    """Receive gate entry events from the campus agent gate counter.

    Body: [{"timestamp": "...", "camera": "...", "direction": "IN", "attire_color": "blue", "person_crop": "..."}]
    """
    body = await request.json()
    entries = body if isinstance(body, list) else [body]

    db = await _get_db()
    try:
        count = await _store_gate_entries(db, entries)
    finally:
        await db.close()

    # Note: WhatsApp alerts for unknown persons are triggered from
    # /api/gate/visitor-sighting (face-recognition-verified unknowns),
    # not from gate entries (which include all persons, known and unknown).

    logger.info("[GATE] Stored %d gate entry event(s)", count)
    return {"status": "ok", "stored": count}


@router.post("/api/gate/teacher-sighting")
async def receive_teacher_sightings(request: Request):
    """Receive DVR teacher face sightings from the campus agent.

    Body: [{"person_id": "TEACHER_X", "name": "...", "camera": "...",
            "timestamp": "...", "date": "...", "confidence": 0.85}]
    """
    body = await request.json()
    sightings = body if isinstance(body, list) else [body]

    db = await _get_db()
    try:
        count = await _store_teacher_sightings(db, sightings)
    finally:
        await db.close()

    logger.info("[GATE] Stored %d DVR teacher sighting(s)", count)
    return {"status": "ok", "stored": count}


@router.post("/api/gate/visitor-sighting")
async def receive_visitor_sightings(request: Request):
    """Receive DVR visitor (unknown face) sightings from the campus agent.

    Body: [{"camera": "...", "timestamp": "...", "date": "...", "snapshot": "base64..."}]
    These are faces that did NOT match any registered person in the face DB.
    """
    body = await request.json()
    visitors = body if isinstance(body, list) else [body]

    db = await _get_db()
    try:
        count = await _store_visitor_sightings(db, visitors)
    finally:
        await db.close()

    # Send WhatsApp alert for each unknown visitor at entry cameras
    alert_count = 0
    for v in visitors:
        cam = v.get("camera", "")
        cam_upper = cam.upper()
        is_entry_cam = any(k in cam_upper for k in _ENTRY_CAMERA_KEYWORDS)
        if is_entry_cam:
            alert_entry = {
                "camera": cam,
                "timestamp": v.get("timestamp", ""),
                "direction": "IN",
                "person_crop": v.get("snapshot", ""),
            }
            alert_count += 1
            asyncio.create_task(_send_unknown_person_alert(alert_entry))
    if alert_count:
        logger.info("[GATE] Queued %d unknown visitor alert(s)", alert_count)

    # Log classification breakdown
    cls_log = {}
    for v in visitors:
        cam = v.get("camera", "")
        ts = v.get("timestamp", "")
        cls = _classify_visitor(cam, ts)
        cls_log[cls] = cls_log.get(cls, 0) + 1
    cls_str = ", ".join(f"{k}={v}" for k, v in sorted(cls_log.items()))
    logger.info("[GATE] Stored %d DVR visitor sighting(s) [%s]", count, cls_str)
    return {"status": "ok", "stored": count}


@router.post("/api/gate/vehicle-entry")
async def receive_vehicle_entries(request: Request):
    """Receive vehicle entry events from the campus agent gate counter.

    Body: [{"timestamp": "...", "camera": "...", "direction": "IN", "vehicle_type": "car"}]
    """
    body = await request.json()
    entries = body if isinstance(body, list) else [body]

    db = await _get_db()
    try:
        count = await _store_vehicle_entries(db, entries)
    finally:
        await db.close()

    logger.info("[GATE] Stored %d vehicle entry event(s)", count)
    return {"status": "ok", "stored": count}


@router.get("/api/gate/status")
async def gate_status():
    """Get today's running head count totals."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    db = await _get_db()
    try:
        gate_in = await _get_gate_entries(db, today, direction="IN")
        gate_out = await _get_gate_entries(db, today, direction="OUT")
        trueface = await _get_trueface_attendance(db, today)
        dvr_sightings = await _get_teacher_sightings(db, today)
        visitor_sightings = await _get_visitor_sightings(db, today)
        vehicle_entries = await _get_vehicle_entries(db, today)
    finally:
        await db.close()

    # Count unique teachers seen on DVR
    dvr_unique = len({s["person_id"] for s in dvr_sightings})

    # Estimate visitors: gate entries minus identified staff
    staff_detected = len(trueface) + len({s["person_id"] for s in dvr_sightings
                                          if s["name"].upper().strip() not in
                                          {t["name"].upper().strip() for t in trueface}})
    estimated_visitors = max(0, len(gate_in) - staff_detected)
    final_visitors = max(len(visitor_sightings), estimated_visitors)

    # Vehicle counts by type
    vehicles_in = [v for v in vehicle_entries if v["direction"] == "IN"]
    vehicles_out = [v for v in vehicle_entries if v["direction"] == "OUT"]
    from collections import Counter
    vehicle_types = dict(Counter(v["vehicle_type"] for v in vehicles_in))

    return {
        "date": today,
        "gate_in": len(gate_in),
        "gate_out": len(gate_out),
        "trueface_identified": len(trueface),
        "dvr_teachers_sighted": dvr_unique,
        "dvr_total_sightings": len(dvr_sightings),
        "visitors_detected": final_visitors,
        "vehicles_in": len(vehicles_in),
        "vehicles_out": len(vehicles_out),
        "vehicle_types": vehicle_types,
    }


@router.get("/api/gate/reconciliation/{date}")
async def get_reconciliation(date: str):
    """Get full reconciliation data for a specific date."""
    db = await _get_db()
    try:
        result = await _reconcile(db, date)
    finally:
        await db.close()
    return result


# ============================================================
# Excel Report Generation
# ============================================================

def _generate_reconciliation_excel(recon: dict) -> bytes:
    """Generate the comprehensive School Headcount Reconciliation report.

    Sheets:
      1. Summary — overall counts, category breakdown, reconciliation status
      2. Verified Movements — per-person detail with category, outfit, timestamps
      3. Sighting Timeline — every DVR sighting chronologically with category
      4. Reconciliation — side-by-side comparison by status category
      5. Visitors — visitor head count, per-camera, timeline
      6. Outfit Reconciliation — outfit tracking across sightings
      7. AI Observations — automated intelligence notes
      8. Time Trail — movement reconstruction per person across cameras
      9. Mismatch Alerts — attendance/entry mismatches, unknown persons
    """
    from collections import Counter

    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5496")
    green_fill = PatternFill("solid", fgColor="C6EFCE")
    red_fill = PatternFill("solid", fgColor="FFC7CE")
    yellow_fill = PatternFill("solid", fgColor="FFEB9C")
    gray_fill = PatternFill("solid", fgColor="D9D9D9")
    light_blue_fill = PatternFill("solid", fgColor="D9E2F3")
    orange_fill = PatternFill("solid", fgColor="FFA500")
    dark_red_fill = PatternFill("solid", fgColor="C00000")
    border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    date_str = recon["date"]
    side_by_side = recon.get("side_by_side", {})
    staff_detail = recon.get("staff_detail", recon.get("teacher_detail", []))
    visitor_sightings = recon.get("visitor_sightings", [])
    visitor_by_camera = recon.get("visitor_by_camera", {})
    unknown_persons = recon.get("unknown_persons", [])

    staff_inside = [s for s in staff_detail if s.get("occupancy_status") == "INSIDE"]
    staff_exited = [s for s in staff_detail if s.get("occupancy_status") == "EXITED"]
    staff_absent = [s for s in staff_detail if s.get("occupancy_status") == "ABSENT"]

    fully_verified = side_by_side.get("both_present", [])
    trueface_only = side_by_side.get("trueface_only", [])
    dvr_only = side_by_side.get("dvr_only", [])
    absent = side_by_side.get("neither", [])

    total_inside = recon.get("total_inside", 0)
    estimated_unknown_inside = recon.get("estimated_unknown_inside", 0)

    # ── Sheet 1: Summary (Occupancy-Based) ──
    ws = wb.active
    ws.title = "Live Occupancy"

    ws.merge_cells("A1:D1")
    ws["A1"] = "SCHOOL HEADCOUNT RECONCILIATION"
    ws["A1"].font = Font(bold=True, size=14)

    ws.merge_cells("A2:D2")
    ws["A2"] = f"PP International School - {date_str}"
    ws["A2"].font = Font(bold=True, size=11)

    classification_counts = recon.get("classification_counts", {})

    r = 4
    summary_rows = [
        ("LIVE OCCUPANCY (Unique People Inside)", "", None),
        ("TOTAL INSIDE", total_inside, green_fill),
        ("  Verified Staff Inside", len(staff_inside), light_blue_fill),
        ("  Unknown / Visitors Inside", estimated_unknown_inside, orange_fill),
        ("", "", None),
        ("UNKNOWN PERSON CLASSIFICATION", "", None),
    ]
    for cls_name, cls_count in sorted(classification_counts.items()):
        summary_rows.append((f"  {cls_name}", cls_count, orange_fill))
    summary_rows += [
        ("", "", None),
        ("VERIFIED STAFF INSIDE BY CATEGORY", "", None),
    ]
    for cat_name, cat_count in sorted(recon.get("staff_category_inside", {}).items()):
        summary_rows.append((f"  {cat_name}", cat_count, None))
    summary_rows += [
        ("", "", None),
        ("EXITED", "", None),
        ("  Verified Staff Exited", len(staff_exited), None),
        ("  Unknown Exited", recon.get("estimated_unknown_out", 0), None),
        ("", "", None),
        ("ABSENT (Not Detected)", len(staff_absent), gray_fill),
        ("", "", None),
        ("RAW GATE DETECTIONS (not unique people)", "", None),
        ("  Gate Detections IN", recon.get("raw_gate_in", 0), None),
        ("  Gate Detections OUT", recon.get("raw_gate_out", 0), None),
        ("  (Same person detected multiple times)", "", None),
        ("", "", None),
        ("VEHICLE COUNT", "", None),
        ("  Vehicles IN", recon.get("vehicles_in", 0), light_blue_fill),
        ("  Vehicles OUT", recon.get("vehicles_out", 0), None),
    ]
    for vtype, vcount in sorted(recon.get("vehicle_types", {}).items()):
        summary_rows.append((f"    {vtype.title()}", vcount, None))
    summary_rows += [
        ("", "", None),
        ("GATE vs FACE DISCREPANCY", "", None),
        ("  Gate Head Count (IN)", disc.get("gate_head_count", 0), None),
        ("  Faces Identified", disc.get("faces_identified", 0), None),
        ("  UNRECONCILED", disc.get("unreconciled", 0),
         red_fill if disc.get("unreconciled", 0) > 0 else green_fill),
        ("  Reconciliation Rate", f"{disc.get('reconciliation_rate', 100)}%",
         green_fill if disc.get("reconciliation_rate", 100) >= 80 else red_fill),
        ("", "", None),
        ("STAFF RECONCILIATION", "", None),
        ("  Fully Verified (TrueFace + DVR)", len(fully_verified), green_fill),
        ("  Entry Only (DVR - Not on TrueFace)", len(dvr_only), red_fill),
        ("  TrueFace Only (No DVR Sighting)", len(trueface_only), yellow_fill),
        ("  Not Detected", len(absent), gray_fill),
    ]
    for label, value, fill in summary_rows:
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.cell(row=r, column=1).border = border
        ws.cell(row=r, column=2, value=value).font = Font(bold=True, size=12)
        ws.cell(row=r, column=2).border = border
        if fill:
            ws.cell(row=r, column=1).fill = fill
            ws.cell(row=r, column=2).fill = fill
        r += 1

    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 15

    # ── Sheet 2: Staff Detail (with occupancy status) ──
    ws2 = wb.create_sheet("Staff Detail")

    ws2.merge_cells("A1:I1")
    ws2["A1"] = f"STAFF DETAIL - {date_str}"
    ws2["A1"].font = Font(bold=True, size=14)

    headers2 = [
        "#", "Name", "Category", "Status", "Occupancy",
        "Arrived", "Departed", "Cameras Seen", "Wearing",
    ]
    for col, h in enumerate(headers2, 1):
        cell = ws2.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    r = 4
    for i, t in enumerate(staff_detail, 1):
        ws2.cell(row=r, column=1, value=i).border = border
        ws2.cell(row=r, column=2, value=t["name"]).border = border
        ws2.cell(row=r, column=3, value=t.get("category", "Staff")).border = border

        status_cell = ws2.cell(row=r, column=4, value=t["status"])
        status_cell.border = border
        if "FULLY VERIFIED" in t["status"]:
            status_cell.fill = green_fill
        elif "TRUEFACE ONLY" in t["status"]:
            status_cell.fill = yellow_fill
        elif "ENTRY ONLY" in t["status"]:
            status_cell.fill = red_fill
        else:
            status_cell.fill = gray_fill

        occ = t.get("occupancy_status", "ABSENT")
        occ_cell = ws2.cell(row=r, column=5, value=occ)
        occ_cell.border = border
        if occ == "INSIDE":
            occ_cell.fill = green_fill
        elif occ == "EXITED":
            occ_cell.fill = light_blue_fill
        else:
            occ_cell.fill = gray_fill

        ws2.cell(row=r, column=6,
                 value=t.get("trueface_arrival") or t.get("dvr_first_seen") or "-").border = border
        ws2.cell(row=r, column=7,
                 value=t.get("trueface_departure") or "-").border = border
        ws2.cell(row=r, column=8,
                 value=", ".join(t.get("dvr_cameras", [])) or "-").border = border

        outfit_cell = ws2.cell(row=r, column=9, value=t.get("outfit_summary", "-"))
        outfit_cell.border = border
        outfit_cell.alignment = Alignment(wrap_text=True)
        r += 1

    for col_letter, w in [("A", 5), ("B", 25), ("C", 18), ("D", 22),
                           ("E", 12), ("F", 14), ("G", 14), ("H", 35), ("I", 30)]:
        ws2.column_dimensions[col_letter].width = w

    # ── Sheet 3: Sighting Timeline ──
    ws3 = wb.create_sheet("Sighting Timeline")

    ws3.merge_cells("A1:H1")
    ws3["A1"] = f"════════ DVR SIGHTING TIMELINE — {date_str} ════════"
    ws3["A1"].font = Font(bold=True, size=14)

    timeline_headers = ["#", "Name", "Category", "Time", "Camera Location",
                        "Confidence", "Outfit Color", "Outfit Detail"]
    for col, h in enumerate(timeline_headers, 1):
        cell = ws3.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border

    # Collect all sightings and sort chronologically
    all_sightings = []
    for t in staff_detail:
        for det in t.get("dvr_sighting_details", []):
            all_sightings.append({"name": t["name"], "category": t.get("category", "Staff"), **det})
    all_sightings.sort(key=lambda x: x.get("time", ""))

    r = 4
    for idx, s in enumerate(all_sightings, 1):
        ws3.cell(row=r, column=1, value=idx).border = border
        ws3.cell(row=r, column=2, value=s["name"]).border = border
        ws3.cell(row=r, column=3, value=s["category"]).border = border
        ws3.cell(row=r, column=4, value=s.get("time", "")).border = border
        ws3.cell(row=r, column=5, value=s.get("camera", "")).border = border
        conf = s.get("confidence", 0)
        ws3.cell(row=r, column=6, value=f"{conf:.1%}" if conf else "—").border = border
        ws3.cell(row=r, column=7, value=s.get("outfit_color", "—")).border = border
        colors = s.get("outfit_colors", [])
        if colors:
            color_str = ", ".join(f"{c['color']} ({c['percentage']}%)" for c in colors)
        else:
            color_str = s.get("outfit_description", "—")
        detail_cell = ws3.cell(row=r, column=8, value=color_str)
        detail_cell.border = border
        detail_cell.alignment = Alignment(wrap_text=True)
        r += 1

    for col_letter, w in [("A", 5), ("B", 25), ("C", 10), ("D", 12),
                           ("E", 40), ("F", 12), ("G", 15), ("H", 40)]:
        ws3.column_dimensions[col_letter].width = w

    # ── Sheet 4: Reconciliation (Side-by-Side) ──
    ws4 = wb.create_sheet("Reconciliation")

    def _write_section(ws, start_row, title, teachers, fill):
        ws.merge_cells(start_row=start_row, start_column=1,
                       end_row=start_row, end_column=8)
        title_cell = ws.cell(row=start_row, column=1,
                             value=f"{title} ({len(teachers)})")
        title_cell.font = Font(bold=True, size=12, color="FFFFFF")
        title_cell.fill = header_fill
        title_cell.border = border

        row = start_row + 1
        sub_headers = ["#", "Name", "TrueFace Arrival", "TrueFace Departure",
                       "DVR First Seen", "DVR Last Seen",
                       "Outfit / Clothing", "Sighting Count"]
        for col, h in enumerate(sub_headers, 1):
            cell = ws.cell(row=row, column=col, value=h)
            cell.font = Font(bold=True)
            cell.fill = fill
            cell.border = border

        for idx, t in enumerate(teachers, 1):
            row += 1
            ws.cell(row=row, column=1, value=idx).border = border
            ws.cell(row=row, column=2, value=t["name"]).border = border
            ws.cell(row=row, column=3,
                    value=t.get("trueface_arrival") or "—").border = border
            ws.cell(row=row, column=4,
                    value=t.get("trueface_departure") or "—").border = border
            ws.cell(row=row, column=5,
                    value=t.get("dvr_first_seen") or "—").border = border
            ws.cell(row=row, column=6,
                    value=t.get("dvr_last_seen") or "—").border = border
            ws.cell(row=row, column=7,
                    value=t.get("outfit_summary", "—")).border = border
            ws.cell(row=row, column=8,
                    value=t.get("dvr_sighting_count", 0)).border = border

        return row + 2

    ws4.merge_cells("A1:H1")
    ws4["A1"] = f"════════ RECONCILIATION — {date_str} ════════"
    ws4["A1"].font = Font(bold=True, size=14)
    row = 3

    row = _write_section(ws4, row, "✓ FULLY VERIFIED (Both TrueFace + DVR)",
                         fully_verified, green_fill)
    row = _write_section(ws4, row, "⚠ ENTRY ONLY — DVR but NOT on TrueFace",
                         dvr_only, red_fill)
    row = _write_section(ws4, row, "⚠ TRUEFACE ONLY — No DVR Sighting",
                         trueface_only, yellow_fill)
    row = _write_section(ws4, row, "— ABSENT (Neither System)",
                         absent, gray_fill)

    for col_letter, w in [("A", 5), ("B", 25), ("C", 16), ("D", 16),
                           ("E", 14), ("F", 14), ("G", 25), ("H", 14)]:
        ws4.column_dimensions[col_letter].width = w

    # ── Sheet 5: Unknown / Unregistered People ──
    ws5 = wb.create_sheet("Unknown Persons")

    ws5.merge_cells("A1:F1")
    ws5["A1"] = f"UNKNOWN / UNREGISTERED PEOPLE - {date_str}"
    ws5["A1"].font = Font(bold=True, size=14)

    ws5["A3"] = "Estimated Unknown Entered"
    ws5["A3"].font = Font(bold=True)
    ws5["A3"].border = border
    ws5["B3"] = recon.get("estimated_unknown_in", 0)
    ws5["B3"].font = Font(bold=True, size=14)
    ws5["B3"].border = border
    ws5["B3"].fill = orange_fill

    ws5["A4"] = "Estimated Unknown Exited"
    ws5["A4"].font = Font(bold=True)
    ws5["A4"].border = border
    ws5["B4"] = recon.get("estimated_unknown_out", 0)
    ws5["B4"].font = Font(bold=True, size=12)
    ws5["B4"].border = border

    ws5["A5"] = "Estimated Unknown INSIDE"
    ws5["A5"].font = Font(bold=True)
    ws5["A5"].border = border
    ws5["A5"].fill = orange_fill
    ws5["B5"] = estimated_unknown_inside
    ws5["B5"].font = Font(bold=True, size=14)
    ws5["B5"].border = border
    ws5["B5"].fill = orange_fill

    # Unknown person detail table
    r = 7
    ws5.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    title_cell = ws5.cell(row=r, column=1, value=f"Unknown Person Log ({len(unknown_persons)} entries)")
    title_cell.font = Font(bold=True, size=12, color="FFFFFF")
    title_cell.fill = header_fill
    title_cell.border = border

    r += 1
    for col, h in enumerate(["ID", "Time", "Camera", "Classification", "Direction", "Status"], 1):
        cell = ws5.cell(row=r, column=col, value=h)
        cell.font = Font(bold=True)
        cell.fill = light_blue_fill
        cell.border = border

    parent_fill = PatternFill("solid", fgColor="B4C6E7")
    vendor_fill = PatternFill("solid", fgColor="E2EFDA")
    for u in unknown_persons[:50]:
        r += 1
        ws5.cell(row=r, column=1, value=u["id"]).border = border
        ws5.cell(row=r, column=2, value=u.get("time", "-")).border = border
        ws5.cell(row=r, column=3, value=u.get("camera", "-")).border = border
        cls_cell = ws5.cell(row=r, column=4, value=u.get("classification", "Visitor"))
        cls_cell.border = border
        cls = u.get("classification", "Visitor")
        if cls == "Parent":
            cls_cell.fill = parent_fill
        elif cls == "Vendor":
            cls_cell.fill = vendor_fill
        elif cls == "Visitor":
            cls_cell.fill = orange_fill
        ws5.cell(row=r, column=5, value=u.get("direction", "IN")).border = border
        status_cell = ws5.cell(row=r, column=6, value=u.get("status", "INSIDE"))
        status_cell.border = border
        if u.get("status") == "INSIDE":
            status_cell.fill = orange_fill
        elif u.get("status") == "EXITED":
            status_cell.fill = light_blue_fill

    # Camera breakdown
    r += 2
    ws5.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    cam_title = ws5.cell(row=r, column=1, value="Detections by Camera")
    cam_title.font = Font(bold=True, size=12, color="FFFFFF")
    cam_title.fill = header_fill
    cam_title.border = border

    r += 1
    for col, h in enumerate(["Camera", "Count"], 1):
        cell = ws5.cell(row=r, column=col, value=h)
        cell.font = Font(bold=True)
        cell.fill = light_blue_fill
        cell.border = border

    for cam_name in sorted(visitor_by_camera.keys()):
        cam_visitors = visitor_by_camera[cam_name]
        r += 1
        ws5.cell(row=r, column=1, value=cam_name).border = border
        ws5.cell(row=r, column=2, value=len(cam_visitors)).border = border

    ws5.column_dimensions["A"].width = 30
    ws5.column_dimensions["B"].width = 15
    ws5.column_dimensions["C"].width = 40
    ws5.column_dimensions["D"].width = 12
    ws5.column_dimensions["E"].width = 12

    # ── Sheet 6: Outfit Reconciliation ──
    ws6 = wb.create_sheet("Outfit Reconciliation")

    ws6.merge_cells("A1:E1")
    ws6["A1"] = f"════════ OUTFIT RECONCILIATION — {date_str} ════════"
    ws6["A1"].font = Font(bold=True, size=14)

    outfit_headers = ["#", "Name", "Outfit / Clothing", "Total Sightings",
                      "Cameras (with outfit)"]
    for col, h in enumerate(outfit_headers, 1):
        cell = ws6.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border

    r = 4
    idx = 0
    for t in staff_detail:
        outfit_obs = t.get("outfit_observations", [])
        if not outfit_obs:
            continue
        idx += 1
        outfit_desc = t.get("outfit_summary", "-")
        cam_set = set()
        for obs in outfit_obs:
            if obs.get("camera"):
                cam_set.add(obs["camera"])
        ws6.cell(row=r, column=1, value=idx).border = border
        ws6.cell(row=r, column=2, value=t["name"]).border = border
        outfit_cell = ws6.cell(row=r, column=3, value=outfit_desc)
        outfit_cell.border = border
        outfit_cell.alignment = Alignment(wrap_text=True)
        ws6.cell(row=r, column=4, value=len(outfit_obs)).border = border
        ws6.cell(row=r, column=5,
                 value=", ".join(sorted(cam_set)) or "—").border = border
        r += 1

    for col_letter, w in [("A", 5), ("B", 25), ("C", 35), ("D", 15), ("E", 45)]:
        ws6.column_dimensions[col_letter].width = w

    # ── Sheet 7: AI Observations ──
    ws7 = wb.create_sheet("AI Observations")

    ws7.merge_cells("A1:B1")
    ws7["A1"] = f"════════ AI OBSERVATIONS — {date_str} ════════"
    ws7["A1"].font = Font(bold=True, size=14)

    observations = _generate_ai_observations(recon)
    r = 3
    for obs in observations:
        ws7.cell(row=r, column=1, value="•").border = border
        obs_cell = ws7.cell(row=r, column=2, value=obs)
        obs_cell.border = border
        obs_cell.alignment = Alignment(wrap_text=True)
        r += 1

    ws7.column_dimensions["A"].width = 5
    ws7.column_dimensions["B"].width = 80

    # ── Sheet 8: Time Trail ──
    ws8 = wb.create_sheet("Time Trail")

    ws8.merge_cells("A1:E1")
    ws8["A1"] = f"════════ TIME TRAIL RECONSTRUCTION — {date_str} ════════"
    ws8["A1"].font = Font(bold=True, size=14)

    trail_headers = ["#", "Name", "Category", "Time", "Source / Camera", "Event"]
    for col, h in enumerate(trail_headers, 1):
        cell = ws8.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border

    r = 4
    # Show time trail for detected persons only
    detected = [t for t in staff_detail if t.get("time_trail")]
    for t in detected:
        # Person header row
        ws8.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        person_cell = ws8.cell(row=r, column=1,
                               value=f"{t['name']} - {t.get('category', 'Staff')}")
        person_cell.font = Font(bold=True, size=10)
        person_cell.fill = light_blue_fill
        person_cell.border = border
        r += 1
        for idx, trail in enumerate(t["time_trail"], 1):
            ws8.cell(row=r, column=1, value=idx).border = border
            ws8.cell(row=r, column=2, value=t["name"]).border = border
            ws8.cell(row=r, column=3, value=t.get("category", "Staff")).border = border
            ws8.cell(row=r, column=4, value=trail.get("time", "")).border = border
            ws8.cell(row=r, column=5, value=trail.get("source", "")).border = border
            ws8.cell(row=r, column=6, value=trail.get("event", "")).border = border
            r += 1
        r += 1  # blank row between persons

    for col_letter, w in [("A", 5), ("B", 25), ("C", 20), ("D", 12),
                           ("E", 40), ("F", 30)]:
        ws8.column_dimensions[col_letter].width = w

    # ── Sheet 9: Mismatch Alerts ──
    alerts = recon.get("alerts", [])
    ws9 = wb.create_sheet("Mismatch Alerts")

    ws9.merge_cells("A1:E1")
    ws9["A1"] = f"════════ MISMATCH ALERTS — {date_str} ════════"
    ws9["A1"].font = Font(bold=True, size=14)

    alert_headers = ["#", "Alert Type", "Severity", "Person", "Detail"]
    for col, h in enumerate(alert_headers, 1):
        cell = ws9.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border

    high_fill = PatternFill("solid", fgColor="FF4444")
    medium_fill = PatternFill("solid", fgColor="FFA500")
    low_fill = PatternFill("solid", fgColor="FFEB9C")
    severity_fills = {"high": high_fill, "medium": medium_fill, "low": low_fill}

    r = 4
    for idx, alert in enumerate(alerts, 1):
        ws9.cell(row=r, column=1, value=idx).border = border
        ws9.cell(row=r, column=2, value=alert.get("type", "")).border = border
        sev = alert.get("severity", "medium")
        sev_cell = ws9.cell(row=r, column=3, value=sev.upper())
        sev_cell.border = border
        sev_cell.fill = severity_fills.get(sev, medium_fill)
        ws9.cell(row=r, column=4, value=alert.get("person", "—") or "—").border = border
        detail_cell = ws9.cell(row=r, column=5, value=alert.get("detail", ""))
        detail_cell.border = border
        detail_cell.alignment = Alignment(wrap_text=True)
        r += 1

    if not alerts:
        ws9.cell(row=4, column=1, value="No mismatch alerts for this period.").font = Font(italic=True)

    for col_letter, w in [("A", 5), ("B", 30), ("C", 12), ("D", 25), ("E", 60)]:
        ws9.column_dimensions[col_letter].width = w

    # ── Sheet 10: Gate vs Face Discrepancy ──
    disc = recon.get("discrepancy", {})
    ws10d = wb.create_sheet("Discrepancy")

    ws10d.merge_cells("A1:F1")
    ws10d["A1"] = f"════════ GATE vs FACE DISCREPANCY — {date_str} ════════"
    ws10d["A1"].font = Font(bold=True, size=14)

    r = 3
    disc_summary = [
        ("Gate Head Count (IN)", disc.get("gate_head_count", 0), None),
        ("Faces Identified (Total)", disc.get("faces_identified", 0), None),
        ("  Staff Identified", disc.get("staff_identified", 0), light_blue_fill),
        ("  Visitors Identified", disc.get("visitors_identified", 0), orange_fill),
        ("UNRECONCILED", disc.get("unreconciled", 0),
         red_fill if disc.get("unreconciled", 0) > 0 else green_fill),
        ("Reconciliation Rate", f"{disc.get('reconciliation_rate', 100)}%",
         green_fill if disc.get("reconciliation_rate", 100) >= 80 else red_fill),
    ]
    for label, value, fill in disc_summary:
        ws10d.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws10d.cell(row=r, column=1).border = border
        ws10d.cell(row=r, column=2, value=value).font = Font(bold=True, size=12)
        ws10d.cell(row=r, column=2).border = border
        if fill:
            ws10d.cell(row=r, column=1).fill = fill
            ws10d.cell(row=r, column=2).fill = fill
        r += 1

    # Hourly breakdown table
    hourly = disc.get("hourly_breakdown", [])
    if hourly:
        r += 1
        ws10d.cell(row=r, column=1, value="HOURLY BREAKDOWN").font = Font(bold=True, size=12)
        r += 1
        for col, h in enumerate(["Hour", "Gate Entries", "Identified", "Unreconciled"], 1):
            cell = ws10d.cell(row=r, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.border = border
        r += 1
        for hd in hourly:
            ws10d.cell(row=r, column=1, value=hd["hour"]).border = border
            ws10d.cell(row=r, column=2, value=hd["gate_entries"]).border = border
            ws10d.cell(row=r, column=3, value=hd["identified"]).border = border
            unrec_cell = ws10d.cell(row=r, column=4, value=hd["unreconciled"])
            unrec_cell.border = border
            if hd["unreconciled"] > 0:
                unrec_cell.fill = red_fill
            r += 1

    # Unmatched entries detail
    unmatched = disc.get("unmatched_entries", [])
    if unmatched:
        r += 1
        ws10d.cell(row=r, column=1, value="UNMATCHED ENTRIES (not identified by face recognition)").font = Font(bold=True, size=12)
        r += 1
        for col, h in enumerate(["#", "Time", "Camera", "Attire Color"], 1):
            cell = ws10d.cell(row=r, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.border = border
        r += 1
        for idx, ue in enumerate(unmatched, 1):
            ws10d.cell(row=r, column=1, value=idx).border = border
            ws10d.cell(row=r, column=2, value=ue.get("time", "")).border = border
            ws10d.cell(row=r, column=3, value=ue.get("camera", "")).border = border
            ws10d.cell(row=r, column=4, value=ue.get("attire_color", "")).border = border
            r += 1

    for col_letter, w in [("A", 40), ("B", 18), ("C", 20), ("D", 20)]:
        ws10d.column_dimensions[col_letter].width = w

    # ── Sheet 11: Snapshots ──
    import base64
    import tempfile
    import os
    from openpyxl.drawing.image import Image as XlImage

    vehicle_entries = recon.get("vehicle_entries", [])
    gate_crops = recon.get("gate_entries_with_crops", [])
    vehicle_snaps = [v for v in vehicle_entries if v.get("snapshot")]
    person_snaps = [g for g in gate_crops if g.get("person_crop")]

    # Keep temp files alive until after wb.save()
    _tmp_files: list[str] = []

    if vehicle_snaps or person_snaps:
        ws10 = wb.create_sheet("Snapshots")
        r = 1
        for col, val in enumerate(["#", "Type", "Direction", "Time", "Camera", "Snapshot"], 1):
            c = ws10.cell(row=r, column=col, value=val)
            c.font = header_font
            c.fill = header_fill
            c.border = border
        r += 1

        ws10.cell(row=r, column=1, value="VEHICLE SNAPSHOTS").font = Font(bold=True, size=12)
        r += 1

        for idx, v in enumerate(vehicle_snaps[-12:], 1):
            ws10.cell(row=r, column=1, value=idx).border = border
            ws10.cell(row=r, column=2, value=v.get("vehicle_type", "").upper()).border = border
            ws10.cell(row=r, column=3, value=v.get("direction", "")).border = border
            ts = v.get("timestamp", "")
            time_part = ts.split(" ")[1] if " " in ts else ts
            ws10.cell(row=r, column=4, value=time_part).border = border
            ws10.cell(row=r, column=5, value=v.get("camera", "")).border = border
            try:
                img_bytes = base64.b64decode(v["snapshot"])
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                _tmp_files.append(tmp_path)
                img = XlImage(tmp_path)
                img.width = 200
                img.height = 130
                ws10.add_image(img, f"F{r}")
                ws10.row_dimensions[r].height = 100
            except Exception:
                pass
            r += 1

        r += 2
        ws10.cell(row=r, column=1, value="GATE ENTRY PERSON SNAPSHOTS").font = Font(bold=True, size=12)
        r += 1

        for idx, g in enumerate(person_snaps[-16:], 1):
            ws10.cell(row=r, column=1, value=idx).border = border
            ws10.cell(row=r, column=2, value="PERSON").border = border
            ws10.cell(row=r, column=3, value=g.get("direction", "IN")).border = border
            ts = g.get("timestamp", "")
            time_part = ts.split(" ")[1] if " " in ts else ts
            ws10.cell(row=r, column=4, value=time_part).border = border
            ws10.cell(row=r, column=5, value=g.get("camera", "")).border = border
            try:
                img_bytes = base64.b64decode(g["person_crop"])
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                _tmp_files.append(tmp_path)
                img = XlImage(tmp_path)
                img.width = 120
                img.height = 160
                ws10.add_image(img, f"F{r}")
                ws10.row_dimensions[r].height = 125
            except Exception:
                pass
            r += 1

        for col_letter, w in [("A", 5), ("B", 12), ("C", 10), ("D", 12), ("E", 18), ("F", 30)]:
            ws10.column_dimensions[col_letter].width = w

    buf = io.BytesIO()
    wb.save(buf)

    # Clean up temp files after save
    for tmp_path in _tmp_files:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return buf.getvalue()


def _generate_ai_observations(recon: dict) -> list[str]:
    """Generate AI-powered observations from the reconciliation data."""
    observations = []
    side_by_side = recon.get("side_by_side", {})
    staff_detail = recon.get("staff_detail", recon.get("teacher_detail", []))
    unknown_inside = recon.get("estimated_unknown_inside", 0)

    dvr_only = side_by_side.get("dvr_only", [])
    trueface_only = side_by_side.get("trueface_only", [])
    fully_verified = side_by_side.get("both_present", [])
    absent = side_by_side.get("neither", [])

    total_inside = recon.get("total_inside", 0)
    staff_inside = recon.get("staff_inside", 0)

    # Occupancy summary
    observations.append(
        f"LIVE OCCUPANCY: {total_inside} unique people inside campus "
        f"({staff_inside} verified staff + {unknown_inside} unknown/visitors)."
    )

    if dvr_only:
        observations.append(
            f"{len(dvr_only)} staff seen on DVR cameras but NOT marked "
            f"on TrueFace 3000. They may have bypassed facial recognition."
        )
    if trueface_only:
        observations.append(
            f"{len(trueface_only)} staff marked on TrueFace but NOT spotted "
            f"on any DVR camera. Possible camera blind spot or delayed sync."
        )

    if unknown_inside > 0:
        observations.append(
            f"{unknown_inside} unregistered/unknown person(s) estimated inside campus. "
            f"These could be parents, vendors, or visitors."
        )

    # Peak activity
    from collections import Counter
    hourly_counts: Counter = Counter()
    for t in staff_detail:
        for det in t.get("dvr_sighting_details", []):
            time_str = det.get("time", "")
            if time_str and len(time_str) >= 2:
                try:
                    hour = int(time_str[:2])
                    hourly_counts[hour] += 1
                except ValueError:
                    pass

    if hourly_counts:
        peak_hour = hourly_counts.most_common(1)[0]
        observations.append(
            f"Peak DVR activity at {peak_hour[0]:02d}:00 hour with {peak_hour[1]} sightings."
        )

    # Staff detection rate
    total = recon.get("total_registered", recon.get("total_registered_teachers", 0))
    if total > 0:
        present = len(fully_verified) + len(trueface_only) + len(dvr_only)
        rate = round(present / total * 100, 1)
        observations.append(
            f"Staff detection rate: {rate}% ({present} detected out of {total} registered)."
        )

    # Raw vs unique clarification
    raw_in = recon.get("raw_gate_in", 0)
    if raw_in > 0:
        observations.append(
            f"Raw gate detections: {raw_in} IN, {recon.get('raw_gate_out', 0)} OUT. "
            f"These are NOT unique people counts - same person can trigger multiple detections."
        )

    if not observations:
        observations.append("No significant observations for this period.")

    return observations


# ============================================================
# PDF Snapshot Gallery
# ============================================================

def _add_snapshot_gallery(pdf, recon: dict, section_header):
    """Add live snapshot images to the PDF report."""
    import base64
    import tempfile

    vehicle_entries = recon.get("vehicle_entries", [])
    gate_entries_raw = recon.get("gate_entries_with_crops", [])

    # Collect vehicle snapshots (latest 12)
    vehicle_snaps = [v for v in vehicle_entries if v.get("snapshot")]
    # Collect person crops from gate entries (latest 12)
    person_snaps = [g for g in gate_entries_raw if g.get("person_crop")]

    if not vehicle_snaps and not person_snaps:
        return

    def _embed_b64_image(b64_data: str, img_w: int = 45, img_h: int = 35):
        """Decode base64 image and embed in PDF."""
        try:
            img_bytes = base64.b64decode(b64_data)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            pdf.image(tmp_path, w=img_w, h=img_h)
            import os
            os.unlink(tmp_path)
            return True
        except Exception:
            return False

    # ── Vehicle Snapshots ──
    if vehicle_snaps:
        section_header("VEHICLE SNAPSHOTS (Live DVR Captures)")
        pdf.set_font("Helvetica", "", 8)
        cols = 3
        img_w = 58
        img_h = 40
        for i, v in enumerate(vehicle_snaps[-12:]):
            col = i % cols
            if col == 0 and i > 0:
                pdf.ln(img_h + 12)
            x = pdf.l_margin + col * (img_w + 5)
            y = pdf.get_y()
            if y + img_h + 15 > pdf.h - pdf.b_margin:
                pdf.add_page()
                y = pdf.get_y()
            try:
                img_bytes = base64.b64decode(v["snapshot"])
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                pdf.image(tmp_path, x=x, y=y, w=img_w, h=img_h)
                import os
                os.unlink(tmp_path)
                # Caption below image
                pdf.set_xy(x, y + img_h)
                ts = v.get("timestamp", "")
                time_part = ts.split(" ")[1] if " " in ts else ts
                vtype = v.get("vehicle_type", "vehicle").upper()
                direction = v.get("direction", "")
                caption = f"{vtype} {direction} - {time_part}"
                pdf.cell(img_w, 4, caption, align="C")
            except Exception:
                pass
        pdf.ln(img_h + 15)

    # ── Gate Entry Person Snapshots ──
    if person_snaps:
        section_header("GATE ENTRY SNAPSHOTS (Live DVR Captures)")
        pdf.set_font("Helvetica", "", 8)
        cols = 4
        img_w = 42
        img_h = 50
        for i, g in enumerate(person_snaps[-16:]):
            col = i % cols
            if col == 0 and i > 0:
                pdf.ln(img_h + 12)
            x = pdf.l_margin + col * (img_w + 5)
            y = pdf.get_y()
            if y + img_h + 15 > pdf.h - pdf.b_margin:
                pdf.add_page()
                y = pdf.get_y()
            try:
                img_bytes = base64.b64decode(g["person_crop"])
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                pdf.image(tmp_path, x=x, y=y, w=img_w, h=img_h)
                import os
                os.unlink(tmp_path)
                # Caption
                pdf.set_xy(x, y + img_h)
                ts = g.get("timestamp", "")
                time_part = ts.split(" ")[1] if " " in ts else ts
                cam = g.get("camera", "")
                direction = g.get("direction", "IN")
                caption = f"{direction} {time_part}"
                pdf.cell(img_w, 4, caption, align="C")
            except Exception:
                pass
        pdf.ln(img_h + 15)


# ============================================================
# PDF Report Generation
# ============================================================

def _generate_reconciliation_pdf(recon: dict, date_display: str,
                                  time_display: str) -> bytes:
    """Generate a professionally formatted PDF reconciliation report.

    New architecture:
      - LIVE OCCUPANCY (unique people inside, not raw detections)
      - VERIFIED STAFF INSIDE (by category: Teachers, Admin, Security, Support)
      - UNKNOWN / VISITOR INSIDE (with UNKNOWN-001 IDs and snapshots)
      - Staff & unknown exited counts
    """
    from fpdf import FPDF

    staff_detail = recon.get("staff_detail", recon.get("teacher_detail", []))
    side_by_side = recon.get("side_by_side", {})
    unknown_persons = recon.get("unknown_persons", [])

    staff_inside = [s for s in staff_detail if s.get("occupancy_status") == "INSIDE"]
    staff_exited = [s for s in staff_detail if s.get("occupancy_status") == "EXITED"]
    staff_absent = [s for s in staff_detail if s.get("occupancy_status") == "ABSENT"]
    staff_present = [s for s in staff_detail if s.get("occupancy_status") in ("INSIDE", "EXITED")]

    fully_verified = side_by_side.get("both_present", [])
    trueface_only = side_by_side.get("trueface_only", [])
    dvr_only = side_by_side.get("dvr_only", [])

    total_inside = recon.get("total_inside", 0)
    estimated_unknown_inside = recon.get("estimated_unknown_inside", 0)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    _orig_cell = pdf.cell

    def _safe_cell(*args, **kwargs):
        new_args = list(args)
        for i, a in enumerate(new_args):
            if isinstance(a, str):
                new_args[i] = a.replace("\u2014", "-").replace("\u2013", "-").replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
        for k, v in kwargs.items():
            if isinstance(v, str):
                kwargs[k] = v.replace("\u2014", "-").replace("\u2013", "-").replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
        return _orig_cell(*new_args, **kwargs)

    pdf.cell = _safe_cell

    # ── Title ──
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "SCHOOL HEADCOUNT RECONCILIATION", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"PP International School  |  {date_display}  |  {time_display} IST", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(6)

    # ── Helper functions ──
    def section_header(title: str):
        pdf.set_fill_color(47, 84, 150)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 9, f"  {title}", new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    def key_value(key: str, value, indent: int = 0, bold_val: bool = False):
        pdf.set_font("Helvetica", "", 10)
        prefix = "    " * indent
        pdf.cell(90 + indent * 10, 6, f"{prefix}{key}", new_x="RIGHT")
        pdf.set_font("Helvetica", "B" if bold_val else "", 10)
        pdf.cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")

    def status_row(label: str, count: int, r: int, g: int, b: int):
        pdf.set_fill_color(r, g, b)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(120, 7, f"  {label}", border=1, fill=True, new_x="RIGHT")
        pdf.cell(30, 7, str(count), border=1, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")

    # ══════════════════════════════════════════════
    # SECTION 1: LIVE OCCUPANCY (unique people inside)
    # ══════════════════════════════════════════════
    section_header("LIVE OCCUPANCY (Unique People Currently Inside)")
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(0, 100, 0)
    pdf.cell(0, 10, f"TOTAL INSIDE: {total_inside}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)
    key_value("Verified Staff Inside", len(staff_inside), bold_val=True)
    key_value("Unknown / Visitors Inside", estimated_unknown_inside, bold_val=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 5,
             f"Unique crossings: IN={recon.get('unique_gate_in', 0)}, OUT={recon.get('unique_gate_out', 0)}"
             f"  (raw detections: IN={recon.get('raw_gate_in', 0)}, OUT={recon.get('raw_gate_out', 0)})",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    # ══════════════════════════════════════════════
    # SECTION 2: VERIFIED STAFF INSIDE (by category)
    # ══════════════════════════════════════════════
    section_header("VERIFIED STAFF INSIDE")
    staff_cat_inside = recon.get("staff_category_inside", {})
    if staff_cat_inside:
        for cat_name in sorted(staff_cat_inside.keys()):
            key_value(cat_name, staff_cat_inside[cat_name], indent=1)
    key_value("Total Verified Staff Inside", len(staff_inside), bold_val=True)
    pdf.ln(2)

    # Staff inside table
    if staff_inside:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(198, 239, 206)
        pdf.cell(8, 7, "#", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(55, 7, "Name", border=1, fill=True, new_x="RIGHT")
        pdf.cell(35, 7, "Category", border=1, fill=True, new_x="RIGHT")
        pdf.cell(25, 7, "Arrived", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(25, 7, "Status", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(0, 7, "Cameras", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        for idx, s in enumerate(staff_inside, 1):
            arrival = s.get("trueface_arrival") or s.get("dvr_first_seen") or "-"
            cams = ", ".join(s.get("dvr_cameras", [])[:3]) or "-"
            pdf.cell(8, 6, str(idx), border=1, align="C", new_x="RIGHT")
            pdf.cell(55, 6, s["name"], border=1, new_x="RIGHT")
            pdf.cell(35, 6, s["category"], border=1, new_x="RIGHT")
            pdf.cell(25, 6, arrival, border=1, align="C", new_x="RIGHT")
            pdf.cell(25, 6, "INSIDE", border=1, align="C", new_x="RIGHT")
            pdf.cell(0, 6, cams, border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ══════════════════════════════════════════════
    # SECTION 3: VERIFIED STAFF EXITED
    # ══════════════════════════════════════════════
    if staff_exited:
        section_header("VERIFIED STAFF EXITED")
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(217, 226, 243)
        pdf.cell(8, 7, "#", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(55, 7, "Name", border=1, fill=True, new_x="RIGHT")
        pdf.cell(35, 7, "Category", border=1, fill=True, new_x="RIGHT")
        pdf.cell(25, 7, "Arrived", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(25, 7, "Departed", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(0, 7, "Status", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        for idx, s in enumerate(staff_exited, 1):
            pdf.cell(8, 6, str(idx), border=1, align="C", new_x="RIGHT")
            pdf.cell(55, 6, s["name"], border=1, new_x="RIGHT")
            pdf.cell(35, 6, s["category"], border=1, new_x="RIGHT")
            pdf.cell(25, 6, s.get("trueface_arrival") or "-", border=1, align="C", new_x="RIGHT")
            pdf.cell(25, 6, s.get("trueface_departure") or "-", border=1, align="C", new_x="RIGHT")
            pdf.cell(0, 6, "EXITED", border=1, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    # ══════════════════════════════════════════════
    # SECTION 4: UNKNOWN / UNREGISTERED PEOPLE
    # ══════════════════════════════════════════════
    section_header("UNKNOWN / UNREGISTERED PEOPLE")
    key_value("Estimated Unknown Entered", recon.get("estimated_unknown_in", 0), bold_val=True)
    key_value("Estimated Unknown Exited", recon.get("estimated_unknown_out", 0))
    key_value("Estimated Unknown Inside", estimated_unknown_inside, bold_val=True)
    pdf.ln(2)

    if unknown_persons:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(255, 235, 156)
        pdf.cell(28, 7, "ID", border=1, fill=True, new_x="RIGHT")
        pdf.cell(25, 7, "Time", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(50, 7, "Camera", border=1, fill=True, new_x="RIGHT")
        pdf.cell(20, 7, "Dir", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(0, 7, "Status", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        for u in unknown_persons[:30]:
            pdf.cell(28, 6, u["id"], border=1, new_x="RIGHT")
            pdf.cell(25, 6, u.get("time", "-"), border=1, align="C", new_x="RIGHT")
            pdf.cell(50, 6, u.get("camera", "-"), border=1, new_x="RIGHT")
            pdf.cell(20, 6, u.get("direction", "IN"), border=1, align="C", new_x="RIGHT")
            pdf.cell(0, 6, u.get("status", "INSIDE"), border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ══════════════════════════════════════════════
    # SECTION 5: VEHICLE COUNT
    # ══════════════════════════════════════════════
    vehicles_in = recon.get("vehicles_in", 0)
    vehicles_out = recon.get("vehicles_out", 0)
    vehicle_types = recon.get("vehicle_types", {})
    if vehicles_in > 0 or vehicles_out > 0:
        section_header("VEHICLE COUNT")
        key_value("Vehicles IN", vehicles_in, bold_val=True)
        key_value("Vehicles OUT", vehicles_out)
        for vtype, vcount in sorted(vehicle_types.items()):
            key_value(vtype.title(), vcount, indent=1)
        pdf.ln(2)

    # ══════════════════════════════════════════════
    # SECTION 6: STAFF RECONCILIATION
    # ══════════════════════════════════════════════
    section_header("STAFF RECONCILIATION")
    status_row("Fully Verified (TrueFace + DVR)", len(fully_verified), 198, 239, 206)
    status_row("Entry Only (DVR - Not on TrueFace)", len(dvr_only), 255, 199, 206)
    status_row("TrueFace Only (No DVR Sighting)", len(trueface_only), 255, 235, 156)
    status_row("Not Detected / Absent", len(staff_absent), 217, 217, 217)
    pdf.ln(3)

    # Staff category breakdown
    staff_cat_present = recon.get("staff_category_present", {})
    if staff_cat_present:
        section_header("STAFF CATEGORY BREAKDOWN (Present)")
        for cat_name in sorted(staff_cat_present.keys()):
            key_value(cat_name, staff_cat_present[cat_name], indent=1)
        pdf.ln(3)

    # ══════════════════════════════════════════════
    # SECTION 7: ENTRY WITHOUT ATTENDANCE (alerts)
    # ══════════════════════════════════════════════
    if dvr_only:
        section_header("ENTRY ONLY (DVR - NOT MARKED ON TRUEFACE)")
        for t in dvr_only:
            cams = ", ".join(t.get("dvr_cameras", []))
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 6, f"{t['name']} - {t.get('category', 'Staff')}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(0, 5, f"    DVR: {t.get('dvr_first_seen', '-')}  |  Cameras: {cams}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(200, 0, 0)
            pdf.cell(0, 5, "    NOT marked on TrueFace 3000", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

    # ══════════════════════════════════════════════
    # SECTION 8: OUTFIT RECONCILIATION
    # ══════════════════════════════════════════════
    dvr_with_outfits = [t for t in staff_detail
                        if t.get("dvr_seen") and t.get("outfit_observations")]
    if dvr_with_outfits:
        section_header("OUTFIT RECONCILIATION")
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(217, 226, 243)
        pdf.cell(8, 7, "#", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(50, 7, "Name", border=1, fill=True, new_x="RIGHT")
        pdf.cell(60, 7, "Outfit / Clothing", border=1, fill=True, new_x="RIGHT")
        pdf.cell(20, 7, "Sightings", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(0, 7, "Cameras", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for idx, t in enumerate(dvr_with_outfits, 1):
            outfit = t.get("outfit_summary", "-")
            cam_set = set()
            for obs in t.get("outfit_observations", []):
                if obs.get("camera"):
                    cam_set.add(obs["camera"])
            pdf.cell(8, 6, str(idx), border=1, align="C", new_x="RIGHT")
            pdf.cell(50, 6, t["name"], border=1, new_x="RIGHT")
            pdf.cell(60, 6, outfit, border=1, new_x="RIGHT")
            pdf.cell(20, 6, str(t.get("dvr_sighting_count", 0)), border=1, align="C", new_x="RIGHT")
            pdf.cell(0, 6, ", ".join(sorted(cam_set)) or "-", border=1, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)

    # ── Time Trail (top 10 detected persons) ──
    detected_with_trail = [t for t in staff_detail if t.get("time_trail")]
    if detected_with_trail:
        section_header("TIME TRAIL RECONSTRUCTION")
        for t in detected_with_trail[:15]:
            cat = t.get('category', 'Staff')
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_fill_color(217, 226, 243)
            pdf.cell(0, 6, f"{t['name']} ({cat})", fill=True, new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 8)
            for trail in t["time_trail"]:
                pdf.cell(25, 5, trail.get("time", ""), new_x="RIGHT")
                pdf.cell(60, 5, trail.get("source", ""), new_x="RIGHT")
                pdf.cell(0, 5, trail.get("event", ""), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        if len(detected_with_trail) > 15:
            pdf.set_font("Helvetica", "I", 8)
            pdf.cell(0, 5, f"... and {len(detected_with_trail) - 15} more (see Excel for full list)",
                     new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    # ── Mismatch Alerts ──
    alerts = recon.get("alerts", [])
    if alerts:
        section_header("MISMATCH ALERTS")
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(217, 226, 243)
        pdf.cell(8, 7, "#", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(45, 7, "Alert Type", border=1, fill=True, new_x="RIGHT")
        pdf.cell(18, 7, "Severity", border=1, fill=True, align="C", new_x="RIGHT")
        pdf.cell(0, 7, "Detail", border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        for idx, alert in enumerate(alerts, 1):
            sev = alert.get("severity", "medium")
            if sev == "high":
                pdf.set_fill_color(255, 68, 68)
            elif sev == "medium":
                pdf.set_fill_color(255, 165, 0)
            else:
                pdf.set_fill_color(255, 235, 156)
            pdf.cell(8, 6, str(idx), border=1, align="C", new_x="RIGHT")
            pdf.cell(45, 6, alert.get("type", ""), border=1, new_x="RIGHT")
            pdf.cell(18, 6, sev.upper(), border=1, align="C", fill=True, new_x="RIGHT")
            pdf.set_fill_color(255, 255, 255)
            pdf.cell(0, 6, alert.get("detail", ""), border=1, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)

    # ── AI Observations ──
    ai_obs = _generate_ai_observations(recon)
    section_header("AI OBSERVATIONS")
    pdf.set_font("Helvetica", "", 10)
    for obs in ai_obs:
        pdf.multi_cell(0, 6, f"  * {obs}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # ── Live Snapshots Gallery ──
    _add_snapshot_gallery(pdf, recon, section_header)

    # ── Footer ──
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, "PPIS Headcount Reconciliation & Entry Monitoring System", new_x="LMARGIN", new_y="NEXT", align="C")

    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


# ============================================================
# Hourly Reconciliation Report (7 AM - 5 PM IST)
# ============================================================


@router.post("/api/gate/send-report")
async def trigger_reconciliation_report():
    """Manual trigger to send the reconciliation report immediately."""
    await send_reconciliation_report()
    return {"status": "sent"}


@router.post("/api/gate/test-alert")
async def test_unknown_person_alert():
    """Send a test WhatsApp template alert to verify the unknown person alert pipeline."""
    test_entry = {
        "camera": "ENTRY GATE-1",
        "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "direction": "IN",
        "person_crop": "",
    }
    # Clear cooldown so the test alert always sends
    _unknown_alert_last.pop("ENTRY GATE-1", None)
    await _send_unknown_person_alert(test_entry)
    return {"status": "sent", "phone": UNKNOWN_ALERT_PHONE, "template": UNKNOWN_ALERT_TEMPLATE}


async def send_reconciliation_report():
    """Generate and send the hourly head count reconciliation report."""
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    today_display = now.strftime("%d-%m-%Y")
    time_display = now.strftime("%I:%M %p")

    db = await _get_db()
    try:
        recon = await _reconcile(db, today)
    finally:
        await db.close()

    side_by_side = recon.get("side_by_side", {})
    if recon["trueface_identified"] == 0 and recon.get("dvr_sighted", 0) == 0 and recon.get("raw_gate_in", 0) == 0:
        logger.info("[GATE] No TrueFace, DVR, or gate records for %s — skipping report", today)
        return

    # Generate Excel
    xlsx_bytes = _generate_reconciliation_excel(recon)
    filename = f"Head_Count_Reconciliation_{today}_{now.strftime('%H%M')}.xlsx"

    # Build counts from new architecture
    total_inside = recon.get("total_inside", 0)
    staff_inside_count = recon.get("staff_inside", 0)
    unknown_inside = recon.get("estimated_unknown_inside", 0)
    staff_exited_count = recon.get("staff_exited", 0)

    fully_verified = side_by_side.get("both_present", [])
    tf_only_list = side_by_side.get("trueface_only", [])
    dvr_only_list = side_by_side.get("dvr_only", [])
    absent_list = side_by_side.get("neither", [])

    # Generate PDF report
    pdf_bytes = _generate_reconciliation_pdf(recon, today_display, time_display)
    pdf_filename = f"Head_Count_Reconciliation_{today}_{now.strftime('%H%M')}.pdf"

    # Vehicle summary for body
    v_in = recon.get("vehicles_in", 0)
    v_out = recon.get("vehicles_out", 0)
    v_types = recon.get("vehicle_types", {})
    vehicle_line = ""
    if v_in > 0 or v_out > 0:
        type_parts = ", ".join(f"{vt.title()}: {vc}" for vt, vc in sorted(v_types.items()))
        vehicle_line = f"\nVehicles IN: {v_in} | OUT: {v_out}"
        if type_parts:
            vehicle_line += f" ({type_parts})"
        vehicle_line += "\n"

    # Staff category breakdown for email
    staff_cat = recon.get("staff_category_inside", {})
    cat_lines = "\n".join(f"  {cat}: {cnt}" for cat, cnt in sorted(staff_cat.items()))
    if cat_lines:
        cat_lines = f"\n{cat_lines}\n"

    # Brief email body using occupancy-based architecture
    body = (
        f"School Headcount Reconciliation — {today_display} at {time_display} IST\n\n"
        f"LIVE OCCUPANCY (Unique People Inside): {total_inside}\n"
        f"  Verified Staff Inside: {staff_inside_count}\n"
        f"  Unknown / Visitors Inside: {unknown_inside}\n"
        f"{cat_lines}"
        f"\nVerified Staff Exited: {staff_exited_count}\n"
        f"Unique Gate Crossings: IN={recon.get('unique_gate_in', 0)}, OUT={recon.get('unique_gate_out', 0)}\n"
        f"(Raw detections: IN={recon.get('raw_gate_in', 0)}, OUT={recon.get('raw_gate_out', 0)})\n"
        f"{vehicle_line}\n"
        f"Staff Reconciliation: Verified={len(fully_verified)} | "
        f"TrueFace Only={len(tf_only_list)} | "
        f"DVR Only={len(dvr_only_list)} | "
        f"Absent={len(absent_list)}\n\n"
        f"See attached PDF for the full detailed report.\n\n"
        f"-- PPIS Headcount Reconciliation & Entry Monitoring System"
    )

    from app.services.email_service import send_email_async
    recipients = [r.strip() for r in REPORT_RECIPIENTS.split(",") if r.strip()]
    for email in recipients:
        ok = await send_email_async(
            email,
            f"Hourly Head Count Reconciliation — {today_display} {time_display} IST",
            body,
            "PP International School",
            attachments=[
                (pdf_filename, pdf_bytes),
                (filename, xlsx_bytes),
            ],
        )
        logger.info("[GATE] Reconciliation report → %s: %s", email, "OK" if ok else "FAILED")

    logger.info(
        "[GATE] Reconciliation report sent at %s: Inside=%d, Staff=%d, Unknown=%d, Verified=%d",
        time_display, total_inside, staff_inside_count, unknown_inside,
        len(fully_verified),
    )


def send_reconciliation_report_sync():
    """Sync wrapper for scheduler."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(send_reconciliation_report())
        else:
            loop.run_until_complete(send_reconciliation_report())
    except RuntimeError:
        asyncio.run(send_reconciliation_report())
