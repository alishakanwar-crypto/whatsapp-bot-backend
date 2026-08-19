"""Durable auditing, self-healing and daily reporting for live snapshot requests.

Every live-snapshot request a parent or admin sends is recorded with its
outcome, so a request that was refused or never delivered can be found and
fixed instead of being reconstructed from chat logs.

Failures are repaired while the parent is still in the conversation, not the
next day:

* ``recover_snapshot_access`` — when a request is refused but the sender is a
  known parent in the school's student records, the missing snapshot-access
  row is rebuilt on the spot and a full PI Sheet re-sync is triggered.
* ``repair_access_now`` — the same repair plus, when the sender is still not
  found, a live PI Sheet re-read before the request is refused, so a genuine
  parent is served on their first attempt.
* ``resolve_open_failures`` — replays the repair over every request logged as
  undelivered and marks the ones that are now fixed as resolved.
* ``run_daily_snapshot_audit`` — safety net: repairs first, then alerts the
  administrators about the requests that could not be resolved automatically.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

OUTCOME_DELIVERED = "delivered"
OUTCOME_BLOCKED_UNAUTHORIZED = "blocked_unauthorized"
OUTCOME_RECOVERED = "recovered_access"
OUTCOME_AGENT_UNAVAILABLE = "agent_unavailable"
OUTCOME_QUEUED = "queued"
OUTCOME_NO_CLASSROOM = "no_classroom"
OUTCOME_CAPTURE_FAILED = "capture_failed"
OUTCOME_DELIVERY_FAILED = "delivery_failed"

# Outcomes that mean the parent did not get their photo.
FAILED_OUTCOMES = (
    OUTCOME_BLOCKED_UNAUTHORIZED,
    OUTCOME_AGENT_UNAVAILABLE,
    OUTCOME_NO_CLASSROOM,
    OUTCOME_CAPTURE_FAILED,
    OUTCOME_DELIVERY_FAILED,
)

OUTCOME_LABELS = {
    OUTCOME_BLOCKED_UNAUTHORIZED: "Refused — number not in saved parent data",
    OUTCOME_AGENT_UNAVAILABLE: "Campus camera agent was offline",
    OUTCOME_NO_CLASSROOM: "Could not determine the child's classroom",
    OUTCOME_CAPTURE_FAILED: "Camera capture failed",
    OUTCOME_DELIVERY_FAILED: "Photo captured but WhatsApp delivery failed",
}

AUDIT_ALERT_PHONES = ("919971166562", "919599488106")

# The self-heal repair runs a full PI Sheet re-sync at most this often.
_RESYNC_MIN_GAP = timedelta(minutes=30)
# A live re-read blocks the parent's request, so it is given a hard budget.
_LIVE_RESYNC_TIMEOUT = 75.0
_last_resync_at: datetime | None = None


def _last10(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


async def log_snapshot_request(
    sender: str,
    message_text: str,
    outcome: str,
    reason: str = "",
    student_name: str = "",
    grade: str = "",
    location: str = "",
    is_admin: bool = False,
) -> None:
    """Record one snapshot request outcome (IST timestamps)."""
    from app.database import get_db

    now = datetime.now(IST)
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT 1 FROM snapshot_access_students "
            "WHERE father_mobile LIKE ? OR mother_mobile LIKE ? LIMIT 1",
            (f"%{_last10(sender)}%", f"%{_last10(sender)}%"),
        )
        in_cache = 1 if await cur.fetchone() else 0
        await db.execute(
            "INSERT INTO snapshot_request_audit "
            "(request_date, requested_at_ist, sender_phone, message_text, "
            "is_admin, student_name, grade, location, outcome, reason, "
            "in_pi_sheet_cache, resolved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now.strftime("%Y-%m-%d"),
                now.strftime("%d-%m-%Y %H:%M:%S IST"),
                sender,
                (message_text or "")[:200],
                1 if is_admin else 0,
                student_name,
                grade,
                location or "",
                outcome,
                reason,
                in_cache,
                1 if outcome not in FAILED_OUTCOMES else 0,
            ),
        )
        await db.commit()
    except Exception as exc:
        logger.warning(f"SNAPSHOT AUDIT: could not log request from {sender}: {exc}")
    finally:
        await db.close()


async def _schedule_full_resync() -> None:
    """Trigger a full PI Sheet re-sync in the background (rate limited)."""
    global _last_resync_at

    now = datetime.now(IST)
    if _last_resync_at is not None and now - _last_resync_at < _RESYNC_MIN_GAP:
        return
    _last_resync_at = now

    from app.services.sheet_refresh_service import fetch_all_pi_sheet_tabs

    async def _run() -> None:
        try:
            await fetch_all_pi_sheet_tabs()
        except Exception as exc:
            logger.error(f"SNAPSHOT AUDIT: triggered PI Sheet re-sync failed: {exc}")

    asyncio.create_task(_run())


async def recover_snapshot_access(sender: str) -> list[dict]:
    """Rebuild missing snapshot access for a sender who is a known parent.

    The snapshot-access table is a cache of the PI Sheet. If it is incomplete
    (a class tab failed to sync), a genuine parent gets refused. The school's
    student records still hold the parent's number, so the missing row is
    restored from there and a full re-sync is triggered.

    Returns the recovered children (empty if the sender is genuinely unknown).
    """
    from app.database import get_db
    from app.services.sheet_refresh_service import _normalize_grade_for_db

    last10 = _last10(sender)
    if len(last10) < 10:
        return []

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT student_name, grade, father_phone, mother_phone "
            "FROM student_birthdays "
            "WHERE father_phone LIKE ? OR mother_phone LIKE ?",
            (f"%{last10}%", f"%{last10}%"),
        )
        rows = await cur.fetchall()
        recovered: list[dict] = []
        for student_name, grade, father_phone, mother_phone in rows:
            normalized_grade = _normalize_grade_for_db(_expand_grade(grade or ""))
            if not student_name or not normalized_grade:
                continue
            exists = await db.execute(
                "SELECT 1 FROM snapshot_access_students "
                "WHERE UPPER(TRIM(student_name)) = ? AND grade = ? LIMIT 1",
                (student_name.upper().strip(), normalized_grade),
            )
            if not await exists.fetchone():
                await db.execute(
                    "INSERT INTO snapshot_access_students "
                    "(student_name, grade, father_mobile, mother_mobile) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        student_name,
                        normalized_grade,
                        _normalize_parent_phone(father_phone),
                        _normalize_parent_phone(mother_phone),
                    ),
                )
            recovered.append(
                {
                    "student_name": student_name,
                    "grade": normalized_grade,
                    "parent_phones": [
                        p
                        for p in (
                            _normalize_parent_phone(father_phone),
                            _normalize_parent_phone(mother_phone),
                        )
                        if p
                    ],
                }
            )
        if recovered:
            await db.commit()
            logger.warning(
                f"SNAPSHOT AUDIT: restored snapshot access for {sender} from "
                f"student records: "
                f"{', '.join(r['student_name'] for r in recovered)}"
            )
    except Exception as exc:
        logger.error(f"SNAPSHOT AUDIT: recovery failed for {sender}: {exc}")
        return []
    finally:
        await db.close()

    if recovered:
        await _schedule_full_resync()
    return recovered


async def _cached_wards(sender: str) -> list[dict]:
    """Students in the snapshot cache whose parent number is this sender."""
    from app.database import get_db

    last10 = _last10(sender)
    if len(last10) < 10:
        return []
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT student_name, grade FROM snapshot_access_students "
            "WHERE father_mobile LIKE ? OR mother_mobile LIKE ?",
            (f"%{last10}%", f"%{last10}%"),
        )
        rows = await cur.fetchall()
    except Exception as exc:
        logger.warning(f"SNAPSHOT AUDIT: access check failed for {sender}: {exc}")
        return []
    finally:
        await db.close()
    return [
        {"student_name": row[0], "grade": row[1], "parent_phones": [sender]}
        for row in rows
    ]


async def repair_access_now(sender: str) -> list[dict]:
    """Repair a refused sender's access immediately, before refusing them.

    First rebuilds the access row from the school's student records. If the
    number is still not found, the PI Sheet is re-read live (rate limited) and
    the cache is checked again — a class tab that failed to sync earlier is
    therefore fixed during this very request instead of the next day.
    """
    global _last_resync_at

    recovered = await recover_snapshot_access(sender)
    if recovered:
        return recovered

    now = datetime.now(IST)
    if _last_resync_at is not None and now - _last_resync_at < _RESYNC_MIN_GAP:
        return []
    _last_resync_at = now

    from app.services.sheet_refresh_service import fetch_all_pi_sheet_tabs

    try:
        await asyncio.wait_for(fetch_all_pi_sheet_tabs(), timeout=_LIVE_RESYNC_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            f"SNAPSHOT AUDIT: live PI Sheet re-read for {sender} timed out"
        )
        return []
    except Exception as exc:
        logger.error(f"SNAPSHOT AUDIT: live PI Sheet re-read failed: {exc}")
        return []

    wards = await _cached_wards(sender)
    if wards:
        logger.warning(
            f"SNAPSHOT AUDIT: live PI Sheet re-read restored access for {sender}"
        )
    return wards


async def mark_resolved(sender: str, day: str = "", note: str = "") -> None:
    """Mark this sender's undelivered requests for the day as resolved."""
    from app.database import get_db

    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    db = await get_db()
    try:
        await db.execute(
            "UPDATE snapshot_request_audit SET resolved = 1, "
            "reason = CASE WHEN ? = '' THEN reason ELSE reason || ' | ' || ? END "
            "WHERE request_date = ? AND sender_phone = ? AND resolved = 0",
            (note, note, day, sender),
        )
        await db.commit()
    except Exception as exc:
        logger.warning(f"SNAPSHOT AUDIT: could not mark {sender} resolved: {exc}")
    finally:
        await db.close()


