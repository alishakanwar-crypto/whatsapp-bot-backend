"""Watch the campus systems so a fault is found by the bot, not by a parent.

Every fault this term (a recorder locked out, the campus PC left running stale
code, a camera that stopped answering, photos taking minutes) was reported by
somebody at school first, because nothing checked the cameras unless a parent
asked for one. This service checks continuously and says what broke, where, and
what to do about it:

* ``check_campus_link`` — every 5 minutes: WhatsApp the admins once when the
  campus PC's link has been down for more than 5 minutes during school hours,
  and once again when it returns.
* ``sweep_cameras`` — every 30 minutes during school hours: capture from one
  classroom per recorder, rotating through the classrooms so every camera is
  proved over the day. Alerts once per incident when a recorder stops
  answering or gets slow, and reports recovery.
* ``morning_readiness`` — 07:15 IST on working days: one message stating
  whether the campus PC is up, which commit it runs, and whether every
  recorder captured — before the school day starts.

A recorder that is refusing our login is never probed: retrying a rejected
password is what kept DVR 2 locked, and those recorders are already alerted on.
"""

import asyncio
import logging
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Who can walk up to the campus PC or a recorder.
CAMPUS_WATCH_NUMBERS = [
    number.strip()
    for number in os.environ.get(
        "CAMPUS_WATCH_NUMBERS", "919971166562,919599488106"
    ).split(",")
    if number.strip()
]

SCHOOL_DAY_START_HOUR = 7
SCHOOL_DAY_END_HOUR = 17
# A parent waits for their photo, so a capture this slow is a fault even
# though it eventually succeeds.
SLOW_CAPTURE_SECONDS = float(os.environ.get("CAMPUS_WATCH_SLOW_SECONDS", "25"))
LINK_DOWN_ALERT_SECONDS = float(
    os.environ.get("CAMPUS_WATCH_LINK_DOWN_SECONDS", "300")
)
PROBE_TIMEOUT_SECONDS = float(
    os.environ.get("CAMPUS_WATCH_PROBE_TIMEOUT_SECONDS", "45")
)

# Last result per recorder camera, so the sweep can be read from the dashboard
# and an alert is sent on the change rather than on every sweep. Keyed by
# camera, not recorder: consecutive sweeps test different classrooms, so a
# healthy room must not announce a broken room's recovery.
_recorder_state: dict[tuple[str, str], dict] = {}
# Which classroom on each recorder to probe next, so the whole campus is
# covered over the day without capturing 128 cameras every sweep.
_next_classroom_index: dict[str, int] = {}
_link_alert_sent = False
# When we first saw the link down ourselves. The connection state only knows
# about a disconnect it witnessed, so a deploy while the campus PC is off would
# otherwise look like zero downtime forever and never alert.
_link_down_since: float | None = None
# The scheduler runs jobs on worker threads, but a probe has to travel over the
# campus WebSocket, which only exists on the app's own event loop.
_app_loop: asyncio.AbstractEventLoop | None = None


def remember_event_loop() -> None:
    """Called at startup so scheduled checks can reach the campus link."""
    global _app_loop
    _app_loop = asyncio.get_running_loop()


def _run_on_app_loop(coro, timeout: float) -> None:
    if _app_loop is None or _app_loop.is_closed():
        logger.warning("CAMPUS WATCH: app event loop unavailable, skipping")
        coro.close()
        return
    asyncio.run_coroutine_threadsafe(coro, _app_loop).result(timeout=timeout)


def watch_state() -> dict:
    """What the last sweep found, for /api/agent/health and troubleshooting."""
    return {
        "cameras": {
            f"{ip} {classroom}": dict(state)
            for (ip, classroom), state in sorted(_recorder_state.items())
        },
        "link_alert_sent": _link_alert_sent,
        "alerting_numbers": len(CAMPUS_WATCH_NUMBERS),
    }


def within_school_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    return SCHOOL_DAY_START_HOUR <= now.hour < SCHOOL_DAY_END_HOUR


