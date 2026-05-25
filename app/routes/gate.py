"""
Gate Head Count Reconciliation
==============================
Receives person-entry events from the gate camera counter running
on the school PC, stores them, and generates an end-of-day
reconciliation report comparing gate head count vs TrueFace
face-recognition matches.

Endpoints:
    POST /api/gate/entry       — receive gate entry events (from campus agent)
    GET  /api/gate/status       — today's running totals
    GET  /api/gate/reconciliation/{date}  — reconciliation data for a date

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


# ============================================================
# Reconciliation Logic
# ============================================================

async def _reconcile(db, date: str) -> dict:
    """Perform head count reconciliation for a given date.

    Compares gate entry count (IN direction) vs TrueFace attendance count.
    Returns reconciliation summary.
    """
    gate_in = await _get_gate_entries(db, date, direction="IN")
    gate_out = await _get_gate_entries(db, date, direction="OUT")
    trueface = await _get_trueface_attendance(db, date)
    total_teachers = await _get_total_teachers(db)

    total_in = len(gate_in)
    total_out = len(gate_out)
    trueface_count = len(trueface)
    unreconciled = max(0, total_in - trueface_count)

    # Build timing trail: all gate entries with their details
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

    # TrueFace identified teachers
    identified = []
    for t in trueface:
        identified.append({
            "pin": t["pin"],
            "name": t["name"],
            "arrival_time": t["arrival_time"],
            "departure_time": t["departure_time"],
        })

    # Update daily summary
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
        "total_registered_teachers": total_teachers,
        "unreconciled_count": unreconciled,
        "timing_trail": timing_trail,
        "identified_teachers": identified,
    }


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


@router.get("/api/gate/status")
async def gate_status():
    """Get today's running gate head count totals."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    db = await _get_db()
    try:
        gate_in = await _get_gate_entries(db, today, direction="IN")
        gate_out = await _get_gate_entries(db, today, direction="OUT")
        trueface = await _get_trueface_attendance(db, today)
    finally:
        await db.close()

    return {
        "date": today,
        "gate_in": len(gate_in),
        "gate_out": len(gate_out),
        "trueface_identified": len(trueface),
        "unreconciled": max(0, len(gate_in) - len(trueface)),
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
    """Generate an Excel reconciliation report."""
    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5496")
    matched_fill = PatternFill("solid", fgColor="C6EFCE")
    unmatched_fill = PatternFill("solid", fgColor="FFC7CE")
    border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    date_str = recon["date"]

    # --- Sheet 1: Summary ---
    ws = wb.active
    ws.title = "Summary"

    ws.merge_cells("A1:D1")
    ws["A1"] = f"PP International School — Gate Reconciliation Report — {date_str}"
    ws["A1"].font = Font(bold=True, size=14)

    summary_data = [
        ("Total Gate Entries (IN)", recon["total_gate_in"]),
        ("Total Gate Exits (OUT)", recon["total_gate_out"]),
        ("TrueFace Identified", recon["trueface_identified"]),
        ("Total Registered Teachers", recon["total_registered_teachers"]),
        ("Unreconciled Entries", recon["unreconciled_count"]),
    ]

    for i, (label, value) in enumerate(summary_data, start=3):
        ws[f"A{i}"] = label
        ws[f"A{i}"].font = Font(bold=True)
        ws[f"B{i}"] = value
        ws[f"B{i}"].font = Font(bold=True, size=12)
        if label == "Unreconciled Entries" and value > 0:
            ws[f"B{i}"].fill = unmatched_fill

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 15

    # --- Sheet 2: Gate Entry Timing Trail ---
    ws2 = wb.create_sheet("Gate Entries")

    headers = ["#", "Time (IST)", "Camera", "Attire Color", "Reconciled", "Matched Teacher"]
    for col, h in enumerate(headers, 1):
        cell = ws2.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center")

    for i, entry in enumerate(recon["timing_trail"], start=2):
        ws2.cell(row=i, column=1, value=i - 1).border = border
        ws2.cell(row=i, column=2, value=entry["time"]).border = border
        ws2.cell(row=i, column=3, value=entry["camera"]).border = border
        ws2.cell(row=i, column=4, value=entry["attire_color"]).border = border

        reconciled_cell = ws2.cell(row=i, column=5, value="Yes" if entry["reconciled"] else "No")
        reconciled_cell.border = border
        reconciled_cell.fill = matched_fill if entry["reconciled"] else unmatched_fill

        ws2.cell(row=i, column=6, value=entry.get("matched_pin", "")).border = border

    for col_letter in ["A", "B", "C", "D", "E", "F"]:
        ws2.column_dimensions[col_letter].width = 18

    # --- Sheet 3: TrueFace Identified Teachers ---
    ws3 = wb.create_sheet("TrueFace Identified")

    headers3 = ["#", "PIN", "Name", "Arrival Time", "Departure Time"]
    for col, h in enumerate(headers3, 1):
        cell = ws3.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center")

    for i, teacher in enumerate(recon["identified_teachers"], start=2):
        ws3.cell(row=i, column=1, value=i - 1).border = border
        ws3.cell(row=i, column=2, value=teacher["pin"]).border = border
        ws3.cell(row=i, column=3, value=teacher["name"]).border = border
        ws3.cell(row=i, column=4, value=teacher.get("arrival_time", "")).border = border
        ws3.cell(row=i, column=5, value=teacher.get("departure_time", "")).border = border

    for col_letter in ["A", "B", "C", "D", "E"]:
        ws3.column_dimensions[col_letter].width = 20

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ============================================================
# EOD Reconciliation Report
# ============================================================

async def send_reconciliation_report():
    """Generate and send the hourly gate reconciliation report (7 AM - 5 PM IST)."""
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    today_display = now.strftime("%d-%m-%Y")
    time_display = now.strftime("%I:%M %p")

    db = await _get_db()
    try:
        recon = await _reconcile(db, today)
    finally:
        await db.close()

    if recon["total_gate_in"] == 0 and recon["trueface_identified"] == 0:
        logger.info("[GATE] No gate entries or TrueFace records for %s — skipping report", today)
        return

    # Generate Excel
    xlsx_bytes = _generate_reconciliation_excel(recon)
    filename = f"Gate_Reconciliation_{today}_{now.strftime('%H%M')}.xlsx"

    # Email report
    body = (
        f"Gate Head Count Reconciliation — {today_display} at {time_display} IST\n\n"
        f"Gate Entries (IN): {recon['total_gate_in']}\n"
        f"Gate Exits (OUT): {recon['total_gate_out']}\n"
        f"TrueFace Identified: {recon['trueface_identified']}\n"
        f"Registered Teachers: {recon['total_registered_teachers']}\n"
        f"Unreconciled: {recon['unreconciled_count']}\n\n"
        f"Please find the detailed reconciliation report attached.\n\n"
        f"— PPIS Gate Reconciliation System"
    )

    from app.services.email_service import send_email_async
    recipients = [r.strip() for r in REPORT_RECIPIENTS.split(",") if r.strip()]
    for email in recipients:
        ok = await send_email_async(
            email,
            f"Gate Reconciliation Report — {today_display} {time_display} IST",
            body,
            "PP International School",
            attachments=[(filename, xlsx_bytes)],
        )
        logger.info("[GATE] Reconciliation report → %s: %s", email, "OK" if ok else "FAILED")

    # WhatsApp summary to chairman
    try:
        from app.services.whatsapp_service import send_whatsapp_message
        summary = (
            f"Gate Reconciliation — {today_display} {time_display} IST\n\n"
            f"Gate Entries: {recon['total_gate_in']}\n"
            f"TrueFace Identified: {recon['trueface_identified']}\n"
            f"Unreconciled: {recon['unreconciled_count']}\n\n"
            f"Detailed report sent to email."
        )
        await send_whatsapp_message(CHAIRMAN_PHONE, summary)
        logger.info("[GATE] WhatsApp summary sent to chairman")
    except Exception as e:
        logger.warning("[GATE] WhatsApp summary failed: %s", e)

    logger.info(
        "[GATE] Hourly report sent at %s: IN=%d, TrueFace=%d, Unreconciled=%d",
        time_display,
        recon["total_gate_in"], recon["trueface_identified"], recon["unreconciled_count"],
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
