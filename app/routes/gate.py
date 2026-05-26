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
import io
import json
import logging
import os
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

        await db.execute(
            "INSERT INTO visitor_dvr_sightings (date, timestamp, camera) "
            "VALUES (?, ?, ?)",
            (date_part, ts, v.get("camera", "")),
        )
        count += 1
    await db.commit()
    return count


async def _get_visitor_sightings(db, date: str) -> list[dict]:
    """Get all DVR visitor sightings for a date."""
    cur = await db.execute(
        "SELECT id, timestamp, camera FROM visitor_dvr_sightings "
        "WHERE date = ? ORDER BY timestamp",
        (date,),
    )
    return [
        {"id": r[0], "timestamp": r[1], "camera": r[2]}
        for r in await cur.fetchall()
    ]


# ============================================================
# Reconciliation Logic
# ============================================================

async def _reconcile(db, date: str) -> dict:
    """Perform head count reconciliation for a given date.

    Compares DVR teacher sightings vs TrueFace attendance records,
    and includes visitor (unknown face) sightings.
    Returns:
      - teacher_detail: per-teacher list with DVR sightings + TrueFace status
      - side_by_side: DVR sightings column vs TrueFace attendance column
      - visitor_sightings: list of unknown-face sightings with timestamps/cameras
      - gate summary: entry/exit counts
    """
    gate_in = await _get_gate_entries(db, date, direction="IN")
    gate_out = await _get_gate_entries(db, date, direction="OUT")
    trueface = await _get_trueface_attendance(db, date)
    all_teachers = await _get_all_teachers(db)
    total_teachers = len(all_teachers)
    dvr_sightings = await _get_teacher_sightings(db, date)
    visitor_sightings = await _get_visitor_sightings(db, date)

    total_in = len(gate_in)
    total_out = len(gate_out)
    trueface_count = len(trueface)

    # Build TrueFace lookup by name (case-insensitive)
    tf_by_name: dict[str, dict] = {}
    for t in trueface:
        tf_by_name[t["name"].upper().strip()] = t

    # Build DVR sightings grouped by person
    dvr_by_person: dict[str, list[dict]] = {}
    dvr_names: dict[str, str] = {}
    dvr_outfits: dict[str, list[dict]] = {}
    for s in dvr_sightings:
        key = s["name"].upper().strip()
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

    # Collect all unique teacher names from all sources
    all_names: set[str] = set()
    for t in all_teachers:
        all_names.add(t["name"].upper().strip())
    for key in tf_by_name:
        all_names.add(key)
    for key in dvr_by_person:
        all_names.add(key)

    # --- Teacher-by-teacher detail ---
    teacher_detail = []
    for name_upper in sorted(all_names):
        tf = tf_by_name.get(name_upper)
        dvr_list = dvr_by_person.get(name_upper, [])

        # DVR sighting summary
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
                "camera": cam,
                "time": time_part,
                "confidence": s.get("confidence", 0),
                "outfit_color": s.get("outfit_color", ""),
                "outfit_description": s.get("outfit_description", ""),
                "outfit_colors": s.get("outfit_colors", []),
            })

        display_name = dvr_names.get(name_upper, tf["name"] if tf else name_upper.title())

        # Determine dominant outfit from all sightings
        outfit_observations = dvr_outfits.get(name_upper, [])
        if outfit_observations:
            latest_outfit = outfit_observations[-1]
            outfit_summary = latest_outfit.get("description", "unknown")
        else:
            outfit_summary = "—"

        teacher_detail.append({
            "name": display_name,
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
            "status": _reconciliation_status(tf is not None, len(dvr_list) > 0),
        })

    # --- Side-by-side comparison ---
    side_by_side = {
        "both_present": [t for t in teacher_detail if t["trueface_present"] and t["dvr_seen"]],
        "trueface_only": [t for t in teacher_detail if t["trueface_present"] and not t["dvr_seen"]],
        "dvr_only": [t for t in teacher_detail if not t["trueface_present"] and t["dvr_seen"]],
        "neither": [t for t in teacher_detail if not t["trueface_present"] and not t["dvr_seen"]],
    }

    # Gate timing trail (legacy format)
    timing_trail = []
    for entry in gate_in:
        time_part = entry["timestamp"].split(" ")[1] if " " in entry["timestamp"] else entry["timestamp"]
        timing_trail.append({
            "time": time_part,
            "camera": entry["camera"],
            "attire_color": entry["attire_color"],
            "reconciled": entry["reconciled"],
            "matched_pin": entry["matched_pin"],
        })

    # Update daily summary
    dvr_seen_count = len([t for t in teacher_detail if t["dvr_seen"]])
    unreconciled = abs(trueface_count - dvr_seen_count)
    await db.execute(
        "INSERT OR REPLACE INTO gate_daily_summary "
        "(date, total_in, total_out, trueface_matched, unreconciled) "
        "VALUES (?, ?, ?, ?, ?)",
        (date, total_in, total_out, trueface_count, unreconciled),
    )
    await db.commit()

    # Build visitor summary grouped by camera
    visitor_by_camera: dict[str, list[dict]] = {}
    for v in visitor_sightings:
        cam = v.get("camera", "Unknown")
        visitor_by_camera.setdefault(cam, []).append(v)

    return {
        "date": date,
        "total_gate_in": total_in,
        "total_gate_out": total_out,
        "trueface_identified": trueface_count,
        "dvr_sighted": dvr_seen_count,
        "total_registered_teachers": total_teachers,
        "unreconciled_count": unreconciled,
        "teacher_detail": teacher_detail,
        "side_by_side": side_by_side,
        "timing_trail": timing_trail,
        "visitor_count": len(visitor_sightings),
        "visitor_sightings": visitor_sightings,
        "visitor_by_camera": visitor_by_camera,
    }


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

    Body: [{"camera": "...", "timestamp": "...", "date": "..."}]
    """
    body = await request.json()
    visitors = body if isinstance(body, list) else [body]

    db = await _get_db()
    try:
        count = await _store_visitor_sightings(db, visitors)
    finally:
        await db.close()

    logger.info("[GATE] Stored %d DVR visitor sighting(s)", count)
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
    finally:
        await db.close()

    # Count unique teachers seen on DVR
    dvr_unique = len({s["person_id"] for s in dvr_sightings})

    return {
        "date": today,
        "gate_in": len(gate_in),
        "gate_out": len(gate_out),
        "trueface_identified": len(trueface),
        "dvr_teachers_sighted": dvr_unique,
        "dvr_total_sightings": len(dvr_sightings),
        "visitors_detected": len(visitor_sightings),
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
      1. Summary — overall counts & reconciliation breakdown
      2. Verified Movements — per-person detail with outfit + timestamps
      3. Sighting Timeline — every DVR sighting chronologically
      4. Reconciliation — side-by-side comparison by status category
      5. Visitors — visitor head count, per-camera, timeline
      6. Outfit Reconciliation — outfit tracking across sightings
      7. AI Observations — automated intelligence notes
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
    teacher_detail = recon.get("teacher_detail", [])
    visitor_count = recon.get("visitor_count", 0)
    visitor_sightings = recon.get("visitor_sightings", [])
    visitor_by_camera = recon.get("visitor_by_camera", {})

    fully_verified = side_by_side.get("both_present", [])
    trueface_only = side_by_side.get("trueface_only", [])
    dvr_only = side_by_side.get("dvr_only", [])
    absent = side_by_side.get("neither", [])

    # ── Sheet 1: Summary ──
    ws = wb.active
    ws.title = "Summary"

    ws.merge_cells("A1:D1")
    ws["A1"] = "════════ SCHOOL HEADCOUNT RECONCILIATION ════════"
    ws["A1"].font = Font(bold=True, size=14)

    ws.merge_cells("A2:D2")
    ws["A2"] = f"PP International School — {date_str}"
    ws["A2"].font = Font(bold=True, size=11)

    # Compute overall campus headcount
    teachers_detected = recon["trueface_identified"] + len(dvr_only)
    total_people = teachers_detected + visitor_count

    r = 4
    summary_rows = [
        ("════════ OVERALL CAMPUS HEADCOUNT ════════", "", None),
        ("Total People Detected", total_people, light_blue_fill),
        ("Gate Entries (IN)", recon["total_gate_in"], None),
        ("Gate Exits (OUT)", recon["total_gate_out"], None),
        ("", "", None),
        ("════════ CATEGORY BREAKDOWN ════════", "", None),
        ("Teachers / Staff", teachers_detected, None),
        ("  — TrueFace Recognized", recon["trueface_identified"], None),
        ("  — DVR Cameras Only", recon.get("dvr_sighted", 0), None),
        ("Visitors / Parents / Vendors", visitor_count, orange_fill),
        ("", "", None),
        ("════════ TEACHER RECONCILIATION ════════", "", None),
        ("✓ Fully Verified (TrueFace + DVR)", len(fully_verified), green_fill),
        ("⚠ Entry Only (DVR — Not on TrueFace)", len(dvr_only), red_fill),
        ("⚠ TrueFace Only (No DVR Sighting)", len(trueface_only), yellow_fill),
        ("— Not Detected", len(absent), gray_fill),
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

    # ── Sheet 2: Verified Movements ──
    ws2 = wb.create_sheet("Verified Movements")

    ws2.merge_cells("A1:H1")
    ws2["A1"] = f"════════ VERIFIED MOVEMENTS — {date_str} ════════"
    ws2["A1"].font = Font(bold=True, size=14)

    headers2 = [
        "#", "Name", "Category", "Status",
        "Entry Gate (DVR)", "TrueFace 3000", "Cameras Seen",
        "Wearing",
    ]
    for col, h in enumerate(headers2, 1):
        cell = ws2.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    r = 4
    for i, t in enumerate(teacher_detail, 1):
        ws2.cell(row=r, column=1, value=i).border = border
        ws2.cell(row=r, column=2, value=t["name"]).border = border
        ws2.cell(row=r, column=3, value="Teacher").border = border

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

        dvr_info = t.get("dvr_first_seen", "—") or "—"
        if t.get("dvr_last_seen") and t["dvr_last_seen"] != t.get("dvr_first_seen"):
            dvr_info += f" → {t['dvr_last_seen']}"
        ws2.cell(row=r, column=5, value=dvr_info).border = border

        tf_info = "Recognized" if t["trueface_present"] else "Not Marked"
        if t.get("trueface_arrival"):
            tf_info = f"Recognized ({t['trueface_arrival']})"
        ws2.cell(row=r, column=6, value=tf_info).border = border

        ws2.cell(row=r, column=7,
                 value=", ".join(t.get("dvr_cameras", [])) or "—").border = border

        outfit_cell = ws2.cell(row=r, column=8, value=t.get("outfit_summary", "—"))
        outfit_cell.border = border
        outfit_cell.alignment = Alignment(wrap_text=True)
        r += 1

    for col_letter, w in [("A", 5), ("B", 25), ("C", 10), ("D", 22),
                           ("E", 20), ("F", 22), ("G", 35), ("H", 30)]:
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
    for t in teacher_detail:
        for det in t.get("dvr_sighting_details", []):
            all_sightings.append({"name": t["name"], "category": "Teacher", **det})
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

    # ── Sheet 5: Visitors ──
    ws5 = wb.create_sheet("Visitors")

    ws5.merge_cells("A1:D1")
    ws5["A1"] = f"════════ VISITOR TRACKING — {date_str} ════════"
    ws5["A1"].font = Font(bold=True, size=14)

    ws5["A3"] = "Total Visitors / Unknown Persons"
    ws5["A3"].font = Font(bold=True)
    ws5["A3"].border = border
    ws5["A3"].fill = orange_fill
    ws5["B3"] = visitor_count
    ws5["B3"].font = Font(bold=True, size=14)
    ws5["B3"].border = border
    ws5["B3"].fill = orange_fill

    # Camera breakdown
    cam_row = 5
    ws5.merge_cells(start_row=cam_row, start_column=1, end_row=cam_row, end_column=3)
    cam_title = ws5.cell(row=cam_row, column=1, value="Visitors by Camera")
    cam_title.font = Font(bold=True, size=12, color="FFFFFF")
    cam_title.fill = header_fill
    cam_title.border = border

    cam_row += 1
    for col, h in enumerate(["Camera", "Count"], 1):
        cell = ws5.cell(row=cam_row, column=col, value=h)
        cell.font = Font(bold=True)
        cell.fill = light_blue_fill
        cell.border = border

    for cam_name in sorted(visitor_by_camera.keys()):
        cam_visitors = visitor_by_camera[cam_name]
        cam_row += 1
        ws5.cell(row=cam_row, column=1, value=cam_name).border = border
        ws5.cell(row=cam_row, column=2, value=len(cam_visitors)).border = border

    # Visitor timeline
    cam_row += 2
    ws5.merge_cells(start_row=cam_row, start_column=1, end_row=cam_row, end_column=3)
    tl = ws5.cell(row=cam_row, column=1,
                  value=f"Visitor Timeline ({len(visitor_sightings)} sightings)")
    tl.font = Font(bold=True, size=12, color="FFFFFF")
    tl.fill = header_fill
    tl.border = border

    cam_row += 1
    for col, h in enumerate(["#", "Time", "Camera"], 1):
        cell = ws5.cell(row=cam_row, column=col, value=h)
        cell.font = Font(bold=True)
        cell.fill = light_blue_fill
        cell.border = border

    for idx, v in enumerate(visitor_sightings, 1):
        cam_row += 1
        ws5.cell(row=cam_row, column=1, value=idx).border = border
        ts = v.get("timestamp", "")
        time_part = ts.split(" ")[1] if " " in ts else ts
        ws5.cell(row=cam_row, column=2, value=time_part).border = border
        ws5.cell(row=cam_row, column=3, value=v.get("camera", "")).border = border

    ws5.column_dimensions["A"].width = 30
    ws5.column_dimensions["B"].width = 15
    ws5.column_dimensions["C"].width = 40

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
    for t in teacher_detail:
        outfit_obs = t.get("outfit_observations", [])
        if not outfit_obs:
            continue
        idx += 1
        # Aggregate outfits
        outfit_desc = t.get("outfit_summary", "—")
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

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _generate_ai_observations(recon: dict) -> list[str]:
    """Generate AI-powered observations from the reconciliation data."""
    observations = []
    side_by_side = recon.get("side_by_side", {})
    teacher_detail = recon.get("teacher_detail", [])
    visitor_count = recon.get("visitor_count", 0)
    visitor_sightings = recon.get("visitor_sightings", [])

    dvr_only = side_by_side.get("dvr_only", [])
    trueface_only = side_by_side.get("trueface_only", [])
    fully_verified = side_by_side.get("both_present", [])
    absent = side_by_side.get("neither", [])

    # Unrecognized / mismatched alerts
    if dvr_only:
        observations.append(
            f"{len(dvr_only)} teacher(s) seen on DVR cameras but NOT marked "
            f"on TrueFace 3000. They may have bypassed facial recognition."
        )
    if trueface_only:
        observations.append(
            f"{len(trueface_only)} teacher(s) marked on TrueFace but NOT spotted "
            f"on any DVR camera. Possible camera blind spot or delayed sync."
        )

    # Visitor alerts
    if visitor_count > 0:
        observations.append(
            f"{visitor_count} unknown person(s) / visitor(s) detected on gate/reception cameras."
        )

    # Crowd density analysis from sighting timeline
    from collections import Counter
    hourly_counts: Counter = Counter()
    for t in teacher_detail:
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
            f"Peak activity at {peak_hour[0]:02d}:00 hour with {peak_hour[1]} DVR sightings."
        )

    # Attendance summary
    total = recon.get("total_registered_teachers", 0)
    if total > 0:
        present = len(fully_verified) + len(trueface_only) + len(dvr_only)
        rate = round(present / total * 100, 1)
        observations.append(
            f"Staff/teacher detection rate: {rate}% ({present} detected out of {total})."
        )
        if len(absent) > 10:
            observations.append(
                f"{len(absent)} staff not detected on any system today."
            )

    # Outfit tracking completeness
    outfit_tracked = sum(1 for t in teacher_detail
                         if t.get("outfit_observations"))
    dvr_seen = sum(1 for t in teacher_detail if t.get("dvr_seen"))
    if dvr_seen > 0:
        observations.append(
            f"Outfit/clothing tracked for {outfit_tracked} of {dvr_seen} "
            f"DVR-detected staff."
        )

    if not observations:
        observations.append("No significant observations for this period.")

    return observations


# ============================================================
# Hourly Reconciliation Report (7 AM - 5 PM IST)
# ============================================================

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
    visitor_count = recon.get("visitor_count", 0)
    if recon["trueface_identified"] == 0 and recon.get("dvr_sighted", 0) == 0 and visitor_count == 0:
        logger.info("[GATE] No TrueFace, DVR, or visitor records for %s — skipping report", today)
        return

    # Generate Excel
    xlsx_bytes = _generate_reconciliation_excel(recon)
    filename = f"Head_Count_Reconciliation_{today}_{now.strftime('%H%M')}.xlsx"

    # Email report
    fully_verified = side_by_side.get("both_present", [])
    tf_only_list = side_by_side.get("trueface_only", [])
    dvr_only_list = side_by_side.get("dvr_only", [])
    absent_list = side_by_side.get("neither", [])
    teacher_detail = recon.get("teacher_detail", [])
    visitor_by_camera = recon.get("visitor_by_camera", {})

    teachers_detected = recon["trueface_identified"] + len(dvr_only_list)
    total_people = teachers_detected + visitor_count

    body = (
        f"════════ SCHOOL HEADCOUNT RECONCILIATION ════════\n\n"
        f"DATE: {today_display}\n"
        f"TIME: {time_display} IST\n\n"
        f"════════ OVERALL CAMPUS HEADCOUNT ════════\n"
        f"Total People Detected: {total_people}\n"
        f"Gate Entries (IN): {recon['total_gate_in']}\n"
        f"Gate Exits (OUT): {recon['total_gate_out']}\n\n"
        f"════════ CATEGORY BREAKDOWN ════════\n"
        f"Teachers / Staff: {teachers_detected}\n"
        f"  — TrueFace Recognized: {recon['trueface_identified']}\n"
        f"  — DVR Cameras Only: {recon.get('dvr_sighted', 0)}\n"
        f"Visitors / Parents / Vendors: {visitor_count}\n\n"
        f"════════ TEACHER RECONCILIATION ════════\n"
        f"✓ Fully Verified (TrueFace + DVR): {len(fully_verified)}\n"
        f"⚠ Entry Only (DVR — Not on TrueFace): {len(dvr_only_list)}\n"
        f"⚠ TrueFace Only (No DVR Sighting): {len(tf_only_list)}\n"
        f"— Not Detected: {len(absent_list)}\n\n"
    )

    # Verified movements
    if fully_verified:
        body += "════════ VERIFIED MOVEMENTS ════════\n"
        for t in fully_verified:
            cams = ", ".join(t.get("dvr_cameras", []))
            outfit = t.get("outfit_summary", "—")
            body += (
                f"• {t['name']} — Teacher\n"
                f"  Entry Gate: {t.get('dvr_first_seen', '—')}\n"
                f"  TrueFace: Recognized ({t.get('trueface_arrival', '—')})\n"
                f"  Cameras: {cams}\n"
                f"  Wearing: {outfit}\n\n"
            )

    # Entry only (DVR but not TrueFace)
    if dvr_only_list:
        body += "════════ ENTRY ONLY (DVR — NOT MARKED) ════════\n"
        for t in dvr_only_list:
            cams = ", ".join(t.get("dvr_cameras", []))
            outfit = t.get("outfit_summary", "—")
            body += (
                f"• {t['name']} — Teacher\n"
                f"  DVR: {t.get('dvr_first_seen', '—')}\n"
                f"  Cameras: {cams}\n"
                f"  Wearing: {outfit}\n"
                f"  ⚠ NOT marked on TrueFace 3000\n\n"
            )

    # TrueFace only
    if tf_only_list:
        body += "════════ TRUEFACE ONLY (No DVR Sighting) ════════\n"
        for t in tf_only_list:
            body += (
                f"• {t['name']} — Teacher\n"
                f"  TrueFace: {t.get('trueface_arrival', '—')}\n"
                f"  ⚠ Not spotted on any DVR camera\n\n"
            )

    # Visitor tracking
    body += (
        f"════════ VISITOR TRACKING ════════\n"
        f"Total Visitors / Unknown: {visitor_count}\n"
    )
    if visitor_by_camera:
        for cam_name in sorted(visitor_by_camera.keys()):
            body += f"  • {cam_name}: {len(visitor_by_camera[cam_name])}\n"
    body += "\n"

    # Outfit reconciliation
    dvr_seen = [t for t in teacher_detail if t["dvr_seen"] and t.get("outfit_observations")]
    if dvr_seen:
        body += "════════ OUTFIT RECONCILIATION ════════\n"
        for t in dvr_seen:
            outfit = t.get("outfit_summary", "—")
            count = t.get("dvr_sighting_count", 0)
            body += f"• {t['name']} — {outfit} ({count} sightings)\n"
        body += "\n"

    # AI observations
    ai_obs = _generate_ai_observations(recon)
    body += "════════ AI OBSERVATIONS ════════\n"
    for obs in ai_obs:
        body += f"• {obs}\n"
    body += "\n"

    body += "— PPIS Headcount Reconciliation & Entry Monitoring System"

    from app.services.email_service import send_email_async
    recipients = [r.strip() for r in REPORT_RECIPIENTS.split(",") if r.strip()]
    for email in recipients:
        ok = await send_email_async(
            email,
            f"Hourly Head Count Reconciliation — {today_display} {time_display} IST",
            body,
            "PP International School",
            attachments=[(filename, xlsx_bytes)],
        )
        logger.info("[GATE] Reconciliation report → %s: %s", email, "OK" if ok else "FAILED")

    logger.info(
        "[GATE] Reconciliation report sent at %s: People=%d, Teachers=%d, Visitors=%d, Verified=%d",
        time_display, total_people, teachers_detected, visitor_count,
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
