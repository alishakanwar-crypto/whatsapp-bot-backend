"""
Gate Head Count Reconciliation
==============================
Receives person-entry events and DVR teacher sightings from the campus
agent, stores them, and generates reconciliation reports comparing:
  1. Teacher-by-teacher list: each teacher's DVR sightings + TrueFace status
  2. Side-by-side comparison: DVR sightings vs TrueFace attendance

Endpoints:
    POST /api/gate/entry             — receive gate entry events
    POST /api/gate/teacher-sighting  — receive DVR teacher face sightings
    GET  /api/gate/status            — today's running totals
    GET  /api/gate/reconciliation/{date} — full reconciliation data

Scheduled:
    4:30 PM IST — EOD reconciliation report emailed + WhatsApp summary

All timestamps use IST (Asia/Kolkata, UTC+05:30).
"""

from __future__ import annotations

import asyncio
import io
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
    """Get total registered teachers count."""
    cur = await db.execute("SELECT COUNT(*) FROM trueface_teachers WHERE phone != ''")
    row = await cur.fetchone()
    return row[0] if row else 0


async def _get_all_teachers(db) -> list[dict]:
    """Get all registered teachers with phone numbers."""
    cur = await db.execute(
        "SELECT pin, name, phone FROM trueface_teachers WHERE phone != '' ORDER BY name"
    )
    return [{"pin": r[0], "name": r[1], "phone": r[2]} for r in await cur.fetchall()]


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

        await db.execute(
            "INSERT INTO teacher_dvr_sightings (date, timestamp, person_id, name, camera, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date_part, ts, s.get("person_id", ""), s.get("name", ""),
             s.get("camera", ""), s.get("confidence", 0.0)),
        )
        count += 1
    await db.commit()
    return count


async def _get_teacher_sightings(db, date: str) -> list[dict]:
    """Get all DVR teacher sightings for a date."""
    cur = await db.execute(
        "SELECT person_id, name, camera, timestamp, confidence "
        "FROM teacher_dvr_sightings WHERE date = ? ORDER BY name, timestamp",
        (date,),
    )
    return [
        {"person_id": r[0], "name": r[1], "camera": r[2],
         "timestamp": r[3], "confidence": r[4]}
        for r in await cur.fetchall()
    ]


# ============================================================
# Reconciliation Logic
# ============================================================

async def _reconcile(db, date: str) -> dict:
    """Perform head count reconciliation for a given date.

    Compares DVR teacher sightings vs TrueFace attendance records.
    Returns:
      - teacher_detail: per-teacher list with DVR sightings + TrueFace status
      - side_by_side: DVR sightings column vs TrueFace attendance column
      - gate summary: entry/exit counts
    """
    gate_in = await _get_gate_entries(db, date, direction="IN")
    gate_out = await _get_gate_entries(db, date, direction="OUT")
    trueface = await _get_trueface_attendance(db, date)
    all_teachers = await _get_all_teachers(db)
    total_teachers = len(all_teachers)
    dvr_sightings = await _get_teacher_sightings(db, date)

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
    for s in dvr_sightings:
        key = s["name"].upper().strip()
        dvr_by_person.setdefault(key, []).append(s)
        dvr_names[key] = s["name"]

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
        for s in dvr_list:
            cam = s["camera"]
            ts = s["timestamp"]
            time_part = ts.split(" ")[1] if " " in ts else ts
            if cam not in dvr_cameras:
                dvr_cameras.append(cam)
            dvr_times.append(time_part)

        display_name = dvr_names.get(name_upper, tf["name"] if tf else name_upper.title())

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
    }


