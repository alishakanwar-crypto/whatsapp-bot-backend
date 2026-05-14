"""
Homework Delivery Service — automated homework broadcast via Google Docs.

Each class has a dedicated Google Doc where the teacher types homework.
After each period ends, the system:
  1. Fetches the doc's text content via the published URL
  2. Detects NEW homework entries (compares with last known content)
  3. Sends the homework to all parents of that class via WhatsApp template

Bell Timings (Summer):
  Period 1: 08:10 – 08:50  → check at 08:53
  Period 2: 09:00 – 09:35  → check at 09:38
  Period 3: 09:35 – 10:10  → check at 10:13
  Period 4: 10:10 – 10:45  → check at 10:48
  Period 5: 10:45 – 11:20  → check at 11:23
  Period 6: 11:45 – 12:20  → check at 12:23
  Period 7: 12:20 – 12:55  → check at 12:58
  Period 8: 12:55 – 01:30  → check at 01:33
"""

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Period labels for messages
PERIOD_LABELS = {
    1: "Period 1 (08:10–08:50)",
    2: "Period 2 (09:00–09:35)",
    3: "Period 3 (09:35–10:10)",
    4: "Period 4 (10:10–10:45)",
    5: "Period 5 (10:45–11:20)",
    6: "Period 6 (11:45–12:20)",
    7: "Period 7 (12:20–12:55)",
    8: "Period 8 (12:55–01:30)",
}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def _get_homework_docs() -> list[dict]:
    """Return all registered homework docs: [{grade, doc_id, doc_url}]."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT grade, doc_id, doc_url FROM homework_docs ORDER BY grade"
        )
        rows = await cursor.fetchall()
        return [{"grade": r[0], "doc_id": r[1], "doc_url": r[2]} for r in rows]
    finally:
        await db.close()


async def _get_last_content_hash(grade: str) -> str:
    """Return the last known content hash for a grade's homework doc."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT content_hash FROM homework_doc_state WHERE grade = ?",
            (grade,),
        )
        row = await cursor.fetchone()
        return row[0] if row else ""
    finally:
        await db.close()


async def _update_content_hash(grade: str, content_hash: str,
                                last_content: str) -> None:
    """Update the stored content hash for a grade."""
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO homework_doc_state (grade, content_hash, last_content, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(grade) DO UPDATE SET "
            "content_hash = excluded.content_hash, "
            "last_content = excluded.last_content, "
            "updated_at = datetime('now')",
            (grade, content_hash, last_content),
        )
        await db.commit()
    finally:
        await db.close()


async def _log_homework_delivery(grade: str, period: int, content: str,
                                  parents_sent: int, parents_failed: int,
                                  status: str) -> None:
    """Log a homework delivery event."""
    from app.database import get_db
    db = await get_db()
    try:
        now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO homework_delivery_logs "
            "(grade, period, content, parents_sent, parents_failed, "
            "status, delivery_time, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (grade, period, content[:500], parents_sent, parents_failed,
             status, now_ist),
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to log homework delivery: {e}")
    finally:
        await db.close()


