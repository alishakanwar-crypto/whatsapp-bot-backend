"""
Homework Delivery Service — automated homework broadcast via Google Docs.

Each class has a dedicated Google Doc where the teacher types homework.
After each period ends, the system:
  1. Fetches the doc's text content via Google Docs API (authenticated)
  2. Detects NEW homework entries (compares with last known content)
  3. Renders the content as a clean image (screenshot)
  4. Sends BOTH the image + text to parents via WhatsApp template
  5. If no new content → silently skips (no message sent)

Bell Timings (current timetable):
  Period 0: 08:00 – 08:10  → Assembly (10 min)
  Period 1: 08:10 – 08:45  → check at 08:48
  SHORT BREAK: 08:45 – 09:00
  Period 2: 09:00 – 09:30  → check at 09:33
  Period 3: 09:30 – 10:00  → check at 10:03
  Period 4: 10:00 – 10:30  → check at 10:33
  Period 5: 10:30 – 11:00  → check at 11:03
  Period 6: 11:00 – 11:30  → check at 11:33
  Lunch & Dispersal: 11:30 – 12:00

Google API auth uses a refresh token stored in GOOGLE_DOCS_REFRESH_TOKEN env var
along with GOOGLE_DOCS_CLIENT_ID and GOOGLE_DOCS_CLIENT_SECRET.
"""

import asyncio
import hashlib
import io
import logging
import os
import re
import textwrap
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Period labels for parent messages
PERIOD_LABELS = {
    0: "Assembly (08:00–08:10)",
    1: "Period 1 (08:10–08:45)",
    2: "Period 2 (09:00–09:30)",
    3: "Period 3 (09:30–10:00)",
    4: "Period 4 (10:00–10:30)",
    5: "Period 5 (10:30–11:00)",
    6: "Period 6 (11:00–11:30)",
}

# Grades eligible for automated homework delivery.
# Phase 1 rollout: only Grade 9-12. Other grades will be added later.
_HOMEWORK_ELIGIBLE_GRADES = re.compile(r"Grade\s*(9|10|11|12)", re.IGNORECASE)

# Cached Google access token
_google_access_token: str = ""
_google_token_expiry: datetime | None = None


# ---------------------------------------------------------------------------
# Google OAuth token management
# ---------------------------------------------------------------------------

async def _get_google_access_token() -> str:
    """Get a valid Google access token, refreshing if needed."""
    global _google_access_token, _google_token_expiry

    # Return cached token if still valid (with 60s buffer)
    if (_google_access_token and _google_token_expiry
            and datetime.now(timezone.utc) < _google_token_expiry - timedelta(seconds=60)):
        return _google_access_token

    refresh_token = os.getenv("GOOGLE_DOCS_REFRESH_TOKEN", "")
    client_id = os.getenv("GOOGLE_DOCS_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_DOCS_CLIENT_SECRET", "")

    if not refresh_token or not client_id or not client_secret:
        logger.error(
            "Google Docs API credentials not configured. "
            "Set GOOGLE_DOCS_REFRESH_TOKEN, GOOGLE_DOCS_CLIENT_ID, "
            "GOOGLE_DOCS_CLIENT_SECRET env vars."
        )
        return ""

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=15.0,
            )
            data = resp.json()
            if "access_token" in data:
                _google_access_token = data["access_token"]
                expires_in = data.get("expires_in", 3600)
                _google_token_expiry = (
                    datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                )
                logger.info("Google access token refreshed successfully")
                return _google_access_token
            logger.error(f"Failed to refresh Google token: {data}")
            return ""
    except Exception as e:
        logger.error(f"Error refreshing Google token: {e}")
        return ""


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