async def resolve_open_failures(day: str = "") -> dict:
    """Repair every undelivered request of the day; returns what is still open.

    Access problems are repaired from the school's student records. Operational
    failures (camera offline, capture or delivery error) count as resolved only
    when that same sender was served a photo later in the day — the request-time
    retry usually does this within seconds. Everything else stays open so the
    administrators are told about it.
    """
    from app.database import get_db

    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT DISTINCT sender_phone, outcome FROM snapshot_request_audit "
            "WHERE request_date = ? AND resolved = 0 AND outcome IN "
            f"({','.join('?' * len(FAILED_OUTCOMES))})",
            (day, *FAILED_OUTCOMES),
        )
        open_rows = await cur.fetchall()
        cur = await db.execute(
            "SELECT DISTINCT sender_phone FROM snapshot_request_audit "
            "WHERE request_date = ? AND outcome = ?",
            (day, OUTCOME_DELIVERED),
        )
        served = {row[0] for row in await cur.fetchall()}
    finally:
        await db.close()

    repaired = 0
    for sender_phone, outcome in open_rows:
        if outcome in (OUTCOME_BLOCKED_UNAUTHORIZED, OUTCOME_NO_CLASSROOM):
            if await recover_snapshot_access(sender_phone) or await _cached_wards(
                sender_phone
            ):
                await mark_resolved(
                    sender_phone, day, "auto-repaired: parent access restored"
                )
                repaired += 1
            continue
        if sender_phone in served:
            await mark_resolved(
                sender_phone, day, "auto-repaired: photo delivered on retry"
            )
            repaired += 1

    logger.info(
        f"SNAPSHOT AUDIT {day}: auto-repaired {repaired} of {len(open_rows)} "
        f"undelivered request(s)"
    )
    return {"date": day, "open": len(open_rows), "repaired": repaired}


