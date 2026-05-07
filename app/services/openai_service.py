import os
import re
import time
import json
import logging
import tempfile
import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

client: AsyncOpenAI | None = None
USE_FALLBACK = False
FALLBACK_SINCE: float = 0  # timestamp when fallback was activated
FALLBACK_RETRY_INTERVAL = 300  # retry OpenAI every 5 minutes

# Low-balance alert state
_QUOTA_ALERT_SENT = False  # Only send one alert per quota-exceeded event
ADMIN_PHONE_FOR_ALERTS = "918076455224"

# Teacher details for PP International School
TEACHER_DATA = [
    {"grade": "Popsicles", "teacher": "Sanya Mehra / Anu", "email": "sanya.mehra@ppischool.in", "class_email": "popsicles@ppischool.in", "parents_email": "popsicles.parents@ppischool.in", "whatsapp": "9289234655"},
    {"grade": "Nursery 1", "teacher": "Jasleen Kaur / Deepti", "email": "jasleen.kaur1@ppischool.in", "class_email": "nursery1@ppischool.in", "parents_email": "nursery1.parents@ppischool.in", "whatsapp": "9289234654"},
    {"grade": "Nursery 2", "teacher": "Priyanka Budhiraja / Geet", "email": "priyanka.budhiraja@ppischool.in", "class_email": "nursery2@ppischool.in", "parents_email": "nursery2.parents@ppischool.in", "whatsapp": "9289234657"},
    {"grade": "Nursery 3", "teacher": "Nashra / Deepti", "email": "nashra.naim@ppischool.in", "class_email": "nursery3@ppischool.in", "parents_email": "nursery3.parents@ppischool.in", "whatsapp": "9289236042"},
    {"grade": "Prep 1", "teacher": "Meenal Harjika", "email": "meenal.harjika@ppischool.in", "class_email": "prep1@ppischool.in", "parents_email": "prep1.parents@ppischool.in", "whatsapp": "9289234656"},
    {"grade": "Prep 2", "teacher": "Amita Sachdeva / Anjali", "email": "amita.sachdeva1@ppischool.in", "class_email": "prep2@ppischool.in", "parents_email": "prep2.parents@ppischool.in", "whatsapp": "9289234658"},
    {"grade": "Prep 3", "teacher": "Mahak Jain / Pooja", "email": "mahak.jain@ppischool.in", "class_email": "prep3@ppischool.in", "parents_email": "prep3.parents@ppischool.in", "whatsapp": "9289236056"},
    {"grade": "Grade 1A", "teacher": "Shreya Sikka / Pallavi", "email": "shreya.sikka@ppischool.in", "class_email": "grade1a@ppischool.in", "parents_email": "grade1a.parents@ppischool.in", "whatsapp": "9289234652"},
    {"grade": "Grade 1B", "teacher": "Muskan Motwani", "email": "muskan.motwani@ppischool.in", "class_email": "grade1b@ppischool.in", "parents_email": "grade1b.parents@ppischool.in", "whatsapp": "9289234660"},
    {"grade": "Grade 2A", "teacher": "Gargi Arora", "email": "gargi.arora@ppischool.in", "class_email": "grade2a@ppischool.in", "parents_email": "grade2a.parents@ppischool.in", "whatsapp": "9289234661"},
    {"grade": "Grade 2B", "teacher": "Tanvi Goyal / Sanchita", "email": "tanvi.goyal@ppischool.in", "class_email": "grade2b@ppischool.in", "parents_email": "grade2b.parents@ppischool.in", "whatsapp": "9289234662"},
    {"grade": "Grade 3A", "teacher": "Reva Rajput", "email": "reva.rajput@ppischool.in", "class_email": "grade3a@ppischool.in", "parents_email": "grade3a.parents@ppischool.in", "whatsapp": "9289236072"},
    {"grade": "Grade 3B", "teacher": "Seema Bakshi", "email": "seema.bakshi@ppischool.in", "class_email": "grade3b@ppischool.in", "parents_email": "grade3b.parents@ppischool.in", "whatsapp": "9289234664"},
    {"grade": "Grade 3C", "teacher": "Harnoor Kaur", "email": "harnoor.kaur@ppischool.in", "class_email": "grade3c@ppischool.in", "parents_email": "grade3c.parents@ppischool.in", "whatsapp": "9289234659"},
    {"grade": "Grade 4A", "teacher": "Prabhjot Kaur", "email": "prabhjot.kaur@ppischool.in", "class_email": "grade4a@ppischool.in", "parents_email": "grade4a.parents@ppischool.in", "whatsapp": "9289234663"},
    {"grade": "Grade 4B", "teacher": "Damanpreet Kaur", "email": "damanpreet.kaur@ppischool.in", "class_email": "grade4b@ppischool.in", "parents_email": "grade4b.parents@ppischool.in", "whatsapp": "9289236041"},
    {"grade": "Grade 5A", "teacher": "Poshika Narula", "email": "poshika.narula@ppischool.in", "class_email": "grade5a@ppischool.in", "parents_email": "grade5a.parents@ppischool.in", "whatsapp": "9289236045"},
    {"grade": "Grade 5B", "teacher": "Aastha Khattar", "email": "aastha.khattar@ppischool.in", "class_email": "grade5b@ppischool.in", "parents_email": "grade5b.parents@ppischool.in", "whatsapp": "9289234653"},
    {"grade": "Grade 6A", "teacher": "Kaninika Jain", "email": "kaninika.jain@ppischool.in", "class_email": "grade6a@ppischool.in", "parents_email": "grade6a.parents@ppischool.in", "whatsapp": "9289234665"},
    {"grade": "Grade 6B", "teacher": "Shikha Singh", "email": "shikha.singh@ppischool.in", "class_email": "grade6b@ppischool.in", "parents_email": "grade6b.parents@ppischool.in", "whatsapp": "9289236043"},
    {"grade": "Grade 7A", "teacher": "Shyam Manohar", "email": "shyam.manohar@ppischool.in", "class_email": "grade7a@ppischool.in", "parents_email": "grade7a.parents@ppischool.in", "whatsapp": "9289236049", "gender": "male"},
    {"grade": "Grade 7B", "teacher": "Twinkle Tandon", "email": "twinkle.tandon@ppischool.in", "class_email": "grade7b@ppischool.in", "parents_email": "grade7b.parents@ppischool.in", "whatsapp": "9289236044"},
    {"grade": "Grade 8A", "teacher": "Tarun Dhall", "email": "tarun.dhall@ppischool.in", "class_email": "grade8a@ppischool.in", "parents_email": "grade8a.parents@ppischool.in", "whatsapp": "9289236057", "gender": "male"},
    {"grade": "Grade 8B", "teacher": "Rashmi", "email": "rashmi.pp@ppischool.in", "class_email": "grade8b@ppischool.in", "parents_email": "grade8b.parents@ppischool.in", "whatsapp": "9289236048"},
    {"grade": "Grade 8C", "teacher": "Nikita Chawla", "email": "nikita.chawla@ppischool.in", "class_email": "grade8c@ppischool.in", "parents_email": "grade8c.parents@ppischool.in", "whatsapp": "9289236046"},
    {"grade": "Grade 9A", "teacher": "Mansi Gupta", "email": "mansi.gupta@ppischool.in", "class_email": "grade9a@ppischool.in", "parents_email": "grade9a.parents@ppischool.in", "whatsapp": "9289236058"},
    {"grade": "Grade 9B", "teacher": "Vaishali Arora", "email": "vaishali.arora@ppischool.in", "class_email": "grade9b@ppischool.in", "parents_email": "grade9b.parents@ppischool.in", "whatsapp": "9289236047"},
    {"grade": "Grade 9C", "teacher": "Harjeet Kaur", "email": "harjeet.kaur@ppischool.in", "class_email": "grade9c@ppischool.in", "parents_email": "grade9c.parents@ppischool.in", "whatsapp": "9289236052"},
    {"grade": "Grade 10A", "teacher": "Riya Arora", "email": "riya.arora@ppischool.in", "class_email": "grade10a@ppischool.in", "parents_email": "grade10a.parents@ppischool.in", "whatsapp": "9289236050"},
    {"grade": "Grade 10B", "teacher": "Avneet Kaur", "email": "avneet.kaur1@ppischool.in", "class_email": "grade10b@ppischool.in", "parents_email": "grade10b.parents@ppischool.in", "whatsapp": "9289236051"},
    {"grade": "Grade 11 (Science)", "teacher": "Aradhana Gambhir", "email": "", "class_email": "", "parents_email": "", "whatsapp": ""},
    {"grade": "Grade 11 (Commerce)", "teacher": "Christy Joseph", "email": "christy.joseph@ppischool.in", "class_email": "grade11b@ppischool.in", "parents_email": "grade11b.parents@ppischool.in", "whatsapp": "9289236054", "gender": "male"},
    {"grade": "Grade 11 (Humanities)", "teacher": "Tarleen / Deepak", "email": "", "class_email": "", "parents_email": "", "whatsapp": ""},
    {"grade": "Grade 12 (Science)", "teacher": "Pooja Arora", "email": "pooja.arora@ppischool.in", "class_email": "grade12a@ppischool.in", "parents_email": "grade12a.parents@ppischool.in", "whatsapp": "9289236053"},
    {"grade": "Grade 12 (Commerce)", "teacher": "Sucheta Sinha", "email": "sucheta.sinha@ppischool.in", "class_email": "grade12b@ppischool.in", "parents_email": "grade12b.parents@ppischool.in", "whatsapp": "9289236059"},
    {"grade": "Grade 12 (Humanities)", "teacher": "Sucheta Sinha", "email": "sucheta.sinha@ppischool.in", "class_email": "grade12c@ppischool.in", "parents_email": "grade12c.parents@ppischool.in", "whatsapp": "9289236059"},
]


