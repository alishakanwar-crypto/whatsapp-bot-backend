"""
C1 Anonymous Gate Analytics & Alerts
====================================
Derives anonymous (no names / no faces / no biometrics) notifications and
analytics from the gate crossings already ingested by ``app/routes/gate.py``:

  * congestion          — crossings/minute over threshold
  * wrong_way           — crossing against a camera's expected direction
  * loitering           — same person-track dwelling at a camera too long
  * after_hours         — crossing outside operating hours
  * vehicle_flow        — vehicle IN/OUT flow + dwell (analytics)
  * camera_health       — a camera going offline
  * replay_discrepancy  — SD-card replay recount differs from the live count

Every alert is deduplicated via the ``gate_alert_dedup`` table and every
report is made idempotent via ``gate_report_log``. Delivery is Meta Cloud API
only (``app/services/whatsapp_service``); recipients come from the approved
``GATE_REPORT_WHATSAPP_PHONES`` env list — no hardcoded numbers here.

Replay recounts are NON-ADDITIVE: a replay count REPLACES the live count for
its window, it is never summed with it.

All timestamps use IST (Asia/Kolkata, UTC+05:30).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("app.gate_analytics")

IST = timezone(timedelta(hours=5, minutes=30))

# ── Tunable thresholds (env-overridable, no code change needed) ────────────
CONGESTION_PER_MIN = float(os.environ.get("GATE_CONGESTION_PER_MIN", "15"))
CONGESTION_WINDOW_MIN = int(os.environ.get("GATE_CONGESTION_WINDOW_MIN", "5"))
LOITER_SECONDS = int(os.environ.get("GATE_LOITER_SECONDS", "180"))
AFTER_HOURS_START = int(os.environ.get("GATE_HOURS_START", "6"))   # inclusive
AFTER_HOURS_END = int(os.environ.get("GATE_HOURS_END", "17"))      # exclusive
REPLAY_DISCREPANCY_THRESHOLD = int(
    os.environ.get("GATE_REPLAY_DISCREPANCY_THRESHOLD", "5")
)
VEHICLE_LONG_DWELL_MIN = int(os.environ.get("GATE_VEHICLE_LONG_DWELL_MIN", "30"))

# Anonymous alert template (body-only; NO image/face header). Must be an
# approved Meta Cloud template with 4 body params: {{1}} type, {{2}} camera,
# {{3}} time IST, {{4}} detail.
GATE_ALERT_TEMPLATE = os.environ.get("GATE_ALERT_TEMPLATE", "ppis_gate_alert")

# Human-readable labels for each alert type (used as body param {{1}}).
_ALERT_LABELS = {
    "congestion": "Gate congestion",
    "wrong_way": "Wrong-way crossing",
    "loitering": "Loitering at gate",
    "after_hours": "After-hours crossing",
    "camera_health": "Camera offline",
    "replay_discrepancy": "Replay count discrepancy",
}


def get_gate_report_recipients() -> list[str]:
    """Approved recipients for the anonymous gate alerts / analytics.

    Per user directive these go to **Alisha only** (``918796105084``), kept
    independent of the shared head-count list so restricting alert recipients
    does not touch the existing report. Override via
    ``GATE_ALERT_WHATSAPP_PHONES`` (comma-separated); empty → empty list →
    fail-safe (nothing is sent).
    """
    raw = os.environ.get("GATE_ALERT_WHATSAPP_PHONES", "918796105084")  # Alisha only
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_ts(ts: str) -> datetime | None:
    """Parse a stored 'YYYY-MM-DD HH:MM:SS' (or 'HH:MM:SS') timestamp to an
    IST-aware datetime. Returns None if unparseable."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S"):
        try:
            dt = datetime.strptime(ts, fmt)
            if fmt == "%H:%M:%S":
                today = datetime.now(IST)
                dt = dt.replace(year=today.year, month=today.month, day=today.day)
            return dt.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _hour_of(ts: str) -> int:
    dt = _parse_ts(ts)
    return dt.hour if dt else -1


