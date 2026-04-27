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
import base64
import json
import logging
import os
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Agent connection state
# ---------------------------------------------------------------------------
_agent_ws: WebSocket | None = None
_pending_requests: dict[str, asyncio.Future] = {}
# Accumulate individual images for v2 protocol (request_id -> list of image dicts)
_pending_images: dict[str, list] = {}

AGENT_SECRET = os.environ.get("AGENT_SECRET", "")

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


async def request_snapshot(classroom: str, timeout: float = 60.0) -> dict:
    """Request a snapshot from the campus agent.

    Returns dict with keys:
      success: bool
      images: list[dict] — list of {image_base64, description, filename}
      image_count: int
      error: str — only if not success
    """
    global _agent_ws

    # Wait for agent to reconnect if it's briefly disconnected (e.g. after OOM kill)
    if _agent_ws is None:
        connected = await wait_for_agent(max_wait=30.0)
        if not connected:
            # ---- Fallback: proxy through another app where agent IS connected ----
            logger.info("Agent not connected locally — trying proxy fallback")
            proxy_result = await _proxy_snapshot_request(classroom)
            if proxy_result is not None:
                return proxy_result
            return {"success": False, "error": "Campus agent is not connected"}

    request_id = str(uuid.uuid4())
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_requests[request_id] = future
    _pending_images[request_id] = []  # Initialize image accumulator

    # Capture the WebSocket reference in a local variable to avoid race
    # condition where _agent_ws becomes None between null-check and send.
    ws = _agent_ws
    if ws is None:
        _pending_requests.pop(request_id, None)
        _pending_images.pop(request_id, None)
        return {"success": False, "error": "Campus agent disconnected before request could be sent"}

    # Send the request — if the WebSocket is stale, retry once after reconnect
    try:
        await ws.send_json({
            "type": "snapshot_request",
            "classroom": classroom,
            "request_id": request_id,
        })
        logger.info(f"Sent snapshot request {request_id} for classroom: {classroom}")
    except Exception as send_err:
        # WebSocket may be stale after OOM restart — clear it and wait for reconnect
        logger.warning(f"Failed to send snapshot request (stale WS?): {send_err}")
        _agent_ws = None
        _pending_requests.pop(request_id, None)
        _pending_images.pop(request_id, None)
        # Give the agent time to reconnect, then retry once
        reconnected = await wait_for_agent(max_wait=30.0)
        if not reconnected:
            return {"success": False, "error": "Campus agent disconnected during request"}
        # Retry with a fresh request
        request_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        _pending_requests[request_id] = future
        _pending_images[request_id] = []
        ws = _agent_ws
        if ws is None:
            _pending_requests.pop(request_id, None)
            _pending_images.pop(request_id, None)
            return {"success": False, "error": "Campus agent disconnected before retry"}
        try:
            await ws.send_json({
                "type": "snapshot_request",
                "classroom": classroom,
                "request_id": request_id,
            })
            logger.info(f"Retry snapshot request {request_id} for classroom: {classroom}")
        except Exception as retry_err:
            logger.error(f"Retry snapshot request also failed: {retry_err}")
            _pending_requests.pop(request_id, None)
            _pending_images.pop(request_id, None)
            return {"success": False, "error": "Campus agent connection is unstable"}

    # Now wait for the agent to send back the snapshot images
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        logger.error(f"Snapshot request {request_id} timed out after {timeout}s")
        # Return any images we collected before timeout
        collected = _pending_images.pop(request_id, [])
        if collected:
            logger.info(f"Timeout but collected {len(collected)} images before timeout")
            return {
                "success": True,
                "classroom": classroom,
                "image_count": len(collected),
                "images": collected,
            }
        return {"success": False, "error": "Snapshot request timed out — camera may be offline"}
    except Exception as e:
        logger.error(f"Snapshot request error: {e}")
        return {"success": False, "error": str(e)}
    finally:
        _pending_requests.pop(request_id, None)
        _pending_images.pop(request_id, None)


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
        logger.warning(f"Agent WebSocket rejected: invalid secret")
        await websocket.close(code=4001, reason="Invalid agent secret")
        return

    await websocket.accept()
    _agent_ws = websocket
    logger.info("Campus agent connected via WebSocket")

    try:
        while True:
            # Use receive_text() + json.loads() to handle large messages
            raw_text = await websocket.receive_text()
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON from agent (len={len(raw_text)})")
                continue

            msg_type = data.get("type", "")

            if msg_type == "agent_hello":
                logger.info(
                    f"Agent hello: {data.get('dvr_count', 0)} DVRs, "
                    f"{data.get('camera_count', 0)} camera mappings"
                )

            # --- v2 protocol: individual images ---
            elif msg_type == "snapshot_image":
                request_id = data.get("request_id", "")
                idx = data.get("image_index", 0)
                total = data.get("image_total", 1)
                desc = data.get("description", "")
                logger.info(
                    f"Received snapshot_image {idx+1}/{total} for {request_id} "
                    f"({data.get('size_bytes', 0)} bytes, {desc})"
                )
                if request_id in _pending_images:
                    _pending_images[request_id].append({
                        "image_base64": data.get("image_base64", ""),
                        "description": desc,
                        "filename": data.get("filename", f"snapshot_{idx}.jpg"),
                        "size_bytes": data.get("size_bytes", 0),
                    })

            elif msg_type == "snapshot_complete":
                request_id = data.get("request_id", "")
                image_count = data.get("image_count", 0)
                classroom = data.get("classroom", "")
                collected = _pending_images.get(request_id, [])
                logger.info(
                    f"Snapshot complete for {request_id}: "
                    f"expected={image_count}, received={len(collected)}"
                )
                future = _pending_requests.get(request_id)
                if future and not future.done():
                    future.set_result({
                        "success": True,
                        "classroom": classroom,
                        "image_count": len(collected),
                        "images": collected,
                    })

            # --- v1 protocol: legacy single message with all images ---
            elif msg_type == "snapshot_response":
                request_id = data.get("request_id", "")
                future = _pending_requests.get(request_id)
                if future and not future.done():
                    future.set_result(data)
                    img_count = data.get("image_count", 1 if data.get("image_base64") else 0)
                    logger.info(f"Snapshot response (v1) for {request_id}: success={data.get('success')}, images={img_count}")
                else:
                    logger.warning(f"Snapshot response for unknown/expired request: {request_id}")

            elif msg_type == "pong":
                pass  # Keep-alive response

            elif msg_type == "test_result":
                logger.info(f"DVR test result: {data}")

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
        if _agent_ws is websocket:
            _agent_ws = None
        # Cancel any pending requests
        for req_id, future in list(_pending_requests.items()):
            if not future.done():
                future.set_result({"success": False, "error": "Agent disconnected"})
        _pending_images.clear()


# ---------------------------------------------------------------------------
# REST endpoint for checking agent status
# ---------------------------------------------------------------------------

@router.get("/api/agent/status")
async def agent_status():
    return {
        "connected": is_agent_connected(),
        "pending_requests": len(_pending_requests),
    }


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

    if not is_agent_connected():
        return JSONResponse(
            {"success": False, "error": "Agent not connected on this app"},
            status_code=503,
        )

    result = await request_snapshot(classroom, timeout=55.0)
    return result
