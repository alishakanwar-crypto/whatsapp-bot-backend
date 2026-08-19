"""Durable auditing, self-healing and daily reporting for live snapshot requests.

Every live-snapshot request a parent or admin sends is recorded with its
outcome, so a request that was refused or never delivered can be found and
fixed the next day instead of being reconstructed from chat logs.

Two things keep parents from silently losing access:

* ``recover_snapshot_access`` — when a request is refused but the sender is a
  known parent in the school's student records, the missing snapshot-access
  row is rebuilt on the spot and a full PI Sheet re-sync is triggered.
* ``run_daily_snapshot_audit`` — a daily IST report of every request that was
  not delivered, WhatsApped to the admins with the reason for each one.
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
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT sender_phone, student_name, grade, requested_at_ist, "
            "outcome, reason, in_pi_sheet_cache, COUNT(*) "
            "FROM snapshot_request_audit "
            "WHERE request_date = ? AND outcome IN "
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
            logger.info(f"SNAPSHOT AUDIT {day}: no failed snapshot requests")
            return {"date": day, "failed": 0, "recovered": recovered_count,
                    "alerted": False}

        if already_reported == len(failures):
            logger.info(
                f"SNAPSHOT AUDIT {day}: already reported {already_reported} "
                f"failures, skipping duplicate alert"
            )
            return {"date": day, "failed": len(failures),
                    "recovered": recovered_count, "alerted": False}

        lines = [
            "PPIS Bot — Live Snapshot Audit",
            f"Date: {datetime.strptime(day, '%Y-%m-%d').strftime('%d-%m-%Y')} (IST)",
            "",
            f"Requests not delivered: {len(failures)}",
        ]
        if recovered_count:
            lines.append(
                f"Auto-repaired parent access: {recovered_count} request(s)"
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
                "recovered": recovered_count, "alerted": True}
    finally:
        await db.close()


def run_daily_snapshot_audit_sync() -> None:
    """APScheduler entrypoint for the daily snapshot audit."""
    try:
        asyncio.run(run_daily_snapshot_audit())
    except Exception as exc:
        logger.error(f"SNAPSHOT AUDIT: daily run failed: {exc}")