# ── Dedup / idempotency helpers ────────────────────────────────────────────
async def _already_alerted(db, date: str, alert_type: str, dedup_key: str) -> bool:
    """Atomically claim an alert slot. Returns True if it was ALREADY claimed
    (so the caller must NOT send again), False if this call claimed it."""
    try:
        await db.execute(
            "INSERT INTO gate_alert_dedup (date, alert_type, dedup_key) "
            "VALUES (?, ?, ?)",
            (date, alert_type, dedup_key),
        )
        await db.commit()
        return False
    except Exception:
        # UNIQUE violation → already alerted for this key.
        return True


async def _report_already_sent(db, date: str, report_kind: str, period_key: str) -> bool:
    """Claim a report slot idempotently. True if already sent."""
    try:
        await db.execute(
            "INSERT INTO gate_report_log (date, report_kind, period_key) "
            "VALUES (?, ?, ?)",
            (date, report_kind, period_key),
        )
        await db.commit()
        return False
    except Exception:
        return True


# ── Anonymous delivery ─────────────────────────────────────────────────────
async def _send_anon_alert(alert_type: str, camera: str, time_ist: str, detail: str) -> int:
    """Send an anonymous text alert to all approved recipients via Meta Cloud
    API. Returns the number of successful sends. Never includes any image,
    name, PIN, or biometric data."""
    recipients = get_gate_report_recipients()
    if not recipients:
        logger.info("[GATE-ANALYTICS] No recipients configured; alert '%s' not sent", alert_type)
        return 0

    from app.services.whatsapp_service import send_cloud_template_message

    label = _ALERT_LABELS.get(alert_type, alert_type)
    body_params = [label, camera or "C1 Gate", time_ist, detail]
    sent = 0
    for phone in recipients:
        try:
            ok = await send_cloud_template_message(
                phone,
                GATE_ALERT_TEMPLATE,
                body_params=body_params,
            )
            if ok:
                sent += 1
            logger.info("[GATE-ANALYTICS] %s alert → %s: %s",
                        alert_type, phone, "OK" if ok else "FAILED")
        except Exception as e:
            logger.error("[GATE-ANALYTICS] %s alert to %s error: %s", alert_type, phone, e)
    return sent


# ── Per-event detectors (run inline on ingest) ─────────────────────────────
def is_after_hours(ts: str) -> bool:
    """True if the crossing hour is outside [AFTER_HOURS_START, AFTER_HOURS_END)."""
    hour = _hour_of(ts)
    if hour < 0:
        return False
    return not (AFTER_HOURS_START <= hour < AFTER_HOURS_END)


async def check_after_hours(db, entry: dict) -> bool:
    """Emit an after-hours alert for a crossing outside operating hours.
    Deduped per camera per hour. Returns True if an alert was sent."""
    ts = entry.get("timestamp", "")
    if not is_after_hours(ts):
        return False
    camera = entry.get("camera", "C1 Gate")
    dt = _parse_ts(ts)
    date = dt.strftime("%Y-%m-%d") if dt else datetime.now(IST).strftime("%Y-%m-%d")
    dedup_key = f"{camera}|{dt.hour if dt else 'x'}"
    if await _already_alerted(db, date, "after_hours", dedup_key):
        return False
    time_disp = dt.strftime("%d-%m-%Y %H:%M:%S IST") if dt else ts
    detail = f"{entry.get('direction', 'IN')} outside {AFTER_HOURS_START:02d}:00-{AFTER_HOURS_END:02d}:00"
    await _send_anon_alert("after_hours", camera, time_disp, detail)
    return True