def _grade_search_terms(entry: dict) -> list[str]:
    """Build search term variants for a teacher/grade entry."""
    grade_lower = entry["grade"].lower()
    terms = [grade_lower]
    parts = grade_lower.replace("grade ", "").replace("(", "").replace(")", "").strip()
    terms.append(parts)
    terms.append(f"class {parts}")
    terms.append(f"grade {parts}")
    # Add space-separated variants: "5a" -> "5 a", "10b" -> "10 b"
    import re as _re
    spaced = _re.sub(r"(\d+)\s*([a-z])", r"\1 \2", parts)
    if spaced != parts:
        terms.append(spaced)
        terms.append(f"class {spaced}")
        terms.append(f"grade {spaced}")
    # Also add no-space variant: "5 a" -> "5a"
    nospace = _re.sub(r"(\d+)\s+([a-z])", r"\1\2", parts)
    if nospace != parts:
        terms.append(nospace)
        terms.append(f"class {nospace}")
        terms.append(f"grade {nospace}")
    return terms


def lookup_teacher(query: str) -> str | None:
    """Look up teacher details based on grade/class mentioned in the query."""
    q = query.lower().strip()

    for entry in TEACHER_DATA:
        search_terms = _grade_search_terms(entry)

        # Handle "nursery", "prep", "popsicles" directly
        if any(term in q for term in search_terms):
            result = f"*{entry['grade']}*\n"
            honorific = "Mr." if entry.get("gender") == "male" else "Ms."
            result += f"Class Teacher: {honorific} {entry['teacher']}\n"
            if entry["email"]:
                result += f"Teacher Email: {entry['email']}\n"
            if entry["class_email"]:
                result += f"Class Email: {entry['class_email']}\n"
            if entry["parents_email"]:
                result += f"Parents Group Email: {entry['parents_email']}\n"
            if entry["whatsapp"]:
                result += f"WhatsApp (Airtel): {entry['whatsapp']}"
            return result

    return None