def _expand_grade(grade: str) -> str:
    """Expand short grade forms used in the DOB sheet ('Nur 2' -> 'Nursery 2')."""
    g = " ".join(grade.strip().split())
    m = re.match(r"^(?:NUR|NURSERY)[\s-]*(\d)$", g, re.IGNORECASE)
    if m:
        return f"Nursery {m.group(1)}"
    m = re.match(r"^PREP[\s-]*(\d)$", g, re.IGNORECASE)
    if m:
        return f"Prep {m.group(1)}"
    return g


def _normalize_parent_phone(phone: str | None) -> str:
    digits = _last10(phone or "")
    return f"91{digits}" if len(digits) == 10 else ""


async def run_daily_snapshot_audit(report_date: str = "") -> dict:
    """Report every snapshot request that was not delivered today (IST).

    Sends one WhatsApp summary to the admins per day; repeated runs on the
    same day are suppressed unless new failures appeared.
    """
    from app.database import get_db
    from app.services.whatsapp_service import send_whatsapp_force

    day = report_date or datetime.now(IST).strftime("%Y-%m-%d")

    # Repair before reporting — the administrators are only told about what
    # could not be fixed automatically.
    repair = await resolve_open_failures(day)

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT sender_phone, student_name, grade, requested_at_ist, "
            "outcome, reason, in_pi_sheet_cache, COUNT(*) "
            "FROM snapshot_request_audit "
            "WHERE request_date = ? AND resolved = 0 AND outcome IN "
            f"({','.join('?' * len(FAILED_OUTCOMES))}) "
            "GROUP BY sender_phone, outcome "
            "ORDER BY outcome, sender_phone",
            (day, *FAILED_OUTCOMES),
        )
        failures = await cur.fetchall()

        rec_cur = await db.execute(
            "SELECT COUNT(*) FROM snapshot_request_audit "
            "WHERE request_date = ? AND outcome = ?",
            (day, OUTCOME_RECOVERED),
        )
        recovered_count = (await rec_cur.fetchone())[0]

        sent_cur = await db.execute(
            "SELECT failed_count FROM snapshot_audit_report_log "
            "WHERE report_date = ?",
            (day,),
        )
        sent_row = await sent_cur.fetchone()
        already_reported = sent_row[0] if sent_row else -1

        if not failures:
            logger.info(
                f"SNAPSHOT AUDIT {day}: every undelivered request was "
                f"auto-repaired ({repair['repaired']} of {repair['open']})"
            )
            return {"date": day, "failed": 0, "recovered": recovered_count,
                    "repaired": repair["repaired"], "alerted": False}

        if already_reported == len(failures):
            logger.info(
                f"SNAPSHOT AUDIT {day}: already reported {already_reported} "
                f"failures, skipping duplicate alert"
            )
            return {"date": day, "failed": len(failures),
                    "recovered": recovered_count,
                    "repaired": repair["repaired"], "alerted": False}

        lines = [
            "PPIS Bot — Live Snapshot Audit",
            f"Date: {datetime.strptime(day, '%Y-%m-%d').strftime('%d-%m-%Y')} (IST)",
            "",
            f"Unresolved after auto-repair: {len(failures)}",
        ]
        if recovered_count or repair["repaired"]:
            lines.append(
                f"Auto-repaired: {recovered_count + repair['repaired']} request(s)"
            )
        lines.append("")

        for (
            sender_phone,
            student_name,
            grade,
            requested_at,
            outcome,
            reason,
            in_cache,
            count,
        ) in failures:
            who = student_name or "unknown student"
            where = f" ({grade})" if grade else ""
            lines.append(f"{sender_phone} — {who}{where}")
            lines.append(
                f"  {OUTCOME_LABELS.get(outcome, outcome)}"
                f"{f' — {reason}' if reason else ''}"
            )
            lines.append(f"  Requests: {count}, last at {requested_at}")
            if outcome == OUTCOME_BLOCKED_UNAUTHORIZED and not in_cache:
                lines.append(
                    "  Action: number missing from saved parent data — "
                    "check the PI Sheet row for this child"
                )
            lines.append("")

        text = "\n".join(lines).strip()
        for phone in AUDIT_ALERT_PHONES:
            try:
                await send_whatsapp_force(phone, text)
            except Exception as exc:
                logger.warning(f"SNAPSHOT AUDIT: alert to {phone} failed: {exc}")

        await db.execute(
            "INSERT INTO snapshot_audit_report_log (report_date, failed_count) "
            "VALUES (?, ?) ON CONFLICT(report_date) DO UPDATE SET "
            "failed_count = excluded.failed_count, sent_at = CURRENT_TIMESTAMP",
            (day, len(failures)),
        )
        await db.commit()
        return {"date": day, "failed": len(failures),
                "recovered": recovered_count,
                "repaired": repair["repaired"], "alerted": True}
    finally:
        await db.close()


def run_daily_snapshot_audit_sync() -> None:
    """APScheduler entrypoint for the daily snapshot audit."""
    try:
        asyncio.run(run_daily_snapshot_audit())
    except Exception as exc:
        logger.error(f"SNAPSHOT AUDIT: daily run failed: {exc}")
