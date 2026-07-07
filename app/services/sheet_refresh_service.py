"""
Google Sheet auto-refresh service.

Fetches the latest teacher data from the published Google Sheet CSV every 24 hours
and updates the in-memory TEACHER_DATA used by the bot.

Also refreshes PI Sheet student data from a published Google Sheet CSV URL,
updating the pi_sheet_students table in the database.

Published CSV URL for the first tab (Gmail_Updation_2026_27):
https://docs.google.com/spreadsheets/d/e/2PACX-1vQ6ZUQa6hhQ_9QXYIuJuWsleqSZ5vgXbWrRvDfvFpqdEx0iW28Z1GlpLdt9T1F9AvX4BdgPjAmfvH96/pub?output=csv&gid=74061219
"""

import csv
import io
import json
import logging
import os
import re as _re
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

# Published Google Sheet CSV URL (first tab: Gmail_Updation_2026_27)
SHEET_CSV_URL = os.getenv(
    "TEACHER_SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ6ZUQa6hhQ_9QXYIuJuWsleqSZ5vgXbWrRvDfvFpqdEx0iW28Z1GlpLdt9T1F9AvX4BdgPjAmfvH96/pub?output=csv&gid=74061219",
)

# PI Sheet CSV URL — same spreadsheet, same tab (teacher/grade data is the PI Sheet)
# The user's link: https://docs.google.com/spreadsheets/d/154Q3YmtvRojjmSqArIVTd94u2PZtS7tQ0Zv7DSPn8NQ/edit?gid=74061219
PI_SHEET_CSV_URL = os.getenv(
    "PI_SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ6ZUQa6hhQ_9QXYIuJuWsleqSZ5vgXbWrRvDfvFpqdEx0iW28Z1GlpLdt9T1F9AvX4BdgPjAmfvH96/pub?output=csv&gid=74061219",
)

# Published Google Sheet base URL for all grade tabs
PI_SHEET_PUB_BASE = os.getenv(
    "PI_SHEET_PUB_BASE",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ6ZUQa6hhQ_9QXYIuJuWsleqSZ5vgXbWrRvDfvFpqdEx0iW28Z1GlpLdt9T1F9AvX4BdgPjAmfvH96/pub?output=csv",
)

# All known grade tab GIDs in the PI Sheet
PI_SHEET_GRADE_GIDS = [
    "1288447916", "2004260388", "1830685668", "1370627064", "1786778811",
    "616483027", "1943511617", "1102992088", "352154940",
    "81657730", "2002492962", "390677163", "2068519553", "873031775",
    "149553903", "22291716", "1168194216", "505784395", "1571684282",
    "353406549", "1466129543", "1448970633", "743552850", "43691386",
    "1591415360", "2010955306", "1384983764", "1505127962", "1630447148",
    "1059379388", "187664454", "573773967", "1080176279", "696644819",
    "523098156", "255213929",
]

# Known male teachers (for honorific)
MALE_TEACHERS = {"shyam manohar", "tarun dhall", "christy joseph", "deepak"}


