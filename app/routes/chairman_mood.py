"""
Chairman Mood & Temperament API
================================
Receives mood observations from the campus agent, stores them,
and generates daily mood/temperament reports at 12:00 PM IST.
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request

logger = logging.getLogger("chairman_mood")

IST = timezone(timedelta(hours=5, minutes=30))
router = APIRouter()

REPORT_EMAIL = os.environ.get("MOOD_REPORT_EMAIL", "alisha.kanwar@ppischool.in")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_db():
    from app.database import get_db
    return await get_db()


# ---------------------------------------------------------------------------
# POST /api/chairman/mood — receive mood observation from campus agent
# ---------------------------------------------------------------------------

@router.post("/api/chairman/mood")
async def receive_mood(request: Request):
    """Receive a single mood observation from the campus agent."""
    data = await request.json()
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")

    timestamp = data.get("timestamp", now.strftime("%Y-%m-%d %H:%M:%S"))
    camera = data.get("camera", "unknown")
    dominant_emotion = data.get("dominant_emotion", "unknown")
    emotions = data.get("emotions", {})
    temperament = data.get("temperament", "neutral")
    intensity = data.get("intensity", 0.0)
    face_distance = data.get("face_distance", 0.0)
    face_confidence = data.get("face_confidence", 0.0)
    face_crop = data.get("face_crop", "")

    db = await _get_db()
    try:
        await db.execute(
            "INSERT INTO chairman_mood_log "
            "(date, timestamp, camera, dominant_emotion, emotions_json, "
            "temperament, intensity, face_distance, face_confidence, face_crop) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                today, timestamp, camera, dominant_emotion,
                json.dumps(emotions), temperament, intensity,
                face_distance, face_confidence, face_crop,
            ),
        )
        await db.commit()
    finally:
        await db.close()

    logger.info(
        "[MOOD] Logged: %s on %s — %s (%s) intensity=%.1f",
        timestamp, camera, dominant_emotion, temperament, intensity,
    )

    return {"status": "ok", "emotion": dominant_emotion, "temperament": temperament}


# ---------------------------------------------------------------------------
# GET /api/chairman/mood/today — today's mood summary
# ---------------------------------------------------------------------------

@router.get("/api/chairman/mood/today")
async def today_mood():
    """Get today's mood observations and summary."""
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")

    db = await _get_db()
    try:
        cur = await db.execute(
            "SELECT timestamp, camera, dominant_emotion, temperament, intensity "
            "FROM chairman_mood_log WHERE date = ? ORDER BY timestamp",
            (today,),
        )
        rows = await cur.fetchall()
    finally:
        await db.close()

    observations = [
        {
            "timestamp": r[0],
            "camera": r[1],
            "emotion": r[2],
            "temperament": r[3],
            "intensity": r[4],
        }
        for r in rows
    ]

    # Compute summary
    if observations:
        emotions = [o["emotion"] for o in observations]
        temperaments = [o["temperament"] for o in observations]
        emotion_counts = Counter(emotions)
        temperament_counts = Counter(temperaments)
        total = len(observations)

        emotion_pct = {k: round(v / total * 100, 1) for k, v in emotion_counts.items()}
        temperament_pct = {k: round(v / total * 100, 1) for k, v in temperament_counts.items()}
        avg_intensity = sum(o["intensity"] for o in observations) / total

        dominant_mood = emotion_counts.most_common(1)[0][0]
        dominant_temperament = temperament_counts.most_common(1)[0][0]
    else:
        emotion_pct = {}
        temperament_pct = {}
        avg_intensity = 0.0
        dominant_mood = "no data"
        dominant_temperament = "no data"

    return {
        "date": today,
        "total_observations": len(observations),
        "dominant_mood": dominant_mood,
        "dominant_temperament": dominant_temperament,
        "avg_intensity": round(avg_intensity, 1),
        "emotion_distribution": emotion_pct,
        "temperament_distribution": temperament_pct,
        "observations": observations,
    }


# ---------------------------------------------------------------------------
# GET /api/chairman/mood/report/{date} — full report data
# ---------------------------------------------------------------------------