async def _get_parents_for_grade(grade: str) -> list[dict]:
    """Return list of {student_name, father_phone, mother_phone} for a grade."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT student_name, father_mobile, mother_mobile "
            "FROM pi_sheet_students WHERE grade = ?",
            (grade,),
        )
        rows = await cursor.fetchall()
        return [
            {"student_name": r[0], "father_phone": r[1] or "",
             "mother_phone": r[2] or ""}
            for r in rows
        ]
    finally:
        await db.close()


def _get_teacher_for_grade(grade: str) -> str:
    """Return teacher name for the given grade."""
    from app.services.openai_service import TEACHER_DATA
    for t in TEACHER_DATA:
        if t["grade"].lower() == grade.lower():
            return t["teacher"]
    return ""


# ---------------------------------------------------------------------------
# Google Doc fetching
# ---------------------------------------------------------------------------

async def fetch_doc_content(doc_id: str) -> str | None:
    """Fetch the text content of a published Google Doc.

    Uses the export URL: https://docs.google.com/document/d/{doc_id}/export?format=txt
    The doc must be published to web OR shared with 'Anyone with link'.
    """
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code == 200:
                return resp.text.strip()
            logger.warning(f"Failed to fetch doc {doc_id}: HTTP {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error fetching doc {doc_id}: {e}")
        return None


def _extract_todays_homework(full_text: str) -> str:
    """Extract today's homework section from the document text.

    Teachers are instructed to write the date as a heading (e.g. "07/05/2026"
    or "07-05-2026" or "7 May 2026") followed by homework entries.
    We look for today's date in various formats and return everything until
    the next date heading or end of document.

    If no date heading found, return the entire document text (teacher may
    have just typed homework without a date header).
    """
    today = datetime.now(IST)
    date_patterns = [
        today.strftime("%d/%m/%Y"),       # 07/05/2026
        today.strftime("%d-%m-%Y"),       # 07-05-2026
        today.strftime("%d/%m/%y"),       # 07/05/26
        today.strftime("%d-%m-%y"),       # 07-05-26
        today.strftime("%-d/%m/%Y"),      # 7/05/2026
        today.strftime("%-d-%m-%Y"),      # 7-05-2026
        today.strftime("%-d %B %Y"),      # 7 May 2026
        today.strftime("%d %B %Y"),       # 07 May 2026
        today.strftime("%-d %b %Y"),      # 7 May 2026
        today.strftime("%d %b %Y"),       # 07 May 2026
        today.strftime("%Y-%m-%d"),       # 2026-05-07
    ]

    text_lower = full_text.lower()

    for dp in date_patterns:
        pos = text_lower.find(dp.lower())
        if pos >= 0:
            # Found today's date — extract from after the date line
            after_date = full_text[pos:]
            lines = after_date.split("\n")
            # Skip the date line itself
            content_lines = lines[1:]
            # Collect lines until we hit another date-like heading or end
            result = []
            date_re = re.compile(
                r"^\s*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\s*$|"
                r"^\s*\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                re.IGNORECASE,
            )
            for line in content_lines:
                if date_re.match(line.strip()) and line.strip():
                    break
                result.append(line)
            homework = "\n".join(result).strip()
            if homework:
                return homework

    # No date heading found — return entire text if it's short enough
    if len(full_text) < 2000:
        return full_text

    return ""


def _content_hash(text: str) -> str:
    """SHA-256 hash of text content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Core homework delivery logic
# ---------------------------------------------------------------------------

async def run_homework_delivery(period: int) -> dict:
    """Check all homework docs for new content and deliver to parents.

    Args:
        period: The period number (1-8) that just ended.

    Returns:
        Summary dict with results.
    """
    now_ist = datetime.now(IST)
    date_str = now_ist.strftime("%d/%m/%Y")
    period_label = PERIOD_LABELS.get(period, f"Period {period}")

    logger.info(f"=== HOMEWORK DELIVERY: Starting check after {period_label} ===")

    docs = await _get_homework_docs()
    if not docs:
        logger.warning("No homework docs registered. Skipping.")
        return {"status": "no_docs", "grades_checked": 0}

    results = {
        "period": period,
        "period_label": period_label,
        "date": date_str,
        "grades_checked": 0,
        "grades_with_homework": 0,
        "total_parents_sent": 0,
        "total_parents_failed": 0,
        "details": [],
    }

    for doc_info in docs:
        grade = doc_info["grade"]
        doc_id = doc_info["doc_id"]
        results["grades_checked"] += 1

        # Fetch doc content
        content = await fetch_doc_content(doc_id)
        if content is None:
            logger.warning(f"Could not fetch doc for {grade} (doc_id={doc_id})")
            await _log_homework_delivery(grade, period, "", 0, 0, "fetch_failed")
            results["details"].append({"grade": grade, "status": "fetch_failed"})
            continue

        # Extract today's homework
        homework = _extract_todays_homework(content)
        if not homework:
            logger.info(f"No homework found for {grade} today")
            results["details"].append({"grade": grade, "status": "empty"})
            continue

        # Check if content has changed since last check
        new_hash = _content_hash(homework)
        old_hash = await _get_last_content_hash(grade)
        if new_hash == old_hash:
            logger.info(f"No new homework for {grade} (content unchanged)")
            results["details"].append({"grade": grade, "status": "unchanged"})
            continue

        # New homework detected — deliver to parents
        logger.info(f"New homework for {grade}: {homework[:100]}...")
        results["grades_with_homework"] += 1

        teacher_name = _get_teacher_for_grade(grade)
        sent, failed = await _send_homework_to_parents(
            grade, homework, teacher_name, period_label, date_str,
        )

        # Update stored hash
        await _update_content_hash(grade, new_hash, homework)

        # Log delivery
        await _log_homework_delivery(grade, period, homework, sent, failed,
                                      "delivered" if sent > 0 else "no_parents")

        results["total_parents_sent"] += sent
        results["total_parents_failed"] += failed
        results["details"].append({
            "grade": grade,
            "status": "delivered",
            "sent": sent,
            "failed": failed,
            "content_preview": homework[:100],
        })

    logger.info(
        f"=== HOMEWORK DELIVERY COMPLETE: {results['grades_with_homework']}/"
        f"{results['grades_checked']} grades had new homework, "
        f"{results['total_parents_sent']} parents notified ==="
    )
    return results