async def is_working_day(day: date | None = None) -> bool:
    """Sundays and recorded school holidays are not watched."""
    from app.database import get_db

    day = day or datetime.now(IST).date()
    if day.weekday() == 6:
        return False
    db = None
    try:
        db = await get_db()
        cursor = await db.execute(
            "SELECT 1 FROM school_holidays WHERE date = ?", (day.isoformat(),)
        )
        return await cursor.fetchone() is None
    except Exception as exc:
        logger.warning("CAMPUS WATCH: could not read holidays: %s", exc)
        return True
    finally:
        if db is not None:
            await db.close()


async def _alert(message: str) -> bool:
    """WhatsApp the admins. False if nobody was actually reached.

    The caller records an incident as reported only on a true, otherwise a
    failed send would silence the whole incident.
    """
    from app.services.whatsapp_service import send_whatsapp_force

    delivered = False
    for number in CAMPUS_WATCH_NUMBERS:
        try:
            if await send_whatsapp_force(number, message):
                delivered = True
        except Exception as exc:
            logger.warning("CAMPUS WATCH: could not alert %s: %s", number, exc)
    if not delivered:
        logger.error("CAMPUS WATCH: alert reached nobody: %s", message)
    return delivered


def _now_ist() -> str:
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")


# ---------------------------------------------------------------------------
# Campus link
# ---------------------------------------------------------------------------
async def check_campus_link() -> None:
    """Alert when the campus PC's link is down, and when it comes back."""
    global _link_alert_sent, _link_down_since
    from app.routes.agent_ws import get_health_state

    health = get_health_state()
    if health["connected"]:
        _link_down_since = None
        if _link_alert_sent:
            delivered = await _alert(
                "PPIS Bot — Campus PC Back Online\n\n"
                f"The campus agent reconnected at {_now_ist()} "
                f"(running commit {health.get('agent_code_commit') or 'unknown'}"
                f", started {health.get('agent_started_at_ist') or 'unknown'})."
                "\n\nLive photos are working again."
            )
            if delivered:
                _link_alert_sent = False
        return
    if _link_down_since is None:
        _link_down_since = time.monotonic()
    if not within_school_hours() or not await is_working_day():
        return
    # The connection state only counts a disconnect it witnessed, so after a
    # deploy while the campus PC is off it reports no downtime at all — our own
    # first sighting of the outage is what makes the alert fire either way.
    down_for = max(
        float(health.get("disconnected_seconds") or 0.0),
        time.monotonic() - _link_down_since,
    )
    if down_for < LINK_DOWN_ALERT_SECONDS or _link_alert_sent:
        return
    _link_alert_sent = await _alert(
        "PPIS Bot — Campus PC Offline\n\n"
        f"The campus agent has not been connected for {down_for / 60:.0f} "
        f"minutes as of {_now_ist()}, so no live photo can be captured and "
        "classroom face attendance is not running.\n\n"
        "Please check the campus PC is powered on and online, then run "
        "restart_all_admin.vbs. You will get a message here when it is back."
    )


# ---------------------------------------------------------------------------
# Camera sweep
# ---------------------------------------------------------------------------
async def _classrooms_by_recorder() -> dict[str, list[str]]:
    """Recorder IP -> classrooms mapped to it, from the pushed agent config."""
    from app.database import get_db

    db = None
    try:
        db = await get_db()
        cursor = await db.execute("SELECT ip FROM agent_dvrs ORDER BY id")
        ips = [row["ip"] for row in await cursor.fetchall()]
        cursor = await db.execute(
            "SELECT location, dvr_index FROM agent_camera_mapping "
            "ORDER BY location"
        )
        rooms: dict[str, list[str]] = {}
        for row in await cursor.fetchall():
            index = row["dvr_index"]
            if index is None or index < 0 or index >= len(ips):
                continue
            rooms.setdefault(ips[index], []).append(row["location"])
        return rooms
    except Exception as exc:
        logger.warning("CAMPUS WATCH: could not read camera mapping: %s", exc)
        return {}
    finally:
        if db is not None:
            await db.close()


