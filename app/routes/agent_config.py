"""
Cloud-hosted agent configuration API.

Stores DVR details, camera mappings, and agent settings in the cloud
SQLite database. The Campus Agent fetches its config from here on startup
and receives live updates via WebSocket push.
"""

import json
import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent-config", tags=["agent-config"])

AGENT_SECRET = os.environ.get("AGENT_SECRET", "")


async def verify_agent_secret(x_agent_secret: str = Header("")) -> None:
    """Dependency that verifies the agent secret header on all config endpoints."""
    if not AGENT_SECRET:
        return  # No secret configured — skip auth
    if x_agent_secret != AGENT_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing agent secret")


# ---------------------------------------------------------------------------
# DVR endpoints
# ---------------------------------------------------------------------------

@router.get("/dvrs", dependencies=[Depends(verify_agent_secret)])
async def list_dvrs():
    """Return all DVR entries."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, ip, port, username, password, channels FROM agent_dvrs ORDER BY id"
        )
        rows = await cursor.fetchall()
        dvrs = [dict(r) for r in rows]
        return {"dvrs": dvrs, "count": len(dvrs)}
    finally:
        await db.close()


@router.post("/dvrs", dependencies=[Depends(verify_agent_secret)])
async def save_dvrs(request: Request):
    """Replace all DVRs with the provided list and push to connected agent."""
    body = await request.json()
    dvrs = body.get("dvrs", [])
    db = await get_db()
    try:
        await db.execute("DELETE FROM agent_dvrs")
        for dvr in dvrs:
            await db.execute(
                "INSERT INTO agent_dvrs (name, ip, port, username, password, channels) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dvr.get("name", ""),
                    dvr.get("ip", ""),
                    dvr.get("port", 80),
                    dvr.get("username", "admin"),
                    dvr.get("password", ""),
                    dvr.get("channels", 64),
                ),
            )
        await db.commit()
        logger.info(f"Saved {len(dvrs)} DVRs to cloud DB")

        # Push to connected agent if available
        from app.routes.agent_ws import push_dvrs
        push_result = await push_dvrs(dvrs)
        agent_pushed = push_result.get("success", False)

        return {"status": "ok", "dvr_count": len(dvrs), "agent_pushed": agent_pushed}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Camera mapping endpoints
# ---------------------------------------------------------------------------

@router.get("/camera-mapping", dependencies=[Depends(verify_agent_secret)])
async def get_camera_mapping():
    """Return the full camera mapping."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT location, dvr_index, channel, description, cam_type, all_cameras "
            "FROM agent_camera_mapping"
        )
        rows = await cursor.fetchall()
        mapping = {}
        for r in rows:
            entry = {
                "dvr_index": r["dvr_index"],
                "channel": r["channel"],
                "description": r["description"],
            }
            if r["cam_type"]:
                entry["cam_type"] = r["cam_type"]
            if r["all_cameras"]:
                try:
                    entry["all_cameras"] = json.loads(r["all_cameras"])
                except json.JSONDecodeError:
                    pass
            mapping[r["location"]] = entry
        return {"camera_mapping": mapping, "count": len(mapping)}
    finally:
        await db.close()


@router.post("/camera-mapping", dependencies=[Depends(verify_agent_secret)])
async def save_camera_mapping(request: Request):
    """Replace camera mapping with the provided dict."""
    body = await request.json()
    mapping = body.get("camera_mapping", {})
    if not mapping:
        return JSONResponse({"error": "No camera_mapping provided"}, status_code=400)

    db = await get_db()
    try:
        await db.execute("DELETE FROM agent_camera_mapping")
        for location, data in mapping.items():
            all_cameras = data.get("all_cameras")
            await db.execute(
                "INSERT OR REPLACE INTO agent_camera_mapping "
                "(location, dvr_index, channel, description, cam_type, all_cameras) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    location,
                    data.get("dvr_index", 0),
                    data.get("channel", 1),
                    data.get("description", ""),
                    data.get("cam_type", ""),
                    json.dumps(all_cameras) if all_cameras else None,
                ),
            )
        await db.commit()
        logger.info(f"Saved {len(mapping)} camera mappings to cloud DB")

        # Push to connected agent if available
        from app.routes.agent_ws import push_camera_mapping
        push_result = await push_camera_mapping(mapping)
        agent_pushed = push_result.get("success", False)

        return {
            "status": "ok",
            "mappings_saved": len(mapping),
            "agent_pushed": agent_pushed,
        }
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Agent settings endpoints
# ---------------------------------------------------------------------------