def _parse_teacher_csv(csv_text: str) -> list[dict]:
    """Parse the CSV text from the Google Sheet into TEACHER_DATA format."""
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    # Find the header row that contains "Grades" AND "Class Teacher Email ID" or "Whatsapp"
    # (use the most complete header row, not the partial one above it)
    header_idx = -1
    for i, row in enumerate(rows):
        cells = [c.strip().lower() for c in row]
        if "grades" in cells and any("whatsapp" in c for c in cells):
            header_idx = i
            break
    # Fallback: find any row with "Grades" and "teacher"
    if header_idx < 0:
        for i, row in enumerate(rows):
            cells = [c.strip().lower() for c in row]
            if "grades" in cells and any("teacher" in c for c in cells):
                header_idx = i
                break

    if header_idx < 0:
        logger.error("SHEET REFRESH: Could not find header row in CSV")
        return []

    header = [c.strip().lower() for c in rows[header_idx]]

    # Map column positions
    col_map: dict[str, int] = {}
    for idx, col in enumerate(header):
        if col == "grades":
            col_map["grade"] = idx
        elif "teacher name" in col:
            col_map["teacher"] = idx
        elif "class teacher email" in col or ("teacher email" in col and "class email" not in col):
            col_map["email"] = idx
        elif col.startswith("class email"):
            col_map["class_email"] = idx
        elif "parent" in col and "email" in col:
            col_map["parents_email"] = idx
        elif "whatsapp" in col:
            col_map["whatsapp"] = idx

    # Fallback: if "teacher" not found, try second column
    if "teacher" not in col_map and "grade" in col_map:
        teacher_idx = col_map["grade"] + 1
        if teacher_idx < len(header):
            col_map["teacher"] = teacher_idx

    logger.info(f"SHEET REFRESH: Column mapping: {col_map}")

    if "grade" not in col_map or "teacher" not in col_map:
        logger.error(f"SHEET REFRESH: Missing required columns. Found: {col_map}")
        return []

    # Skip rows that look like duplicate headers or title rows
    skip_values = {"grades", "grade", "class teacher", "class teacher name", ""}

    # Detect the boundary of the actual CT section: stop when we hit a row
    # that contains "NON CT" anywhere, or a second header row with "Grades"
    # in the grade column after we've already started collecting data.
    stop_at_row = len(rows)
    for i in range(header_idx + 1, len(rows)):
        row_text = ",".join(rows[i]).lower()
        if "non ct" in row_text or "non-ct" in row_text:
            stop_at_row = i
            logger.info(f"SHEET REFRESH: Detected NON CT boundary at row {i}, stopping there")
            break
        # Also stop if we see a second header-like row after collecting some data
        if i > header_idx + 5:  # Only after we've seen some real data rows
            cells = [c.strip().lower() for c in rows[i]]
            if "grades" in cells and any("teacher" in c for c in cells):
                stop_at_row = i
                logger.info(f"SHEET REFRESH: Detected second header at row {i}, stopping there")
                break

    teacher_data = []
    for row in rows[header_idx + 1:stop_at_row]:
        if not row or len(row) <= col_map["grade"]:
            continue

        grade = row[col_map["grade"]].strip()
        if not grade or grade.lower() in skip_values:
            continue

        # Stop if we hit a second header-like row (e.g. "NON CT" section)
        teacher_val = row[col_map["teacher"]].strip() if col_map["teacher"] < len(row) else ""
        if teacher_val.lower() in skip_values:
            continue

        teacher = row[col_map["teacher"]].strip() if col_map["teacher"] < len(row) else ""
        email = row[col_map.get("email", 999)].strip() if col_map.get("email", 999) < len(row) else ""
        class_email = row[col_map.get("class_email", 999)].strip() if col_map.get("class_email", 999) < len(row) else ""
        parents_email = row[col_map.get("parents_email", 999)].strip() if col_map.get("parents_email", 999) < len(row) else ""
        whatsapp = row[col_map.get("whatsapp", 999)].strip() if col_map.get("whatsapp", 999) < len(row) else ""

        # Validate email-like fields: if they don't look like emails, discard them
        # This prevents NON-CT staff names from being stored as emails
        if class_email and "@" not in class_email:
            class_email = ""
        if parents_email and "@" not in parents_email:
            parents_email = ""
        if email and "@" not in email:
            email = ""

        # Clean up whatsapp number (remove spaces, dashes)
        whatsapp = whatsapp.replace(" ", "").replace("-", "").replace("+", "")
        # Remove .0 if exported as float
        if whatsapp.endswith(".0"):
            whatsapp = whatsapp[:-2]

        if not teacher:
            continue

        entry: dict = {
            "grade": grade,
            "teacher": teacher,
            "email": email,
            "class_email": class_email,
            "parents_email": parents_email,
            "whatsapp": whatsapp,
        }

        # Add gender for known male teachers
        teacher_lower = teacher.lower()
        for male_name in MALE_TEACHERS:
            if male_name in teacher_lower:
                entry["gender"] = "male"
                break

        teacher_data.append(entry)

    return teacher_data