async def _send_homework_to_parents(grade: str, homework: str,
                                     teacher_name: str, period_label: str,
                                     date_str: str) -> tuple[int, int]:
    """Send homework text to all parents of a grade via WhatsApp template.

    Returns (sent_count, failed_count).
    """
    from app.services.whatsapp_service import send_whatsapp_message

    parents = await _get_parents_for_grade(grade)
    if not parents:
        logger.warning(f"No parents found for {grade}")
        return 0, 0

    # Deduplicate phone numbers
    seen_phones: set[str] = set()
    phones_to_send: list[str] = []
    for p in parents:
        for ph in [p["father_phone"], p["mother_phone"]]:
            digits = re.sub(r"\D", "", ph)
            if len(digits) >= 10:
                digits = digits[-10:]
                if digits not in seen_phones:
                    seen_phones.add(digits)
                    phones_to_send.append(digits)

    sent = 0
    failed = 0

    hw_text = homework[:900] if len(homework) > 900 else homework

    for phone in phones_to_send:
        try:
            recipient = f"91{phone}" if len(phone) == 10 else phone
            hw_msg = f"📚 *Homework — {grade}*\n\n{hw_text}"
            ok = await send_whatsapp_message(recipient, hw_msg)

            if ok:
                sent += 1
            else:
                failed += 1

            # Rate limiting — small delay between sends
            await asyncio.sleep(0.3)

        except Exception as e:
            logger.error(f"Error sending homework to {phone}: {e}")
            failed += 1

    logger.info(f"Homework delivery for {grade}: {sent} sent, {failed} failed")
    return sent, failed


# ---------------------------------------------------------------------------
# Admin functions
# ---------------------------------------------------------------------------

async def register_homework_doc(grade: str, doc_id: str, doc_url: str = "") -> bool:
    """Register a Google Doc for a grade's homework.

    Called after creating the Google Docs.
    """
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO homework_docs (grade, doc_id, doc_url) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(grade) DO UPDATE SET "
            "doc_id = excluded.doc_id, doc_url = excluded.doc_url",
            (grade, doc_id, doc_url),
        )
        await db.commit()
        logger.info(f"Registered homework doc for {grade}: {doc_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to register homework doc for {grade}: {e}")
        return False
    finally:
        await db.close()


async def get_homework_logs(limit: int = 50) -> list[dict]:
    """Return recent homework delivery logs."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT grade, period, content, parents_sent, parents_failed, "
            "status, delivery_time, created_at "
            "FROM homework_delivery_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "grade": r[0], "period": r[1], "content": r[2],
                "parents_sent": r[3], "parents_failed": r[4],
                "status": r[5], "delivery_time": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Sync wrappers for APScheduler
# ---------------------------------------------------------------------------

def run_homework_delivery_sync(period: int) -> None:
    """Synchronous wrapper for APScheduler."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(run_homework_delivery(period))
        else:
            asyncio.run(run_homework_delivery(period))
    except RuntimeError:
        asyncio.run(run_homework_delivery(period))
