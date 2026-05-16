"""Subject-teacher mapping service.

Loads subject→teacher mapping from the school's "Student Code Directory"
sheet and provides lookup: given a student's grade + detected subject,
return the correct subject teacher (name, email, phone).

The mapping is refreshed from a Google Sheet CSV on startup and
periodically.  Teacher contact info is cross-referenced from
TEACHER_DATA (class teachers) and the face database (registered
teacher phones).
"""

import csv
import io
import logging
import os
import re

import httpx

from app.services.openai_service import TEACHER_DATA

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google Sheet URL (published CSV of the "Student Code Directory" tab)
# Override via env var if the sheet moves.
# ---------------------------------------------------------------------------
SUBJECT_SHEET_URL = os.getenv(
    "SUBJECT_TEACHER_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/18Y9oBoIjkLUXvS2a5oLpNqEQH8lNvHl5"
    "/export?format=csv&gid=618529147",
)

# ---------------------------------------------------------------------------
# In-memory mapping populated on startup / periodic refresh
# Structure:  list[ dict{class, subject, teacher} ]
# ---------------------------------------------------------------------------
SUBJECT_TEACHER_MAP: list[dict] = []

# Teacher name → contact cache (email, phone)
_TEACHER_CONTACTS: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Grade normalisation helpers
# ---------------------------------------------------------------------------
_GRADE_ALIASES: dict[str, str] = {}


def _normalise_grade(raw: str) -> str:
    """Normalise grade strings so 'Class 3', 'Grade 3', 'GRADE 3A' etc.
    can be compared consistently."""
    g = raw.strip().upper()
    g = re.sub(r"\s+", " ", g)
    # "GRADE 1A" → "GRADE 1A", "CLASS 3" → "CLASS 3"
    # Unify: remove leading "GRADE " or "CLASS " and re-add "CLASS "
    g = re.sub(r"^(GRADE|CLASS)\s*", "", g)
    # "NUR-1" → "NUR-1", "PREP-1" → "PREP-1"
    return g


def _normalise_subject(raw: str) -> str:
    """Normalise subject names for comparison."""
    s = raw.strip().upper()
    # Remove "(Class Teacher)" suffix
    s = re.sub(r"\(CLASS TEACHER\)", "", s).strip()
    # Normalise common variations
    aliases = {
        "MATH": "MATHS",
        "MATHEMATICS": "MATHS",
        "SOCIAL SCIENCE": "SST",
        "SOCIAL SCIENCE GEOGRAPHY": "SST",
        "SOCIAL STUDIES": "SST",
        "POL. SCIENCE": "POLITICAL SCIENCE",
        "POLITICAL SCIENCE": "POLITICAL SCIENCE",
        "SPORTS/PE": "PE",
        "PHYSICAL EDUCATION": "PE",
        "FINE ARTS": "FINE ARTS",
        "DRAWING/ART": "FINE ARTS",
        "DRAWING": "FINE ARTS",
        "ART": "FINE ARTS",
        "BUSINESS STUDIES": "BUSINESS STUDIES",
        "GK": "GK",
        "GENERAL KNOWLEDGE": "GK",
    }
    return aliases.get(s, s)