def find_teacher_by_grade(query: str) -> dict | None:
    """Find a single teacher entry by grade/class mentioned in the query.
    Returns the first matching TEACHER_DATA entry or None."""
    q = query.lower().strip()
    for entry in TEACHER_DATA:
        search_terms = _grade_search_terms(entry)
        if any(term in q for term in search_terms):
            return entry
    return None


def find_mentioned_teachers(query: str) -> list[dict]:
    """Find all teachers whose grade/class or name is mentioned in the query.

    Returns a list of matching TEACHER_DATA entries (may be empty).
    """
    q = query.lower().strip()
    matched: list[dict] = []
    seen_grades: set[str] = set()

    for entry in TEACHER_DATA:
        if entry["grade"] in seen_grades:
            continue

        # Match by grade/class terms
        search_terms = _grade_search_terms(entry)
        if any(term in q for term in search_terms):
            matched.append(entry)
            seen_grades.add(entry["grade"])
            continue

        # Match by teacher name (first name or full name)
        teacher_name = entry["teacher"].lower()
        # Split on " / " to handle dual-teacher entries like "Jasleen Kaur / Deepti"
        name_parts = [n.strip() for n in teacher_name.split("/")]
        for name in name_parts:
            # Check first name and full name
            first_name = name.split()[0] if name.split() else ""
            if first_name and len(first_name) > 2 and first_name in q:
                matched.append(entry)
                seen_grades.add(entry["grade"])
                break
            if name in q:
                matched.append(entry)
                seen_grades.add(entry["grade"])
                break

    return matched


# ---------------------------------------------------------------------------
# Transport route data — loaded from static JSON file
# ---------------------------------------------------------------------------
_TRANSPORT_DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "static", "transport_data.json"
)
try:
    with open(_TRANSPORT_DATA_FILE) as _f:
        TRANSPORT_DATA: dict = json.load(_f)
except Exception:
    TRANSPORT_DATA = {}


def lookup_transport(query: str) -> str | None:
    """Answer transport / route / bus queries using TRANSPORT_DATA.
    Returns a formatted answer string or None if not transport-related."""
    q = query.lower().strip()

    transport_keywords = [
        "transport", "bus", "route", "pickup", "pick up", "pick-up",
        "drop", "van", "driver", "conductor", "which route",
        "bus number", "bus route", "bus timing", "bus time",
    ]
    # Also match "r-2", "r 5", "r-10" etc. as route shorthand
    has_r_shorthand = bool(re.search(r'\br\s*[-]?\s*\d+', q))
    if not has_r_shorthand and not any(kw in q for kw in transport_keywords):
        return None

    # 1. Check if asking about a specific student
    for route_name, route in TRANSPORT_DATA.items():
        for s in route["students"]:
            student_lower = s["name"].lower()
            # Match full name or significant portion
            name_parts = student_lower.split()
            def _student_route_info(student: dict, rname: str, rdata: dict) -> str:
                lines = [
                    f"*{student['name']}* ({student['grade']}) is on *{rname}*",
                    f"Pickup: {student['pickup_time']}",
                    f"Drop: {student['drop_time']}",
                    f"Address: {student['address']}",
                ]
                if rdata.get('driver'):
                    lines.append(f"Driver: {rdata['driver']}")
                if rdata.get('conductor'):
                    lines.append(f"Conductor: {rdata['conductor']}")
                if rdata.get('driver_mobile'):
                    lines.append(f"Contact: {rdata['driver_mobile']}")
                return "\n".join(lines)

            if len(name_parts) >= 2:
                if name_parts[0] in q and name_parts[1] in q:
                    return _student_route_info(s, route_name, route)
            elif student_lower in q:
                return _student_route_info(s, route_name, route)

    # 2. Check if asking about a specific route number
    # Match "route 5", "route-5", "r-5", "r 5", "R-5" etc.
    route_num_match = re.search(r'(?:route|r)\s*[-#]?\s*(\d+)', q)
    if route_num_match:
        num = route_num_match.group(1)
        route_key = f"Route {num}"
        if route_key in TRANSPORT_DATA:
            route = TRANSPORT_DATA[route_key]
            lines = [f"*{route_key}*"]
            if route.get("driver"):
                lines.append(f"Driver: {route['driver']}")
            if route.get("driver_mobile"):
                lines.append(f"Driver Contact No.: {route['driver_mobile']}")
            if route.get("conductor"):
                lines.append(f"Conductor: {route['conductor']}")
            lines.append(f"Total Students: {len(route['students'])}")
            lines.append("")
            for s in route["students"]:
                lines.append(
                    f"\u2022 {s['name']} ({s['grade']}) \u2014 "
                    f"Pickup: {s['pickup_time']}, Drop: {s['drop_time']}"
                )
            return "\n".join(lines)

    # 3. General transport query — return route summary
    lines = ["*PPIS Transport Routes (Summer 2026-27)*\n"]
    lines.append(f"Total Routes: {len(TRANSPORT_DATA)} | Total Students: {sum(len(r['students']) for r in TRANSPORT_DATA.values())}\n")
    for rname in sorted(TRANSPORT_DATA.keys(), key=lambda x: int(m.group()) if (m := re.search(r'\d+', x)) else 999):
        r = TRANSPORT_DATA[rname]
        # Summarise areas covered
        areas = set()
        for s in r["students"]:
            addr = s.get("address", "").lower()
            for area in ["pitampura", "rohini", "shalimar bagh", "model town",
                         "punjabi bagh", "rajouri garden", "kirti nagar",
                         "patel nagar", "karol bagh", "adarsh nagar",
                         "mukherjee nagar", "kamla nagar", "shastri nagar",
                         "paschim vihar", "subhash nagar", "moti nagar",
                         "burari", "tri nagar", "anand parvat",
                         "saraswati vihar", "maurya enclave", "shakurpur"]:
                if area in addr:
                    areas.add(area.title())
        driver_info = ""
        if r.get('driver'):
            driver_info = f" — Driver: {r['driver']}"
            if r.get('driver_mobile'):
                driver_info += f" ({r['driver_mobile']})"
        area_str = ", ".join(sorted(areas)[:4]) if areas else "Various areas"
        lines.append(f"*{rname}*{driver_info}: {len(r['students'])} students — {area_str}")

    lines.append("\nAsk about a specific route number or student name for detailed info!")
    return "\n".join(lines)


