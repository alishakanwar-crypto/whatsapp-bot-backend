"""
API routes for bulk messaging management.

Endpoints:
- POST /bulk/start  — Start a bulk send job
- POST /bulk/stop   — Stop the current bulk send
- GET  /bulk/status  — Get current bulk send status
"""

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, Request

from app.auth import require_admin

from app.services.bulk_service import (
    run_bulk_send,
    stop_bulk_send,
    get_status,
    get_bulk_state,
)
from app.services.sheet_refresh_service import fetch_and_update_teacher_data

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/bulk",
    tags=["bulk"],
    dependencies=[Depends(require_admin)],
)

# Default file paths
DEFAULT_PERSONALIZED = os.getenv("BULK_PERSONALIZED_FILE", "/data/personalized_parents.json")
DEFAULT_GENERIC = os.getenv("BULK_GENERIC_FILE", "/data/generic_parents.json")
DEFAULT_RESULTS = os.getenv("BULK_RESULTS_FILE", "/data/bulk_results.json")

# Fallback for local dev
if not os.path.exists(os.path.dirname(DEFAULT_PERSONALIZED)):
    DEFAULT_PERSONALIZED = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "personalized_parents.json")
    DEFAULT_GENERIC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "generic_parents.json")
    DEFAULT_RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "bulk_results.json")


@router.post("/start")
async def start_bulk_send(request: Request):
    """Start a bulk send job. Runs as a background task."""
    state = get_bulk_state()
    if state.is_running:
        return {
            "status": "error",
            "message": "Bulk send is already running",
            "current_status": get_status(),
        }

    try:
        body = await request.json()
    except Exception:
        body = {}

    personalized_file = body.get("personalized_file", DEFAULT_PERSONALIZED)
    generic_file = body.get("generic_file", DEFAULT_GENERIC)
    results_file = body.get("results_file", DEFAULT_RESULTS)
    instance_id = body.get("instance_id", os.getenv("GREEN_API_ID_INSTANCE", "7107575951"))
    api_token = body.get("api_token", os.getenv("GREEN_API_TOKEN", ""))
    api_base_url = body.get("api_base_url", os.getenv("GREEN_API_URL", "https://7107.api.greenapi.com"))
    respect_time_windows = body.get("respect_time_windows", True)
    sheet_filter = body.get("sheet_filter", None)  # e.g. "Nur 1" to send only to Nursery 1

    if not api_token:
        return {"status": "error", "message": "GREEN_API_TOKEN not configured"}

    if not os.path.exists(personalized_file):
        return {"status": "error", "message": f"Personalized parents file not found: {personalized_file}"}

    # Launch as background task
    asyncio.create_task(
        run_bulk_send(
            personalized_file=personalized_file,
            generic_file=generic_file,
            results_file=results_file,
            instance_id=instance_id,
            api_token=api_token,
            api_base_url=api_base_url,
            respect_time_windows=respect_time_windows,
            sheet_filter=sheet_filter,
        )
    )

    return {
        "status": "ok",
        "message": f"Bulk send started as background task{f' (filter: {sheet_filter})' if sheet_filter else ''}",
        "daily_cap": int(os.getenv("BULK_DAILY_CAP", "500")),
        "respect_time_windows": respect_time_windows,
        "sheet_filter": sheet_filter,
    }


@router.post("/stop")
async def stop_bulk(request: Request):
    """Stop the current bulk send job."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "Manual stop via API")

    state = get_bulk_state()
    if not state.is_running:
        return {"status": "error", "message": "No bulk send is currently running"}

    stop_bulk_send(reason)
    return {"status": "ok", "message": f"Stop signal sent. Reason: {reason}"}


@router.get("/status")
async def bulk_status():
    """Get current bulk send status."""
    return get_status()


@router.post("/refresh-teachers")
async def refresh_teachers():
    """Manually trigger a refresh of teacher data from Google Sheet."""
    success = await fetch_and_update_teacher_data()
    if success:
        from app.services.openai_service import TEACHER_DATA
        return {
            "status": "ok",
            "message": f"Teacher data refreshed. {len(TEACHER_DATA)} entries loaded.",
            "count": len(TEACHER_DATA),
        }
    return {"status": "error", "message": "Failed to refresh teacher data. Check logs."}