# ---------------------------------------------------------------------------
# Build teacher contact lookup from TEACHER_DATA + face DB
# ---------------------------------------------------------------------------
def _build_teacher_contacts() -> None:
    """Cross-reference teacher names with TEACHER_DATA to find emails and phones."""
    _TEACHER_CONTACTS.clear()

    # 1) From TEACHER_DATA (class teachers — has email + whatsapp)
    for entry in TEACHER_DATA:
        # Teacher field may contain "Name1 / Name2"
        raw_names = entry.get("teacher", "")
        for name in re.split(r"\s*/\s*", raw_names):
            name = name.strip()
            if not name:
                continue
            key = name.upper()
            if key not in _TEACHER_CONTACTS:
                _TEACHER_CONTACTS[key] = {
                    "email": entry.get("email", ""),
                    "phone": entry.get("whatsapp", ""),
                }

    # 2) Generate email from name pattern: firstname.lastname@ppischool.in
    # This covers subject teachers NOT in TEACHER_DATA
    _name_to_email_guesses = {
        "AASTHA KHATTAR": "aastha.khattar@ppischool.in",
        "ARADHANA GAMBHIR": "aradhana.gambhir@ppischool.in",
        "AVNEET KAUR": "avneet.kaur@ppischool.in",
        "CHRISTY JOSEPH": "christy.joseph@ppischool.in",
        "DAMANPREET KAUR": "damanpreet.kaur@ppischool.in",
        "DIVYA SHARMA": "divya.sharma@ppischool.in",
        "HARNOOR KAUR": "harnoor.kaur@ppischool.in",
        "KANINIKA JAIN": "kaninika.jain@ppischool.in",
        "KIRAN RANA": "kiran.rana@ppischool.in",
        "MANSI GUPTA": "mansi.gupta@ppischool.in",
        "MRIDUL PILANI": "mridul.pilani@ppischool.in",
        "NIKITA CHAWLA": "nikita.chawla@ppischool.in",
        "NITIN V": "nitin.v@ppischool.in",
        "POOJA ARORA": "pooja.arora@ppischool.in",
        "POOJA DHIMAJ": "pooja.dhimaj@ppischool.in",
        "POONAM SINGH": "poonam.singh@ppischool.in",
        "POSHIKA NARULA": "poshika.narula@ppischool.in",
        "PRABHJOT KAUR": "prabhjot.kaur@ppischool.in",
        "RASHMI": "rashmi@ppischool.in",
        "REVA RAJPUT": "reva.rajput@ppischool.in",
        "RITIKA DHAMIJA": "ritika.dhamija@ppischool.in",
        "RIYA ARORA": "riya.arora@ppischool.in",
        "SACHI TAHPER": "sachi.tahper@ppischool.in",
        "SEEMA BAKSHI": "seema.bakshi@ppischool.in",
        "SHIKHA SINGH": "shikha.singh@ppischool.in",
        "SHIPRA DHINGRA": "shipra.dhingra@ppischool.in",
        "SHYAM MANOHAR": "shyam.manohar@ppischool.in",
        "SUBIYA ASAD": "subiya.asad@ppischool.in",
        "SUCHETA SINHA": "sucheta.sinha@ppischool.in",
        "TARLEEN KAUR": "tarleen.kaur@ppischool.in",
        "TARUN DHALL": "tarun.dhall@ppischool.in",
        "TWINKLE TANDON": "twinkle.tandon@ppischool.in",
        "VAISHALI ARORA": "vaishali.arora@ppischool.in",
        "ABHISHEK TALUKA": "abhishek.taluka@ppischool.in",
    }
    for name_key, email in _name_to_email_guesses.items():
        if name_key not in _TEACHER_CONTACTS:
            _TEACHER_CONTACTS[name_key] = {"email": email, "phone": ""}
        elif not _TEACHER_CONTACTS[name_key].get("email"):
            _TEACHER_CONTACTS[name_key]["email"] = email


def _get_teacher_contact(teacher_name: str) -> dict:
    """Return {"name": ..., "email": ..., "phone": ...} for a teacher."""
    key = teacher_name.strip().upper()
    contact = _TEACHER_CONTACTS.get(key, {})
    return {
        "name": teacher_name.strip(),
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
    }


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------
def _parse_subject_csv(csv_text: str) -> list[dict]:
    """Parse the subject-teacher CSV into a list of mappings."""
    entries = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        raw_class = row.get("Class", "").strip()
        raw_subject = row.get("Subject", "").strip()
        raw_teacher = row.get("Teacher", "").strip()
        if not raw_class or not raw_subject or not raw_teacher:
            continue
        # Skip class-teacher-only rows (e.g. "Nur-1 (Class Teacher)")
        if "(class teacher)" in raw_subject.lower():
            continue
        entries.append({
            "class": raw_class,
            "class_norm": _normalise_grade(raw_class),
            "subject": raw_subject,
            "subject_norm": _normalise_subject(raw_subject),
            "teacher": raw_teacher,
        })
    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def refresh_subject_teacher_data() -> int:
    """Fetch the subject-teacher CSV from Google Sheets and rebuild the map.

    Returns the number of entries loaded, or -1 on failure.
    """
    global SUBJECT_TEACHER_MAP  # noqa: PLW0603

    url = SUBJECT_SHEET_URL
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(
                    f"[SUBJECT TEACHER] Sheet fetch failed HTTP {resp.status_code}"
                )
                return -1
            csv_text = resp.text
    except Exception as exc:
        logger.error(f"[SUBJECT TEACHER] Sheet fetch error: {exc}")
        return -1

    entries = _parse_subject_csv(csv_text)
    if entries:
        SUBJECT_TEACHER_MAP[:] = entries
        _build_teacher_contacts()
        logger.info(f"[SUBJECT TEACHER] Loaded {len(entries)} subject-teacher mappings")
    else:
        logger.warning("[SUBJECT TEACHER] CSV parsed but no entries found")
    return len(entries)


def load_embedded_data() -> int:
    """Load from embedded CSV constant (fallback if sheet fetch fails)."""
    global SUBJECT_TEACHER_MAP  # noqa: PLW0603

    entries = _parse_subject_csv(_EMBEDDED_CSV)
    SUBJECT_TEACHER_MAP[:] = entries
    _build_teacher_contacts()
    logger.info(f"[SUBJECT TEACHER] Loaded {len(entries)} entries from embedded data")
    return len(entries)