def _recorders_held_for_password() -> set[str]:
    from app.routes.agent_ws import get_health_state

    return {
        str(entry.get("ip"))
        for entry in get_health_state().get("recorders_on_fallback") or []
        if entry.get("reason") == "credentials refused"
    }


def _next_classroom(ip: str, classrooms: list[str]) -> str:
    index = _next_classroom_index.get(ip, 0) % len(classrooms)
    _next_classroom_index[ip] = index + 1
    return classrooms[index]


async def _probe_classroom(classroom: str) -> dict:
    """Capture from one classroom without sending the photo anywhere."""
    from app.routes.agent_ws import request_snapshot

    started = time.monotonic()
    try:
        result = await request_snapshot(
            classroom, timeout=PROBE_TIMEOUT_SECONDS
        )
    except Exception as exc:
        return {
            "classroom": classroom,
            "ok": False,
            "seconds": round(time.monotonic() - started, 1),
            "error": str(exc),
        }
    seconds = round(time.monotonic() - started, 1)
    images = result.get("image_count", 0) if result.get("success") else 0
    return {
        "classroom": classroom,
        "ok": bool(images),
        "images": images,
        "seconds": seconds,
        "error": "" if images else (result.get("error") or "no photo returned"),
    }


async def sweep_cameras(alert: bool = True) -> list[dict]:
    """Prove one classroom per recorder and alert on any change for the worse."""
    from app.routes.agent_ws import is_agent_connected

    if not is_agent_connected():
        return []
    rooms = await _classrooms_by_recorder()
    if not rooms:
        return []
    held = _recorders_held_for_password()
    probes = {
        ip: _next_classroom(ip, classrooms)
        for ip, classrooms in sorted(rooms.items())
        if classrooms and ip not in held
    }
    results = await asyncio.gather(
        *(_probe_classroom(classroom) for classroom in probes.values()),
        return_exceptions=False,
    )
    findings = []
    for ip, probe in zip(probes.keys(), results, strict=True):
        verdict = (
            "ok"
            if probe["ok"] and probe["seconds"] <= SLOW_CAPTURE_SECONDS
            else "slow"
            if probe["ok"]
            else "failed"
        )
        finding = {**probe, "ip": ip, "verdict": verdict, "at_ist": _now_ist()}
        findings.append(finding)
        key = (ip, probe["classroom"])
        seen = _recorder_state.get(key, {})
        previous = seen.get("verdict", "ok")
        _recorder_state[key] = finding
        if not alert:
            continue
        # A recorder's rooms fail together, and each sweep tests a different
        # one, so one incident per recorder is announced and recovery waits
        # until no room on it is still bad.
        elsewhere_bad = any(
            state["verdict"] != "ok"
            for (state_ip, room), state in _recorder_state.items()
            if state_ip == ip and room != probe["classroom"]
        )
        changed = verdict != previous or not seen.get("reported", True)
        if elsewhere_bad or not changed:
            finding["reported"] = True
            continue
        finding["reported"] = await _report_change(
            ip, previous, finding, rooms.get(ip, [])
        )
    logger.info(
        "CAMPUS WATCH: %s",
        "; ".join(
            f"{f['ip']} {f['classroom']} {f['verdict']} {f['seconds']}s"
            for f in findings
        ),
    )
    return findings


