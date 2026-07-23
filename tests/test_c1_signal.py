"""Tests for the anonymous, non-additive CP Plus C1 signal receiver."""

import asyncio
import importlib
import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    # Point the DB at a fresh temp file BEFORE importing the app modules so the
    # module-level DB_PATH resolution picks it up.
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_app.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("AGENT_SECRET", "")

    import app.database as database
    importlib.reload(database)
    import app.routes.gate as gate
    importlib.reload(gate)

    asyncio.run(database.init_db())

    api = FastAPI()
    api.include_router(gate.router)
    with TestClient(api) as test_client:
        yield test_client, gate


def _signal(event_id="e1", signal_type="queue", **data):
    return {
        "event_id": event_id,
        "timestamp": "23-07-2026 09:30:00 IST",
        "camera": "ENTRY GATE-OUTSIDE (CP Plus)",
        "type": signal_type,
        "verification_only": True,
        "data": data or {"occupancy": 7},
    }


def test_stores_signal_list_and_reports_verification_only(client):
    test_client, _ = client
    resp = test_client.post("/api/gate/c1-signal", json=[
        _signal("a1", "queue", occupancy=8),
        _signal("a2", "wrong_way", observed="OUT", expected="IN"),
    ])
    assert resp.status_code == 200
    body = resp.json()
    assert body["verification_only"] is True
    assert body["stored"] == 2
    assert body["duplicates"] == 0


def test_accepts_single_object(client):
    test_client, _ = client
    resp = test_client.post("/api/gate/c1-signal", json=_signal("solo", "loitering"))
    assert resp.status_code == 200
    assert resp.json()["stored"] == 1


def test_deduplicates_on_event_id(client):
    test_client, _ = client
    test_client.post("/api/gate/c1-signal", json=_signal("dup", "queue"))
    resp = test_client.post("/api/gate/c1-signal", json=_signal("dup", "queue"))
    body = resp.json()
    assert body["stored"] == 0
    assert body["duplicates"] == 1


def test_skips_events_missing_id_or_type(client):
    test_client, _ = client
    resp = test_client.post("/api/gate/c1-signal", json=[
        {"timestamp": "x", "type": "queue", "data": {}},  # no event_id
        {"event_id": "z", "data": {}},                     # no type
    ])
    body = resp.json()
    assert body["stored"] == 0
    assert body["skipped"] == 2


def test_strips_pii_fields_before_storing(client):
    test_client, _ = client
    test_client.post("/api/gate/c1-signal", json={
        "event_id": "pii1",
        "timestamp": "t",
        "camera": "C1",
        "type": "queue",
        "data": {"occupancy": 5, "person_crop": "BASE64", "name": "someone"},
    })
    got = test_client.get("/api/gate/c1-signals?signal_type=queue").json()
    stored = got["signals"][0]["data"]
    assert stored == {"occupancy": 5}
    assert "person_crop" not in stored
    assert "name" not in stored


def test_get_filters_by_type(client):
    test_client, _ = client
    test_client.post("/api/gate/c1-signal", json=[
        _signal("q1", "queue"),
        _signal("h1", "camera_health", state="offline"),
    ])
    health = test_client.get("/api/gate/c1-signals?signal_type=camera_health").json()
    assert health["count"] == 1
    assert health["signals"][0]["signal_type"] == "camera_health"
    assert health["signals"][0]["data"]["state"] == "offline"


def test_requires_agent_secret_when_configured(client):
    test_client, gate = client
    gate.AGENT_SECRET = "s3cret"
    try:
        # Missing header → 401.
        resp = test_client.post("/api/gate/c1-signal", json=_signal("sec1"))
        assert resp.status_code == 401
        # Correct header → accepted.
        resp = test_client.post(
            "/api/gate/c1-signal",
            json=_signal("sec2"),
            headers={"X-Agent-Secret": "s3cret"},
        )
        assert resp.status_code == 200
        assert resp.json()["stored"] == 1
    finally:
        gate.AGENT_SECRET = ""