async def _get_last_content_hash(grade: str) -> tuple[str, str]:
    """Return (content_hash, last_content) for a grade's homework doc."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT content_hash, last_content FROM homework_doc_state WHERE grade = ?",
            (grade,),
        )
        row = await cursor.fetchone()
        return (row[0], row[1] or "") if row else ("", "")
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


def _normalize_grade_for_lookup(grade: str) -> str:
    """Strip stream suffix (SCIENCE/COMMERCE/HUMANITIES) for PI sheet lookup.

    E.g. "Grade 11A-SCIENCE" → "Grade 11A" to match PI sheet data.
    """
    return re.sub(r"\s*-\s*(SCIENCE|COMMERCE|HUMANITIES)$", "", grade,
                  flags=re.IGNORECASE)


def _canonicalize_grade(grade: str) -> str:
    """Canonicalize a grade string for fuzzy matching.

    Normalizes case, removes extra whitespace, strips stream suffix.
    E.g. "GRADE 11 A" → "grade11a", "Grade 11A-SCIENCE" → "grade11a"
    """
    g = _normalize_grade_for_lookup(grade)
    return re.sub(r"\s+", "", g).lower()


async def _get_parents_for_grade(grade: str) -> list[dict]:
    """Return list of {student_name, father_phone, mother_phone} for a grade.

    Handles stream-suffixed grade names (e.g. 'Grade 11A-SCIENCE') and
    PI sheet grade variations (e.g. 'GRADE 11A', 'GRADE 11 A', 'Grade 11A')
    by canonicalizing both sides before matching.
    """
    from app.database import get_db
    target = _canonicalize_grade(grade)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT student_name, grade, father_mobile, mother_mobile "
            "FROM pi_sheet_students",
        )
        rows = await cursor.fetchall()
        return [
            {"student_name": r[0], "father_phone": r[2] or "",
             "mother_phone": r[3] or ""}
            for r in rows
            if _canonicalize_grade(r[1]) == target
        ]
    finally:
        await db.close()


def _get_teacher_for_grade(grade: str) -> str:
    """Return teacher name for the given grade."""
    from app.services.openai_service import TEACHER_DATA
    lookup = _normalize_grade_for_lookup(grade).lower()
    for t in TEACHER_DATA:
        if t["grade"].lower() == lookup:
            return t["teacher"]
    return ""


# ---------------------------------------------------------------------------
# Google Doc fetching (authenticated)
# ---------------------------------------------------------------------------

async def fetch_doc_content(doc_id: str) -> str | None:
    """Fetch the text content of a Google Doc via export API (authenticated).

    Uses the OAuth access token to access private docs owned by the bot account.
    Falls back to unauthenticated public URL if no token is available.
    """
    token = await _get_google_access_token()
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
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
    have just typed homework without a date header — fallback mode).
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
            after_date = full_text[pos:]
            lines = after_date.split("\n")
            content_lines = lines[1:]
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
    # (teacher wrote content without a date, our fallback handles it)
    if len(full_text) < 2000:
        return full_text

    return ""


def _extract_new_content(full_homework: str, old_content: str) -> str:
    """Extract only the NEW lines from homework that weren't in old_content.

    Compares line by line and returns only lines added since the last delivery.
    This prevents resending previous periods' homework.
    """
    if not old_content:
        return full_homework

    old_lines = set(line.strip() for line in old_content.split("\n") if line.strip())
    new_lines = []
    for line in full_homework.split("\n"):
        stripped = line.strip()
        if stripped and stripped not in old_lines:
            new_lines.append(line)

    return "\n".join(new_lines).strip()


def _strip_template_boilerplate(text: str) -> str:
    """Remove the initial template instructions from the doc content.

    The docs were created with template text (instructions, example format).
    Strip everything before 'START YOUR ENTRIES BELOW THIS LINE' marker.
    Also skip blank lines at the start.
    """
    marker = "START YOUR ENTRIES BELOW THIS LINE"
    pos = text.find(marker)
    if pos >= 0:
        # Skip the marker line itself
        after = text[pos + len(marker):]
        # Also skip any separator lines (━━━ or ---)
        lines = after.split("\n")
        result_lines = []
        started = False
        for line in lines:
            stripped = line.strip()
            if not started:
                if stripped and not all(c in "━─-= " for c in stripped):
                    started = True
                    result_lines.append(line)
            else:
                result_lines.append(line)
        return "\n".join(result_lines).strip()
    return text.strip()


def _content_hash(text: str) -> str:
    """SHA-256 hash of text content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Image rendering — create a clean homework screenshot using PIL