async def _report_change(
    ip: str, previous: str, finding: dict, classrooms: list[str]
) -> bool:
    """Tell the admins when a camera changes state, once per change.

    Returns whether the message actually reached anybody, so an undelivered
    alert is retried on the next sweep instead of silencing the incident.
    """
    rooms = f"{len(classrooms)} classroom(s)"
    if finding["verdict"] == "ok":
        return await _alert(
            "PPIS Bot — Cameras Recovered\n\n"
            f"Recorder {ip} is capturing again as of {finding['at_ist']} "
            f"({finding['classroom']} in {finding['seconds']}s), so {rooms} "
            "can send live photos."
        )
    if finding["verdict"] == "slow":
        return await _alert(
            "PPIS Bot — Cameras Slow\n\n"
            f"Recorder {ip} took {finding['seconds']}s to capture "
            f"{finding['classroom']} at {finding['at_ist']} — parents on "
            f"{rooms} will wait that long for a photo.\n\n"
            "Usually the recorder is busy or the network to it is congested; "
            "no action needed if the next check is normal."
        )
    return await _alert(
        "PPIS Bot — Cameras Not Capturing\n\n"
        f"Recorder {ip} could not capture {finding['classroom']} at "
        f"{finding['at_ist']} ({finding['error']}), so live photos for {rooms} "
        "are failing.\n\n"
        "Please check the recorder is powered on and reachable at school. "
        "This check repeats every 30 minutes and you will be told when it "
        "recovers."
    )


# ---------------------------------------------------------------------------
# Morning readiness
# ---------------------------------------------------------------------------
async def morning_readiness() -> str:
    """One message before school stating whether the campus systems are ready."""
    from app.routes.agent_ws import get_health_state

    if not await is_working_day():
        return ""
    health = get_health_state()
    lines = [f"PPIS Bot — Morning Check {_now_ist()}", ""]
    if not health["connected"]:
        lines.append(
            "Campus PC: OFFLINE — no live photos and no face attendance. "
            "Please power it on and run restart_all_admin.vbs."
        )
        message = "\n".join(lines)
        await _alert(message)
        return message

    lines.append(
        f"Campus PC: online (commit {health.get('agent_code_commit') or '?'}, "
        f"started {health.get('agent_started_at_ist') or '?'})"
    )
    held = _recorders_held_for_password()
    findings = await sweep_cameras(alert=False)
    rooms = await _classrooms_by_recorder()
    for finding in findings:
        state = (
            f"{finding['seconds']}s"
            if finding["verdict"] == "ok"
            else f"SLOW {finding['seconds']}s"
            if finding["verdict"] == "slow"
            else f"FAILED — {finding['error']}"
        )
        lines.append(
            f"Recorder {finding['ip']}: {state} ({finding['classroom']})"
        )
    for ip in sorted(held):
        lines.append(
            f"Recorder {ip}: login refused — {len(rooms.get(ip, []))} "
            "classroom(s) blocked until it is unlocked or its password is "
            "updated."
        )
    if not findings:
        lines.append(
            "Cameras: COULD NOT BE CHECKED — no camera mapping was readable, "
            "so no classroom was proved this morning."
        )
    elif all(f["verdict"] == "ok" for f in findings) and not held:
        lines += ["", "Everything is ready for the day."]
    message = "\n".join(lines)
    await _alert(message)
    return message


# ---------------------------------------------------------------------------
# APScheduler entrypoints
# ---------------------------------------------------------------------------
def check_campus_link_sync() -> None:
    try:
        _run_on_app_loop(check_campus_link(), timeout=60)
    except Exception as exc:
        logger.error("CAMPUS WATCH: link check failed: %s", exc)


def sweep_cameras_sync() -> None:
    try:
        if not within_school_hours():
            return
        _run_on_app_loop(
            _sweep_if_working_day(), timeout=PROBE_TIMEOUT_SECONDS + 60
        )
    except Exception as exc:
        logger.error("CAMPUS WATCH: camera sweep failed: %s", exc)


async def _sweep_if_working_day() -> None:
    if await is_working_day():
        await sweep_cameras()


def morning_readiness_sync() -> None:
    try:
        _run_on_app_loop(
            morning_readiness(), timeout=PROBE_TIMEOUT_SECONDS + 120
        )
    except Exception as exc:
        logger.error("CAMPUS WATCH: morning check failed: %s", exc)
