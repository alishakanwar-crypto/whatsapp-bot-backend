"""
TrueFace 3000 ADMS Protocol Integration
========================================
Receives attendance events from the TimeWatch TrueFace 3000 face recognition
terminal via the ZKTeco ADMS push protocol.

Protocol:
- Device polls GET /iclock/getrequest?SN=xxx → server responds "OK"
- Device pushes POST /iclock/cdata?SN=xxx&table=ATTLOG → attendance logs
- Device pushes POST /iclock/cdata?SN=xxx&table=OPERLOG → user operations
- Device confirms POST /iclock/devicecmd?SN=xxx → command acknowledgements

When an ATTLOG event is received, we look up the teacher by PIN and send
a WhatsApp notification confirming attendance was marked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger("app.trueface")

# IST timezone offset
IST = timezone(timedelta(hours=5, minutes=30))

router = APIRouter()

# PIN → teacher mapping file
TRUEFACE_USERS_FILE = Path(__file__).parent.parent / "trueface_users.json"

# Command queue for sending commands to device
_command_queue: list[str] = []

# Dedup: track who was already notified today
_notified_today: dict[str, str] = {}  # PIN → date string
_last_dedup_date: str = ""


def _load_users() -> dict[str, dict]:
    """Load PIN → teacher mapping.
    
    Format: {"1": {"name": "Alisha Ahuja", "phone": "918076455224"}, ...}
    """
    if TRUEFACE_USERS_FILE.exists():
        try:
            with open(TRUEFACE_USERS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load trueface_users.json: {e}")
    return {}


def _save_users(users: dict[str, dict]):
    """Save PIN → teacher mapping."""
    with open(TRUEFACE_USERS_FILE, "w") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)


def _clear_daily_dedup():
    """Reset dedup cache if date changed."""
    global _notified_today, _last_dedup_date
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today != _last_dedup_date:
        _notified_today = {}
        _last_dedup_date = today


def _parse_attlog(body: str) -> list[dict]:
    """Parse ATTLOG body from device.
    
    Format: PIN\\tTimestamp\\tStatus\\tVerify\\tWorkCode\\tReserved1\\tReserved2
    Example: 1\\t2026-05-25 07:30:00\\t0\\t15\\t0\\t1\\t0
    """
    records = []
    for line in body.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            records.append({
                "pin": parts[0].strip(),
                "timestamp": parts[1].strip() if len(parts) > 1 else "",
                "status": parts[2].strip() if len(parts) > 2 else "0",
                "verify": parts[3].strip() if len(parts) > 3 else "0",
            })
    return records


async def _send_teacher_whatsapp(name: str, phone: str, time_str: str):
    """Send WhatsApp notification to teacher confirming attendance."""
    from app.services.whatsapp_service import send_cloud_template_message

    display_name = name.title() if name == name.upper() else name
    logger.info(f"[TRUEFACE] Sending WhatsApp to {phone} for {display_name} at {time_str}")

    try:
        ok = await send_cloud_template_message(
            to=phone,
            template_name="ppis_teacher_present_text",
            language_code="en",
            body_params=[display_name, time_str],
        )
        if ok:
            logger.info(f"[TRUEFACE] WhatsApp sent successfully to {phone}")
        else:
            logger.warning(f"[TRUEFACE] WhatsApp send failed for {phone}")
        return ok
    except Exception as e:
        logger.error(f"[TRUEFACE] WhatsApp error for {phone}: {e}")
        return False


# ============================================================
# ADMS Protocol Endpoints
# ============================================================

@router.get("/iclock/cdata")
async def iclock_cdata_get(request: Request):
    """Device initial handshake (GET)."""
    sn = request.query_params.get("SN", "unknown")
    logger.info(f"[TRUEFACE] GET handshake from SN={sn}, params={dict(request.query_params)}")
    return PlainTextResponse("OK")


@router.post("/iclock/cdata")
async def iclock_cdata_post(request: Request):
    """Receive attendance logs and operation logs from device."""
    sn = request.query_params.get("SN", "unknown")
    table = request.query_params.get("table", "").upper()

    body = await request.body()
    body_text = body.decode("utf-8", errors="replace")

    logger.info(f"[TRUEFACE] POST /iclock/cdata SN={sn} table={table} len={len(body_text)}")
    logger.info(f"[TRUEFACE] Body: {body_text[:500]}")

    if table == "ATTLOG":
        records = _parse_attlog(body_text)
        _clear_daily_dedup()
        users = _load_users()

        for record in records:
            pin = record["pin"]
            timestamp = record["timestamp"]

            logger.info(f"[TRUEFACE] Attendance: PIN={pin} time={timestamp} "
                       f"status={record['status']} verify={record['verify']}")

            user = users.get(pin)
            if not user:
                logger.warning(f"[TRUEFACE] Unknown PIN={pin} — not registered")
                continue

            # Dedup: only notify once per day
            today = datetime.now(IST).strftime("%Y-%m-%d")
            if _notified_today.get(pin) == today:
                logger.info(f"[TRUEFACE] Already notified {user['name']} today, skipping")
                continue

            # Weekend check
            now = datetime.now(IST)
            if now.weekday() in (5, 6):
                logger.info(f"[TRUEFACE] Weekend — skipping for {user['name']}")
                continue

            # Extract time for notification
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                time_str = dt.strftime("%I:%M %p")
            except (ValueError, TypeError):
                time_str = datetime.now(IST).strftime("%I:%M %p")

            phone = user.get("phone", "")
            if phone:
                _notified_today[pin] = today
                asyncio.create_task(_send_teacher_whatsapp(user["name"], phone, time_str))
            else:
                logger.warning(f"[TRUEFACE] No phone for {user['name']} (PIN={pin})")

    elif table == "OPERLOG":
        logger.info(f"[TRUEFACE] OPERLOG: {body_text[:500]}")
    else:
        logger.info(f"[TRUEFACE] Unknown table={table}: {body_text[:200]}")

    return PlainTextResponse("OK")


@router.get("/iclock/getrequest")
async def iclock_getrequest(request: Request):
    """Device polls for pending commands."""
    sn = request.query_params.get("SN", "unknown")

    if _command_queue:
        cmd = _command_queue.pop(0)
        logger.info(f"[TRUEFACE] Sending command to SN={sn}: {cmd[:100]}")
        return PlainTextResponse(cmd)

    return PlainTextResponse("OK")


@router.post("/iclock/devicecmd")
async def iclock_devicecmd(request: Request):
    """Device confirms command execution."""
    sn = request.query_params.get("SN", "unknown")
    body = await request.body()
    logger.info(f"[TRUEFACE] CMD ACK from SN={sn}: {body.decode('utf-8', errors='replace')[:200]}")
    return PlainTextResponse("OK")


# ============================================================
# Management API
# ============================================================

@router.get("/api/trueface/users")
async def list_trueface_users():
    """List all registered TrueFace users (PIN → teacher mapping)."""
    return _load_users()


@router.post("/api/trueface/users")
async def register_trueface_user(request: Request):
    """Register a TrueFace user.
    
    Body: {"pin": "1", "name": "Alisha Ahuja", "phone": "918076455224"}
    """
    data = await request.json()
    pin = str(data.get("pin", ""))
    name = data.get("name", "")
    phone = data.get("phone", "")

    if not pin or not name:
        return {"error": "pin and name are required"}

    users = _load_users()
    users[pin] = {"name": name, "phone": phone}
    _save_users(users)
    logger.info(f"[TRUEFACE] User registered: PIN={pin} name={name} phone={phone}")
    return {"status": "ok", "pin": pin, "name": name}


@router.post("/api/trueface/users/bulk")
async def bulk_register_users(request: Request):
    """Bulk register TrueFace users.
    
    Body: [{"pin": "1", "name": "...", "phone": "..."}, ...]
    """
    data = await request.json()
    users = _load_users()
    count = 0
    for entry in data:
        pin = str(entry.get("pin", ""))
        name = entry.get("name", "")
        phone = entry.get("phone", "")
        if pin and name:
            users[pin] = {"name": name, "phone": phone}
            count += 1
    _save_users(users)
    logger.info(f"[TRUEFACE] Bulk registered {count} users")
    return {"status": "ok", "registered": count}


@router.delete("/api/trueface/users/{pin}")
async def delete_trueface_user(pin: str):
    """Remove a TrueFace user mapping."""
    users = _load_users()
    if pin in users:
        del users[pin]
        _save_users(users)
    return {"status": "ok", "deleted": pin}


@router.get("/api/trueface/status")
async def trueface_status():
    """Get TrueFace integration status."""
    users = _load_users()
    return {
        "registered_users": len(users),
        "notified_today": len(_notified_today),
        "pending_commands": len(_command_queue),
        "users": users,
    }
