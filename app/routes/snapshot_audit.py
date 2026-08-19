"""Live snapshot audit endpoints: daily failures, manual run and data re-sync."""

import logging
from datetime import datetime

from fastapi import APIRouter, Query

from app.database import get_db
from app.services.snapshot_audit_service import (
    FAILED_OUTCOMES,
    IST,
    OUTCOME_LABELS,
    run_daily_snapshot_audit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/snapshot/audit", tags=["snapshot-audit"])


@router.get("")
async def snapshot_audit(date: str = Query("", description="YYYY-MM-DD (IST)")) -> dict:
    """List today's (or a given day's) snapshot requests that were not delivered."""
    day = date or datetime.now(IST).strftime("%Y-%m-%d")
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT sender_phone, student_name, grade, location, outcome, "
            "reason, requested_at_ist, in_pi_sheet_cache "
            "FROM snapshot_request_audit "
            "WHERE request_date = ? AND outcome IN "
            f"({','.join('?' * len(FAILED_OUTCOMES))}) "
            "ORDER BY id DESC",
            (day, *FAILED_OUTCOMES),
        )
        failures = [
            {
                "sender_phone": row[0],
                "student_name": row[1],
                "grade": row[2],
                "location": row[3],
                "outcome": row[4],
                "outcome_label": OUTCOME_LABELS.get(row[4], row[4]),
                "reason": row[5],
                "requested_at_ist": row[6],
                "in_snapshot_access_cache": bool(row[7]),
            }
            for row in await cur.fetchall()
        ]
        totals_cur = await db.execute(
            "SELECT outcome, COUNT(*) FROM snapshot_request_audit "
            "WHERE request_date = ? GROUP BY outcome",
            (day,),
        )
        totals = {row[0]: row[1] for row in await totals_cur.fetchall()}
        return {"date": day, "totals": totals, "failures": failures}
    finally:
        await db.close()


@router.post("/run")
async def run_audit(date: str = Query("", description="YYYY-MM-DD (IST)")) -> dict:
    """Run the daily audit now (alerts admins if new failures exist)."""
    return await run_daily_snapshot_audit(report_date=date)


@router.post("/resync")
async def resync_student_data() -> dict:
    """Re-import every PI Sheet class tab into the saved student data."""
    from app.services.sheet_refresh_service import fetch_all_pi_sheet_tabs

    ok = await fetch_all_pi_sheet_tabs()
    db = await get_db()
    try:
        snap_cur = await db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT grade) FROM snapshot_access_students"
        )
        snapshot_rows, snapshot_grades = await snap_cur.fetchone()
        pi_cur = await db.execute("SELECT COUNT(*) FROM pi_sheet_students")
        pi_rows = (await pi_cur.fetchone())[0]
    finally:
        await db.close()
    return {
        "refreshed": ok,
        "snapshot_access_students": snapshot_rows,
        "snapshot_access_grades": snapshot_grades,
        "pi_sheet_students": pi_rows,
    }