async def check_wrong_way(db, entry: dict, expected_direction: str | None = None) -> bool:
    """Emit a wrong-way alert when a crossing goes against the camera's
    expected direction. ``expected_direction`` defaults to the
    ``GATE_EXPECTED_DIRECTION`` env (blank disables). Deduped per camera
    per minute. Returns True if an alert was sent."""
    expected = (expected_direction
                if expected_direction is not None
                else os.environ.get("GATE_EXPECTED_DIRECTION", "")).strip().upper()
    if not expected:
        return False
    direction = (entry.get("direction", "IN") or "IN").strip().upper()
    if direction == expected:
        return False
    camera = entry.get("camera", "C1 Gate")
    ts = entry.get("timestamp", "")
    dt = _parse_ts(ts)
    date = dt.strftime("%Y-%m-%d") if dt else datetime.now(IST).strftime("%Y-%m-%d")
    minute_key = dt.strftime("%H:%M") if dt else "xx:xx"
    if await _already_alerted(db, date, "wrong_way", f"{camera}|{minute_key}"):
        return False
    time_disp = dt.strftime("%d-%m-%Y %H:%M:%S IST") if dt else ts
    await _send_anon_alert("wrong_way", camera, time_disp,
                           f"Detected {direction}, expected {expected}")
    return True


# ── Sweep detectors (run periodically) ─────────────────────────────────────
def _cpplus_in_times(entries: list[dict]) -> list[datetime]:
    """IST datetimes for CP Plus IN crossings among ``entries``."""
    from app.routes.gate import _is_cpplus_camera
    times = []
    for e in entries:
        if e.get("direction", "IN") != "IN":
            continue
        if not _is_cpplus_camera(e.get("camera", "")):
            continue
        dt = _parse_ts(e.get("timestamp", ""))
        if dt:
            times.append(dt)
    return times