# ---------------------------------------------------------------------------

def _render_homework_image(grade: str, period_label: str, date_str: str,
                           homework_text: str) -> bytes:
    """Render homework text as a clean PNG image with school header.

    Returns PNG bytes. Uses PIL (Pillow) — no external dependencies needed.
    """
    from PIL import Image, ImageDraw, ImageFont

    # Try to load a decent font, fall back to default
    font_size_title = 28
    font_size_body = 20
    font_size_meta = 18
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size_title)
        body_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size_body)
        meta_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size_meta)
    except OSError:
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()
        meta_font = ImageFont.load_default()

    # Layout constants
    padding = 30
    max_width = 700
    img_width = max_width + 2 * padding

    # Wrap text to fit width
    wrapped_lines = []
    for line in homework_text.split("\n"):
        if line.strip():
            wrapped = textwrap.wrap(line, width=55)
            wrapped_lines.extend(wrapped)
        else:
            wrapped_lines.append("")

    # Calculate height
    line_height_body = font_size_body + 8
    line_height_meta = font_size_meta + 6
    header_height = 120  # school name + separator
    meta_height = 3 * line_height_meta + 20  # date, class, period
    body_height = len(wrapped_lines) * line_height_body + 20
    footer_height = 40
    img_height = header_height + meta_height + body_height + footer_height + 2 * padding

    # Clamp minimum height
    img_height = max(img_height, 400)

    # Create image
    img = Image.new("RGB", (img_width, img_height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    y = padding

    # School header
    draw.text((padding, y), "PP International School", fill=(0, 51, 102), font=title_font)
    y += font_size_title + 8
    draw.text((padding, y), "Classwork & Homework Update", fill=(0, 102, 153), font=meta_font)
    y += font_size_meta + 15

    # Separator line
    draw.line([(padding, y), (img_width - padding, y)], fill=(0, 102, 153), width=2)
    y += 15

    # Meta info (always auto-filled)
    meta_color = (80, 80, 80)
    draw.text((padding, y), f"Date: {date_str}", fill=meta_color, font=meta_font)
    y += line_height_meta
    draw.text((padding, y), f"Class: {grade}", fill=meta_color, font=meta_font)
    y += line_height_meta
    draw.text((padding, y), f"Period: {period_label}", fill=meta_color, font=meta_font)
    y += line_height_meta + 15

    # Separator
    draw.line([(padding, y), (img_width - padding, y)], fill=(200, 200, 200), width=1)
    y += 15

    # Homework content
    body_color = (30, 30, 30)
    for line in wrapped_lines:
        if line.strip():
            draw.text((padding, y), line, fill=body_color, font=body_font)
        y += line_height_body

    # Footer separator
    y = max(y + 10, img_height - footer_height - padding)
    draw.line([(padding, y), (img_width - padding, y)], fill=(200, 200, 200), width=1)
    y += 10
    footer_font = meta_font
    draw.text((padding, y), "PP International School — Automated Update",
              fill=(150, 150, 150), font=footer_font)

    # Save to bytes
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_no_homework_image(grade: str, period_label: str,
                               date_str: str) -> bytes:
    """Render a 'No Homework Assigned' image."""
    return _render_homework_image(
        grade, period_label, date_str,
        "No Homework Assigned"
    )


# ---------------------------------------------------------------------------
# Format the fallback text message (always includes date, class, period)
# ---------------------------------------------------------------------------

def _format_homework_message(grade: str, period_label: str,
                              homework_text: str) -> str:
    """Build the parent-facing text message with auto-filled metadata."""
    return (
        f"\U0001f4da Classwork & Homework Update\n\n"
        f"\U0001f3eb {grade}\n"
        f"\u23f0 {period_label}\n\n"
        f"{homework_text}\n\n"
        f"\u2014 PP International School"
    )


def _format_no_homework_message(grade: str, period_label: str) -> str:
    """Build the 'no homework' text message."""
    return _format_homework_message(grade, period_label,
                                     "No Homework Assigned")


# ---------------------------------------------------------------------------
# Core homework delivery logic
# ---------------------------------------------------------------------------

async def run_homework_delivery(period: int) -> dict:
    """Check all homework docs for new content and deliver to parents.

    Args:
        period: The period number (0-6) that just ended.

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
        "grades_no_homework": 0,
        "total_parents_sent": 0,
        "total_parents_failed": 0,
        "details": [],
    }

    for doc_info in docs:
        grade = doc_info["grade"]
        doc_id = doc_info["doc_id"]

        # Phase 1: only deliver for eligible grades (Grade 9-12)
        if not _HOMEWORK_ELIGIBLE_GRADES.match(grade):
            continue

        results["grades_checked"] += 1

        # Fetch doc content (authenticated)
        content = await fetch_doc_content(doc_id)
        if content is None:
            logger.warning(f"Could not fetch doc for {grade} (doc_id={doc_id})")
            await _log_homework_delivery(grade, period, "", 0, 0, "fetch_failed")
            results["details"].append({"grade": grade, "status": "fetch_failed"})
            continue

        # Strip template boilerplate (initial instructions)
        content = _strip_template_boilerplate(content)

        # Extract today's homework
        homework = _extract_todays_homework(content)

        if not homework:
            # No content written → skip silently (no message to parents)
            logger.info(f"No homework found for {grade} today → skipping (no message sent)")
            results["grades_no_homework"] += 1
            results["details"].append({"grade": grade, "status": "no_homework"})
            continue

        # Check if content has changed since last check
        new_hash = _content_hash(homework)
        old_hash, old_content = await _get_last_content_hash(grade)
        if new_hash == old_hash:
            logger.info(f"No new homework for {grade} (content unchanged)")
            results["details"].append({"grade": grade, "status": "unchanged"})
            continue

        # Extract only the NEW content (delta) — don't resend previous periods
        new_portion = _extract_new_content(homework, old_content)
        if not new_portion:
            # Hash changed but no meaningful new text (whitespace edits etc.)
            logger.info(f"Content changed for {grade} but no new lines — skipping")
            await _update_content_hash(grade, new_hash, homework)
            results["details"].append({"grade": grade, "status": "minor_edit"})
            continue

        # New homework detected — deliver only the new portion to parents
        logger.info(f"New homework for {grade}: {new_portion[:100]}...")
        results["grades_with_homework"] += 1

        sent, failed = await _send_homework_to_parents(
            grade, new_portion, period_label,
        )

        # Update stored hash
        await _update_content_hash(grade, new_hash, homework)

        # Log delivery
        status_label = "delivered" if sent > 0 else "all_failed"
        await _log_homework_delivery(grade, period, homework, sent, failed,
                                      status_label)

        results["total_parents_sent"] += sent
        results["total_parents_failed"] += failed
        results["details"].append({
            "grade": grade,
            "status": status_label,
            "sent": sent,
            "failed": failed,
            "content_preview": homework[:100],
        })

        # Pause between grades to avoid Meta API rate limiting
        if sent > 0 or failed > 0:
            await asyncio.sleep(5.0)

    logger.info(
        f"=== HOMEWORK DELIVERY COMPLETE: {results['grades_with_homework']}/"
        f"{results['grades_checked']} grades had new homework, "
        f"{results['grades_no_homework']} had no homework, "
        f"{results['total_parents_sent']} parents notified ==="
    )
    return results


async def _send_homework_to_parents(grade: str, homework: str,
                                     period_label: str) -> tuple[int, int]:
    """Send homework to all parents of a grade via template message.

    Uses ppis_classwork_homework template with 3 body params:
      {{1}} = Class, {{2}} = Period, {{3}} = Content
    No image header.

    Includes retry logic: if a send fails, retries up to 2 more times
    with exponential backoff (2s, 5s) to handle Meta API rate limiting.

    Returns (sent_count, failed_count).
    """
    from app.services.whatsapp_service import send_cloud_template_message

    parents = await _get_parents_for_grade(grade)
    if not parents:
        logger.warning(f"No parents found for {grade}")
        return 0, 0

    phones_to_send = _deduplicate_phones(parents)
    logger.info(
        f"[HW DELIVERY] {grade}: {len(parents)} parent records → "
        f"{len(phones_to_send)} unique phones to notify"
    )

    # Sanitize content for Meta template parameters:
    # - Replace en/em dashes with hyphens (Meta rejects some Unicode dashes)
    # - Collapse excessive whitespace
    # - Truncate for template parameter limit (~1024 chars)
    hw_content = homework
    hw_content = hw_content.replace("\u2013", "-").replace("\u2014", "-")
    hw_content = re.sub(r"[ \t]+", " ", hw_content)
    hw_content = hw_content.strip()
    hw_content = hw_content[:900] if len(hw_content) > 900 else hw_content

    sent = 0
    failed = 0
    consecutive_failures = 0
    MAX_RETRIES = 2
    RETRY_DELAYS = [2.0, 5.0]

    for phone in phones_to_send:
        ok = False
        for attempt in range(1 + MAX_RETRIES):
            try:
                ok = await send_cloud_template_message(
                    phone,
                    "ppis_classwork_homework",
                    body_params=[grade, period_label, hw_content],
                )
                if ok:
                    break
                # Failed — log and retry
                if attempt < MAX_RETRIES:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning(
                        f"[HW DELIVERY] Send to {phone} failed (attempt {attempt + 1}), "
                        f"retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
            except Exception as e:
                logger.error(
                    f"[HW DELIVERY] Exception sending to {phone} "
                    f"(attempt {attempt + 1}): {e}"
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAYS[attempt])

        if ok:
            sent += 1
            consecutive_failures = 0
        else:
            failed += 1
            consecutive_failures += 1

        # If 10+ consecutive failures, likely rate-limited — pause 30s
        if consecutive_failures == 10:
            logger.warning(
                f"[HW DELIVERY] {grade}: 10 consecutive failures — "
                f"pausing 30s for rate limit cooldown"
            )
            await asyncio.sleep(30.0)
            consecutive_failures = 0

        await asyncio.sleep(0.5)

    logger.info(
        f"[HW DELIVERY] {grade}: {sent} sent, {failed} failed "
        f"out of {len(phones_to_send)} phones"
    )
    return sent, failed


async def _send_no_homework_to_parents(grade: str, period_label: str) -> tuple[int, int]:
    """Send 'No Homework Assigned' notice to all parents of a grade.

    Returns (sent_count, failed_count).
    """
    from app.services.whatsapp_service import send_cloud_template_message

    parents = await _get_parents_for_grade(grade)
    if not parents:
        return 0, 0

    phones_to_send = _deduplicate_phones(parents)

    sent = 0
    failed = 0
    MAX_RETRIES = 2
    RETRY_DELAYS = [2.0, 5.0]

    for phone in phones_to_send:
        ok = False
        for attempt in range(1 + MAX_RETRIES):
            try:
                ok = await send_cloud_template_message(
                    phone,
                    "ppis_classwork_homework",
                    body_params=[grade, period_label, "No Homework Assigned"],
                )
                if ok:
                    break
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAYS[attempt])
            except Exception as e:
                logger.error(f"Error sending no-homework to {phone}: {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAYS[attempt])

        if ok:
            sent += 1
        else:
            failed += 1

        await asyncio.sleep(0.5)

    return sent, failed


def _deduplicate_phones(parents: list[dict]) -> list[str]:
    """Extract and deduplicate phone numbers from parent records."""
    seen: set[str] = set()
    phones: list[str] = []
    for p in parents:
        for ph in [p["father_phone"], p["mother_phone"]]:
            digits = re.sub(r"\D", "", ph)
            if len(digits) >= 10:
                digits = digits[-10:]
                if digits not in seen:
                    seen.add(digits)
                    phones.append(digits)
    return phones


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


async def get_registered_docs() -> list[dict]:
    """Return all registered homework docs for admin dashboard."""
    return await _get_homework_docs()


# ---------------------------------------------------------------------------
# Daily auto-clear — wipe all Google Docs at end of school day
# ---------------------------------------------------------------------------

TEMPLATE_HEADER = """PPIS Classwork & Homework - {class_name}

Just mention the subject and homework/classwork.
Class and period are added automatically.

Example:
English - Read chapter 5, answer Q1-Q5
Maths - Complete worksheet page 32

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
START YOUR ENTRIES BELOW THIS LINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""


async def _clear_google_doc(doc_id: str, class_name: str) -> bool:
    """Clear a Google Doc and restore the template header.

    Uses the Google Docs API to delete all content and re-insert
    the template instructions.
    """
    token = await _get_google_access_token()
    if not token:
        logger.error(f"No Google token for clearing doc {doc_id}")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    api_url = f"https://docs.googleapis.com/v1/documents/{doc_id}"

    try:
        async with httpx.AsyncClient() as client:
            # Get current doc to find content length
            resp = await client.get(api_url, headers=headers, timeout=15.0)
            if resp.status_code != 200:
                logger.error(f"Failed to read doc {doc_id}: HTTP {resp.status_code}")
                return False

            doc = resp.json()
            body = doc.get("body", {})
            content = body.get("content", [])

            # Find the end index of the document body
            end_index = 1
            for element in content:
                ei = element.get("endIndex", 0)
                if ei > end_index:
                    end_index = ei

            # Build batch update requests
            requests_list = []

            # Delete all content (except the very first newline at index 1)
            if end_index > 2:
                requests_list.append({
                    "deleteContentRange": {
                        "range": {
                            "startIndex": 1,
                            "endIndex": end_index - 1,
                        }
                    }
                })

            # Insert the template header
            template_text = TEMPLATE_HEADER.format(class_name=class_name)
            requests_list.append({
                "insertText": {
                    "location": {"index": 1},
                    "text": template_text,
                }
            })

            # Execute batch update
            batch_url = f"{api_url}:batchUpdate"
            resp = await client.post(
                batch_url,
                headers=headers,
                json={"requests": requests_list},
                timeout=15.0,
            )
            if resp.status_code == 200:
                return True
            logger.error(
                f"Failed to clear doc {doc_id}: HTTP {resp.status_code} - "
                f"{resp.text[:200]}"
            )
            return False
    except Exception as e:
        logger.error(f"Error clearing doc {doc_id}: {e}")
        return False


async def rename_google_doc(doc_id: str, new_title: str) -> bool:
    """Rename a Google Doc via the Google Drive API.

    The document title is a Drive file property, so we use the
    Drive v3 files.update endpoint with the new name.
    """
    token = await _get_google_access_token()
    if not token:
        logger.error("No Google token for renaming doc")
        return False

    url = f"https://www.googleapis.com/drive/v3/files/{doc_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                url,
                headers=headers,
                json={"name": new_title},
                timeout=15.0,
            )
            if resp.status_code == 200:
                logger.info(f"Renamed doc {doc_id} to '{new_title}'")
                return True
            logger.error(
                f"Failed to rename doc {doc_id}: HTTP {resp.status_code} - "
                f"{resp.text[:200]}"
            )
            return False
    except Exception as e:
        logger.error(f"Error renaming doc {doc_id}: {e}")
        return False


async def create_google_doc(title: str) -> dict | None:
    """Create a new Google Doc via the Google Docs API.

    Returns {"doc_id": ..., "doc_url": ...} or None on failure.
    The doc is created in the default Drive location of the authenticated user.
    """
    token = await _get_google_access_token()
    if not token:
        logger.error("No Google token for creating doc")
        return None

    url = "https://docs.googleapis.com/v1/documents"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json={"title": title},
                timeout=15.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                doc_id = data.get("documentId", "")
                doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
                logger.info(f"Created new doc '{title}': {doc_id}")

                # Initialize with template header
                class_name = title.replace("PPIS Classwork & Homework - ", "")
                await _clear_google_doc(doc_id, class_name)

                return {"doc_id": doc_id, "doc_url": doc_url}
            logger.error(
                f"Failed to create doc '{title}': HTTP {resp.status_code} - "
                f"{resp.text[:200]}"
            )
            return None
    except Exception as e:
        logger.error(f"Error creating doc '{title}': {e}")
        return None


async def move_doc_to_folder(doc_id: str, folder_id: str) -> bool:
    """Move a Google Doc to a specific Drive folder.

    Uses the Drive v3 files.update endpoint to change the parent folder.
    """
    token = await _get_google_access_token()
    if not token:
        return False

    url = f"https://www.googleapis.com/drive/v3/files/{doc_id}?addParents={folder_id}&fields=id,parents"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(url, headers=headers, timeout=15.0)
            if resp.status_code == 200:
                logger.info(f"Moved doc {doc_id} to folder {folder_id}")
                return True
            logger.warning(f"Move doc failed: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logger.warning(f"Move doc failed: {e}")
        return False


async def share_google_doc(doc_id: str, email: str, role: str = "writer") -> bool:
    """Share a Google Doc with a user via the Google Drive API.

    Creates a permission for the given email with the specified role.
    """
    token = await _get_google_access_token()
    if not token:
        logger.error("No Google token for sharing doc")
        return False

    url = f"https://www.googleapis.com/drive/v3/files/{doc_id}/permissions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "type": "user",
        "role": role,
        "emailAddress": email,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json=body,
                params={"sendNotificationEmail": "true"},
                timeout=15.0,
            )
            if resp.status_code in (200, 201):
                logger.info(f"Shared doc {doc_id} with {email} as {role}")
                return True
            logger.error(
                f"Failed to share doc {doc_id} with {email}: "
                f"HTTP {resp.status_code} - {resp.text[:200]}"
            )
            return False
    except Exception as e:
        logger.error(f"Error sharing doc {doc_id} with {email}: {e}")
        return False


async def daily_clear_all_docs() -> dict:
    """Clear all 34 homework Google Docs and reset content hashes.

    Scheduled to run at 3:00 PM IST (9:30 UTC) every school day.
    Restores each doc to the clean template and resets stored hashes
    so the next day's entries are treated as new content.
    """
    logger.info("=== DAILY DOC CLEAR: Starting end-of-day cleanup ===")

    docs = await _get_homework_docs()
    if not docs:
        logger.warning("No homework docs registered. Skipping daily clear.")
        return {"status": "no_docs", "cleared": 0, "failed": 0}

    cleared = 0
    failed = 0

    for doc_info in docs:
        grade = doc_info["grade"]
        doc_id = doc_info["doc_id"]

        # Phase 1: only clear eligible grades (Grade 9-12)
        if not _HOMEWORK_ELIGIBLE_GRADES.match(grade):
            continue

        ok = await _clear_google_doc(doc_id, grade)
        if ok:
            cleared += 1
            logger.info(f"Cleared doc for {grade}")
        else:
            failed += 1
            logger.error(f"Failed to clear doc for {grade}")

        await asyncio.sleep(0.5)

    # Reset all content hashes so tomorrow's entries are treated as new
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute("DELETE FROM homework_doc_state")
        await db.commit()
        logger.info("Reset all homework content hashes")
    finally:
        await db.close()

    logger.info(
        f"=== DAILY DOC CLEAR COMPLETE: {cleared} cleared, {failed} failed ==="
    )
    return {"status": "done", "cleared": cleared, "failed": failed}


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


def run_daily_clear_sync() -> None:
    """Synchronous wrapper for daily doc clear (APScheduler)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(daily_clear_all_docs())
        else:
            asyncio.run(daily_clear_all_docs())
    except RuntimeError:
        asyncio.run(daily_clear_all_docs())
