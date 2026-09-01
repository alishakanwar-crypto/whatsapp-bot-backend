"""
WebSocket endpoint for the PPIS Campus Agent.

The campus agent (running on a school PC) connects here via WebSocket.
When a parent requests a child's photo, the bot sends a snapshot_request
through this WebSocket, the agent captures it from the DVR, and sends
the image back.

Protocol (v2 — individual images):
  Agent sends:  snapshot_image   (one per captured image)
  Agent sends:  snapshot_complete (final message with total count)

Protocol (v1 — legacy single message):
  Agent sends:  snapshot_response (all images in one message)
"""

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

router = APIRouter()

# ---------------------------------------------------------------------------
# Agent connection state
# ---------------------------------------------------------------------------
_agent_ws: WebSocket | None = None
_agent_websockets: list[WebSocket] = []
_pending_requests: dict[str, asyncio.Future] = {}
_pending_request_websockets: dict[str, WebSocket] = {}
# Accumulate individual images for v2 protocol (request_id -> list of image dicts)
_pending_images: dict[str, list] = {}
SnapshotImageCallback = Callable[[dict], Awaitable[None]]
_pending_image_callbacks: dict[str, SnapshotImageCallback] = {}
# Delivering a photo to WhatsApp takes a couple of seconds, and the agent link
# carries every family's request, so a photo is handed to WhatsApp on its own
# task instead of holding up the images arriving behind it.
_pending_image_deliveries: dict[str, list[asyncio.Task]] = {}

# Queued snapshot requests — filled when agent is offline, drained on reconnect
# Each entry: {"classroom": str, "sender": str, "reply_to": str, "queued_at": float}
_queued_snapshots: list[dict] = []
_MAX_QUEUED = 20  # max pending queued requests
_QUEUE_TTL = 120  # discard queued requests older than 2 minutes

AGENT_SECRET = os.environ.get("AGENT_SECRET", "")

# A dead campus link looks identical to a healthy one until something is sent
# over it, so the socket is pinged and dropped when the agent stops answering.
# Every parent request that arrives during that blind window waits for its full
# timeout and is only then retried, which is what makes a photo take minutes.
_AGENT_PING_INTERVAL_SECONDS = 15.0
_AGENT_SILENCE_LIMIT_SECONDS = 40.0
_agent_last_message_at: dict[int, float] = {}

# ---------------------------------------------------------------------------
# Always-Active Health Monitoring
# ---------------------------------------------------------------------------
_health_state: dict = {
    "last_connected_at": 0.0,        # timestamp of last successful connection
    "last_disconnected_at": 0.0,     # timestamp of last disconnection
    "consecutive_failures": 0,        # snapshot request failures in a row
    "total_snapshots_served": 0,      # lifetime counter
    "total_snapshots_failed": 0,      # lifetime counter
    "last_snapshot_at": 0.0,         # timestamp of last successful snapshot
    "admin_alerted": False,          # True if admin was alerted about persistent failure
    "uptime_start": time.time(),     # when the server started
}

# Threshold: only alert admin after this many consecutive snapshot failures
_ALERT_THRESHOLD = 5

# A recorder that refuses our login can only be unlocked at school, so the
# people who can walk up to it are told once per incident.
_RECORDER_ALERT_NUMBERS = [
    number.strip()
    for number in os.environ.get(
        "RECORDER_ALERT_NUMBERS", "919971166562,919599488106"
    ).split(",")
    if number.strip()
]

# ---------------------------------------------------------------------------
# Fallback proxy: when the agent isn't connected locally, proxy snapshot
# requests to the app where the agent IS connected.  This handles the
# migration period where webhook → new app but agent → old app.
# ---------------------------------------------------------------------------
AGENT_PROXY_URLS = [
    "https://app-itszlsnn.fly.dev",  # Old app where agent may still be connected
]


async def verify_agent_secret(x_agent_secret: str = Header("")) -> None:
    """Dependency that verifies the agent secret header. Skips if env var not set."""
    if not AGENT_SECRET:
        return
    if x_agent_secret != AGENT_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing agent secret")