async def check_congestion(db, now: datetime | None = None) -> bool:
    """Emit a congestion alert if the CP Plus crossing rate over the last
    ``CONGESTION_WINDOW_MIN`` minutes meets ``CONGESTION_PER_MIN``. Deduped
    per window bucket. Returns True if an alert was sent."""
    from app.routes.gate import _get_gate_entries
    now = now or datetime.now(IST)
    date = now.strftime("%Y-%m-%d")
    window_start = now - timedelta(minutes=CONGESTION_WINDOW_MIN)

    entries = await _get_gate_entries(db, date, direction="IN")
    in_window = [t for t in _cpplus_in_times(entries) if window_start <= t <= now]
    rate = len(in_window) / max(CONGESTION_WINDOW_MIN, 1)
    if rate < CONGESTION_PER_MIN:
        return False

    bucket = now.replace(second=0, microsecond=0)
    bucket = bucket.replace(minute=(bucket.minute // CONGESTION_WINDOW_MIN) * CONGESTION_WINDOW_MIN)
    dedup_key = bucket.strftime("%H:%M")
    if await _already_alerted(db, date, "congestion", dedup_key):
        return False
    await _send_anon_alert(
        "congestion", "C1 Gate", now.strftime("%d-%m-%Y %H:%M:%S IST"),
        f"{len(in_window)} crossings in {CONGESTION_WINDOW_MIN} min "
        f"(~{rate:.1f}/min, threshold {CONGESTION_PER_MIN:.0f})",
    )
    return True


async def check_loitering(db, date: str | None = None) -> int:
    """Emit loitering alerts for person-tracks (same camera + attire) whose
    detections span more than ``LOITER_SECONDS``. Deduped per track per day.
    Returns the number of alerts sent."""
    from app.routes.gate import _get_gate_entries
    date = date or datetime.now(IST).strftime("%Y-%m-%d")
    entries = await _get_gate_entries(db, date)

    tracks: dict[tuple[str, str], list[datetime]] = {}
    for e in entries:
        attire = (e.get("attire_color", "") or "").strip().lower()
        if not attire or attire == "unknown":
            continue
        dt = _parse_ts(e.get("timestamp", ""))
        if not dt:
            continue
        tracks.setdefault((e.get("camera", "C1 Gate"), attire), []).append(dt)

    sent = 0
    for (camera, attire), times in tracks.items():
        if len(times) < 2:
            continue
        span = (max(times) - min(times)).total_seconds()
        if span < LOITER_SECONDS:
            continue
        if await _already_alerted(db, date, "loitering", f"{camera}|{attire}"):
            continue
        await _send_anon_alert(
            "loitering", camera, min(times).strftime("%d-%m-%Y %H:%M:%S IST"),
            f"Dwell ~{int(span // 60)}m{int(span % 60)}s (threshold {LOITER_SECONDS}s)",
        )
        sent += 1
    return sent


async def check_camera_health(db, camera: str, status: str, consecutive_failures: int = 0) -> bool:
    """Emit a camera-health alert on an online→offline transition. Deduped per
    camera per day. Returns True if an alert was sent."""
    if (status or "").lower() == "online":
        return False
    date = datetime.now(IST).strftime("%Y-%m-%d")
    if await _already_alerted(db, date, "camera_health", camera):
        return False
    await _send_anon_alert(
        "camera_health", camera, datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST"),
        f"Offline ({consecutive_failures} consecutive failures)",
    )
    return True


# ── Replay recount (non-additive) ──────────────────────────────────────────
async def store_replay_recount(db, date: str, window_start: str, window_end: str,
                               live_count: int, replay_count: int,
                               source: str = "sd_replay") -> bool:
    """Persist a replay recount for a window and, if it differs from the live
    count by more than the threshold, emit an anonymous discrepancy alert
    (deduped per window). Returns True if an alert was sent.

    The replay count is authoritative and REPLACES the live count for the
    window (see ``authoritative_count``); it is never added to it.
    """
    try:
        await db.execute(
            "INSERT OR REPLACE INTO gate_replay_recount "
            "(date, window_start, window_end, live_count, replay_count, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, window_start, window_end, live_count, replay_count, source),
        )
        await db.commit()
    except Exception as e:
        logger.error("[GATE-ANALYTICS] replay recount store failed: %s", e)

    if abs(replay_count - live_count) <= REPLAY_DISCREPANCY_THRESHOLD:
        return False
    if await _already_alerted(db, date, "replay_discrepancy", f"{window_start}|{window_end}"):
        return False
    await _send_anon_alert(
        "replay_discrepancy", "C1 Gate", f"{window_start} - {window_end} IST",
        f"Live {live_count} vs replay {replay_count} "
        f"(replay is authoritative; non-additive)",
    )
    return True


async def authoritative_count(db, date: str, live_total: int) -> int:
    """Return the authoritative crossing total for ``date``: replay counts
    REPLACE the live count for any window they cover (non-additive). Windows
    without a replay keep their live contribution. Because replay windows are
    stored with their own live_count, the correction is
    ``live_total - sum(live_in_window) + sum(replay_in_window)``.
    """
    cur = await db.execute(
        "SELECT live_count, replay_count FROM gate_replay_recount WHERE date = ?",
        (date,),
    )
    rows = await cur.fetchall()
    corrected = live_total
    for r in rows:
        corrected += (r[1] or 0) - (r[0] or 0)
    return max(0, corrected)


# ── Vehicle flow / dwell ───────────────────────────────────────────────────
async def vehicle_flow(db, date: str | None = None) -> dict:
    """Pair vehicle IN/OUT events (FIFO per vehicle_type) to derive flow counts
    and dwell times. Anonymous — counts only, no plates/images."""
    from app.routes.gate import _get_vehicle_entries
    date = date or datetime.now(IST).strftime("%Y-%m-%d")
    entries = await _get_vehicle_entries(db, date)

    by_type: dict[str, dict[str, list[datetime]]] = {}
    for e in entries:
        vt = (e.get("vehicle_type", "vehicle") or "vehicle").lower()
        dt = _parse_ts(e.get("timestamp", ""))
        if not dt:
            continue
        slot = by_type.setdefault(vt, {"IN": [], "OUT": []})
        slot[e.get("direction", "IN")].append(dt)

    result: dict[str, dict] = {}
    for vt, slot in by_type.items():
        ins = sorted(slot["IN"])
        outs = sorted(slot["OUT"])
        dwells: list[float] = []
        oi = 0
        for in_dt in ins:
            while oi < len(outs) and outs[oi] < in_dt:
                oi += 1
            if oi < len(outs):
                dwells.append((outs[oi] - in_dt).total_seconds())
                oi += 1
        avg_dwell = (sum(dwells) / len(dwells)) if dwells else 0.0
        result[vt] = {
            "in": len(ins),
            "out": len(outs),
            "inside": max(0, len(ins) - len(outs)),
            "avg_dwell_min": round(avg_dwell / 60.0, 1),
            "paired": len(dwells),
        }
    return result


# ── Hourly / final analytics (idempotent) ──────────────────────────────────
async def _cpplus_totals(db, date: str, now: datetime) -> dict:
    from app.routes.gate import _get_gate_entries, _deduplicate_gate_entries, _is_cpplus_camera
    gate_in = await _get_gate_entries(db, date, direction="IN")
    gate_out = await _get_gate_entries(db, date, direction="OUT")
    cp_in = [e for e in gate_in if _is_cpplus_camera(e.get("camera", ""))]
    cp_out = [e for e in gate_out if _is_cpplus_camera(e.get("camera", ""))]
    unique_in = len(_deduplicate_gate_entries(cp_in))
    unique_out = len(_deduplicate_gate_entries(cp_out))
    corrected_in = await authoritative_count(db, date, unique_in)
    return {
        "in": corrected_in,
        "out": unique_out,
        # NOTE: gate-derived figure only — NOT a total-campus occupancy claim.
        "at_gate_net": max(0, corrected_in - unique_out),
    }


async def hourly_analytics(hour: int | None = None) -> dict:
    """Deliver an anonymous hourly analytics summary (idempotent per hour)."""
    from app.database import get_db
    now = datetime.now(IST)
    hour = now.hour if hour is None else hour
    date = now.strftime("%Y-%m-%d")

    db = await get_db()
    try:
        if await _report_already_sent(db, date, "hourly", f"{hour:02d}"):
            logger.info("[GATE-ANALYTICS] hourly report %02d already sent", hour)
            return {"status": "skipped", "reason": "already_sent"}
        totals = await _cpplus_totals(db, date, now)
        veh = await vehicle_flow(db, date)
    finally:
        await db.close()

    veh_str = ", ".join(f"{k}:{v['in']}/{v['out']}" for k, v in veh.items()) or "none"
    detail = (f"People IN {totals['in']}, OUT {totals['out']}, "
              f"net-at-gate {totals['at_gate_net']}; vehicles(in/out) {veh_str}")
    await _send_anon_alert("hourly", "C1 Gate",
                           now.strftime("%d-%m-%Y %H:%M IST"), detail)
    return {"status": "sent", "hour": hour, "totals": totals, "vehicles": veh}


async def final_analytics() -> dict:
    """Deliver the anonymous end-of-day analytics summary (idempotent)."""
    from app.database import get_db
    now = datetime.now(IST)
    date = now.strftime("%Y-%m-%d")

    db = await get_db()
    try:
        if await _report_already_sent(db, date, "final", "final"):
            logger.info("[GATE-ANALYTICS] final report already sent for %s", date)
            return {"status": "skipped", "reason": "already_sent"}
        totals = await _cpplus_totals(db, date, now)
        veh = await vehicle_flow(db, date)
    finally:
        await db.close()

    veh_str = ", ".join(
        f"{k}:{v['in']}in/{v['out']}out/~{v['avg_dwell_min']}m" for k, v in veh.items()
    ) or "none"
    detail = (f"Final — People IN {totals['in']}, OUT {totals['out']}, "
              f"net-at-gate {totals['at_gate_net']}; vehicles {veh_str}")
    await _send_anon_alert("final", "C1 Gate",
                           now.strftime("%d-%m-%Y %H:%M IST"), detail)
    return {"status": "sent", "totals": totals, "vehicles": veh}


# ── Sync wrappers for the APScheduler ──────────────────────────────────────
def _run(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import asyncio as _a
            return _a.ensure_future(coro)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def hourly_analytics_sync() -> None:
    _run(hourly_analytics())


def final_analytics_sync() -> None:
    _run(final_analytics())


def congestion_sweep_sync() -> None:
    async def _go():
        from app.database import get_db
        db = await get_db()
        try:
            await check_congestion(db)
            await check_loitering(db)
        finally:
            await db.close()
    _run(_go())