@router.get("/settings", dependencies=[Depends(verify_agent_secret)])
async def get_agent_settings():
    """Return all agent settings."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT key, value FROM agent_settings")
        rows = await cursor.fetchall()
        settings = {r["key"]: r["value"] for r in rows}
        return {"settings": settings}
    finally:
        await db.close()


@router.post("/settings", dependencies=[Depends(verify_agent_secret)])
async def save_agent_settings(request: Request):
    """Save/update agent settings."""
    body = await request.json()
    settings = body.get("settings", {})
    db = await get_db()
    try:
        for key, value in settings.items():
            await db.execute(
                "INSERT OR REPLACE INTO agent_settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        await db.commit()
        return {"status": "ok", "keys_saved": list(settings.keys())}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Full config endpoint (used by Campus Agent on startup)
# ---------------------------------------------------------------------------

@router.get("/full", dependencies=[Depends(verify_agent_secret)])
async def get_full_config(request: Request):
    """Return the complete agent config (DVRs + camera mapping + settings).

    The Campus Agent calls this on startup to load its config from the cloud.
    The cloud_bot_url is derived from the request host so it always points
    to the correct app — no hardcoded URLs that go stale after redeployment.
    """
    db = await get_db()
    try:
        # DVRs
        cursor = await db.execute(
            "SELECT id, name, ip, port, username, password, channels FROM agent_dvrs ORDER BY id"
        )
        dvr_rows = await cursor.fetchall()
        dvrs = [dict(r) for r in dvr_rows]

        # Camera mapping
        cursor = await db.execute(
            "SELECT location, dvr_index, channel, description, cam_type, all_cameras "
            "FROM agent_camera_mapping"
        )
        mapping_rows = await cursor.fetchall()
        camera_mapping = {}
        for r in mapping_rows:
            entry = {
                "dvr_index": r["dvr_index"],
                "channel": r["channel"],
                "description": r["description"],
            }
            if r["cam_type"]:
                entry["cam_type"] = r["cam_type"]
            if r["all_cameras"]:
                try:
                    entry["all_cameras"] = json.loads(r["all_cameras"])
                except json.JSONDecodeError:
                    pass
            camera_mapping[r["location"]] = entry

        # Settings
        cursor = await db.execute("SELECT key, value FROM agent_settings")
        setting_rows = await cursor.fetchall()
        settings = {r["key"]: r["value"] for r in setting_rows}

        # Registered faces count (agent uses /api/face/images to download)
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT person_id) as count FROM agent_registered_faces"
        )
        face_row = await cursor.fetchone()
        registered_faces = face_row["count"] if face_row else 0

        # Derive cloud_bot_url from the request host so the agent always
        # connects back to THIS app, regardless of which Fly.io app URL
        # this code is deployed to.  Env-var override still supported.
        cloud_bot_url = os.environ.get("CLOUD_BOT_WS_URL", "")
        if not cloud_bot_url:
            host = request.headers.get("host", "")
            if host:
                cloud_bot_url = f"wss://{host}/ws/agent"
            else:
                cloud_bot_url = "wss://app-ypdweegy.fly.dev/ws/agent"

        return {
            "dvrs": dvrs,
            "camera_mapping": camera_mapping,
            "settings": settings,
            "registered_faces": registered_faces,
            "cloud_bot_url": cloud_bot_url,
        }
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Seed endpoint — populate from existing config.json data
# ---------------------------------------------------------------------------

@router.post("/seed", dependencies=[Depends(verify_agent_secret)])
async def seed_from_config(request: Request):
    """Seed the cloud DB from a config.json payload (one-time migration)."""
    body = await request.json()
    dvrs = body.get("dvrs", [])
    camera_mapping = body.get("camera_mapping", {})

    db = await get_db()
    try:
        # Seed DVRs
        await db.execute("DELETE FROM agent_dvrs")
        for dvr in dvrs:
            await db.execute(
                "INSERT INTO agent_dvrs (name, ip, port, username, password, channels) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dvr.get("name", ""),
                    dvr.get("ip", ""),
                    dvr.get("port", 80),
                    dvr.get("username", "admin"),
                    dvr.get("password", ""),
                    dvr.get("channels", 64),
                ),
            )

        # Seed camera mapping
        await db.execute("DELETE FROM agent_camera_mapping")
        for location, data in camera_mapping.items():
            all_cameras = data.get("all_cameras")
            await db.execute(
                "INSERT OR REPLACE INTO agent_camera_mapping "
                "(location, dvr_index, channel, description, cam_type, all_cameras) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    location,
                    data.get("dvr_index", 0),
                    data.get("channel", 1),
                    data.get("description", ""),
                    data.get("cam_type", ""),
                    json.dumps(all_cameras) if all_cameras else None,
                ),
            )

        await db.commit()
        logger.info(f"Seeded cloud DB: {len(dvrs)} DVRs, {len(camera_mapping)} camera mappings")
        return {
            "status": "ok",
            "dvrs_seeded": len(dvrs),
            "mappings_seeded": len(camera_mapping),
        }
    finally:
        await db.close()
