"""Tests for the CP Plus C1 facial-identity pilot (audit-only, non-additive).

These build a minimal FastAPI app that mounts only the pilot + dashboard
routers against a throwaway SQLite DB, so they never touch the production
lifespan (schedulers, sheet syncs, etc.).
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.database as database
from app.routes import c1_pilot, dashboard

SECRET = "test-secret"
ADMIN_REVIEWER = "9971166562"  # present in webhook.ADMIN_PANEL_NUMBERS
AUTH = {"x-c1-pilot-secret": SECRET}


def _count(db_path: str, sql: str) -> int:
    async def _run() -> int:
        db = await database.get_db()
        try:
            cur = await db.execute(sql)
            row = await cur.fetchone()
            return row[0] if row else 0
        finally:
            await db.close()
    return asyncio.run(_run())


@pytest.fixture
def env(monkeypatch, tmp_path):
    db_file = tmp_path / "c1_test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    monkeypatch.setenv("C1_PILOT_ENABLED", "1")
    monkeypatch.setenv("C1_PILOT_SECRET", SECRET)
    asyncio.run(database.init_db())
    return {"db_path": str(db_file), "monkeypatch": monkeypatch}


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(c1_pilot.router)
    app.include_router(dashboard.router)
    return TestClient(app)


def _ingest(client, uid="evt-1", count=5, observations=None):
    payload = {
        "event_uid": uid,
        "anonymous_count": count,
        "observations": observations
        if observations is not None
        else [
            {"track_id": "t1", "match_status": "known",
             "candidate_person_id": "STU1", "confidence": 0.95},
            {"track_id": "t2", "match_status": "unknown", "confidence": 0.3},
        ],
    }
    return client.post("/api/c1-pilot/events", json=payload, headers=AUTH)


# --------------------------------------------------------------------------
# 1. Non-additivity: identity observations never change official attendance.
# --------------------------------------------------------------------------

def test_non_additive_does_not_touch_attendance(client, env):
    before = client.get("/api/dashboard/attendance/today").json()["present_today"]
    resp = _ingest(client, count=7)
    assert resp.status_code == 200

    after = client.get("/api/dashboard/attendance/today").json()["present_today"]
    assert after == before  # dashboard number untouched
    assert _count(env["db_path"], "SELECT COUNT(*) FROM attendance_records") == 0

    metrics = client.get("/api/c1-pilot/metrics", headers=AUTH).json()
    assert metrics["official_anonymous_total"] == 7


# --------------------------------------------------------------------------
# 2. The official count is immutable — reviews/merges never change it.
# --------------------------------------------------------------------------

def test_count_is_immutable_across_review(client, env):
    _ingest(client, count=5)
    obs = client.get("/api/c1-pilot/observations", headers=AUTH).json()["observations"]
    obs_id = obs[0]["id"]
    client.post(
        "/api/c1-pilot/reviews",
        json={"observation_id": obs_id, "reviewer": ADMIN_REVIEWER,
              "action": "confirm_known"},
        headers=AUTH,
    )
    total = client.get("/api/c1-pilot/metrics", headers=AUTH).json()["official_anonymous_total"]
    assert total == 5
    assert _count(env["db_path"], "SELECT SUM(anonymous_count) FROM c1_count_events") == 5


# --------------------------------------------------------------------------
# 3. Isolation both ways.
# --------------------------------------------------------------------------

def test_pilot_ingest_creates_no_attendance_rows(client, env):
    _ingest(client)
    assert _count(env["db_path"], "SELECT COUNT(*) FROM attendance_records") == 0


def test_attendance_report_creates_no_pilot_rows(client, env):
    # Calling the production attendance ingest never creates pilot rows,
    # regardless of whether it accepts or blocks the record (day/time gated).
    resp = client.post(
        "/api/dashboard/attendance/report",
        json={"records": [{"person_id": "STU9", "name": "A", "grade": "1A"}]},
    )
    assert resp.status_code == 200
    assert _count(env["db_path"], "SELECT COUNT(*) FROM c1_count_events") == 0
    assert _count(env["db_path"], "SELECT COUNT(*) FROM c1_identity_observations") == 0


# --------------------------------------------------------------------------
# 4. Auth fails CLOSED (unlike the fail-open agent secret).
# --------------------------------------------------------------------------

def test_auth_rejects_missing_secret(client):
    assert client.get("/api/c1-pilot/metrics").status_code == 401


def test_auth_rejects_wrong_secret(client):
    r = client.get("/api/c1-pilot/metrics", headers={"x-c1-pilot-secret": "nope"})
    assert r.status_code == 401


def test_router_disabled_when_flag_off(env):
    env["monkeypatch"].setenv("C1_PILOT_ENABLED", "0")
    app = FastAPI()
    app.include_router(c1_pilot.router)
    c = TestClient(app)
    assert c.get("/api/c1-pilot/metrics", headers=AUTH).status_code == 404


def test_fails_closed_when_secret_unset(env):
    env["monkeypatch"].delenv("C1_PILOT_SECRET", raising=False)
    app = FastAPI()
    app.include_router(c1_pilot.router)
    c = TestClient(app)
    # Not fail-open: no secret configured must NOT grant access.
    assert c.get("/api/c1-pilot/metrics", headers={"x-c1-pilot-secret": ""}).status_code == 503


# --------------------------------------------------------------------------
# 5. Idempotent ingest.
# --------------------------------------------------------------------------

def test_ingest_is_idempotent(client, env):
    _ingest(client, uid="dup", count=4)
    second = _ingest(client, uid="dup", count=4)
    assert second.json()["status"] == "duplicate"
    assert _count(env["db_path"], "SELECT COUNT(*) FROM c1_count_events") == 1
    total = client.get("/api/c1-pilot/metrics", headers=AUTH).json()["official_anonymous_total"]
    assert total == 4  # not doubled


# --------------------------------------------------------------------------
# 6. Temp-ID lifecycle + case preservation (IDs travel in the body).
# --------------------------------------------------------------------------

def test_unknown_temp_id_lifecycle_and_case(client):
    mixed = "AbC123Xyz"
    _ingest(client, uid="e1", observations=[
        {"track_id": "t1", "match_status": "unknown", "temp_id": mixed},
    ])
    _ingest(client, uid="e2", observations=[
        {"track_id": "t2", "match_status": "unknown", "temp_id": mixed},
    ])
    unknowns = client.get("/api/c1-pilot/unknowns", headers=AUTH).json()["unknowns"]
    match = [u for u in unknowns if u["temp_id"] == mixed]
    assert len(match) == 1  # case preserved, single row
    assert match[0]["observation_count"] == 2


# --------------------------------------------------------------------------
# 7. Manual review: writes an audit row and gates on the admin allowlist.
# --------------------------------------------------------------------------

def test_review_writes_audit_and_updates_status(client, env):
    _ingest(client)
    obs_id = client.get("/api/c1-pilot/observations", headers=AUTH).json()["observations"][0]["id"]
    r = client.post(
        "/api/c1-pilot/reviews",
        json={"observation_id": obs_id, "reviewer": ADMIN_REVIEWER, "action": "reject"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["new_status"] == "rejected"
    assert _count(env["db_path"], "SELECT COUNT(*) FROM c1_manual_reviews") == 1


def test_review_rejects_non_admin_reviewer(client):
    _ingest(client)
    obs_id = client.get("/api/c1-pilot/observations", headers=AUTH).json()["observations"][0]["id"]
    r = client.post(
        "/api/c1-pilot/reviews",
        json={"observation_id": obs_id, "reviewer": "9000000000", "action": "reject"},
        headers=AUTH,
    )
    assert r.status_code == 403


def test_review_rejects_invalid_action(client):
    _ingest(client)
    obs_id = client.get("/api/c1-pilot/observations", headers=AUTH).json()["observations"][0]["id"]
    r = client.post(
        "/api/c1-pilot/reviews",
        json={"observation_id": obs_id, "reviewer": ADMIN_REVIEWER, "action": "bogus"},
        headers=AUTH,
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------
# 8. Retention: purge expires unknowns / deletes old obs, keeps the count.
# --------------------------------------------------------------------------

def _age_observations(days_ago: int = 400) -> None:
    async def _run():
        db = await database.get_db()
        try:
            await db.execute(
                "UPDATE c1_identity_observations "
                "SET created_at = datetime('now', ?)",
                (f"-{days_ago} days",),
            )
            await db.commit()
        finally:
            await db.close()
    asyncio.run(_run())


def test_purge_preserves_count_events(client, env):
    env["monkeypatch"].setenv("C1_PILOT_UNKNOWN_TTL_HOURS", "-1")  # expire on ingest
    _ingest(client, count=6)
    _age_observations()  # push observations well past the retention window

    result = asyncio.run(c1_pilot.purge_c1_pilot_data())
    assert result["expired_unknowns"] >= 1
    assert result["deleted_observations"] >= 1
    assert _count(env["db_path"], "SELECT COUNT(*) FROM c1_identity_observations") == 0
    # The official anonymous count survives retention.
    assert _count(env["db_path"], "SELECT COUNT(*) FROM c1_count_events") == 1
    assert _count(env["db_path"], "SELECT SUM(anonymous_count) FROM c1_count_events") == 6


# --------------------------------------------------------------------------
# 9. Metrics correctness and no PII leakage.
# --------------------------------------------------------------------------

def test_metrics_shape_and_no_pii(client):
    _ingest(client, count=10, observations=[
        {"track_id": "t1", "match_status": "known", "candidate_person_id": "STU1"},
        {"track_id": "t2", "match_status": "unknown"},
        {"track_id": "t3", "match_status": "unknown"},
    ])
    m = client.get("/api/c1-pilot/metrics", headers=AUTH).json()
    assert m["official_anonymous_total"] == 10
    assert m["observations_total"] == 3
    assert m["known"] == 1
    assert m["unknown"] == 2
    blob = str(m).lower()
    for pii in ("stu1", "name", "phone", "person_id"):
        assert pii not in blob


# --------------------------------------------------------------------------
# 10. IST timestamps.
# --------------------------------------------------------------------------

def test_captured_at_defaults_to_ist(client, env):
    _ingest(client, uid="ist-evt")

    async def _fetch():
        db = await database.get_db()
        try:
            cur = await db.execute(
                "SELECT captured_at FROM c1_count_events WHERE event_uid = 'ist-evt'"
            )
            row = await cur.fetchone()
            return row[0]
        finally:
            await db.close()

    captured = asyncio.run(_fetch())
    assert "+05:30" in captured