async def fetch_and_update_teacher_data() -> bool:
    """Fetch the latest teacher data from Google Sheet and update in-memory TEACHER_DATA.
    Returns True if successful."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(SHEET_CSV_URL, timeout=30, follow_redirects=True)
            if resp.status_code != 200:
                logger.error(f"SHEET REFRESH: Failed to fetch CSV. HTTP {resp.status_code}")
                return False

            csv_text = resp.text
            if not csv_text or len(csv_text) < 50:
                logger.error("SHEET REFRESH: CSV response too short or empty")
                return False

        new_data = _parse_teacher_csv(csv_text)
        if not new_data:
            logger.error("SHEET REFRESH: No teacher data parsed from CSV")
            return False

        # Only update if we got a reasonable number of entries
        if len(new_data) < 5:
            logger.warning(f"SHEET REFRESH: Only {len(new_data)} entries parsed, skipping update")
            return False

        # Update the in-memory TEACHER_DATA in openai_service
        from app.services.openai_service import TEACHER_DATA
        TEACHER_DATA.clear()
        TEACHER_DATA.extend(new_data)

        logger.info(f"SHEET REFRESH: Updated TEACHER_DATA with {len(new_data)} entries from Google Sheet")

        # Also update the CLASS TEACHERS section in the system prompt
        await _rebuild_system_prompt_teachers(new_data)

        return True

    except Exception as e:
        logger.error(f"SHEET REFRESH: Error fetching/parsing sheet: {e}")
        return False


async def _rebuild_system_prompt_teachers(teacher_data: list[dict]) -> None:
    """Rebuild the CLASS TEACHERS section of the system prompt from fresh data.

    IMPORTANT: Only replaces the CLASS TEACHERS section between its markers,
    preserving ALL other sections of the comprehensive system prompt (school info,
    fees, admissions, facilities, transport, escalation contacts, etc.).
    """
    try:
        from app.database import get_db
        db = await get_db()
        try:
            cursor = await db.execute("SELECT value FROM settings WHERE key = 'system_prompt'")
            row = await cursor.fetchone()
            if not row:
                return
            prompt = row[0]

            # Find and replace the CLASS TEACHERS section
            start_marker = "== CLASS TEACHERS"
            start_idx = prompt.find(start_marker)
            if start_idx < 0:
                logger.warning("SHEET REFRESH: No CLASS TEACHERS section found in system prompt")
                return

            # Find the end of the class teachers section (next == section or end of prompt)
            after_start = prompt.find("\n== ", start_idx + len(start_marker))
            if after_start < 0:
                # Try alternate marker format
                after_start = prompt.find("\n==", start_idx + len(start_marker))
            if after_start < 0:
                after_start = len(prompt)

            # Build new class teachers section with FULL details (email, whatsapp, class email, parents email)
            lines = ["== CLASS TEACHERS (2026-27) =="]
            for entry in teacher_data:
                honorific = "Mr." if entry.get("gender") == "male" else "Ms."
                parts = []
                if entry.get("email"):
                    parts.append(entry["email"])
                if entry.get("whatsapp"):
                    parts.append(f"WhatsApp: {entry['whatsapp']}")
                if entry.get("class_email"):
                    parts.append(f"Class: {entry['class_email']}")
                if entry.get("parents_email"):
                    parts.append(f"Parents: {entry['parents_email']}")
                detail = f" ({', '.join(parts)})" if parts else ""
                lines.append(f"{entry['grade']}: {honorific} {entry['teacher']}{detail}")

            new_section = "\n".join(lines)

            # Replace ONLY the class teachers section, preserve everything else
            new_prompt = prompt[:start_idx] + new_section + prompt[after_start:]

            # Sanity check: new prompt should not be drastically shorter than old one
            if len(new_prompt) < len(prompt) * 0.5:
                logger.error(
                    f"SHEET REFRESH: New prompt ({len(new_prompt)} chars) is much shorter "
                    f"than old ({len(prompt)} chars) — skipping update to protect comprehensive prompt"
                )
                return

            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('system_prompt', ?)",
                (new_prompt,),
            )
            await db.commit()
            logger.info(
                f"SHEET REFRESH: Updated CLASS TEACHERS section ({len(teacher_data)} teachers). "
                f"Prompt length: {len(new_prompt)} chars (was {len(prompt)} chars)"
            )
        finally:
            await db.close()
    except Exception as e:
        logger.error(f"SHEET REFRESH: Error updating system prompt: {e}")


async def fetch_and_update_pi_sheet() -> bool:
    """Fetch latest PI Sheet data from published Google Sheet CSV and update the
    pi_sheet_students table in the database.  Returns True on success."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(PI_SHEET_CSV_URL, timeout=30, follow_redirects=True)
            if resp.status_code != 200:
                logger.error(f"PI SHEET REFRESH: Failed to fetch CSV. HTTP {resp.status_code}")
                return False

            csv_text = resp.text
            if not csv_text or len(csv_text) < 50:
                logger.error("PI SHEET REFRESH: CSV response too short or empty")
                return False

        # Parse the CSV — same structure as teacher CSV but we extract grade info
        # to keep the pi_sheet_students table in sync with any grade/teacher changes
        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)

        # Find header row
        header_idx = -1
        for i, row in enumerate(rows):
            cells = [c.strip().lower() for c in row]
            if "grades" in cells and any("whatsapp" in c or "teacher" in c for c in cells):
                header_idx = i
                break

        if header_idx < 0:
            logger.error("PI SHEET REFRESH: Could not find header row")
            return False

        header = [c.strip().lower() for c in rows[header_idx]]
        col_grade = None
        col_teacher = None
        col_email = None
        col_whatsapp = None
        col_class_email = None
        col_parents_email = None

        for idx, col in enumerate(header):
            if col == "grades":
                col_grade = idx
            elif "teacher name" in col or (col_grade is not None and idx == col_grade + 1 and "teacher" not in col):
                if col_teacher is None:
                    col_teacher = idx
            elif "class teacher email" in col or ("teacher email" in col and "class email" not in col):
                col_email = idx
            elif col.startswith("class email"):
                col_class_email = idx
            elif "parent" in col and "email" in col:
                col_parents_email = idx
            elif "whatsapp" in col:
                col_whatsapp = idx

        if col_grade is None:
            logger.error("PI SHEET REFRESH: Grade column not found")
            return False

        if col_teacher is None and col_grade is not None:
            col_teacher = col_grade + 1

        skip_values = {"grades", "grade", "class teacher", "class teacher name", ""}

        # Detect NON CT boundary — same logic as _parse_teacher_csv
        stop_at_row = len(rows)
        for i in range(header_idx + 1, len(rows)):
            row_text = ",".join(rows[i]).lower()
            if "non ct" in row_text or "non-ct" in row_text:
                stop_at_row = i
                break
            if i > header_idx + 5:
                cells = [c.strip().lower() for c in rows[i]]
                if "grades" in cells and any("teacher" in c for c in cells):
                    stop_at_row = i
                    break

        from app.database import get_db
        db = await get_db()
        try:
            # Update pi_sheet_students metadata with latest grade/teacher mapping.
            # For each grade+teacher pair found in the CSV we update any matching
            # students in pi_sheet_students so their class_teacher column stays
            # current.  If no rows match we still count the entry so the log
            # reflects how many grade rows were processed.
            updated_count = 0
            for row in rows[header_idx + 1:stop_at_row]:
                if not row or len(row) <= col_grade:
                    continue
                grade = row[col_grade].strip()
                if not grade or grade.lower() in skip_values:
                    continue

                teacher = row[col_teacher].strip() if col_teacher is not None and col_teacher < len(row) else ""
                if not teacher or teacher.lower() in skip_values:
                    continue

                # Perform the actual UPDATE so students in this grade get the
                # latest class-teacher name written to the database.
                await db.execute(
                    "UPDATE pi_sheet_students SET class_teacher = ?, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE grade = ?",
                    (teacher, grade),
                )
                updated_count += 1

            await db.commit()
            logger.info(
                f"PI SHEET REFRESH: Processed {updated_count} grade entries from PI Sheet CSV "
                f"at {datetime.utcnow().isoformat()}"
            )

            # Store the last refresh timestamp
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('pi_sheet_last_refresh', ?)",
                (datetime.utcnow().isoformat(),),
            )
            await db.commit()
        finally:
            await db.close()

        return True

    except Exception as e:
        logger.error(f"PI SHEET REFRESH: Error: {e}")
        return False