def is_agent_connected() -> bool:
    return _agent_ws is not None


def _note_agent_message(websocket: WebSocket) -> None:
    _agent_last_message_at[id(websocket)] = time.time()


def _agent_silence_seconds(websocket: WebSocket) -> float:
    last = _agent_last_message_at.get(id(websocket), 0.0)
    return time.time() - last if last else 0.0


async def _keep_agent_link_honest(websocket: WebSocket) -> None:
    """Ping the agent and drop the socket once it stops answering.

    Closing it makes the agent reconnect within seconds and lets the
    disconnect handler retry the requests in flight straight away.
    """
    while True:
        await asyncio.sleep(_AGENT_PING_INTERVAL_SECONDS)
        if websocket not in _agent_websockets:
            return
        silence = _agent_silence_seconds(websocket)
        if silence > _AGENT_SILENCE_LIMIT_SECONDS:
            logger.warning(
                "Campus agent silent for %.0fs — closing the dead link so it "
                "reconnects and pending photos are retried",
                silence,
            )
            try:
                await websocket.close(code=1011, reason="agent link silent")
            except Exception as exc:  # already gone
                logger.debug("Closing silent agent link failed: %s", exc)
            return
        try:
            await websocket.send_json({"type": "ping"})
        except Exception as exc:
            logger.info("Agent ping failed, link is gone: %s", exc)
            return


async def wait_for_agent(max_wait: float = 30.0) -> bool:
    """Wait up to max_wait seconds for the agent to reconnect.

    After an OOM kill, the Fly.io app restarts and `_agent_ws` is None.
    The campus agent auto-reconnects within seconds. This avoids
    immediately telling the user 'camera offline' during that brief window.

    Increased to 30s (from 15s) because OOM restarts can take 20-25s
    on Fly.io free-tier machines.
    """
    if _agent_ws is not None:
        return True
    logger.info("Agent not connected — waiting up to %.0fs for reconnection...", max_wait)
    elapsed = 0.0
    step = 1.0  # Check every 1s (was 2s) for faster detection
    while elapsed < max_wait:
        await asyncio.sleep(step)
        elapsed += step
        if _agent_ws is not None:
            logger.info("Agent reconnected after %.0fs wait", elapsed)
            return True
    logger.warning("Agent did not reconnect within %.0fs", max_wait)
    return False


def get_health_state() -> dict:
    """Return the current health state of the agent connection.

    Used by the /api/agent/health endpoint and the health monitor.
    """
    now = time.time()
    connected = _agent_ws is not None
    uptime_seconds = now - _health_state["uptime_start"]
    last_connected_ago = (
        now - _health_state["last_connected_at"]
        if _health_state["last_connected_at"] > 0
        else -1
    )
    disconnected_seconds = (
        now - _health_state["last_disconnected_at"]
        if not connected and _health_state["last_disconnected_at"] > 0
        else 0.0
    )
    return {
        "connected": connected,
        "consecutive_failures": _health_state["consecutive_failures"],
        "total_snapshots_served": _health_state["total_snapshots_served"],
        "total_snapshots_failed": _health_state["total_snapshots_failed"],
        "last_snapshot_at": _health_state["last_snapshot_at"],
        "last_connected_seconds_ago": round(last_connected_ago, 1),
        "disconnected_seconds": round(disconnected_seconds, 1),
        "uptime_seconds": round(uptime_seconds, 1),
        "admin_alerted": _health_state["admin_alerted"],
        "pending_requests": len(_pending_requests),
        "recorders_on_fallback": _health_state.get("recorders_on_fallback", []),
        "recorders_reported_at_ist": _health_state.get(
            "recorders_reported_at_ist", ""
        ),
        "agent_code_commit": _health_state.get("agent_code_commit", ""),
        "agent_started_at_ist": _health_state.get("agent_started_at_ist", ""),
    }


def _record_agent_version(data: dict) -> None:
    """Remember which commit the connected agent is actually running.

    A restart that leaves the old process alive keeps serving pre-pull code, so
    this is the only way to tell a bad fix from a fix that never took effect.
    """
    _health_state["agent_code_commit"] = data.get("code_commit", "")
    _health_state["agent_started_at_ist"] = data.get("started_at_ist", "")


