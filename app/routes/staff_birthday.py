"""Staff birthday poster endpoints: preview, upcoming list and manual run."""

import logging
from datetime import date, datetime, time

from fastapi import APIRouter, Query

from app.services import staff_birthday_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/birthday/staff", tags=["staff-birthday"])


@router.get("/today")
async def birthdays_today() -> dict:
    """Who is being wished today, and who needs manual attention."""
    summary = await staff_birthday_service.send_birthday_wishes(dry_run=True)
    return summary


@router.get("/upcoming")
async def birthdays_upcoming(days: int = Query(30, ge=1, le=366)) -> dict:
    entries = staff_birthday_service.upcoming(days)
    return {
        "days": days,
        "count": len(entries),
        "birthdays": [
            {
                "name": entry["name"],
                "on": entry["on"],
                "designation": entry["designation"],
                "poster": entry["poster"],
                "phone": entry["phone"],
                "blocking_reason": entry["blocking_reason"],
                "note": entry["note"],
            }
            for entry in entries
        ],
    }


@router.post("/send")
async def send_birthday_wishes(
    dry_run: bool = Query(False),
    on: str = Query("", description="Override date as YYYY-MM-DD (IST)"),
) -> dict:
    """Run today's birthday send now (idempotent per staff member per day)."""
    now = None
    if on:
        try:
            parsed = date.fromisoformat(on)
        except ValueError:
            return {"error": f"Invalid date {on!r}, expected YYYY-MM-DD"}
        now = datetime.combine(
            parsed, time(9, 0), tzinfo=staff_birthday_service.IST
        )
    return await staff_birthday_service.send_birthday_wishes(
        now=now, dry_run=dry_run
    )