async def sync_pi_sheet_phones_to_face_db() -> int:
    """Sync phone numbers from PI Sheet TEACHER_DATA into the face recognition DB.

    Matches teachers by first name (case-insensitive) and updates phone numbers
    for any face DB entries that are missing one. Returns number of entries updated.
    """
    from app.services.openai_service import TEACHER_DATA
    from app.database import get_db

    if not TEACHER_DATA:
        logger.info("FACE-PHONE SYNC: No TEACHER_DATA available, skipping")
        return 0

    # Build name -> phone mapping from PI sheet (only entries with WhatsApp numbers)
    pi_phones: dict[str, str] = {}
    for entry in TEACHER_DATA:
        name = entry.get("teacher", "").strip().lower()
        phone = entry.get("whatsapp", "").strip()
        if name and phone:
            # Normalize phone
            digits = _re.sub(r"[^0-9]", "", phone.split(",")[0])
            if len(digits) == 10:
                digits = "91" + digits
            if len(digits) >= 12:
                pi_phones[name] = digits
                # Also store first name for partial matching
                first = name.split()[0]
                if first not in pi_phones:
                    pi_phones[first] = digits

    if not pi_phones:
        logger.info("FACE-PHONE SYNC: No phone numbers found in TEACHER_DATA")
        return 0

    db = await get_db()
    try:
        # Get face DB teachers without phone numbers
        cursor = await db.execute(
            "SELECT DISTINCT person_id, name, phone FROM agent_registered_faces "
            "WHERE person_id LIKE 'TEACHER_%'"
        )
        rows = await cursor.fetchall()

        updated = 0
        for row in rows:
            pid, face_name, current_phone = row[0], row[1], row[2]
            if current_phone and current_phone.strip():
                continue  # Already has phone

            face_lower = face_name.strip().lower()

            # Try exact full name match
            matched_phone = pi_phones.get(face_lower)

            # Try first name match (only if unambiguous)
            if not matched_phone:
                first = face_lower.split()[0] if face_lower.split() else ""
                if first:
                    matched_phone = pi_phones.get(first)

            if matched_phone:
                await db.execute(
                    "UPDATE agent_registered_faces SET phone = ? "
                    "WHERE person_id = ? COLLATE NOCASE",
                    (matched_phone, pid),
                )
                updated += 1
                logger.info(f"FACE-PHONE SYNC: {face_name} ({pid}) -> {matched_phone}")

        if updated:
            await db.commit()
        logger.info(f"FACE-PHONE SYNC: Updated {updated} teacher phone numbers from PI Sheet")
        return updated
    except Exception as e:
        logger.error(f"FACE-PHONE SYNC: Error: {e}")
        return 0
    finally:
        await db.close()


def refresh_teacher_data_sync() -> None:
    """Synchronous wrapper for the scheduler — refreshes both teacher data AND PI Sheet."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(fetch_and_update_teacher_data())
        if result:
            logger.info("SHEET REFRESH: Teacher data refresh completed successfully")
        else:
            logger.error("SHEET REFRESH: Teacher data refresh failed")

        # Also refresh PI Sheet data
        pi_result = loop.run_until_complete(fetch_and_update_pi_sheet())
        if pi_result:
            logger.info("PI SHEET REFRESH: Scheduled refresh completed successfully")
        else:
            logger.error("PI SHEET REFRESH: Scheduled refresh failed")

        # Sync teacher phone numbers from PI Sheet to face DB
        loop.run_until_complete(sync_pi_sheet_phones_to_face_db())
    except Exception as e:
        logger.error(f"SHEET REFRESH: Scheduled refresh error: {e}")
    finally:
        loop.close()


def refresh_pi_sheet_sync() -> None:
    """Synchronous wrapper for PI Sheet refresh only."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(fetch_and_update_pi_sheet())
        if result:
            logger.info("PI SHEET REFRESH: Standalone refresh completed successfully")
        else:
            logger.error("PI SHEET REFRESH: Standalone refresh failed")
    except Exception as e:
        logger.error(f"PI SHEET REFRESH: Standalone refresh error: {e}")
    finally:
        loop.close()