def lookup_student_transport(student_name: str) -> str | None:
    """Look up transport details for a specific student by name."""
    q = student_name.lower().strip()
    for route_name, route in TRANSPORT_DATA.items():
        for s in route["students"]:
            if q in s["name"].lower():
                lines = [
                    f"*{s['name']}* ({s['grade']}) — *{route_name}*",
                    f"Pickup: {s['pickup_time']} | Drop: {s['drop_time']}",
                    f"Address: {s['address']}",
                ]
                if route.get('driver'):
                    lines.append(f"Driver: {route['driver']}")
                if route.get('conductor'):
                    lines.append(f"Conductor: {route['conductor']}")
                if route.get('driver_mobile'):
                    lines.append(f"Contact: {route['driver_mobile']}")
                return "\n".join(lines)
    return None


def lookup_person_by_name_or_phone(query: str) -> dict | None:
    """Look up a person from TEACHER_DATA by name or phone number.
    Returns the matching entry dict or None."""
    q = query.lower().strip()

    # Try matching by phone number (strip non-digits)
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 10:
        bare = digits[-10:]  # last 10 digits
        for entry in TEACHER_DATA:
            if entry.get("whatsapp", "") == bare:
                return entry

    # Try matching by name
    for entry in TEACHER_DATA:
        teacher_name = entry["teacher"].lower()
        name_parts = [n.strip() for n in teacher_name.split("/")]
        for name in name_parts:
            first_name = name.split()[0] if name.split() else ""
            if first_name and len(first_name) > 2 and first_name in q:
                return entry
            if name in q:
                return entry

    # Try matching by grade/class
    for entry in TEACHER_DATA:
        search_terms = _grade_search_terms(entry)
        if any(term in q for term in search_terms):
            return entry

    return None


def _get_openai_key() -> str:
    """Read OpenAI API key from env var first, then fall back to settings DB."""
    key = os.getenv("OPENAI_API_KEY", "")
    if key and key != "placeholder":
        return key
    # Fallback: read from settings table in the database
    import sqlite3
    db_path = os.getenv("DB_PATH", "/data/app.db")
    if not os.path.exists(db_path):
        alt = os.path.join(os.path.dirname(__file__), "..", "..", "app.db")
        if os.path.exists(alt):
            db_path = alt
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT value FROM settings WHERE key = 'OPENAI_API_KEY'"
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return ""


def get_client() -> AsyncOpenAI | None:
    global client
    api_key = _get_openai_key()
    if not api_key:
        return None
    if client is None:
        client = AsyncOpenAI(api_key=api_key)
    return client


def _is_hindi(text: str) -> bool:
    """Detect if the message contains Hindi (Devanagari) characters."""
    for ch in text:
        if '\u0900' <= ch <= '\u097F':
            return True
    return False