@router.get("/api/chairman/mood/report/{date}")
async def mood_report(date: str):
    """Get full mood report data for a given date."""
    db = await _get_db()
    try:
        cur = await db.execute(
            "SELECT timestamp, camera, dominant_emotion, emotions_json, "
            "temperament, intensity, face_confidence "
            "FROM chairman_mood_log WHERE date = ? ORDER BY timestamp",
            (date,),
        )
        rows = await cur.fetchall()
    finally:
        await db.close()

    observations = []
    for r in rows:
        emotions = json.loads(r[3]) if r[3] else {}
        observations.append({
            "timestamp": r[0],
            "camera": r[1],
            "dominant_emotion": r[2],
            "emotions": emotions,
            "temperament": r[4],
            "intensity": r[5],
            "face_confidence": r[6],
        })

    if not observations:
        return {"date": date, "total_observations": 0, "summary": "No data"}

    # Hourly breakdown
    hourly: dict[str, list] = {}
    for obs in observations:
        try:
            hour = obs["timestamp"].split(" ")[1][:2] + ":00"
        except (IndexError, AttributeError):
            hour = "unknown"
        hourly.setdefault(hour, []).append(obs)

    hourly_summary = {}
    for hour, obs_list in sorted(hourly.items()):
        emotions = [o["dominant_emotion"] for o in obs_list]
        temperaments = [o["temperament"] for o in obs_list]
        hourly_summary[hour] = {
            "observations": len(obs_list),
            "dominant_emotion": Counter(emotions).most_common(1)[0][0],
            "dominant_temperament": Counter(temperaments).most_common(1)[0][0],
            "avg_intensity": round(
                sum(o["intensity"] for o in obs_list) / len(obs_list), 1
            ),
        }

    # Overall
    all_emotions = [o["dominant_emotion"] for o in observations]
    all_temperaments = [o["temperament"] for o in observations]
    total = len(observations)
    emotion_counts = Counter(all_emotions)
    temperament_counts = Counter(all_temperaments)

    # Best time to approach (lowest intensity hour)
    best_hour = min(hourly_summary, key=lambda h: hourly_summary[h]["avg_intensity"])

    return {
        "date": date,
        "total_observations": total,
        "dominant_mood": emotion_counts.most_common(1)[0][0],
        "dominant_temperament": temperament_counts.most_common(1)[0][0],
        "avg_intensity": round(sum(o["intensity"] for o in observations) / total, 1),
        "emotion_distribution": {
            k: round(v / total * 100, 1) for k, v in emotion_counts.items()
        },
        "temperament_distribution": {
            k: round(v / total * 100, 1) for k, v in temperament_counts.items()
        },
        "hourly_summary": hourly_summary,
        "best_time_to_approach": best_hour,
    }


# ---------------------------------------------------------------------------
# Daily Report (called by scheduler at 12:00 PM IST)
# ---------------------------------------------------------------------------

