"""
CP Plus C1 facial-identity pilot — audit-only, non-additive backend.

This router is deliberately isolated from the production attendance / gate /
trueface flows:

  * The official anonymous count is written once to ``c1_count_events`` at
    ingest and is NEVER mutated by identity code — this preserves the
    "official anonymous count".
  * Identity observations are annotations linked to a count event. They are
    non-additive: they never change the count and never flow into
    ``attendance_records``, ``gate_entries`` or the notification pipeline.
  * The whole router is gated behind a feature flag and a dedicated secret,
    and it fails CLOSED (unlike the fail-open agent-secret check elsewhere),
    so the pilot can be revoked by flipping one env var.

Env:
  C1_PILOT_ENABLED         "1"/"true" to enable the router (default: off)
  C1_PILOT_SECRET          required header value (x-c1-pilot-secret)
  C1_PILOT_UNKNOWN_TTL_HOURS   TTL for unknown temp IDs (default: 72)
  C1_PILOT_OBS_TTL_DAYS        retention for observations  (default: 30)

All timestamps use IST (Asia/Kolkata, UTC+05:30).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from app.database import get_db

logger = logging.getLogger("app.c1_pilot")

IST = timezone(timedelta(hours=5, minutes=30))

router = APIRouter(prefix="/api/c1-pilot", tags=["c1-pilot"])

VALID_MATCH_STATUS = {"known", "unknown"}
VALID_REVIEW_STATUS = {"pending", "confirmed", "rejected"}
# review action -> resulting observation review_status
REVIEW_ACTIONS = {
    "confirm_known": "confirmed",
    "reject": "rejected",
    "assign_temp": "pending",
    "label_unknown": "pending",
    "discard": "rejected",
}


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def _pilot_enabled() -> bool:
    return os.environ.get("C1_PILOT_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _unknown_ttl_hours() -> int:
    try:
        return int(os.environ.get("C1_PILOT_UNKNOWN_TTL_HOURS", "72"))
    except ValueError:
        return 72


def _obs_ttl_days() -> int:
    try:
        return int(os.environ.get("C1_PILOT_OBS_TTL_DAYS", "30"))
    except ValueError:
        return 30


async def verify_c1_pilot(x_c1_pilot_secret: str = Header("")) -> None:
    """Fail-closed gate for the whole pilot router.

    Unlike ``verify_agent_secret`` (which skips auth when its env var is
    blank), this rejects every request when the pilot is disabled or the
    secret is unset/mismatched.
    """
    if not _pilot_enabled():
        raise HTTPException(status_code=404, detail="C1 pilot is disabled")
    expected = os.environ.get("C1_PILOT_SECRET", "")
    if not expected:
        # Fail CLOSED: no secret configured means no access.
        raise HTTPException(status_code=503, detail="C1 pilot secret not configured")
    if x_c1_pilot_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing C1 pilot secret")


def _require_admin_reviewer(reviewer: str) -> None:
    """Restrict manual-review writes to the existing admin allowlist."""
    try:
        from app.routes.webhook import _is_admin_panel
    except Exception:  # pragma: no cover - webhook import should always work
        # If the allowlist is unavailable, fail closed rather than open.
        raise HTTPException(status_code=403, detail="Reviewer allowlist unavailable")
    if not reviewer or not _is_admin_panel(reviewer):
        raise HTTPException(status_code=403, detail="Reviewer is not an authorized admin")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ObservationIn(BaseModel):
    track_id: str = ""
    match_status: str = "unknown"
    candidate_person_id: str | None = None
    confidence: float = 0.0
    temp_id: str | None = None
    thumb_ref: str = ""


class CountEventIn(BaseModel):
    event_uid: str
    anonymous_count: int
    camera_label: str = "C1"
    captured_at: str | None = None
    source: str = "cpplus_c1"
    frame_hash: str = ""
    observations: list[ObservationIn] = []


class ReviewIn(BaseModel):
    observation_id: int
    reviewer: str
    action: str
    note: str = ""


class MergeUnknownIn(BaseModel):
    temp_id: str
    person_id: str
    reviewer: str
    note: str = ""


# ---------------------------------------------------------------------------
# Ingest — official count (write-once) + non-additive identity observations
# ---------------------------------------------------------------------------

@router.post("/events", dependencies=[Depends(verify_c1_pilot)])
async def ingest_event(payload: CountEventIn):
    """Ingest one anonymous count event plus its identity observations.

    Idempotent on ``event_uid``: re-posting the same event neither creates a
    second count row nor duplicates observations, so agent retries can never
    inflate the official count.
    """
    if payload.anonymous_count < 0:
        raise HTTPException(status_code=400, detail="anonymous_count must be >= 0")

    captured_at = payload.captured_at or _now_ist_iso()
    db = await get_db()
    try:
        # Write-once official count. If the event already exists, we treat the
        # request as a no-op replay and do NOT touch the count or observations.
        cur = await db.execute(
            "SELECT id FROM c1_count_events WHERE event_uid = ?", (payload.event_uid,)
        )
        existing = await cur.fetchone()
        if existing is not None:
            return {
                "status": "duplicate",
                "event_id": existing["id"],
                "event_uid": payload.event_uid,
                "observations_inserted": 0,
            }

        cur = await db.execute(
            "INSERT INTO c1_count_events "
            "(event_uid, camera_label, anonymous_count, captured_at, source, frame_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                payload.event_uid,
                payload.camera_label,
                payload.anonymous_count,
                captured_at,
                payload.source,
                payload.frame_hash,
            ),
        )
        event_id = cur.lastrowid

        inserted = 0
        ttl_hours = _unknown_ttl_hours()
        for obs in payload.observations:
            match_status = obs.match_status if obs.match_status in VALID_MATCH_STATUS else "unknown"
            temp_id = obs.temp_id
            if match_status == "unknown" and not temp_id:
                temp_id = uuid.uuid4().hex  # opaque, lowercase-safe

            if match_status == "unknown" and temp_id:
                await _upsert_unknown(db, temp_id, captured_at, ttl_hours)

            await db.execute(
                "INSERT INTO c1_identity_observations "
                "(event_id, track_id, match_status, candidate_person_id, "
                "confidence, temp_id, thumb_ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    obs.track_id,
                    match_status,
                    obs.candidate_person_id,
                    obs.confidence,
                    temp_id if match_status == "unknown" else None,
                    obs.thumb_ref,
                ),
            )
            inserted += 1

        await db.commit()
        return {
            "status": "ok",
            "event_id": event_id,
            "event_uid": payload.event_uid,
            "anonymous_count": payload.anonymous_count,
            "observations_inserted": inserted,
        }
    finally:
        await db.close()


async def purge_c1_pilot_data() -> dict:
    """Retention sweep for the pilot.

    * Expires unknown temp IDs past their ``expires_at`` (status -> 'expired').
    * Deletes identity observations older than the retention window.
    * NEVER deletes ``c1_count_events`` — the official anonymous count is kept.

    Returns a dict of counts for logging. Safe to run repeatedly.
    """
    now_iso = _now_ist_iso()
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE c1_unknown_identities SET status = 'expired' "
            "WHERE status = 'active' AND expires_at < ?",
            (now_iso,),
        )
        expired_unknowns = cur.rowcount

        # created_at is a UTC CURRENT_TIMESTAMP, so compare against the same
        # clock via SQLite's datetime('now', ...) rather than an IST string.
        cur = await db.execute(
            "DELETE FROM c1_identity_observations "
            "WHERE created_at < datetime('now', ?)",
            (f"-{_obs_ttl_days()} days",),
        )
        deleted_observations = cur.rowcount

        await db.commit()
        return {
            "expired_unknowns": expired_unknowns,
            "deleted_observations": deleted_observations,
        }
    finally:
        await db.close()


async def _upsert_unknown(db, temp_id: str, seen_at: str, ttl_hours: int) -> None:
    expires_at = (datetime.now(IST) + timedelta(hours=ttl_hours)).isoformat()
    cur = await db.execute(
        "SELECT id FROM c1_unknown_identities WHERE temp_id = ?", (temp_id,)
    )
    row = await cur.fetchone()
    if row is None:
        await db.execute(
            "INSERT INTO c1_unknown_identities "
            "(temp_id, first_seen_at, last_seen_at, observation_count, status, expires_at) "
            "VALUES (?, ?, ?, 1, 'active', ?)",
            (temp_id, seen_at, seen_at, expires_at),
        )
    else:
        await db.execute(
            "UPDATE c1_unknown_identities SET "
            "last_seen_at = ?, observation_count = observation_count + 1, "
            "expires_at = ? WHERE temp_id = ?",
            (seen_at, expires_at, temp_id),
        )


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

@router.get("/observations", dependencies=[Depends(verify_c1_pilot)])
async def list_observations(
    event_id: int | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List identity observations for manual review."""
    clauses: list[str] = []
    params: list = []
    if event_id is not None:
        clauses.append("event_id = ?")
        params.append(event_id)
    if status is not None:
        if status not in VALID_REVIEW_STATUS:
            raise HTTPException(status_code=400, detail="Invalid status filter")
        clauses.append("review_status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, event_id, track_id, match_status, candidate_person_id, "
            "confidence, temp_id, thumb_ref, review_status, created_at "
            "FROM c1_identity_observations" + where +
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        rows = await cur.fetchall()
        return {"observations": [dict(r) for r in rows], "count": len(rows)}
    finally:
        await db.close()


@router.post("/reviews", dependencies=[Depends(verify_c1_pilot)])
async def record_review(payload: ReviewIn, request: Request):
    """Record a manual review decision.

    Updates the observation's ``review_status`` and appends an append-only row
    to ``c1_manual_reviews``. Never touches the official anonymous count.
    """
    if payload.action not in REVIEW_ACTIONS:
        raise HTTPException(status_code=400, detail="Invalid review action")
    _require_admin_reviewer(payload.reviewer)

    new_status = REVIEW_ACTIONS[payload.action]
    ip_address = request.client.host if request.client else ""

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT review_status FROM c1_identity_observations WHERE id = ?",
            (payload.observation_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Observation not found")
        previous_status = row["review_status"]

        await db.execute(
            "UPDATE c1_identity_observations SET review_status = ? WHERE id = ?",
            (new_status, payload.observation_id),
        )
        await db.execute(
            "INSERT INTO c1_manual_reviews "
            "(observation_id, reviewer, action, previous_status, new_status, note, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload.observation_id,
                payload.reviewer,
                payload.action,
                previous_status,
                new_status,
                payload.note,
                ip_address,
            ),
        )
        await db.commit()
        return {
            "status": "ok",
            "observation_id": payload.observation_id,
            "previous_status": previous_status,
            "new_status": new_status,
        }
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Unknown temporary identities
# ---------------------------------------------------------------------------

@router.get("/unknowns", dependencies=[Depends(verify_c1_pilot)])
async def list_unknowns(
    status: str = Query("active"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List temporary IDs assigned to unmatched (unknown) faces."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, temp_id, first_seen_at, last_seen_at, observation_count, "
            "status, merged_into_person_id, expires_at "
            "FROM c1_unknown_identities WHERE status = ? "
            "ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
        rows = await cur.fetchall()
        return {"unknowns": [dict(r) for r in rows], "count": len(rows)}
    finally:
        await db.close()


@router.post("/unknowns/merge", dependencies=[Depends(verify_c1_pilot)])
async def merge_unknown(payload: MergeUnknownIn, request: Request):
    """Merge an unknown temp ID into a known person (a review action).

    IDs are taken from the request body (never the URL path) because
    ``LowercaseURLMiddleware`` lowercases every path.
    """
    _require_admin_reviewer(payload.reviewer)
    ip_address = request.client.host if request.client else ""

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, status FROM c1_unknown_identities WHERE temp_id = ?",
            (payload.temp_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown temp_id not found")

        await db.execute(
            "UPDATE c1_unknown_identities SET status = 'merged', "
            "merged_into_person_id = ? WHERE temp_id = ?",
            (payload.person_id, payload.temp_id),
        )
        # Audit the merge against every observation that carried this temp_id.
        cur = await db.execute(
            "SELECT id FROM c1_identity_observations WHERE temp_id = ?",
            (payload.temp_id,),
        )
        obs_rows = await cur.fetchall()
        for obs in obs_rows:
            await db.execute(
                "INSERT INTO c1_manual_reviews "
                "(observation_id, reviewer, action, previous_status, new_status, note, ip_address) "
                "VALUES (?, ?, 'assign_temp', '', '', ?, ?)",
                (
                    obs["id"],
                    payload.reviewer,
                    f"merged temp {payload.temp_id} -> {payload.person_id}. {payload.note}".strip(),
                    ip_address,
                ),
            )
        await db.commit()
        return {
            "status": "ok",
            "temp_id": payload.temp_id,
            "merged_into_person_id": payload.person_id,
            "observations_relinked": len(obs_rows),
        }
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Metrics — official count reported straight from c1_count_events
# ---------------------------------------------------------------------------

@router.get("/metrics", dependencies=[Depends(verify_c1_pilot)])
async def metrics(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
):
    """Pilot metrics. The official anonymous total is read directly from
    ``c1_count_events`` and is independent of any identity data. No PII
    (names / phones / person_ids) is returned.
    """
    clauses: list[str] = []
    params: list = []
    if date_from:
        clauses.append("captured_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("captured_at <= ?")
        params.append(date_to)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT COALESCE(SUM(anonymous_count), 0) AS total, COUNT(*) AS events "
            "FROM c1_count_events" + where,
            tuple(params),
        )
        row = await cur.fetchone()
        official_total = row["total"] if row else 0
        event_count = row["events"] if row else 0

        # Identity metrics are scoped to the same events via a join, but never
        # alter the official total above.
        obs_where = ""
        if clauses:
            obs_where = " WHERE o.event_id IN (SELECT id FROM c1_count_events" + where + ")"
        cur = await db.execute(
            "SELECT "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN match_status = 'known' THEN 1 ELSE 0 END) AS known, "
            "SUM(CASE WHEN match_status = 'unknown' THEN 1 ELSE 0 END) AS unknown, "
            "SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN review_status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed, "
            "SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected "
            "FROM c1_identity_observations o" + obs_where,
            tuple(params),
        )
        o = await cur.fetchone()
        obs_total = o["total"] if o and o["total"] else 0
        known = o["known"] if o and o["known"] else 0
        unknown = o["unknown"] if o and o["unknown"] else 0

        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM c1_unknown_identities WHERE status = 'active'"
        )
        au = await cur.fetchone()

        coverage = round(100.0 * obs_total / official_total, 1) if official_total else 0.0
        return {
            "official_anonymous_total": official_total,
            "count_events": event_count,
            "observations_total": obs_total,
            "known": known,
            "unknown": unknown,
            "identity_coverage_pct": coverage,
            "review_backlog": o["pending"] if o and o["pending"] else 0,
            "confirmed": o["confirmed"] if o and o["confirmed"] else 0,
            "rejected": o["rejected"] if o and o["rejected"] else 0,
            "active_unknown_temp_ids": au["c"] if au else 0,
        }
    finally:
        await db.close()