def generate_fallback_response(user_message: str) -> str:
    """Generate a polite school administrator reply when OpenAI is not available."""
    msg = user_message.lower().strip()
    hindi = _is_hindi(user_message)

    contact_info = (
        "\n\nFor assistance, please contact:\n"
        "- School Helpline / Front Desk: 8800935552\n"
        "- Ms. Harpreet Kaur (Administration Incharge): 9599488106\n\n"
        "Thank you for your cooperation.\n"
        "Warm regards,\n"
        "PP International School"
    )
    contact_info_hi = (
        "\n\nसहायता के लिए संपर्क करें:\n"
        "- School Helpline / Front Desk: 8800935552\n"
        "- Ms. Harpreet Kaur (Administration Incharge): 9599488106\n\n"
        "आपके सहयोग के लिए धन्यवाद।\n"
        "सादर,\n"
        "PP International School"
    )
    ci = contact_info_hi if hindi else contact_info

    # Check for teacher/class teacher lookup first
    teacher_keywords = ["teacher", "ct ", "class teacher", "who is", "ct of",
                        "grade ", "class ", "nursery", "prep ", "popsicle",
                        "\u0936\u093f\u0915\u094d\u0937\u0915", "\u091f\u0940\u091a\u0930", "\u0915\u0915\u094d\u0937\u093e"]
    if any(kw in msg for kw in teacher_keywords):
        teacher_info = lookup_teacher(msg)
        if teacher_info:
            return f"Here are the class teacher details:\n\n{teacher_info}\n\nThank you for your cooperation.\nWarm regards,\nPP International School"

    # Hindi greetings
    hindi_greetings = ["\u0928\u092e\u0938\u094d\u0924\u0947", "\u0928\u092e\u0938\u094d\u0915\u093e\u0930", "\u092a\u094d\u0930\u0923\u093e\u092e"]
    if any(word in msg for word in ["hi", "hello", "hey", "hii", "helloo"] + hindi_greetings):
        if hindi:
            return (
                "\u0928\u092e\u0938\u094d\u0924\u0947! PP International School (PPIS) \u092e\u0947\u0902 \u0906\u092a\u0915\u093e \u0938\u094d\u0935\u093e\u0917\u0924 \u0939\u0948\u0964 "
                "\u0939\u092e \u092a\u093f\u0924\u092e\u092a\u0941\u0930\u093e, \u0928\u0908 \u0926\u093f\u0932\u094d\u0932\u0940 \u092e\u0947\u0902 \u0938\u094d\u0925\u093f\u0924 CBSE \u0938\u0902\u092c\u0926\u094d\u0927 \u0938\u0940\u0928\u093f\u092f\u0930 \u0938\u0947\u0915\u0947\u0902\u0921\u0930\u0940 \u0938\u094d\u0915\u0942\u0932 \u0939\u0948\u0902\u0964 "
                "\u092e\u0948\u0902 \u0906\u092a\u0915\u0940 \u0938\u094d\u0915\u0942\u0932 \u0938\u0947 \u091c\u0941\u0921\u093c\u0940 \u0915\u093f\u0938\u0940 \u092d\u0940 \u091c\u093e\u0928\u0915\u093e\u0930\u0940 \u092e\u0947\u0902 \u092e\u0926\u0926 \u0915\u0930 \u0938\u0915\u0924\u093e \u0939\u0942\u0901\u0964 "
                "\u0906\u091c \u092e\u0948\u0902 \u0906\u092a\u0915\u0940 \u0915\u094d\u092f\u093e \u0938\u0939\u093e\u092f\u0924\u093e \u0915\u0930 \u0938\u0915\u0924\u093e \u0939\u0942\u0901?"
            )
        return (
            "Welcome to PP International School (PPIS).\n\n"
            "We are a CBSE affiliated Senior Secondary School located in Pitampura, New Delhi. "
            "I am here to assist you with any school-related queries.\n\n"
            "How may I help you today?\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\n"
            "PP International School"
        )
    elif any(word in msg for word in ["thank", "thanks", "thx"]):
        return (
            "You are most welcome. We are always happy to assist you at PP International School.\n\n"
            "Please feel free to reach out anytime you need assistance.\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\n"
            "PP International School"
        )
    elif any(word in msg for word in ["bye", "goodbye", "see you"]):
        return (
            "Thank you for reaching out. Wishing you a wonderful day.\n\n"
            "Please do not hesitate to contact us if you need any further assistance.\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\n"
            "PP International School"
        )
    elif any(word in msg for word in ["admission", "enroll", "registration", "seat"]):
        return (
            "Thank you for your interest in admissions at PP International School.\n\n"
            "Nursery Admissions 2026-27:\n"
            "- Age: Above 3 years and less than 4 years (as on 31st March 2026)\n"
            "- Total open seats: 40 (General: 30, EWS/DG: 10)\n"
            "- Registration fee: Rs. 25 (non-refundable)\n"
            "- Forms available at school office or online at www.ppi.school\n\n"
            "We offer classes from Pre-nursery to Grade 12 (CBSE affiliated).\n"
            "For detailed information, please contact our admissions office."
            + contact_info
        )
    elif any(word in msg for word in ["fee", "payment", "charges"]):
        return (
            "Thank you for your query regarding fees at PP International School. "
            "For detailed fee structure and payment information, "
            "please contact our school administration office.\n\n"
            "Online payment is also available through our website: www.ppi.school"
            + contact_info
        )
    elif any(word in msg for word in ["transport", "bus", "van", "route", "pickup", "pick up", "drop", "driver", "conductor"]):
        transport_answer = lookup_transport(msg)
        if transport_answer:
            return transport_answer + contact_info
        return (
            "PP International School provides safe transport facilities:\n\n"
            "- Fully air-conditioned buses with GPRS tracking and CCTV\n"
            "- Well-trained drivers with caretakers in every bus\n\n"
            "We have 23 bus routes covering Delhi NCR with 390+ students.\n"
            "Please ask about a specific route number (e.g. 'Route 5') "
            "or a student's name for detailed transport information."
            + contact_info
        )
    elif any(word in msg for word in ["sport", "game", "play", "activity", "activities"]):
        return (
            "PP International School offers a wide range of sports and activities.\n\n"
            "Sports: Skating, Basketball, Soccer, Lawn Tennis, Table Tennis, "
            "Taekwondo, Badminton, Golf\n\n"
            "Activities: Cooking classes, creative writing, story-telling, "
            "drama, debate, hobby classes, educational tours\n\n"
            "Competitions: Spelling bee, quiz, olympiad, declamation, "
            "drawing, calligraphy, music, dance, and more.\n\n"
            "For queries about a specific activity (skating, dance, theatre, music, art), "
            "please mention the activity by name and I will share the relevant contact."
            + contact_info
        )
    elif any(word in msg for word in ["address", "location", "where", "direction", "map"]):
        return (
            "PP International School is located at:\n\n"
            "LD Block, Pitampura,\n"
            "Near Kohat Enclave Metro Station, Pillar No. 333,\n"
            "New Delhi - 110034\n\n"
            "Nearest Metro: Kohat Enclave (Yellow Line)"
            + contact_info
        )
    elif any(word in msg for word in ["lab", "laboratory", "science", "computer", "robotics"]):
        return (
            "PP International School has 7 well-equipped laboratories:\n\n"
            "1. General/Composite Science Lab\n"
            "2. Computer Lab\n"
            "3. Chemistry Lab\n"
            "4. Biology Lab\n"
            "5. Physics Lab\n"
            "6. Math Lab\n"
            "7. Robotics Lab (Lego Mindstorms NXT)\n\n"
            "Our labs provide hands-on experience for students to "
            "interrogate, examine, explore, hypothesize and infer."
            + contact_info
        )
    elif any(word in msg for word in ["medical", "health", "nurse", "doctor", "hospital"]):
        return (
            "PP International School provides world-class healthcare:\n\n"
            "- Medical room in partnership with Max Healthcare, Shalimar Bagh\n"
            "- Full-time nurse from Max Healthcare on campus\n"
            "- Visiting doctor for emergencies and routine check-ups\n"
            "- Regular medical check-ups for all students\n"
            "- Healthy Neighbourhood Scheme: parent discount cards for Max Healthcare\n"
            "  (also covers Senior Citizens)"
            + contact_info
        )
    elif any(word in msg for word in ["principal", "staff", "faculty"]):
        return (
            "PP International School has a team of 44+ qualified teachers.\n"
            "Our School Principal is Ms. Deepi Bector.\n\n"
            "We believe in practical-based learning and empower students "
            "to become representatives of a meaningful and value-based society.\n\n"
            "To find a specific class teacher, just ask me — e.g. "
            "'Who is the class teacher of Grade 5A?'"
            + contact_info
        )
    elif any(word in msg for word in ["saturday", "weekend", "off day", "holiday", "working day"]):
        return (
            "Kindly note that Saturdays are non-working except one Saturday designated for clubs. "
            "Parents may reach out to teachers on Saturdays for any query."
            + contact_info
        )
    elif any(word in msg for word in ["timing", "time", "schedule", "hour", "when"]):
        return (
            "PP International School Timings:\n\n"
            "Summer Schedule:\n"
            "- Pre-primary: 7:30 AM to 11:30 AM\n"
            "- Grade 1 onwards: 7:30 AM to 1:30 PM\n\n"
            "Winter Schedule:\n"
            "- Pre-primary: 8:00 AM to 12:00 PM\n"
            "- Grade 1 onwards: 8:00 AM to 2:00 PM\n\n"
            "Kindly note that Saturdays are non-working except one Saturday designated for clubs. "
            "Parents may reach out to teachers on Saturdays for any query."
            + contact_info
        )
    elif any(word in msg for word in ["stream", "subject", "cbse", "curriculum"]):
        return (
            "PP International School offers:\n\n"
            "- Classes: Pre-nursery (Popsicles) to 12th (CBSE affiliated)\n"
            "- CBSE Affiliation No: 2730720\n"
            "- Medium: English\n"
            "- Languages: English, Hindi, German, French\n"
            "- Streams (Senior Secondary): Science (PCM/PCB), Commerce, Humanities\n"
            "- 36 classrooms with smart/digital boards\n"
            "- Centrally air-conditioned campus\n"
            "- School area: 7200 sq. metres (2 Acres)"
            + contact_info
        )
    elif "?" in msg:
        if hindi:
            return (
                "PP International School \u0938\u0947 \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0928\u0947 \u0915\u0947 \u0932\u093f\u090f \u0927\u0928\u094d\u092f\u0935\u093e\u0926\u0964 "
                "\u0906\u092a\u0915\u0947 \u092a\u094d\u0930\u0936\u094d\u0928 \u0915\u0947 \u0932\u093f\u090f \u0915\u0943\u092a\u092f\u093e \u0938\u094d\u0915\u0942\u0932 \u0911\u092b\u093f\u0938 \u0938\u0947 \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0947\u0902\u0964"
                + ci
            )
        return (
            "Thank you for reaching out to PP International School. "
            "For detailed and accurate information regarding your query, "
            "please feel free to contact our school office."
            + ci
        )
    else:
        if hindi:
            return (
                "PP International School \u0938\u0947 \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0928\u0947 \u0915\u0947 \u0932\u093f\u090f \u0927\u0928\u094d\u092f\u0935\u093e\u0926\u0964 "
                "\u0905\u0927\u093f\u0915 \u091c\u093e\u0928\u0915\u093e\u0930\u0940 \u0915\u0947 \u0932\u093f\u090f \u0915\u0943\u092a\u092f\u093e \u0938\u094d\u0915\u0942\u0932 \u0911\u092b\u093f\u0938 \u0938\u0947 \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0947\u0902\u0964"
                + ci
            )
        return (
            "Thank you for reaching out to PP International School. "
            "We appreciate your message. For detailed assistance, "
            "please contact our school office."
            + ci
        )