def find_subject_teacher(grade: str, subject: str) -> dict | None:
    """Find the subject teacher for a given grade and subject.

    Returns {"name": ..., "email": ..., "phone": ..., "subject": ..., "class": ...}
    or None if not found.
    """
    g_norm = _normalise_grade(grade)
    s_norm = _normalise_subject(subject)

    for entry in SUBJECT_TEACHER_MAP:
        if entry["class_norm"] == g_norm and entry["subject_norm"] == s_norm:
            contact = _get_teacher_contact(entry["teacher"])
            return {
                **contact,
                "subject": entry["subject"],
                "class": entry["class"],
            }

    # Fuzzy: try partial match on grade (e.g. "3" matches "3")
    for entry in SUBJECT_TEACHER_MAP:
        g_digits = re.sub(r"[^0-9A-Z]", "", g_norm)
        e_digits = re.sub(r"[^0-9A-Z]", "", entry["class_norm"])
        if g_digits and g_digits == e_digits and entry["subject_norm"] == s_norm:
            contact = _get_teacher_contact(entry["teacher"])
            return {
                **contact,
                "subject": entry["subject"],
                "class": entry["class"],
            }

    # Fuzzy: strip section letter (e.g. "3A" → "3") and try again
    g_num_only = re.sub(r"[^0-9]", "", g_norm)
    if g_num_only and g_num_only != g_norm:
        for entry in SUBJECT_TEACHER_MAP:
            e_num_only = re.sub(r"[^0-9]", "", entry["class_norm"])
            if g_num_only and g_num_only == e_num_only and entry["subject_norm"] == s_norm:
                contact = _get_teacher_contact(entry["teacher"])
                return {
                    **contact,
                    "subject": entry["subject"],
                    "class": entry["class"],
                }

    return None


def get_subjects_for_grade(grade: str) -> list[str]:
    """Return all available subjects for a grade (normalised names)."""
    g_norm = _normalise_grade(grade)
    g_num_only = re.sub(r"[^0-9]", "", g_norm)
    subjects = set()
    for entry in SUBJECT_TEACHER_MAP:
        g_digits = re.sub(r"[^0-9A-Z]", "", g_norm)
        e_digits = re.sub(r"[^0-9A-Z]", "", entry["class_norm"])
        e_num_only = re.sub(r"[^0-9]", "", entry["class_norm"])
        if (entry["class_norm"] == g_norm
                or (g_digits and g_digits == e_digits)
                or (g_num_only and g_num_only == e_num_only)):
            subjects.add(entry["subject"])
    return sorted(subjects)


