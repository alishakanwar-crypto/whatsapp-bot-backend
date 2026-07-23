"""
CP Plus C1 (outside gate) signal receiver.

The Campus Agent's C1 worker posts anonymous, verification-only behaviour and
health signals here (queue, wrong-way, loitering, after-hours, vehicle,
camera-health, replay-discrepancy). These signals:

  * are ANONYMOUS — no faces, names, or biometrics are accepted or stored, and
  * are NON-ADDITIVE — they NEVER modify official head-count totals; they are
    stored separately purely for review/alerting.

Events are de-duplicated on their stable ``event_id`` so the agent can safely
retry a failed POST without creating duplicates.
"""

import json
import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gate", tags=["gate"])

AGENT_SECRET = os.environ.get("AGENT_SECRET", "")

# Keys that must never be persisted for an anonymous camera, even if a future
# agent build accidentally includes them.
_FORBIDDEN_KEYS = {"person_crop", "name", "face", "embedding", "snapshot", "image"}

_KNOWN_SIGNAL_TYPES = {
    "queue",
    "wrong_way",
    "loitering",
    "after_hours",
    "vehicle",
    "vehicle_dwell",
    "camera_health",
    "replay_discrepancy",
}


async def verify_agent_secret(x_agent_secret: str = Header("")) -> None:
    """Verify the agent secret header (skipped when no secret is configured)."""
    if not AGENT_SECRET:
        return
    if x_agent_secret != AGENT_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing agent secret")


def _strip_pii(data: dict) -> dict:
    """Drop any biometric/identity keys so nothing sensitive is persisted."""
    return {k: v for k, v in data.items() if k.lower() not in _FORBIDDEN_KEYS}


@router.post("/c1-signal", dependencies=[Depends(verify_agent_secret)])
async def receive_c1_signal(request: Request):
    """Store anonymous, non-additive C1 signal events.

    Accepts either a single event object or a list of them. Official totals are
    never touched here — these events live only in the ``c1_signals`` table.
    """
    body = await request.json()
    events = body if isinstance(body, list) else [body]

    stored = 0
    duplicates = 0
    skipped = 0
    db = await get_db()
    try:
        for event in events:
            if not isinstance(event, dict):
                skipped += 1
                continue
            event_id = str(event.get("event_id") or "").strip()
            signal_type = str(event.get("type") or "").strip()
            if not event_id or not signal_type:
                skipped += 1
                continue

            raw_data = event.get("data")
            data = _strip_pii(raw_data) if isinstance(raw_data, dict) else {}

            cursor = await db.execute(
                "INSERT OR IGNORE INTO c1_signals "
                "(event_id, signal_type, camera, event_timestamp, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event_id,
                    signal_type,
                    str(event.get("camera") or ""),
                    str(event.get("timestamp") or ""),
                    json.dumps(data),
                ),
            )
            if cursor.rowcount and cursor.rowcount > 0:
                stored += 1
                if signal_type not in _KNOWN_SIGNAL_TYPES:
                    logger.warning("C1 signal with unknown type: %s", signal_type)
            else:
                duplicates += 1
        await db.commit()
    finally:
        await db.close()

    logger.info(
        "C1 signals received: stored=%d duplicates=%d skipped=%d",
        stored, duplicates, skipped,
    )
    return {
        "status": "ok",
        "verification_only": True,
        "stored": stored,
        "duplicates": duplicates,
        "skipped": skipped,
    }


@router.get("/c1-signals", dependencies=[Depends(verify_agent_secret)])
async def list_c1_signals(signal_type: str = "", limit: int = 100):
    """Return recent C1 signals (most recent first), optionally filtered by type."""
    limit = max(1, min(limit, 1000))
    db = await get_db()
    try:
        if signal_type:
            cursor = await db.execute(
                "SELECT event_id, signal_type, camera, event_timestamp, data, "
                "created_at FROM c1_signals WHERE signal_type = ? "
                "ORDER BY id DESC LIMIT ?",
                (signal_type, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT event_id, signal_type, camera, event_timestamp, data, "
                "created_at FROM c1_signals ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        signals = []
        for r in rows:
            item = dict(r)
            try:
                item["data"] = json.loads(item["data"])
            except (ValueError, TypeError):
                item["data"] = {}
            signals.append(item)
        return {"signals": signals, "count": len(signals)}
    finally:
        await db.close()