async def transcribe_audio(
    audio_url: str | None = None,
    audio_bytes: bytes | None = None,
    content_type: str = "",
) -> str | None:
    """Transcribe audio using OpenAI Whisper.

    Accepts either a publicly-accessible *audio_url* (downloaded with a
    plain GET) **or** pre-downloaded *audio_bytes*.  The latter is
    required for Cloud API media whose CDN URLs need Bearer
    authentication — callers should use ``download_cloud_media()``
    first and pass the bytes here.

    Returns the transcribed text, or None on failure.
    """
    ai_client = get_client()
    if ai_client is None:
        logger.warning("OpenAI client unavailable — cannot transcribe audio")
        return None

    try:
        if audio_bytes is None:
            if not audio_url:
                logger.error("transcribe_audio called with no URL and no bytes")
                return None
            # Download the audio file (works for publicly-accessible URLs)
            async with httpx.AsyncClient() as http_client:
                resp = await http_client.get(audio_url, timeout=60.0, follow_redirects=True)
                if resp.status_code != 200:
                    logger.error(f"Failed to download audio: HTTP {resp.status_code}")
                    return None
                audio_bytes = resp.content
                content_type = content_type or resp.headers.get("content-type", "")

        if not audio_bytes or len(audio_bytes) < 100:
            logger.error("Downloaded audio is empty or too small")
            return None

        # Write to a temp file — Whisper needs a file-like object with a name
        suffix = ".ogg"
        if "mp4" in content_type or "m4a" in content_type:
            suffix = ".m4a"
        elif "mpeg" in content_type or "mp3" in content_type:
            suffix = ".mp3"
        elif "wav" in content_type:
            suffix = ".wav"
        elif "webm" in content_type:
            suffix = ".webm"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            with open(tmp_path, "rb") as audio_file:
                transcription = await ai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    # No language hint — let Whisper auto-detect (supports Hindi, English, Hinglish)
                )
            text = transcription.text.strip() if transcription.text else ""
            if text:
                logger.info(f"Audio transcribed ({len(audio_bytes)} bytes): {text[:100]}...")
                return text
            logger.warning("Whisper returned empty transcription")
            return None
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Audio transcription error: {e}")
        return None