# ---------------------------------------------------------------------------
# Embedded CSV data (fallback — last updated from the school sheet)
# ---------------------------------------------------------------------------
_EMBEDDED_CSV = """\
S.No.,Class,Subject,Teacher,Khanmigo Class Name,MagicSchool Room Name,Khanmigo Code,MagicSchool Code,Sharing Status with Parents
13,Class 3,EVS,Damanpreet Kaur,,,,,
14,Class 3,English,Harnoor Kaur,,,,,
15,Class 3,Hindi,Seema Bakshi,,,,,
16,Class 3,Math,Shreya Sikka,,,,,
17,Class 3,Computer,Reva Rajput,,,,,
18,Class 3,Computer,Pooja Dhimaj,,,,,
18,Class 4,Computer,Reva Rajput,,,,,
19,Class 4,EVS,Damanpreet Kaur,,,,,
20,Class 4,English,Prabhjot Kaur,,,,,
21,Class 4,French,Twinkle Tandon,,,,,
22,Class 4,German,Tarun Dhall,,,,,
23,Class 4,Hindi,Seema Bakshi,,,,,
24,Class 4,Maths,Aastha Khattar,,,,,
25,Class 5,Computer,Reva Rajput,,,,,
26,Class 5,English,Prabhjot Kaur,,,,,
27,Class 5,French,Twinkle Tandon,,,,,
28,Class 5,German,Tarun Dhall,,,,,
29,Class 5,Maths,Aastha Khattar,,,,,
30,Class 5,SST,Kaninika Jain,,,,,
31,Class 5,Science,Poshika Narula,,,,,
32,Class 6,Computer,Reva Rajput,,,,,
33,Class 6,English,Poonam Singh,,,,,
34,Class 6,English,Prabhjot Kaur,,,,,
35,Class 6,French,Twinkle Tandon,,,,,
36,Class 6,Sanskrit,Tarun Dhall,,,,,
37,Class 6,Hindi,Shikha Singh,,,,,
38,Class 6,Maths,Aastha Khattar,,,,,
39,Class 6,SST,Kaninika Jain,,,,,
40,Class 6,Science,Poshika Narula,,,,,
41,Class 7,Computer,Reva Rajput,,,,,
42,Class 7,English,Poonam Singh,,,,,
43,Class 7,French,Twinkle Tandon,,,,,
44,Class 7,German,Tarun Dhall,,,,,
45,Class 7,Hindi,Rashmi,,,,,
46,Class 7,Hindi,Shikha Singh,,,,,
47,Class 7,Maths,Nikita Chawla,,,,,
48,Class 7,SST,Vaishali Arora,,,,,
49,Class 7,Social Science,Ritika Dhamija,,,,,
50,Class 7,Science,Shyam Manohar,,,,,
51,Class 8,Computer,Mridul Pilani,,,,,
52,Class 8,English,Mansi Gupta,,,,,
53,Class 8,French,Riya Arora,,,,,
54,Class 8,German,Tarun Dhall,,,,,
55,Class 8,Hindi,Rashmi,,,,,
56,Class 8,Maths,Nikita Chawla,,,,,
57,Class 8,Psychology,Aradhana Gambhir,,,,,
58,Class 8,SST,Vaishali Arora,,,,,
59,Class 8,Social Science Geography,Ritika Dhamija,,,,,
60,Class 8,Science,Shyam Manohar,,,,,
61,Class 9,Biology,Nitin V,,,,,
62,Class 9,Chemistry,Pooja Arora,,,,,
63,Class 9,Computer,Kiran Rana,,,,,
64,Class 9,English,Mansi Gupta,,,,,
65,Class 9,French,Riya Arora,,,,,
66,Class 9,German,Tarun Dhall,,,,,
67,Class 9,Hindi,Harjeet Kaur,,,,,
68,Class 9,Hindi,Rashmi,,,,,
69,Class 9,History,Subiya Asad,,,,,
70,Class 9,Maths,Shipra Dhingra,,,,,
71,Class 9,Psychology,Aradhana Gambhir,,,,,
72,Class 9,SST,Vaishali Arora,,,,,
73,Class 9,Social Science Geography,Ritika Dhamija,,,,,
74,Class 10,Biology,Nitin V,,,,,
75,Class 10,Chemistry,Pooja Arora,,,,,
76,Class 10,Computer,Mridul Pilani,,,,,
77,Class 10,English,Avneet Kaur,,,,,
78,Class 10,French,Riya Arora,,,,,
79,Class 10,Hindi,Harjeet Kaur,,,,,
80,Class 10,History,Subiya Asad,,,,,
81,Class 10,Maths,Christy Joseph,,,,,
82,Class 10,Psychology,Aradhana Gambhir,,,,,
83,Class 10,SST,Vaishali Arora,,,,,
84,Class 10,Social Science Geography,Ritika Dhamija,,,,,
85,Class 11,Accounts,Sucheta Sinha,,,,,
86,Class 11,Biology,Nitin V,,,,,
87,Class 11,Chemistry,Pooja Arora,,,,,
88,Class 11,Computer,Kiran Rana,,,,,
89,Class 11,Economics,Sachi Tahper,,,,,
90,Class 11,English,Avneet Kaur,,,,,
91,Class 11,Entrepreneurship,Tarleen Kaur,,,,,
92,Class 11,Hindi,Harjeet Kaur,,,,,
93,Class 11,History,Subiya Asad,,,,,
94,Class 11,Maths,Christy Joseph,,,,,
95,Class 11,Psychology,Aradhana Gambhir,,,,,
96,Class 11,Political Science,Ritika Dhamija,,,,,
97,Class 11,Sports/PE,Divya Sharma,,,,,
98,Class 12,Accounts,Sucheta Sinha,,,,,
99,Class 12,Biology,Nitin V,,,,,
100,Class 12,Chemistry,Pooja Arora,,,,,
101,Class 12,Computer,Mridul Pilani,,,,,
102,Class 12,Economics,Sachi Tahper,,,,,
103,Class 12,English,Avneet Kaur,,,,,
104,Class 12,Entrepreneurship,Tarleen Kaur,,,,,
105,Class 12,Hindi,Harjeet Kaur,,,,,
106,Class 12,History,Subiya Asad,,,,,
107,Class 12,Maths,Christy Joseph,,,,,
108,Class 12,Psychology,Aradhana Gambhir,,,,,
109,Class 12,Political Science,Ritika Dhamija,,,,,
110,Class 12,Sports/PE,Divya Sharma,,,,,
111,Class 12,Fine Arts,Abhishek Taluka,,,,,
"""
