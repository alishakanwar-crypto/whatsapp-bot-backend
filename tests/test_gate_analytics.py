"""Targeted tests for C1 anonymous gate analytics & idempotency.

Run: poetry run pytest tests/test_gate_analytics.py

All WhatsApp sends are mocked — no Meta Cloud calls are made. A temporary
SQLite DB is used (DB_PATH set before importing app modules).
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# Point the app at a throwaway DB before importing anything that reads DB_PATH.
_TMP_DB = os.path.join(tempfile.mkdtemp(), "test_app.db")
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("GATE_ALERT_WHATSAPP_PHONES", "911111111111,922222222222")
os.environ.setdefault("GATE_EXPECTED_DIRECTION", "IN")
os.environ.setdefault("GATE_CONGESTION_PER_MIN", "5")
os.environ.setdefault("GATE_CONGESTION_WINDOW_MIN", "5")
os.environ.setdefault("GATE_LOITER_SECONDS", "180")

import app.services.gate_analytics_service as ga  # noqa: E402
import app.services.whatsapp_service as wa  # noqa: E402
from app.database import init_db, get_db  # noqa: E402
from app.routes import gate  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
async def _fresh_db():
    if os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)
    await init_db()
    yield


@pytest.fixture(autouse=True)
def _mock_send(monkeypatch):
    """Capture every anonymous alert instead of hitting Meta Cloud."""
    calls = []

    async def _fake_send(to, template_name, **kwargs):
        calls.append({"to": to, "template": template_name, "kwargs": kwargs})
        return True

    monkeypatch.setattr(wa, "send_cloud_template_message", _fake_send)
    _mock_send.calls = calls
    return calls


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _assert_anonymous(calls):
    """No alert payload may carry names, PINs, faces, or images."""
    banned = ("image", "person_crop", "snapshot", "pin", "name", "face")
    for c in calls:
        blob = repr(c).lower()
        for key in ("header_image_id", "header_image_url", "media_id"):
            assert key not in c["kwargs"], f"alert leaked media via {key}"
        for word in banned:
            assert word not in blob, f"alert payload leaked '{word}': {c}"


# 1. Idempotent ingest -------------------------------------------------------
async def test_idempotent_ingest():
    db = await get_db()
    try:
        e = {"timestamp": _ts(datetime.now(IST)), "camera": "ENTRY GATE-OUTSIDE (CP Plus)",
             "direction": "IN", "event_id": "evt-1"}
        assert await gate._store_gate_entries(db, [e]) == 1
        assert await gate._store_gate_entries(db, [e]) == 0  # duplicate ignored
        rows = await gate._get_gate_entries(db, e["timestamp"].split(" ")[0], direction="IN")
        assert len(rows) == 1
    finally:
        await db.close()


async def test_ingest_without_event_id_not_deduped():
    db = await get_db()
    try:
        e = {"timestamp": _ts(datetime.now(IST)), "camera": "CP Plus", "direction": "IN"}
        assert await gate._store_gate_entries(db, [e]) == 1
        assert await gate._store_gate_entries(db, [e]) == 1  # no event_id → both kept
    finally:
        await db.close()


# 2. Congestion --------------------------------------------------------------
async def test_congestion_alert_and_dedup(_mock_send):
    now = datetime.now(IST).replace(second=0, microsecond=0)
    db = await get_db()
    try:
        entries = [
            {"timestamp": _ts(now - timedelta(minutes=1, seconds=i)),
             "camera": "ENTRY GATE-OUTSIDE (CP Plus)", "direction": "IN",
             "event_id": f"c{i}"}
            for i in range(40)
        ]
        await gate._store_gate_entries(db, entries)
        assert await ga.check_congestion(db, now=now) is True
        assert await ga.check_congestion(db, now=now) is False  # deduped
    finally:
        await db.close()
    assert any(c["template"] == ga.GATE_ALERT_TEMPLATE for c in _mock_send)
    _assert_anonymous(_mock_send)


# 3. Wrong-way ---------------------------------------------------------------
async def test_wrong_way_alert(_mock_send):
    db = await get_db()
    try:
        entry = {"timestamp": _ts(datetime.now(IST)), "camera": "CP Plus", "direction": "OUT"}
        assert await ga.check_wrong_way(db, entry, expected_direction="IN") is True
        assert await ga.check_wrong_way(db, entry, expected_direction="IN") is False
        assert await ga.check_wrong_way(db, {**entry, "direction": "IN"},
                                        expected_direction="IN") is False
    finally:
        await db.close()
    _assert_anonymous(_mock_send)


# 4. Loitering ---------------------------------------------------------------
async def test_loitering_alert(_mock_send):
    now = datetime.now(IST)
    db = await get_db()
    try:
        entries = [
            {"timestamp": _ts(now - timedelta(seconds=300)), "camera": "CP Plus",
             "direction": "IN", "attire_color": "red", "event_id": "l1"},
            {"timestamp": _ts(now), "camera": "CP Plus",
             "direction": "IN", "attire_color": "red", "event_id": "l2"},
        ]
        await gate._store_gate_entries(db, entries)
        assert await ga.check_loitering(db) == 1
        assert await ga.check_loitering(db) == 0  # deduped
    finally:
        await db.close()
    _assert_anonymous(_mock_send)


async def test_no_loitering_short_dwell():
    now = datetime.now(IST)
    db = await get_db()
    try:
        entries = [
            {"timestamp": _ts(now - timedelta(seconds=10)), "camera": "CP Plus",
             "direction": "IN", "attire_color": "blue", "event_id": "s1"},
            {"timestamp": _ts(now), "camera": "CP Plus",
             "direction": "IN", "attire_color": "blue", "event_id": "s2"},
        ]
        await gate._store_gate_entries(db, entries)
        assert await ga.check_loitering(db) == 0
    finally:
        await db.close()


# 5. After-hours -------------------------------------------------------------
async def test_after_hours_boundary(_mock_send):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    db = await get_db()
    try:
        late = {"timestamp": f"{today} 18:30:00", "camera": "CP Plus", "direction": "IN"}
        ontime = {"timestamp": f"{today} 09:00:00", "camera": "CP Plus", "direction": "IN"}
        assert ga.is_after_hours(late["timestamp"]) is True
        assert ga.is_after_hours(ontime["timestamp"]) is False
        assert await ga.check_after_hours(db, late) is True
        assert await ga.check_after_hours(db, ontime) is False
    finally:
        await db.close()
    _assert_anonymous(_mock_send)


# 6. Vehicle flow / dwell ----------------------------------------------------
async def test_vehicle_flow_dwell():
    now = datetime.now(IST)
    date = now.strftime("%Y-%m-%d")
    db = await get_db()
    try:
        await gate._store_vehicle_entries(db, [
            {"timestamp": _ts(now - timedelta(minutes=20)), "camera": "Gate",
             "direction": "IN", "vehicle_type": "car", "event_id": "v1"},
            {"timestamp": _ts(now), "camera": "Gate",
             "direction": "OUT", "vehicle_type": "car", "event_id": "v2"},
            {"timestamp": _ts(now), "camera": "Gate",
             "direction": "IN", "vehicle_type": "bus", "event_id": "v3"},
        ])
        flow = await ga.vehicle_flow(db, date)
    finally:
        await db.close()
    assert flow["car"]["in"] == 1 and flow["car"]["out"] == 1
    assert flow["car"]["paired"] == 1 and flow["car"]["avg_dwell_min"] == 20.0
    assert flow["bus"]["in"] == 1 and flow["bus"]["inside"] == 1  # unpaired IN, no crash


# 7. Camera health -----------------------------------------------------------
async def test_camera_health_alert(_mock_send):
    db = await get_db()
    try:
        assert await ga.check_camera_health(db, "CP Plus", "offline", 5) is True
        assert await ga.check_camera_health(db, "CP Plus", "offline", 6) is False  # deduped
        assert await ga.check_camera_health(db, "CP Plus", "online", 0) is False
    finally:
        await db.close()
    _assert_anonymous(_mock_send)


# 8. Replay non-additive -----------------------------------------------------
async def test_replay_non_additive_and_alert(_mock_send):
    date = datetime.now(IST).strftime("%Y-%m-%d")
    db = await get_db()
    try:
        alerted = await ga.store_replay_recount(db, date, "09:00", "10:00",
                                                live_count=100, replay_count=92)
        assert alerted is True
        # authoritative correction: 100 (live total) - 100 (live in window) + 92 = 92
        assert await ga.authoritative_count(db, date, 100) == 92
        # re-post same window → deduped
        assert await ga.store_replay_recount(db, date, "09:00", "10:00",
                                             live_count=100, replay_count=92) is False
    finally:
        await db.close()
    _assert_anonymous(_mock_send)


async def test_replay_within_threshold_no_alert():
    date = datetime.now(IST).strftime("%Y-%m-%d")
    db = await get_db()
    try:
        assert await ga.store_replay_recount(db, date, "11:00", "12:00",
                                             live_count=50, replay_count=48) is False
    finally:
        await db.close()


# 9. Report idempotency ------------------------------------------------------
async def test_hourly_report_idempotent(_mock_send):
    r1 = await ga.hourly_analytics(hour=9)
    r2 = await ga.hourly_analytics(hour=9)
    assert r1["status"] == "sent"
    assert r2["status"] == "skipped"


# 11. Recipient reuse --------------------------------------------------------
def test_recipient_reuse(monkeypatch):
    monkeypatch.setenv("GATE_ALERT_WHATSAPP_PHONES", "933333333333, 944444444444")
    assert ga.get_gate_report_recipients() == ["933333333333", "944444444444"]


async def test_no_recipients_no_send(monkeypatch, _mock_send):
    monkeypatch.setenv("GATE_ALERT_WHATSAPP_PHONES", "")
    sent = await ga._send_anon_alert("congestion", "CP Plus", "now", "detail")
    assert sent == 0
    assert _mock_send == []
