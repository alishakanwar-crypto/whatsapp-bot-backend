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


async def verify_agent_secret(x_agent_secret: str = Header("")) -> None:
    """Dependency that verifies the agent secret header. Skips if env var not set."""
    if not AGENT_SECRET:
        return
    if x_agent_secret != AGENT_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing agent secret")


def is_agent_connected() -> bool:
    return _agent_ws is not None


async def request_snapshot(classroom: str, timeout: float = 60.0) -> dict:
    """Request a snapshot from the campus agent.

    Returns dict with keys:
      success: bool
      images: list[dict] — list of {image_base64, description, filename}
      image_count: int
      error: str — only if not success
    """
    global _agent_ws
    if _agent_ws is None:
        return {"success": False, "error": "Campus agent is not connected"}

    request_id = str(uuid.uuid4())
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_requests[request_id] = future
    _pending_images[request_id] = []  # Initialize image accumulator

    try:
        await _agent_ws.send_json({
            "type": "snapshot_request",
            "classroom": classroom,
            "request_id": request_id,
        })
        logger.info(f"Sent snapshot request {request_id} for classroom: {classroom}")

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
    if secret != AGENT_SECRET:
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