async def generate_response(
    user_message: str,
    system_prompt: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> str:
    """Generate a response using OpenAI GPT, falling back to fixed replies."""
    global USE_FALLBACK, FALLBACK_SINCE, _QUOTA_ALERT_SENT

    if USE_FALLBACK:
        # Retry OpenAI periodically in case credits were added
        if time.time() - FALLBACK_SINCE > FALLBACK_RETRY_INTERVAL:
            logger.info("Retrying OpenAI after fallback cooldown...")
            USE_FALLBACK = False
            _QUOTA_ALERT_SENT = False  # Reset alert flag so next quota event triggers a new alert
        else:
            return generate_fallback_response(user_message)

    ai_client = get_client()
    if ai_client is None:
        return generate_fallback_response(user_message)

    try:
        # Detect Hindi to add bilingual instruction
        hindi = _is_hindi(user_message)
        # Also detect Romanized Hindi (Hinglish) — common words
        hinglish_words = [
            "kya", "hai", "kaise", "kab", "kahan", "kaun", "kyun", "kyu",
            "mera", "meri", "mere", "aap", "aapka", "aapki", "hum", "humara",
            "namaste", "namaskar", "dhanyavad", "shukriya", "accha", "achha",
            "theek", "thik", "nahi", "nahin", "haan", "ji", "bhai",
            "bachche", "bachcha", "bacha", "bache", "bacche",
            "madam", "sahab", "sahib", "sir ji", "madam ji",
            "padhai", "parhai", "padhna", "likhna", "school ka",
            "chutti", "chhutti", "holiday kab", "fees kitni",
            "kripya", "bataye", "bataiye", "batao", "dijiye", "karein",
            "samay", "waqt", "subah", "dopahar", "shaam",
        ]
        msg_lower = user_message.lower()
        is_hinglish = not hindi and any(w in msg_lower.split() for w in hinglish_words)

        # Build language-aware system prompt
        lang_instruction = ""
        if hindi:
            lang_instruction = (
                "\n\nIMPORTANT: The user is writing in Hindi. "
                "You MUST reply in Hindi (Devanagari script). "
                "Be warm, respectful, and use polite Hindi. "
                "You may include English words for school-specific terms like grades, subjects, etc."
            )
        elif is_hinglish:
            lang_instruction = (
                "\n\nIMPORTANT: The user is writing in Hinglish (Hindi in Roman script). "
                "Reply in the same style — use Hinglish (Hindi written in English letters) "
                "mixed with English as appropriate. Be warm and respectful."
            )

        # Inject current date/time (IST) so GPT knows the actual day and can greet accordingly
        ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        day_name = now_ist.strftime("%A")  # e.g. "Sunday"
        date_str = now_ist.strftime("%d %B %Y")  # e.g. "27 April 2026"
        time_str = now_ist.strftime("%I:%M %p")  # e.g. "02:30 PM"
        hour = now_ist.hour
        if hour < 12:
            greeting = "Good Morning"
        elif hour < 16:
            greeting = "Good Afternoon"
        elif hour < 20:
            greeting = "Good Evening"
        else:
            greeting = "Good Night"

        is_sunday = day_name == "Sunday"
        school_status = "TODAY IS SUNDAY — THE SCHOOL IS CLOSED. It is a holiday." if is_sunday else f"Today is {day_name} — the school is OPEN (working day)."

        datetime_context = (
            f"\n\n== CURRENT DATE & TIME (IST) ==\n"
            f"Date: {date_str} ({day_name})\n"
            f"Time: {time_str} IST\n"
            f"Appropriate greeting: {greeting}\n"
            f"{school_status}\n"
            f"IMPORTANT: If anyone asks whether school is open today, check the day above. "
            f"Sunday is ALWAYS a holiday. Monday to Saturday are working days.\n"
        )

        # Activity contacts are already in the comprehensive system prompt — no need to inject separately
        full_system_prompt = system_prompt + lang_instruction + datetime_context

        messages: list[dict[str, str]] = [
            {"role": "system", "content": full_system_prompt}
        ]

        if conversation_history:
            messages.extend(conversation_history[-10:])

        messages.append({"role": "user", "content": user_message})

        response = await ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=500,
            temperature=0.7,
        )

        reply = response.choices[0].message.content
        if reply is None:
            return generate_fallback_response(user_message)
        return reply.strip()

    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        if "insufficient_quota" in str(e) or "429" in str(e):
            logger.warning("OpenAI quota exceeded. Switching to fallback mode.")
            USE_FALLBACK = True
            FALLBACK_SINCE = time.time()
            # Send low-balance WhatsApp alert to admin (once per quota event)
            asyncio.ensure_future(_send_quota_alert())
        return generate_fallback_response(user_message)


async def generate_vision_response(
    image_bytes: bytes,
    mime_type: str,
    caption: str,
    system_prompt: str,
    conversation_history: list[dict[str, str]] | None = None,
    sender_name: str = "",
) -> str:
    """Generate a response for an image using GPT-4o-mini vision."""
    import base64

    ai_client = get_client()
    if ai_client is None:
        name_part = f" {sender_name}" if sender_name else ""
        return (
            f"Thank you{name_part} for sharing the image! "
            "Our AI assistant is currently unavailable. "
            "Please try again later or type your question."
        )

    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{b64}"

        user_text = caption.strip() if caption.strip() else ""
        name_hint = f" Their name is {sender_name}." if sender_name else ""

        vision_instruction = (
            "The user has shared an image with you."
            f"{name_hint} "
            f"First, thank them by name if known (e.g. 'Thank you {sender_name or 'for sharing'}!'). "
            "Then briefly describe what you see in the image. "
            "If the image has a caption, address the caption too. "
            "If you're not sure what the image is about or why they sent it, politely ask "
            "'Could you tell me what this image is about?' "
            "Keep your response concise, warm and friendly."
        )
        prompt_text = f"{vision_instruction}\n\nUser caption: {user_text}" if user_text else vision_instruction

        user_content: list[dict] = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": data_uri, "detail": "low"}},
        ]

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history[-6:])
        messages.append({"role": "user", "content": user_content})

        response = await ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=500,
            temperature=0.7,
        )

        reply = response.choices[0].message.content
        if reply is None:
            return "I received your image but could not generate a response."
        return reply.strip()

    except Exception as e:
        logger.error(f"OpenAI Vision API error: {e}")
        return "I received your image but encountered an error processing it. Please try again."