def _reconciliation_status(trueface_present: bool, dvr_seen: bool) -> str:
    """Return a human-readable reconciliation status."""
    if trueface_present and dvr_seen:
        return "Confirmed (Both)"
    if trueface_present and not dvr_seen:
        return "TrueFace Only"
    if not trueface_present and dvr_seen:
        return "DVR Only — Not Marked"
    return "Absent"


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
    """Generate an Excel reconciliation report with teacher-by-teacher
    detail and side-by-side comparison sheets."""
    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5496")
    green_fill = PatternFill("solid", fgColor="C6EFCE")
    red_fill = PatternFill("solid", fgColor="FFC7CE")
    yellow_fill = PatternFill("solid", fgColor="FFEB9C")
    gray_fill = PatternFill("solid", fgColor="D9D9D9")
    border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    date_str = recon["date"]
    side_by_side = recon.get("side_by_side", {})
    teacher_detail = recon.get("teacher_detail", [])

    # --- Sheet 1: Summary ---
    ws = wb.active
    ws.title = "Summary"

    ws.merge_cells("A1:D1")
    ws["A1"] = f"PP International School — Head Count Reconciliation — {date_str}"
    ws["A1"].font = Font(bold=True, size=14)

    summary_data = [
        ("Total Registered Teachers", recon["total_registered_teachers"]),
        ("TrueFace Marked Present", recon["trueface_identified"]),
        ("Seen on DVR Cameras", recon.get("dvr_sighted", 0)),
        ("Confirmed (Both Systems)", len(side_by_side.get("both_present", []))),
        ("TrueFace Only", len(side_by_side.get("trueface_only", []))),
        ("DVR Only — Not Marked", len(side_by_side.get("dvr_only", []))),
        ("Absent (Neither)", len(side_by_side.get("neither", []))),
        ("Gate Entries (IN)", recon["total_gate_in"]),
        ("Gate Exits (OUT)", recon["total_gate_out"]),
    ]

    status_fills = {
        "Confirmed (Both Systems)": green_fill,
        "TrueFace Only": yellow_fill,
        "DVR Only — Not Marked": red_fill,
        "Absent (Neither)": gray_fill,
    }

    for i, (label, value) in enumerate(summary_data, start=3):
        ws[f"A{i}"] = label
        ws[f"A{i}"].font = Font(bold=True)
        ws[f"A{i}"].border = border
        ws[f"B{i}"] = value
        ws[f"B{i}"].font = Font(bold=True, size=12)
        ws[f"B{i}"].border = border
        if label in status_fills:
            ws[f"A{i}"].fill = status_fills[label]
            ws[f"B{i}"].fill = status_fills[label]

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 15

    # --- Sheet 2: Teacher-by-Teacher Detail ---
    ws2 = wb.create_sheet("Teacher Detail")

    headers2 = [
        "#", "Teacher Name", "Status",
        "TrueFace Arrival", "TrueFace Departure",
        "DVR Cameras Seen", "DVR First Seen", "DVR Last Seen",
        "DVR Sighting Count",
    ]
    for col, h in enumerate(headers2, 1):
        cell = ws2.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center")

    for i, t in enumerate(teacher_detail, start=2):
        ws2.cell(row=i, column=1, value=i - 1).border = border
        ws2.cell(row=i, column=2, value=t["name"]).border = border

        status_cell = ws2.cell(row=i, column=3, value=t["status"])
        status_cell.border = border
        if t["status"] == "Confirmed (Both)":
            status_cell.fill = green_fill
        elif t["status"] == "TrueFace Only":
            status_cell.fill = yellow_fill
        elif t["status"] == "DVR Only — Not Marked":
            status_cell.fill = red_fill
        else:
            status_cell.fill = gray_fill

        ws2.cell(row=i, column=4, value=t.get("trueface_arrival") or "—").border = border
        ws2.cell(row=i, column=5, value=t.get("trueface_departure") or "—").border = border
        ws2.cell(row=i, column=6, value=", ".join(t.get("dvr_cameras", [])) or "—").border = border
        ws2.cell(row=i, column=7, value=t.get("dvr_first_seen") or "—").border = border
        ws2.cell(row=i, column=8, value=t.get("dvr_last_seen") or "—").border = border
        ws2.cell(row=i, column=9, value=t.get("dvr_sighting_count", 0)).border = border

    for col_letter in ["A", "B", "C", "D", "E", "F", "G", "H", "I"]:
        ws2.column_dimensions[col_letter].width = 22

    # --- Sheet 3: Side-by-Side Comparison ---
    ws3 = wb.create_sheet("Side-by-Side")

    def _write_section(ws, start_row: int, title: str, teachers: list,
                       fill: PatternFill) -> int:
        ws.merge_cells(start_row=start_row, start_column=1,
                       end_row=start_row, end_column=5)
        title_cell = ws.cell(row=start_row, column=1,
                             value=f"{title} ({len(teachers)})")
        title_cell.font = Font(bold=True, size=12, color="FFFFFF")
        title_cell.fill = header_fill
        title_cell.border = border

        row = start_row + 1
        sub_headers = ["#", "Name", "TrueFace Arrival", "DVR First Seen", "DVR Cameras"]
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
                    value=t.get("dvr_first_seen") or "—").border = border
            ws.cell(row=row, column=5,
                    value=", ".join(t.get("dvr_cameras", [])) or "—").border = border

        return row + 2  # leave a blank row

    row = 1
    ws3.merge_cells("A1:E1")
    ws3["A1"] = f"Side-by-Side Comparison — {date_str}"
    ws3["A1"].font = Font(bold=True, size=14)
    row = 3

    row = _write_section(ws3, row, "✓ Confirmed (Both TrueFace + DVR)",
                         side_by_side.get("both_present", []), green_fill)
    row = _write_section(ws3, row, "⚠ TrueFace Only (Not seen on DVR)",
                         side_by_side.get("trueface_only", []), yellow_fill)
    row = _write_section(ws3, row, "✗ DVR Only (Seen but NOT marked present)",
                         side_by_side.get("dvr_only", []), red_fill)
    row = _write_section(ws3, row, "— Absent (Neither system)",
                         side_by_side.get("neither", []), gray_fill)

    for col_letter in ["A", "B", "C", "D", "E"]:
        ws3.column_dimensions[col_letter].width = 25

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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
    if recon["trueface_identified"] == 0 and recon.get("dvr_sighted", 0) == 0:
        logger.info("[GATE] No TrueFace or DVR records for %s — skipping report", today)
        return

    # Generate Excel
    xlsx_bytes = _generate_reconciliation_excel(recon)
    filename = f"Head_Count_Reconciliation_{today}_{now.strftime('%H%M')}.xlsx"

    # Email report
    both = len(side_by_side.get("both_present", []))
    tf_only = len(side_by_side.get("trueface_only", []))
    dvr_only = len(side_by_side.get("dvr_only", []))
    absent = len(side_by_side.get("neither", []))

    body = (
        f"Head Count Reconciliation — {today_display} at {time_display} IST\n\n"
        f"Registered Teachers: {recon['total_registered_teachers']}\n"
        f"TrueFace Marked Present: {recon['trueface_identified']}\n"
        f"Seen on DVR Cameras: {recon.get('dvr_sighted', 0)}\n\n"
        f"— Confirmed (Both): {both}\n"
        f"— TrueFace Only: {tf_only}\n"
        f"— DVR Only (Not Marked): {dvr_only}\n"
        f"— Absent (Neither): {absent}\n\n"
    )

    # Add DVR-only teachers to email body (important mismatches)
    if dvr_only_list := side_by_side.get("dvr_only", []):
        body += "⚠ Teachers seen on DVR but NOT marked present on TrueFace:\n"
        for t in dvr_only_list:
            cams = ", ".join(t.get("dvr_cameras", []))
            body += f"  • {t['name']} — seen at {t.get('dvr_first_seen', '?')} on {cams}\n"
        body += "\n"

    body += (
        f"Please find the detailed reconciliation report attached.\n\n"
        f"— PPIS Head Count Reconciliation System"
    )

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
        "[GATE] Reconciliation report sent at %s: TrueFace=%d, DVR=%d, Both=%d, Mismatches=%d",
        time_display,
        recon["trueface_identified"], recon.get("dvr_sighted", 0),
        both, dvr_only,
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
