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


# ---------------------------------------------------------------------------
# Parent phone data population from personalized_parents.json
# ---------------------------------------------------------------------------

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
        # Prefer "sheet" field (e.g. "Nur 2") over "grade" (e.g. "Nursery")
        # because "sheet" preserves the section/number.
        raw_grade = (entry.get("sheet") or entry.get("grade") or "").strip()
        phone = (entry.get("phone") or "").strip()
        role = (entry.get("role") or "").lower()

        if not raw_name or not phone:
            continue

        grades = _normalize_grade_name(raw_grade)
        # Handle multi-student entries like 'SEERAT SETHI & Rushank Sethi'
        names = [n.strip() for n in raw_name.split("&")] if "&" in raw_name else [raw_name]

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
        seen_students: dict[str, set[str]] = {}
        for (norm_name, grade), _ in student_map.items():
            seen_students.setdefault(norm_name, set()).add(grade)
        for name, grades_set in seen_students.items():
            if len(grades_set) <= 1:
                continue
            # If we have both "Nursery" and "Nursery 2", remove "Nursery"
            for g in list(grades_set):
                more_specific = [g2 for g2 in grades_set if g2 != g and g2.startswith(g)]
                if more_specific:
                    c = await db.execute(
                        "DELETE FROM pi_sheet_students "
                        "WHERE UPPER(student_name) = ? AND grade = ?",
                        (name, g),
                    )
                    cleaned += c.rowcount

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