async def generate_homework_review(
    image_bytes: bytes,
    mime_type: str,
    caption: str,
    student_name: str = "",
    grade: str = "",
) -> str:
    """Analyze a homework/notebook photo and return corrections & feedback.

    Uses GPT-4o vision to read handwritten work and provide:
    - Subject identification
    - Error detection (math, spelling, grammar, content)
    - Specific corrections
    - Encouraging overall feedback
    """
    import base64

    ai_client = get_client()
    if ai_client is None:
        return (
            "Thank you for sharing the homework! "
            "Our AI assistant is currently unavailable. "
            "Please try again later."
        )

    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{b64}"

        student_context = ""
        if student_name and grade:
            student_context = f"Student: {student_name} ({grade}). "
        elif student_name:
            student_context = f"Student: {student_name}. "
        elif grade:
            student_context = f"Grade: {grade}. "

        review_prompt = (
            "You are an experienced school teacher reviewing a student's homework. "
            f"{student_context}"
            "A parent sent this photo of their child's work.\n\n"
            "CRITICAL INSTRUCTIONS — read carefully:\n\n"
            "1. IDENTIFY the subject and topic.\n"
            "2. READ each problem carefully. For math:\n"
            "   - If numbers are in columns (Th H T O = Thousands Hundreds Tens Ones), "
            "read each row as a COMPLETE number. Example: if Th=3 H=2 T=4 O=6, that is 3246.\n"
            "   - Identify the operation (addition, subtraction, etc.)\n"
            "   - Read the student's final answer as a COMPLETE number.\n"
            "3. VERIFY each answer yourself by computing the correct result. "
            "ACTUALLY DO THE MATH — do not guess or assume.\n"
            "4. COMPARE the student's answer to your computed answer:\n"
            "   - If they MATCH → mark ✅ and state the problem briefly\n"
            "   - If they DON'T MATCH → mark ❌, show what the student wrote, "
            "and show the correct answer\n"
            "5. Do NOT mark correct answers as wrong. Do NOT show ❌ then ✅ for the same problem.\n\n"
            "FORMAT (use WhatsApp-compatible formatting):\n"
            "📚 *Homework Review*\n"
            f"{'*Student:* ' + student_name + chr(10) if student_name else ''}"
            "*Subject:* [subject]\n"
            "*Topic:* [topic]\n\n"
            "*Results:*\n"
            "For each problem, write ONE line:\n"
            "• ✅ (a) 3246 + 3123 = 6369 — Correct!\n"
            "• ❌ (b) 5682 + 3125 = 8807 — Student wrote 8907. Correct answer: 8807.\n\n"
            "Then at the end:\n"
            "*Score:* X out of Y correct\n\n"
            "*Overall:* [Brief encouraging feedback. Praise what they did well. "
            "If there are mistakes, gently explain the pattern of errors.]\n\n"
            "IMPORTANT RULES:\n"
            "- Only use ❌ for ACTUAL errors where the student's answer is wrong\n"
            "- Only use ✅ for correct answers\n"
            "- Never show both ❌ and ✅ for the same problem\n"
            "- If the image is not homework, say so politely\n"
            "- If handwriting is too unclear, ask for a clearer photo\n"
            "- Keep language simple (for parents)\n"
            "- Be encouraging and positive"
        )

        caption_text = caption.strip() if caption else ""
        if caption_text:
            review_prompt += f"\n\nParent's message: {caption_text}"

        user_content: list[dict] = [
            {"type": "text", "text": review_prompt},
            {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
        ]

        messages: list[dict] = [
            {"role": "system", "content": "You are an expert school teacher who carefully checks homework. You ALWAYS verify math by computing the answer yourself before marking correct or incorrect. You NEVER mark a correct answer as wrong."},
            {"role": "user", "content": user_content},
        ]

        response = await ai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=1200,
            temperature=0.1,
        )

        reply = response.choices[0].message.content
        if reply is None:
            return "I received the homework photo but could not generate a review. Please try again."
        return reply.strip()

    except Exception as e:
        logger.error(f"Homework review vision error: {e}")
        return "I received the homework photo but encountered an error while reviewing. Please try again."


async def _send_quota_alert() -> None:
    """Send a WhatsApp alert to the admin when OpenAI quota is exceeded."""
    global _QUOTA_ALERT_SENT
    if _QUOTA_ALERT_SENT:
        return
    try:
        from app.services.whatsapp_service import send_whatsapp_message
        alert_msg = (
            "*PPIS Bot — OpenAI Credit Alert*\n\n"
            "OpenAI credits have run out or are very low. "
            "The bot is currently using basic fixed replies instead of AI responses.\n\n"
            "Please top up your OpenAI credits:\n"
            "https://platform.openai.com/settings/organization/billing/overview\n\n"
            "Once you add credits, the bot will automatically switch back to AI replies within 5 minutes."
        )
        await send_whatsapp_message(ADMIN_PHONE_FOR_ALERTS, alert_msg)
        _QUOTA_ALERT_SENT = True
        logger.info(f"Quota alert sent to {ADMIN_PHONE_FOR_ALERTS}")
    except Exception as exc:
        logger.warning(f"Failed to send quota alert: {exc}")