def refresh_pi_sheet_full_sync() -> None:
    """Synchronous wrapper for full PI Sheet refresh (all grade tabs + cross-grade dedup)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(fetch_all_pi_sheet_tabs())
        if result:
            logger.info("PI SHEET FULL REFRESH: Daily refresh completed successfully")
        else:
            logger.error("PI SHEET FULL REFRESH: Daily refresh failed")
    except Exception as e:
        logger.error(f"PI SHEET FULL REFRESH: Daily refresh error: {e}")
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Parent phone data population from personalized_parents.json
# ---------------------------------------------------------------------------

def _normalize_grade_for_db(grade: str) -> str:
    """Normalize a raw grade string to canonical DB format."""
    g = grade.strip()
    g_upper = g.upper()
    if g_upper.startswith("GRADE "):
        g = "Grade " + g_upper[6:]
    elif g_upper.startswith("PREP "):
        g = "Prep " + g_upper[5:]
    elif g_upper in ("POPSICLE", "POPSICLES"):
        g = "Popsicles"
    return g


def _find_phone_column(header_cells: list[str], parent_type: str) -> int:
    """Find the column index for a parent phone number.

    Handles all known variations: 'FATHER MOBILE NO.', 'FATHERMOBILE',
    'FATHER MOB', 'FATHER MOBILE', "FATHER'S MOBILE", etc.
    """
    parent_upper = parent_type.upper()  # "FATHER" or "MOTHER"
    phone_keywords = ("MOBILE", "MOB", "PHONE", "CONTACT", "NO")
    for idx, cell in enumerate(header_cells):
        cu = cell.strip().upper().replace("'S", "").replace("'S", "")
        if parent_upper not in cu:
            continue
        # Direct matches like FATHERMOBILE, FATHER MOBILE NO, etc.
        remaining = cu.replace(parent_upper, "").strip()
        if any(kw in remaining or kw in cu for kw in phone_keywords):
            return idx
        # Exact combined form e.g. FATHERMOBILE
        if cu == f"{parent_upper}MOBILE":
            return idx
    return -1


def _normalize_phone(phone: str) -> str:
    """Normalize an Indian phone number to 91XXXXXXXXXX format.

    Handles multiple numbers separated by / or , (e.g. '8233855150 / 01161380361')
    by preferring the first valid 10-digit mobile number (starting with 6-9).
    """
    if not phone:
        return ""
    # Split on common separators to handle multiple numbers
    parts = _re.split(r"[/,]", phone)
    candidates = []
    for part in parts:
        digits = _re.sub(r"\D", "", part.strip())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]  # strip country code for uniform comparison
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]  # strip leading 0 for landlines
        if len(digits) == 10:
            candidates.append(digits)
    # Prefer mobile numbers (start with 6-9) over landlines
    for d in candidates:
        if d[0] in "6789":
            return f"91{d}"
    # Fall back to first candidate if no mobile found
    if candidates:
        return f"91{candidates[0]}"
    # Last resort: old behavior
    digits = _re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    if len(digits) > 10:
        return f"91{digits[-10:]}"
    return digits


async def fetch_all_pi_sheet_tabs() -> bool:
    """Fetch ALL grade tabs from the published PI Sheet and rebuild pi_sheet_students.

    This is the authoritative student import function. It:
    - Fetches every grade tab (38+ tabs) from the published Google Sheet
    - Handles all column-name variations (FATHERMOBILE, FATHER MOBILE NO., etc.)
    - Excludes withdrawn students (rows after 'Withdrawal' marker)
    - Deduplicates by (name, grade)
    - Replaces all rows in pi_sheet_students
    """
    active_students: list[dict] = []

    async with httpx.AsyncClient() as client:
        for gid in PI_SHEET_GRADE_GIDS:
            try:
                url = f"{PI_SHEET_PUB_BASE}&gid={gid}"
                resp = await client.get(url, timeout=20, follow_redirects=True)
                if resp.status_code != 200:
                    logger.warning(f"PI SHEET TAB gid={gid}: HTTP {resp.status_code}")
                    continue

                text = resp.text
                if not text or len(text) < 30:
                    continue

                reader = csv.reader(io.StringIO(text))
                rows = list(reader)
                if len(rows) < 2:
                    continue

                # --- Find header row ---
                header_idx = -1
                name_col = -1
                for i, row in enumerate(rows):
                    cells_upper = [c.strip().upper() for c in row]
                    for j, cell in enumerate(cells_upper):
                        if cell in ("STUDENT NAME", "STUDENTNAME", "STUDENT  NAME"):
                            header_idx = i
                            name_col = j
                            break
                    if header_idx >= 0:
                        break
                if header_idx < 0 or name_col < 0:
                    continue

                header_cells = [c.strip() for c in rows[header_idx]]
                header_upper = [c.upper() for c in header_cells]

                grade_col = -1
                for j, cell in enumerate(header_upper):
                    if cell == "GRADE":
                        grade_col = j
                        break

                father_phone_col = _find_phone_column(header_cells, "FATHER")
                mother_phone_col = _find_phone_column(header_cells, "MOTHER")

                # Determine tab grade from first data row
                tab_grade = ""
                if grade_col >= 0:
                    for row in rows[header_idx + 1 :]:
                        if grade_col < len(row) and row[grade_col].strip():
                            tab_grade = row[grade_col].strip()
                            break

                # --- Parse student rows ---
                in_withdrawal = False
                for i in range(header_idx + 1, len(rows)):
                    row = rows[i]
                    row_text = ",".join(row).strip().lower()

                    if not row_text or row_text.replace(",", "").strip() == "":
                        continue

                    # Detect withdrawal section markers.
                    # A marker row is any row whose first non-empty cell
                    # contains "withdraw" (covers "Withdrawal 2025-26",
                    # "Withdrawals in Session 2023-24", "Potential
                    # Withdrawal 2023-24", etc.).  Once we enter a
                    # withdrawal section, every subsequent row is skipped
                    # (withdrawn students are never followed by active
                    # students in these sheets).
                    first_cell = ""
                    for cell in row:
                        if cell.strip():
                            first_cell = cell.strip().lower()
                            break
                    if "withdraw" in first_cell:
                        in_withdrawal = True
                        continue

                    if in_withdrawal:
                        continue

                    if name_col >= len(row):
                        continue

                    name = row[name_col].strip()
                    if (
                        not name
                        or len(name) < 2
                        or name.upper()
                        in ("STUDENT NAME", "STUDENTNAME", "S.NO.", "SR. NO.", "NAME")
                        or name.isdigit()
                    ):
                        continue

                    # Skip individual students marked as withdrawn
                    # inline (e.g. "(WITHDRAWAN THE SERVICES)" in the
                    # Previous GRADE column).  Only trigger on cells
                    # whose text *starts with* "withdraw" or is wrapped
                    # like "(withdraw…)" — ignore casual mentions in
                    # notes/comments columns.
                    _is_inline_withdrawn = False
                    for c in row:
                        ct = c.strip().lower()
                        if not ct:
                            continue
                        if ct.startswith("withdraw") or ct.startswith("(withdraw"):
                            _is_inline_withdrawn = True
                            break
                    if _is_inline_withdrawn:
                        logger.debug(
                            f"PI SHEET TAB gid={gid}: skipping withdrawn "
                            f"student '{name}' (inline marker)"
                        )
                        continue

                    grade = (
                        row[grade_col].strip()
                        if grade_col >= 0
                        and grade_col < len(row)
                        and row[grade_col].strip()
                        else tab_grade
                    )
                    grade = _normalize_grade_for_db(grade)

                    fp = (
                        _normalize_phone(row[father_phone_col].strip())
                        if father_phone_col >= 0 and father_phone_col < len(row)
                        else ""
                    )
                    mp = (
                        _normalize_phone(row[mother_phone_col].strip())
                        if mother_phone_col >= 0 and mother_phone_col < len(row)
                        else ""
                    )

                    active_students.append(
                        {
                            "name": name,
                            "grade": grade,
                            "father_phone": fp,
                            "mother_phone": mp,
                        }
                    )

            except Exception as e:
                logger.warning(f"PI SHEET TAB gid={gid}: {e}")
                continue

    if not active_students:
        logger.error("PI SHEET FULL REFRESH: No students found across any tab")
        return False

    # --- Deduplicate by (NAME, GRADE) ---
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for s in active_students:
        key = (s["name"].upper().strip(), s["grade"])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    same_grade_dupes = len(active_students) - len(unique)

    # --- Cross-grade dedup: keep highest grade per (name, phone) ---
    # Students who appear in multiple grade tabs (e.g. promoted but old tab
    # not cleaned) should only be kept in the highest grade.
    def _extract_grade_num(grade_str: str) -> int:
        m = _re.search(r"(\d+)", grade_str)
        return int(m.group(1)) if m else -1

    # Build map: (name_upper, phone_last10) → highest grade number
    highest: dict[tuple[str, str], int] = {}
    for s in unique:
        name_key = s["name"].upper().strip()
        gnum = _extract_grade_num(s["grade"])
        for ph in [s["father_phone"], s["mother_phone"]]:
            digits = _re.sub(r"\D", "", ph)
            if len(digits) >= 10:
                k = (name_key, digits[-10:])
                if k not in highest or gnum > highest[k]:
                    highest[k] = gnum

    cross_grade_removed = 0
    final: list[dict] = []
    for s in unique:
        name_key = s["name"].upper().strip()
        gnum = _extract_grade_num(s["grade"])
        dominated = False
        for ph in [s["father_phone"], s["mother_phone"]]:
            digits = _re.sub(r"\D", "", ph)
            if len(digits) >= 10:
                k = (name_key, digits[-10:])
                if highest.get(k, gnum) > gnum:
                    dominated = True
                    break
        if dominated:
            cross_grade_removed += 1
            logger.info(
                f"PI SHEET DEDUP: Removing {s['name']} from {s['grade']} "
                f"(also in a higher grade)"
            )
        else:
            final.append(s)

    unique = final

    logger.info(
        f"PI SHEET FULL REFRESH: {len(unique)} unique active students "
        f"(from {len(active_students)} raw, {same_grade_dupes} same-grade "
        f"dupes removed, {cross_grade_removed} cross-grade dupes removed)"
    )

    # --- Replace all rows in pi_sheet_students ---
    from app.database import get_db

    db = await get_db()
    try:
        # Save allowlisted students before deleting — these are withdrawal
        # students whose parents still need temporary access.
        al_phones = await db.execute(
            "SELECT phone_number, label FROM allowlist "
            "WHERE label LIKE '%temp until%'"
        )
        al_entries = await al_phones.fetchall()
        protected: list[dict] = []
        for phone, label in al_entries:
            import re as _re_al
            from datetime import date as _date
            exp_match = _re_al.search(r"temp until (\d{4}-\d{2}-\d{2})", label or "")
            if exp_match and _date.today() > _date.fromisoformat(exp_match.group(1)):
                continue  # expired
            rows_to_save = await db.execute(
                "SELECT student_name, grade, father_name, mother_name, "
                "father_mobile, mother_mobile, address, transport "
                "FROM pi_sheet_students "
                "WHERE father_mobile = ? OR mother_mobile = ?",
                (phone, phone),
            )
            for row in await rows_to_save.fetchall():
                protected.append(dict(zip(
                    ["student_name", "grade", "father_name", "mother_name",
                     "father_mobile", "mother_mobile", "address", "transport"],
                    row,
                )))

        await db.execute("DELETE FROM pi_sheet_students")

        for s in unique:
            await db.execute(
                "INSERT INTO pi_sheet_students "
                "(student_name, grade, father_name, mother_name, "
                "father_mobile, mother_mobile, address, transport) "
                "VALUES (?, ?, '', '', ?, ?, '', '')",
                (s["name"], s["grade"], s["father_phone"], s["mother_phone"]),
            )

        # Re-insert protected (allowlisted) students
        seen_names: set[str] = set()
        for p in protected:
            key = f"{p['student_name']}|{p['grade']}"
            if key in seen_names:
                continue
            seen_names.add(key)
            await db.execute(
                "INSERT INTO pi_sheet_students "
                "(student_name, grade, father_name, mother_name, "
                "father_mobile, mother_mobile, address, transport) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (p["student_name"], p["grade"], p["father_name"],
                 p["mother_name"], p["father_mobile"], p["mother_mobile"],
                 p["address"], p["transport"]),
            )

        if protected:
            logger.info(
                f"PI SHEET FULL REFRESH: re-added {len(seen_names)} "
                f"allowlisted student entries"
            )

        await db.commit()

        # Report phone coverage
        cur = await db.execute(
            "SELECT COUNT(*) FROM pi_sheet_students "
            "WHERE (father_mobile != '' AND father_mobile IS NOT NULL) "
            "OR (mother_mobile != '' AND mother_mobile IS NOT NULL)"
        )
        with_phones = (await cur.fetchone())[0]
        logger.info(
            f"PI SHEET FULL REFRESH: {len(unique)} students written, "
            f"{with_phones} with phone numbers, "
            f"{len(unique) - with_phones} missing phones"
        )
        return True
    except Exception as e:
        logger.error(f"PI SHEET FULL REFRESH: DB error: {e}")
        return False
    finally:
        await db.close()


def _normalize_grade_name(grade_raw: str) -> list[str]:
    """Normalize a grade string to canonical form (e.g. 'GRADE 3B' -> 'Grade 3B').

    Returns a list because multi-grade entries like 'POPSICLE & Grade 3B' yield
    multiple grades.
    """
    grade_raw = grade_raw.strip()
    if not grade_raw:
        return []
    if "&" in grade_raw:
        results = []
        for part in grade_raw.split("&"):
            results.extend(_normalize_grade_name(part))
        return results

    g = grade_raw.strip()
    g_upper = g.upper().strip()

    if g_upper.startswith("GRADE"):
        m = _re.match(r"GRADE\s*(\d{1,2})\s*([A-D])?", g_upper)
        if m:
            num = m.group(1)
            sec = m.group(2) or ""
            return [f"Grade {num}{sec}"]
    elif g_upper.startswith(("NURSERY", "NUR")):
        m = _re.match(r"(?:NURSERY|NUR)[\s-]*(\d)?", g_upper)
        if m:
            n = m.group(1) or ""
            return [f"Nursery {n}".strip()] if n else ["Nursery"]
    elif g_upper.startswith("PREP"):
        m = _re.match(r"PREP\s*(\d)?", g_upper)
        if m:
            n = m.group(1) or ""
            return [f"Prep {n}".strip()] if n else ["Prep"]
    elif "POPSICLE" in g_upper:
        return ["Popsicles"]
    return [g]


async def populate_parent_phones() -> bool:
    """Populate pi_sheet_students with parent phone numbers from personalized_parents.json.

    This reads the JSON file (which maps student names + grades to parent phones)
    and UPSERTS rows into pi_sheet_students so that _get_parents_by_grade() and
    _lookup_parent_child_class() can find parent phone numbers.

    Called on startup and during the 24-hour refresh cycle.
    """
    json_path = os.path.join(os.path.dirname(__file__), "..", "personalized_parents.json")
    # Also check project root
    if not os.path.isfile(json_path):
        json_path = os.path.join(os.path.dirname(__file__), "..", "..", "personalized_parents.json")
    if not os.path.isfile(json_path):
        logger.warning("PARENT PHONES: personalized_parents.json not found — skipping")
        return False

    try:
        with open(json_path, "r") as f:
            parents = json.load(f)
    except Exception as e:
        logger.error(f"PARENT PHONES: Failed to read JSON: {e}")
        return False

    if not parents:
        logger.warning("PARENT PHONES: JSON is empty")
        return False

    # Build student_map: (normalized_name, grade) -> {father_mobile, mother_mobile}
    student_map: dict[tuple[str, str], dict] = {}
    for entry in parents:
        raw_name = (entry.get("student_name") or "").strip()
        # For multi-student entries (e.g. "Nitya & Aarna"), use "grade" field
        # because it has grades for ALL children ("Grade 7A & Grade 9A"),
        # whereas "sheet" only has the first child's grade ("Grade 7A").
        # For single-student entries, prefer "sheet" over "grade" because
        # "sheet" preserves the section/number (e.g. "Nur 2" vs "Nursery").
        is_multi_student = "&" in raw_name
        if is_multi_student:
            raw_grade = (entry.get("grade") or entry.get("sheet") or "").strip()
        else:
            raw_grade = (entry.get("sheet") or entry.get("grade") or "").strip()
        phone = (entry.get("phone") or "").strip()
        role = (entry.get("role") or "").lower()

        if not raw_name or not phone:
            continue

        grades = _normalize_grade_name(raw_grade)
        # Handle multi-student entries like 'SEERAT SETHI & Rushank Sethi'
        names = [n.strip() for n in raw_name.split("&")] if is_multi_student else [raw_name]

        for i, name in enumerate(names):
            grade = grades[i] if i < len(grades) else (grades[0] if grades else "")
            if not grade:
                continue
            key = (name.upper(), grade)
            if key not in student_map:
                student_map[key] = {
                    "student_name": name,
                    "grade": grade,
                    "father_mobile": "",
                    "mother_mobile": "",
                }
            if role == "father":
                existing = student_map[key]["father_mobile"]
                if existing and phone not in existing:
                    student_map[key]["father_mobile"] = f"{existing},{phone}"
                else:
                    student_map[key]["father_mobile"] = phone
            elif role == "mother":
                existing = student_map[key]["mother_mobile"]
                if existing and phone not in existing:
                    student_map[key]["mother_mobile"] = f"{existing},{phone}"
                else:
                    student_map[key]["mother_mobile"] = phone

    if not student_map:
        logger.warning("PARENT PHONES: No valid records found in JSON")
        return False

    # Upsert into the database
    from app.database import get_db
    db = await get_db()
    try:
        inserted = 0
        updated = 0
        for (norm_name, grade), rec in student_map.items():
            # Check if the student already exists
            cursor = await db.execute(
                "SELECT id, father_mobile, mother_mobile FROM pi_sheet_students "
                "WHERE UPPER(student_name) = ? AND grade = ?",
                (norm_name, grade),
            )
            existing = await cursor.fetchone()

            if existing:
                # Update if phone data changed
                old_father = (existing["father_mobile"] or "").strip()
                old_mother = (existing["mother_mobile"] or "").strip()
                new_father = rec["father_mobile"] or old_father
                new_mother = rec["mother_mobile"] or old_mother
                if new_father != old_father or new_mother != old_mother:
                    await db.execute(
                        "UPDATE pi_sheet_students SET father_mobile = ?, mother_mobile = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (new_father, new_mother, existing["id"]),
                    )
                    updated += 1
            else:
                # Insert new record
                await db.execute(
                    "INSERT INTO pi_sheet_students "
                    "(student_name, grade, father_mobile, mother_mobile) "
                    "VALUES (?, ?, ?, ?)",
                    (rec["student_name"], grade, rec["father_mobile"], rec["mother_mobile"]),
                )
                inserted += 1

        # Clean up stale duplicate rows where the same student has a less
        # specific grade (e.g. "Nursery" vs "Nursery 2") from a prior run.
        cleaned = 0
        for (norm_name, grade), _ in student_map.items():
            # Find all rows for this student in the DB
            dup_cur = await db.execute(
                "SELECT id, grade FROM pi_sheet_students "
                "WHERE UPPER(student_name) = ?",
                (norm_name,),
            )
            all_rows = await dup_cur.fetchall()
            if len(all_rows) <= 1:
                continue
            # Delete rows with less specific grades
            for row in all_rows:
                row_grade = row["grade"] or ""
                if row_grade != grade and grade.startswith(row_grade):
                    await db.execute(
                        "DELETE FROM pi_sheet_students WHERE id = ?",
                        (row["id"],),
                    )
                    cleaned += 1

        await db.commit()
        logger.info(
            f"PARENT PHONES: Populated pi_sheet_students — "
            f"{inserted} inserted, {updated} updated, {cleaned} stale cleaned, "
            f"{len(student_map)} total records"
        )
        return True
    except Exception as e:
        logger.error(f"PARENT PHONES: Database error: {e}")
        return False
    finally:
        await db.close()


def populate_parent_phones_sync() -> None:
    """Synchronous wrapper for populate_parent_phones."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(populate_parent_phones())
        if result:
            logger.info("PARENT PHONES: Sync population completed successfully")
        else:
            logger.error("PARENT PHONES: Sync population failed")
    except Exception as e:
        logger.error(f"PARENT PHONES: Sync error: {e}")
    finally:
        loop.close()