def _generate_mood_excel(report: dict) -> bytes:
    """Generate an Excel report for the chairman's mood data."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()

    # --- Summary sheet ---
    ws = wb.active
    ws.title = "Summary"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2F5496")
    border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    ws.cell(row=1, column=1, value="Chairman Mood Report").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=f"Date: {report['date']}")
    ws.cell(row=3, column=1, value=f"Total Observations: {report['total_observations']}")
    ws.cell(row=4, column=1, value=f"Dominant Mood: {report['dominant_mood']}")
    ws.cell(row=5, column=1, value=f"Dominant Temperament: {report['dominant_temperament']}")
    ws.cell(row=6, column=1, value=f"Avg Expression Intensity: {report['avg_intensity']}%")
    ws.cell(row=7, column=1, value=f"Best Time to Approach: {report.get('best_time_to_approach', 'N/A')}")

    # Emotion distribution
    row = 9
    ws.cell(row=row, column=1, value="Emotion").font = Font(bold=True)
    ws.cell(row=row, column=2, value="Percentage").font = Font(bold=True)
    for emo, pct in sorted(report.get("emotion_distribution", {}).items(), key=lambda x: -x[1]):
        row += 1
        ws.cell(row=row, column=1, value=emo).border = border
        ws.cell(row=row, column=2, value=f"{pct}%").border = border

    # Temperament distribution
    row += 2
    ws.cell(row=row, column=1, value="Temperament").font = Font(bold=True)
    ws.cell(row=row, column=2, value="Percentage").font = Font(bold=True)
    for temp, pct in sorted(report.get("temperament_distribution", {}).items(), key=lambda x: -x[1]):
        row += 1
        ws.cell(row=row, column=1, value=temp).border = border
        ws.cell(row=row, column=2, value=f"{pct}%").border = border

    ws.column_dimensions["A"].width = 25
    ws.column_dimensions["B"].width = 15

    # --- Hourly breakdown sheet ---
    ws2 = wb.create_sheet("Hourly Breakdown")
    headers = ["Hour", "Observations", "Dominant Mood", "Dominant Temperament", "Avg Intensity"]
    for col, h in enumerate(headers, 1):
        cell = ws2.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border

    row = 2
    for hour, data in sorted(report.get("hourly_summary", {}).items()):
        ws2.cell(row=row, column=1, value=hour).border = border
        ws2.cell(row=row, column=2, value=data["observations"]).border = border
        ws2.cell(row=row, column=3, value=data["dominant_emotion"]).border = border
        ws2.cell(row=row, column=4, value=data["dominant_temperament"]).border = border
        ws2.cell(row=row, column=5, value=f"{data['avg_intensity']}%").border = border
        row += 1

    for col_letter in ["A", "B", "C", "D", "E"]:
        ws2.column_dimensions[col_letter].width = 20

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def send_daily_mood_report():
    """Generate and send the daily mood report at 12:00 PM IST."""
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    today_display = now.strftime("%d-%m-%Y")

    # Fetch report data via the API logic
    db = await _get_db()
    try:
        cur = await db.execute(
            "SELECT COUNT(*) FROM chairman_mood_log WHERE date = ?", (today,),
        )
        row = await cur.fetchone()
        count = row[0] if row else 0
    finally:
        await db.close()

    if count == 0:
        logger.info("[MOOD] No mood observations for %s — skipping report", today)
        return

    # Build report using the report endpoint logic
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://ppis-whatsapp-bot.fly.dev/api/chairman/mood/report/{today}"
            )
            report = resp.json()
    except Exception as e:
        logger.error("[MOOD] Failed to fetch report data: %s", e)
        return

    if report.get("total_observations", 0) == 0:
        logger.info("[MOOD] No observations in report for %s", today)
        return

    # Generate Excel
    xlsx_bytes = _generate_mood_excel(report)
    filename = f"Chairman_Mood_Report_{today}.xlsx"

    # Email report
    body = (
        f"Chairman Mood & Temperament Report — {today_display}\n\n"
        f"Total Observations: {report['total_observations']}\n"
        f"Dominant Mood: {report['dominant_mood']}\n"
        f"Dominant Temperament: {report['dominant_temperament']}\n"
        f"Avg Expression Intensity: {report['avg_intensity']}%\n"
        f"Best Time to Approach: {report.get('best_time_to_approach', 'N/A')}\n\n"
        f"Emotion Breakdown:\n"
    )
    for emo, pct in sorted(report.get("emotion_distribution", {}).items(), key=lambda x: -x[1]):
        body += f"  {emo}: {pct}%\n"
    body += f"\n— PPIS Chairman Mood Monitor"

    from app.services.email_service import send_email_async
    recipients = [r.strip() for r in REPORT_EMAIL.split(",") if r.strip()]
    for email in recipients:
        ok = await send_email_async(
            email,
            f"Chairman Mood Report — {today_display}",
            body,
            "PP International School",
            attachments=[(filename, xlsx_bytes)],
        )
        logger.info("[MOOD] Report → %s: %s", email, "OK" if ok else "FAILED")

    logger.info(
        "[MOOD] Daily report sent: %d observations, mood=%s, temperament=%s",
        report["total_observations"], report["dominant_mood"], report["dominant_temperament"],
    )


def send_daily_mood_report_sync():
    """Sync wrapper for scheduler."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(send_daily_mood_report())
        else:
            loop.run_until_complete(send_daily_mood_report())
    except RuntimeError:
        asyncio.run(send_daily_mood_report())