def _record_recorder_health(data: dict) -> None:
    """Remember which recorders the agent is currently bypassing ISAPI on."""
    health = data.get("dvr_health")
    if health is None:
        return
    _health_state["recorders_on_fallback"] = health
    _health_state["recorders_reported_at_ist"] = datetime.now(IST).strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )
    if health:
        logger.warning(
            "Recorders on snapshot fallback: %s",
            ", ".join(f"{r.get('ip')} ({r.get('reason')})" for r in health),
        )
    try:
        asyncio.get_running_loop().create_task(
            _alert_refused_recorders(health)
        )
    except RuntimeError:
        # Called outside the event loop, so there is nothing to alert over.
        pass


async def _classrooms_on_recorder(ip: str) -> list[str]:
    """Classrooms whose cameras live on this recorder, so an alert can say who
    is affected instead of only naming an IP."""
    from app.database import get_db

    db = None
    try:
        db = await get_db()
        # dvr_index is the recorder's position in the configured list, which is
        # what the agent is given, so the ip has to be resolved through it.
        cursor = await db.execute("SELECT ip FROM agent_dvrs ORDER BY id")
        ips = [row["ip"] for row in await cursor.fetchall()]
        if ip not in ips:
            return []
        cursor = await db.execute(
            "SELECT location FROM agent_camera_mapping WHERE dvr_index = ? "
            "ORDER BY location",
            (ips.index(ip),),
        )
        return [row["location"] for row in await cursor.fetchall()]
    except Exception as exc:
        logger.warning("Could not list classrooms on %s: %s", ip, exc)
        return []
    finally:
        if db is not None:
            await db.close()


async def _alert_refused_recorders(health: list) -> None:
    """Tell the admins when a recorder stops accepting our login.

    A locked recorder blocks every classroom on it until someone unlocks it at
    school, and nothing on the campus PC can fix that — so it must not wait for
    parents to report that photos stopped arriving.
    """
    refused = {
        str(entry.get("ip"))
        for entry in health or []
        if entry.get("reason") == "credentials refused"
    }
    alerted: set[str] = _health_state.setdefault("recorders_alerted", set())
    recovered = alerted - refused
    if recovered:
        alerted.difference_update(recovered)
    new = refused - alerted
    if not new:
        return
    alerted.update(new)
    from app.services.whatsapp_service import send_whatsapp_force

    when = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")
    for ip in sorted(new):
        classrooms = await _classrooms_on_recorder(ip)
        rooms = ", ".join(classrooms) if classrooms else "unknown"
        message = (
            "PPIS Bot — Recorder Login Refused\n\n"
            f"Recorder {ip} is rejecting the bot's login as of {when}, so live "
            f"photos cannot be captured for {len(classrooms)} classroom(s): "
            f"{rooms}.\n\n"
            "This is usually the recorder's admin account locked after failed "
            "logins, or a changed password. Please unlock or reboot it at "
            "school (or share the new password).\n\n"
            "The bot has paused its login attempts so the lockout can clear, "
            "and will resume automatically once the recorder accepts us again."
        )
        for admin_phone in _RECORDER_ALERT_NUMBERS:
            try:
                await send_whatsapp_force(admin_phone, message)
            except Exception as exc:
                logger.warning(
                    "Could not alert %s about recorder %s: %s",
                    admin_phone, ip, exc,
                )


def record_snapshot_success() -> None:
    """Record a successful snapshot delivery."""
    _health_state["total_snapshots_served"] += 1
    _health_state["last_snapshot_at"] = time.time()
    _health_state["consecutive_failures"] = 0
    _health_state["admin_alerted"] = False  # reset alert flag on success


def record_snapshot_failure() -> None:
    """Record a failed snapshot attempt."""
    _health_state["total_snapshots_failed"] += 1
    _health_state["consecutive_failures"] += 1


def should_alert_admin() -> bool:
    """Return True if admin should be alerted about persistent camera failure.

    Only returns True once per failure streak (resets after success).
    """
    if _health_state["consecutive_failures"] >= _ALERT_THRESHOLD and not _health_state["admin_alerted"]:
        _health_state["admin_alerted"] = True
        return True
    return False


def queue_snapshot_request(classroom: str, sender: str, reply_to: str) -> bool:
    """Queue a snapshot request to be fulfilled when the agent reconnects.

    Returns True if queued successfully, False if queue is full.
    """
    # Purge expired entries
    now = time.time()
    _queued_snapshots[:] = [
        q for q in _queued_snapshots
        if now - q["queued_at"] < _QUEUE_TTL
    ]
    # Avoid duplicate requests from same sender for same classroom
    for q in _queued_snapshots:
        if q["sender"] == sender and q["classroom"] == classroom:
            return True  # already queued
    if len(_queued_snapshots) >= _MAX_QUEUED:
        return False
    _queued_snapshots.append({
        "classroom": classroom,
        "sender": sender,
        "reply_to": reply_to,
        "queued_at": now,
    })
    logger.info(f"Queued snapshot request for '{classroom}' from {sender} ({len(_queued_snapshots)} in queue)")
    return True


async def _drain_queued_snapshots():
    """Process all queued snapshot requests after agent reconnects.

    Called automatically when the agent WebSocket reconnects.
    """
    if not _queued_snapshots:
        return

    now = time.time()
    # Copy and clear the queue atomically
    pending = [q for q in _queued_snapshots if now - q["queued_at"] < _QUEUE_TTL]
    _queued_snapshots.clear()

    if not pending:
        return

    logger.info(f"Draining {len(pending)} queued snapshot request(s)")

    for q in pending:
        try:
            result = await request_snapshot(q["classroom"], timeout=55.0)
            if result.get("success") and result.get("images"):
                from app.services.whatsapp_service import (
                    upload_base64_image_cloud,
                    send_cloud_media,
                )
                for img_data in result["images"]:
                    img_b64 = img_data.get("image_base64", "")
                    desc = img_data.get("description", q["classroom"])
                    if img_b64:
                        media_id = await upload_base64_image_cloud(img_b64)
                        if media_id:
                            await send_cloud_media(
                                q["reply_to"], media_id, "image",
                                caption=f"📸 {desc}"
                            )
                record_snapshot_success()
                logger.info(f"Delivered queued snapshot for '{q['classroom']}' to {q['sender']}")
            else:
                logger.warning(f"Queued snapshot for '{q['classroom']}' failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"Error delivering queued snapshot for '{q['classroom']}': {e}")


async def _proxy_snapshot_request(classroom: str, timeout: float = 55.0) -> dict | None:
    """Try to proxy a snapshot request to another app where the agent IS connected.

    Returns the snapshot result dict if a proxy app has the agent connected
    and returns a successful result, or None if no proxy is available.
    """
    for proxy_url in AGENT_PROXY_URLS:
        try:
            # First check if the agent is connected on the proxy app
            async with httpx.AsyncClient(timeout=10.0) as client:
                status_resp = await client.get(f"{proxy_url}/api/agent/status")
                if status_resp.status_code != 200:
                    continue
                status = status_resp.json()
                if not status.get("connected"):
                    logger.info(f"Proxy {proxy_url}: agent not connected, skipping")
                    continue

            # Agent is connected on the proxy app — send the snapshot request
            logger.info(
                f"Proxying snapshot request for '{classroom}' to {proxy_url}"
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{proxy_url}/api/agent/snapshot",
                    json={"classroom": classroom},
                    headers={"x-agent-secret": AGENT_SECRET} if AGENT_SECRET else {},
                )
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("success"):
                        logger.info(
                            f"Proxy snapshot via {proxy_url} succeeded: "
                            f"{result.get('image_count', 0)} images"
                        )
                        return result
                    else:
                        logger.warning(
                            f"Proxy snapshot via {proxy_url} returned failure: "
                            f"{result.get('error', 'unknown')}"
                        )
                else:
                    logger.warning(
                        f"Proxy snapshot via {proxy_url} HTTP {resp.status_code}"
                    )
        except Exception as exc:
            logger.warning(f"Proxy snapshot via {proxy_url} failed: {exc}")

    return None  # No proxy available


async def request_snapshot(
    classroom: str,
    timeout: float = 60.0,
    image_callback: SnapshotImageCallback | None = None,
) -> dict:
    """Request a snapshot from the campus agent."""
    global _agent_ws

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error = "Campus agent connection is unstable"

    for attempt in range(3):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break

        if _agent_ws is None:
            connected = await wait_for_agent(max_wait=min(15.0, remaining))
            if not connected:
                if attempt == 0:
                    logger.info(
                        "Agent not connected locally — trying proxy fallback"
                    )
                    proxy_result = await _proxy_snapshot_request(classroom)
                    if proxy_result is not None:
                        return proxy_result
                last_error = "Campus agent is not connected"
                continue

        ws = _agent_ws
        if ws is None:
            last_error = "Campus agent disconnected before request could be sent"
            continue

        request_id = str(uuid.uuid4())
        future: asyncio.Future = loop.create_future()
        _pending_requests[request_id] = future
        _pending_request_websockets[request_id] = ws
        _pending_images[request_id] = []
        if image_callback is not None:
            _pending_image_callbacks[request_id] = image_callback

        try:
            await ws.send_json({
                "type": "snapshot_request",
                "classroom": classroom,
                "request_id": request_id,
            })
            logger.info(
                "Sent snapshot request %s for classroom: %s (attempt %d/3)",
                request_id,
                classroom,
                attempt + 1,
            )
        except Exception as send_err:
            logger.warning(
                "Failed to send snapshot request (stale WS?): %s", send_err
            )
            if _agent_ws is ws:
                _agent_ws = next(
                    (
                        candidate
                        for candidate in reversed(_agent_websockets)
                        if candidate is not ws
                    ),
                    None,
                )
            _pending_requests.pop(request_id, None)
            _pending_request_websockets.pop(request_id, None)
            _pending_images.pop(request_id, None)
            _pending_image_callbacks.pop(request_id, None)
            last_error = "Campus agent disconnected during request"
            continue

        try:
            result = await asyncio.wait_for(
                future, timeout=max(0.1, deadline - loop.time())
            )
            if (
                result.get("success")
                or result.get("error") != "Agent disconnected"
            ):
                return result
            last_error = "Campus agent disconnected during request"
            logger.info(
                "Retrying snapshot for %s after agent disconnect", classroom
            )
        except asyncio.TimeoutError:
            logger.error(
                "Snapshot request %s timed out after %.1fs", request_id, timeout
            )
            collected = _pending_images.get(request_id, [])
            if collected:
                logger.info(
                    "Timeout but collected %d images before timeout",
                    len(collected),
                )
                # Do not report on images still being handed to WhatsApp, or
                # the caller sends them a second time.
                await _await_image_deliveries(request_id)
                return {
                    "success": True,
                    "classroom": classroom,
                    "image_count": len(collected),
                    "images": collected,
                }
            return {
                "success": False,
                "error": "Snapshot request timed out — camera may be offline",
            }
        except Exception as error:
            logger.error("Snapshot request error: %s", error)
            return {"success": False, "error": str(error)}
        finally:
            _pending_requests.pop(request_id, None)
            _pending_request_websockets.pop(request_id, None)
            _pending_images.pop(request_id, None)
            _pending_image_callbacks.pop(request_id, None)
            _pending_image_deliveries.pop(request_id, None)

    return {"success": False, "error": last_error}


async def _store_snapshot_image(data: dict) -> None:
    request_id = data.get("request_id", "")
    idx = data.get("image_index", 0)
    total = data.get("image_total", 1)
    desc = data.get("description", "")
    capture = data.get("capture") or {}
    logger.info(
        "Received snapshot_image %d/%d for %s (%d bytes, %dx%d, %s) "
        "capture=%.2fs slot_wait=%.2fs recorder=%s ch%s attempts=%s "
        "door=%s door_timeouts=%s rtsp=%s",
        idx + 1,
        total,
        request_id,
        data.get("size_bytes", 0),
        data.get("width", 0),
        data.get("height", 0),
        desc,
        capture.get("seconds", 0.0),
        capture.get("slot_wait_seconds", 0.0),
        capture.get("recorder", "-"),
        capture.get("channel", "-"),
        capture.get("attempt_seconds", []),
        capture.get("door", "-"),
        capture.get("door_timeouts", 0),
        capture.get("rtsp", False),
    )
    if request_id not in _pending_images:
        return

    image = {
        "image_base64": data.get("image_base64", ""),
        "description": desc,
        "filename": data.get("filename", f"snapshot_{idx}.jpg"),
        "size_bytes": data.get("size_bytes", 0),
        "image_index": idx,
        "image_total": total,
    }
    _pending_images[request_id].append(image)
    callback = _pending_image_callbacks.get(request_id)
    if callback is not None:
        _start_image_delivery(request_id, callback, image)


def _start_image_delivery(
    request_id: str, callback: SnapshotImageCallback, image: dict
) -> None:
    """Hand one image to WhatsApp without blocking the campus link.

    Images of the same request stay in order (C1 before C2) by waiting on the
    delivery started before them.
    """
    deliveries = _pending_image_deliveries.setdefault(request_id, [])
    previous = deliveries[-1] if deliveries else None
    deliveries.append(
        asyncio.create_task(_deliver_image(request_id, previous, callback, image))
    )


async def _deliver_image(
    request_id: str,
    previous: asyncio.Task | None,
    callback: SnapshotImageCallback,
    image: dict,
) -> None:
    if previous is not None:
        await asyncio.wait([previous])
    try:
        await callback(image)
    except Exception as exc:
        logger.error(
            "Snapshot image callback failed for %s: %s",
            request_id,
            exc,
            exc_info=True,
        )


async def _complete_snapshot_request(request_id: str, classroom: str) -> None:
    """Finish a request once its streamed photos have reached the parent.

    Run off the campus link so the next family's images keep arriving while
    WhatsApp is still accepting this one's.
    """
    await _await_image_deliveries(request_id)
    collected = _pending_images.get(request_id, [])
    future = _pending_requests.get(request_id)
    if future and not future.done():
        future.set_result({
            "success": True,
            "classroom": classroom,
            "image_count": len(collected),
            "images": collected,
        })


async def _await_image_deliveries(request_id: str) -> None:
    """Wait until every streamed image of this request has been sent."""
    while True:
        deliveries = list(_pending_image_deliveries.get(request_id, []))
        pending = [task for task in deliveries if not task.done()]
        if not pending:
            return
        await asyncio.wait(pending)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/agent")
async def agent_websocket(websocket: WebSocket):
    """WebSocket endpoint for the PPIS Campus Agent."""
    global _agent_ws

    # Verify agent secret
    secret = websocket.headers.get("x-agent-secret", "")
    if AGENT_SECRET and secret != AGENT_SECRET:
        logger.warning("Agent WebSocket rejected: invalid secret")
        await websocket.close(code=4001, reason="Invalid agent secret")
        return

    await websocket.accept()
    _agent_websockets.append(websocket)
    _agent_ws = websocket
    _health_state["last_connected_at"] = time.time()
    _health_state["consecutive_failures"] = 0  # reset on fresh connection
    _health_state["admin_alerted"] = False
    logger.info("Campus agent connected via WebSocket")
    _note_agent_message(websocket)
    keepalive = asyncio.create_task(_keep_agent_link_honest(websocket))

    # Drain any queued snapshot requests from while agent was offline
    asyncio.create_task(_drain_queued_snapshots())

    try:
        while True:
            # Use receive_text() + json.loads() to handle large messages
            raw_text = await websocket.receive_text()
            _note_agent_message(websocket)
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON from agent (len={len(raw_text)})")
                continue

            msg_type = data.get("type", "")

            if msg_type == "agent_hello":
                logger.info(
                    "Agent hello: %s DVRs, %s camera mappings, code %s, "
                    "process started %s",
                    data.get("dvr_count", 0),
                    data.get("camera_count", 0),
                    data.get("code_commit") or "unknown",
                    data.get("started_at_ist") or "unknown",
                )
                _record_agent_version(data)
                _record_recorder_health(data)

            # --- v2 protocol: individual images ---
            elif msg_type == "snapshot_image":
                await _store_snapshot_image(data)

            elif msg_type == "snapshot_complete":
                request_id = data.get("request_id", "")
                image_count = data.get("image_count", 0)
                classroom = data.get("classroom", "")
                collected = _pending_images.get(request_id, [])
                logger.info(
                    f"Snapshot complete for {request_id}: "
                    f"expected={image_count}, received={len(collected)}"
                )
                asyncio.create_task(
                    _complete_snapshot_request(request_id, classroom)
                )

            # --- v1 protocol: legacy single message with all images ---
            elif msg_type == "snapshot_response":
                request_id = data.get("request_id", "")
                future = _pending_requests.get(request_id)
                if future and not future.done():
                    future.set_result(data)
                    img_count = data.get("image_count", 1 if data.get("image_base64") else 0)
                    detail = data.get("detail", "")
                    detail_str = f", detail={detail}" if detail else ""
                    logger.info(f"Snapshot response (v1) for {request_id}: success={data.get('success')}, images={img_count}{detail_str}")
                else:
                    logger.warning(f"Snapshot response for unknown/expired request: {request_id}")

            elif msg_type == "pong":
                _record_recorder_health(data)

            elif msg_type == "test_result":
                logger.info(f"DVR test result: {data}")

            elif msg_type == "test_all_dvrs_result":
                request_id = data.get("request_id", "")
                future = _pending_requests.get(request_id)
                if future and not future.done():
                    future.set_result(data)
                logger.info(f"DVR test results: {data.get('results', [])}")

            elif msg_type == "mapping_updated":
                logger.info(
                    f"Agent mapping update result: success={data.get('success')}, "
                    f"count={data.get('count', 0)}"
                )

            elif msg_type == "dvrs_updated":
                logger.info(
                    f"Agent DVR update result: success={data.get('success')}, "
                    f"count={data.get('count', 0)}"
                )

            else:
                logger.warning(f"Unknown agent message type: {msg_type}")

    except WebSocketDisconnect:
        logger.info("Campus agent disconnected")
    except Exception as e:
        logger.error(f"Agent WebSocket error: {e}")
    finally:
        keepalive.cancel()
        _agent_last_message_at.pop(id(websocket), None)
        if websocket in _agent_websockets:
            _agent_websockets.remove(websocket)
        if _agent_ws is websocket:
            _agent_ws = (
                _agent_websockets[-1] if _agent_websockets else None
            )
            _health_state["last_disconnected_at"] = time.time()

        disconnected_requests = [
            req_id
            for req_id, request_ws in _pending_request_websockets.items()
            if request_ws is websocket
        ]
        for req_id in disconnected_requests:
            future = _pending_requests.get(req_id)
            if future and not future.done():
                collected = _pending_images.pop(req_id, [])
                if collected:
                    future.set_result({
                        "success": True,
                        "classroom": "",
                        "image_count": len(collected),
                        "images": collected,
                    })
                else:
                    future.set_result({
                        "success": False,
                        "error": "Agent disconnected",
                    })
            _pending_request_websockets.pop(req_id, None)


# ---------------------------------------------------------------------------
# REST endpoint for checking agent status
# ---------------------------------------------------------------------------

@router.get("/api/agent/status")
async def agent_status():
    return {
        "connected": is_agent_connected(),
        "pending_requests": len(_pending_requests),
    }


@router.get("/api/agent/health")
async def agent_health():
    """Detailed health status for the always-active monitoring system.

    Returns connection state, failure counts, uptime, and alert status.
    Used by the health monitor and admin dashboard.
    """
    from app.services.campus_watch_service import watch_state

    return {**get_health_state(), "campus_watch": watch_state()}


async def push_camera_mapping(mapping: dict) -> dict:
    """Push updated camera mapping to the connected agent via WebSocket."""
    global _agent_ws
    if _agent_ws is None:
        return {"success": False, "error": "Campus agent is not connected"}
    try:
        await _agent_ws.send_json({
            "type": "update_camera_mapping",
            "camera_mapping": mapping,
        })
        logger.info(f"Pushed camera mapping to agent: {len(mapping)} entries")
        return {"success": True, "count": len(mapping)}
    except Exception as e:
        logger.error(f"Failed to push mapping: {e}")
        return {"success": False, "error": str(e)}


async def push_dvrs(dvrs: list) -> dict:
    """Push updated DVR list to the connected agent via WebSocket."""
    global _agent_ws
    if _agent_ws is None:
        return {"success": False, "error": "Campus agent is not connected"}
    try:
        await _agent_ws.send_json({
            "type": "update_dvrs",
            "dvrs": dvrs,
        })
        logger.info(f"Pushed DVRs to agent: {len(dvrs)} entries")
        return {"success": True, "count": len(dvrs)}
    except Exception as e:
        logger.error(f"Failed to push DVRs: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/agent/push-mapping", dependencies=[Depends(verify_agent_secret)])
async def push_mapping_endpoint(request: Request):
    """Push camera mapping to the connected Campus Agent."""
    body = await request.json()
    mapping = body.get("camera_mapping", {})
    if not mapping:
        return JSONResponse({"error": "No camera_mapping provided"}, status_code=400)
    result = await push_camera_mapping(mapping)
    return result


@router.post("/api/agent/restart")
async def restart_agent_endpoint():
    """Send a restart command to the campus agent via WebSocket.

    The agent process will exit, and run_forever.bat will auto-restart it
    with a fresh `git pull` (picking up latest code changes).
    """
    global _agent_ws
    if _agent_ws is None:
        return JSONResponse({"error": "Agent not connected"}, status_code=503)
    try:
        await _agent_ws.send_json({"type": "restart"})
        return {"status": "restart_sent", "message": "Agent will restart with git pull"}
    except Exception as e:
        return JSONResponse({"error": f"Failed to send restart: {e}"}, status_code=500)


@router.post("/api/agent/sync-faces")
async def sync_faces_endpoint():
    """Tell the campus agent to sync new faces from cloud immediately."""
    global _agent_ws
    if _agent_ws is None:
        return JSONResponse({"error": "Agent not connected"}, status_code=503)
    try:
        await _agent_ws.send_json({"type": "sync_faces"})
        return {"status": "sync_requested", "message": "Agent will sync faces from cloud"}
    except Exception as e:
        return JSONResponse({"error": f"Failed to send sync: {e}"}, status_code=500)


@router.get("/api/agent/test-dvrs")
async def test_dvrs_endpoint():
    """Remotely test DVR connectivity through the campus agent."""
    global _agent_ws
    if _agent_ws is None:
        return JSONResponse(
            {"error": "Agent not connected"}, status_code=503
        )

    request_id = str(uuid.uuid4())
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_requests[request_id] = future

    try:
        await _agent_ws.send_json({
            "type": "test_all_dvrs",
            "request_id": request_id,
        })
    except Exception as e:
        _pending_requests.pop(request_id, None)
        return JSONResponse(
            {"error": f"Failed to send test request: {e}"}, status_code=500
        )

    try:
        result = await asyncio.wait_for(future, timeout=30.0)
        return result
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "DVR test timed out after 30s"}, status_code=504
        )
    finally:
        _pending_requests.pop(request_id, None)


@router.post("/api/agent/snapshot")
async def snapshot_endpoint(request: Request):
    """REST endpoint for requesting a snapshot from the campus agent.

    Used by the proxy mechanism: new app proxies to this endpoint on the
    old app (where the agent IS connected) during the migration period.
    Also usable directly for testing.
    """
    # Optional auth check
    secret = request.headers.get("x-agent-secret", "")
    if AGENT_SECRET and secret != AGENT_SECRET:
        return JSONResponse(
            {"error": "Unauthorized"}, status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON body"}, status_code=400
        )

    classroom = body.get("classroom", "")
    if not classroom:
        return JSONResponse(
            {"error": "No classroom specified"}, status_code=400
        )

    result = await request_snapshot(classroom, timeout=55.0)
    return result
