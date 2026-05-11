import re
import logging
from fastapi import APIRouter, Request, Response

from app.database import get_db
from app.services.whatsapp_service import (
    parse_incoming_message,
    parse_cloud_api_message,
    get_cloud_media_url,
    send_whatsapp_message,
    send_whatsapp_image,
    send_whatsapp_image_file,
    forward_file_by_url,
)
from app.services.sms_service import parse_incoming_sms, send_sms
from app.services.email_service import send_email_async
from app.services.bulk_service import pause_for_bot_reply, resume_after_bot_reply
from app.services.openai_service import (
    generate_response,
    find_mentioned_teachers,
    lookup_person_by_name_or_phone,
    lookup_transport,
    TEACHER_DATA,
    find_teacher_by_grade,
    transcribe_audio,
)

# School images stored as local files for direct upload
import os
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
SCHOOL_IMAGES = [
    {
        "file": os.path.join(STATIC_DIR, "ppis_logo.jpg"),
        "caption": (
            "PP International School\n"
            "CBSE Affiliated Senior Secondary School\n"
            "LD Block, Pitampura, Near Kohat Enclave Metro Station\n"
            "New Delhi - 110034"
        ),
    },
]

# Meal menu image (April 2026) — stored as local file for direct upload
MEAL_MENU_IMAGE_FILE = os.path.join(STATIC_DIR, "meal_menu_april_2026.jpg")
MEAL_MENU_KEYWORDS = ["meal", "menu", "lunch", "food", "tiffin", "snack", "breakfast", "meal menu", "short meal", "what's for lunch", "today's meal", "today's menu", "what is the meal", "what is the menu"]

# ---------------------------------------------------------------------------
# School photo gallery: publicly accessible images from ppi.school, organized
# by category.  Each category maps to a list of keyword triggers and image URLs.
# When a user asks for photos of a category the bot sends up to 3 images.
# ---------------------------------------------------------------------------
SCHOOL_PHOTO_GALLERY: dict[str, dict] = {
    "sports": {
        "keywords": ["sport", "sports", "athletics", "games", "football", "cricket", "hockey", "basketball", "sports day", "sports fiesta"],
        "caption": "🏅 PPIS Sports",
        "images": [
            "https://www.ppi.school/wp-content/uploads/2025/03/28-768x432.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/03/27-768x432.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/03/26-768x432.jpg",
        ],
    },
    "creative_activities": {
        "keywords": ["creative", "art", "craft", "drawing", "painting", "dance", "music", "activity", "activities"],
        "caption": "🎨 PPIS Creative Activities",
        "images": [
            "https://www.ppi.school/wp-content/uploads/2025/03/IMG_04561-600x600.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/03/22-540x600.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/03/8-540x600.jpg",
        ],
    },
    "science_lab": {
        "keywords": ["lab", "laboratory", "science", "physics", "chemistry", "biology", "experiment"],
        "caption": "🔬 PPIS Science Labs",
        "images": [
            "https://www.ppi.school/wp-content/uploads/2025/07/IMG_5273-600x600.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/07/IMG_5240-600x600.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/07/IMG_5302-scaled.jpg",
        ],
    },
    "math_lab": {
        "keywords": ["math lab", "maths lab", "mathematics lab"],
        "caption": "🔢 PPIS Math Lab",
        "images": [
            "https://www.ppi.school/wp-content/uploads/elementor/thumbs/math-lab1-pqor4cyshtlylg86c89jhohkrh0pcyyuhkxhljxlbg.jpg",
            "https://www.ppi.school/wp-content/uploads/elementor/thumbs/math-lab2-e1643108790164-pqor4q4j5i3z3zp27dybgl612v7ucqf37e2abfe2wc.jpg",
            "https://www.ppi.school/wp-content/uploads/elementor/thumbs/math-lab4-pqor5j9j1d7v3wiqh8jr3vtbht87zcmrneac706vjg.jpg",
        ],
    },
    "library": {
        "keywords": ["library", "books", "reading"],
        "caption": "📚 PPIS Library",
        "images": [
            "https://www.ppi.school/wp-content/uploads/2025/06/fgr-600x600.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/06/frec-600x600.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/03/WhatsApp-Image-2024-05-10-at-07.22.04-16-600x600.jpeg",
        ],
    },
    "happy_meal": {
        "keywords": ["happy meal", "cafeteria", "canteen", "dining"],
        "caption": "🍱 PPIS Happy Meal",
        "images": [
            "https://www.ppi.school/wp-content/uploads/2025/03/IMG-20250110-WA00191.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/03/IMG_04561-800x450.jpg",
            "https://www.ppi.school/wp-content/uploads/2024/06/WhatsApp-Image-2024-05-23-at-9.53.13-AM-scaled.jpeg",
        ],
    },
    "pre_primary": {
        "keywords": ["pre primary", "pre-primary", "nursery", "kindergarten", "kg", "prep", "tiny tots"],
        "caption": "👶 PPIS Pre-Primary",
        "images": [
            "https://www.ppi.school/wp-content/uploads/elementor/thumbs/Picture24-pqqci205vje3b5ot4mymlwq9ylgwaqmw5tp0jv3t4c.jpg",
            "https://www.ppi.school/wp-content/uploads/elementor/thumbs/IMG_8693-q570xxxoslkcax7bqlniyiakyipupbetv5ybx2l4q4.jpg",
            "https://www.ppi.school/wp-content/uploads/elementor/thumbs/IMG_8798-q56z70mn9vfb2olt6egn1dxpbwv9erlpvpq5c7vx9o.jpg",
        ],
    },
    "achievement": {
        "keywords": ["achievement", "award", "trophy", "winner", "topper", "result"],
        "caption": "🏆 PPIS Achievements",
        "images": [
            "https://www.ppi.school/wp-content/uploads/2025/06/fhr-768x768.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/06/febb-768x768.jpg",
            "https://www.ppi.school/wp-content/uploads/2025/06/rrhf-768x768.jpg",
        ],
    },
    "school": {
        "keywords": ["school photo", "school pic", "school building", "campus", "infrastructure"],
        "caption": "🏫 PP International School",
        "images": [
            "https://www.ppi.school/wp-content/uploads/2022/01/ppis-school-photos-1.jpg",
        ],
    },
    "holi": {
        "keywords": ["holi", "phoolon ki holi", "holi celebration", "holi function", "holi fest", "colour", "color"],
        "caption": "🎨 PPIS Holi Celebration 2026",
        "local_files": [
            os.path.join(STATIC_DIR, "holi_photos", "holi_function_1.jpg"),
            os.path.join(STATIC_DIR, "holi_photos", "holi_function_2.jpg"),
            os.path.join(STATIC_DIR, "holi_photos", "holi_function_3.jpg"),
        ],
    },
}


def _match_photo_category(text: str) -> dict | None:
    """Check if the message asks for photos of a specific category. Return the category dict or None."""
    lower = text.lower()
    # Only trigger if user is asking for photos/images/pics
    photo_ask = any(kw in lower for kw in ["photo", "pic", "image", "picture", "show me", "send me", "share"])
    if not photo_ask:
        return None
    for cat_data in SCHOOL_PHOTO_GALLERY.values():
        for kw in cat_data["keywords"]:
            if kw in lower:
                return cat_data
    return None


logger = logging.getLogger(__name__)


def _extract_person_name(caption: str) -> str:
    """Extract a clean person name from a photo caption.

    Handles captions like:
    - "Sachi Thaper"
    - "Ms. Sachi Thaper"
    - "Good Morning This the photo of Ms Sachi Thaper (PGT Economics) as required. Regards"
    - "Harpreet Kaur"
    """
    name = caption.strip()
    # Remove parenthetical info first
    name = re.sub(r"\s*\(.*?\)", "", name)
    # Remove common greeting/filler phrases
    _FILLER_PHRASES = [
        r"good\s+morning\b", r"good\s+afternoon\b", r"good\s+evening\b",
        r"\bthis\s+(?:is\s+)?the\s+photo\s+of\b",
        r"\bthis\s+(?:is\s+)?(?:my|a)\s+photo\b",
        r"\bphoto\s+(?:of|for)\b",
        r"\bas\s+required\b", r"\bregards\b", r"\bkindly\b",
        r"\bplease\s+(?:find|register)\b",
        r"\bfor\s+(?:face\s+)?(?:recognition|attendance|registration)\b",
        r"\bsharing\b", r"\bshared\b",
    ]
    for pat in _FILLER_PHRASES:
        name = re.sub(pat, "", name, flags=re.IGNORECASE)
    # Clean whitespace/punctuation, then re-check for honorifics
    name = re.sub(r"\s+", " ", name).strip(" ,-./\n")
    # Remove honorifics (may appear after filler removal)
    for _pf in ["Ms ", "Ms. ", "Mrs ", "Mrs. ", "Mr ", "Mr. ", "Dr ", "Dr. ",
                "ms ", "ms. ", "mrs ", "mrs. ", "mr ", "mr. ", "dr ", "dr. "]:
        if name.lower().startswith(_pf.lower()):
            name = name[len(_pf):]
            break
    # Remove designation keywords
    _DESIGNATION_KW = [
        "pgt", "tgt", "ntt", "teacher", "ma'am", "maam", "sir",
        "register", "face", "selfie", "attendance", "register me",
        "my photo", "my face", "my", "economics", "physics", "chemistry",
        "biology", "maths", "mathematics", "english", "hindi",
        "science", "social", "computer", "art", "music", "dance",
        "sports", "physical education", "commerce", "accounts",
        "business", "history", "geography", "sanskrit", "french",
        "coordinator", "hod", "head of department", "staff",
        "librarian", "counselor", "counsellor", "nurse",
        "receptionist", "security", "guard", "driver", "ayah",
        "principal", "vice principal", "admin", "administration",
        "incharge", "in-charge", "required",
        "photo", "image", "regards", "morning", "afternoon",
        "sharing", "shared",
    ]
    for _kw in _DESIGNATION_KW:
        name = re.sub(r"\b" + re.escape(_kw) + r"\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" ,-./\n")
    return name


router = APIRouter()

# ---------------------------------------------------------------------------
# Recent image buffer: When a sender sends an image without a caption and then
# sends a text message with their name, we link them for face registration.
# Maps sender -> {"media_info": ..., "timestamp": ..., "reply_to": ..., "bot_phone": ...}
# ---------------------------------------------------------------------------
import time as _time_mod
_recent_images: dict[str, dict] = {}
_RECENT_IMAGE_TTL = 30  # seconds — link image + name within 30s

# Buffer for forwarded messages — when a parent forwards content, we ask
# them to clarify what they want before forwarding to the teacher.
_recent_forwarded: dict[str, dict] = {}
_FORWARDED_MSG_TTL = 120  # seconds — wait up to 2 minutes for parent's reply

# ---------------------------------------------------------------------------
# Global kill switch: when True the bot will NOT reply to any incoming message.
# Toggle via POST /webhook/bot-enabled {"enabled": true/false}
# ---------------------------------------------------------------------------
BOT_ENABLED = True  # Re-enabled per user request

@router.post("/webhook/bot-enabled")
async def set_bot_enabled(request: Request):
    # Auth check — admin-only endpoint
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            return {"error": "Unauthorized"}
    global BOT_ENABLED
    body = await request.json()
    BOT_ENABLED = bool(body.get("enabled", False))
    return {"bot_enabled": BOT_ENABLED}

@router.get("/webhook/bot-enabled")
async def get_bot_enabled():
    return {"bot_enabled": BOT_ENABLED}

# ---------------------------------------------------------------------------
# Deduplication: Green API retries webhooks if we respond slowly.
# Persist processed message IDs in the database so dedup survives restarts.
# ---------------------------------------------------------------------------

async def _is_duplicate(message_id: str) -> bool:
    """Return True if this message_id was already processed. Persists in DB."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        )
        row = await cursor.fetchone()
        if row:
            return True
        # Insert and clean up old entries (keep last 24h)
        await db.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)", (message_id,)
        )
        await db.execute(
            "DELETE FROM processed_messages WHERE created_at < datetime('now', '-24 hours')"
        )
        await db.commit()
        return False
    finally:
        await db.close()


def normalize_phone_variants(phone_number: str) -> list[str]:
    """Generate phone number variants for matching (with/without country code 91)."""
    variants = [phone_number]
    if phone_number.startswith("91") and len(phone_number) > 10:
        variants.append(phone_number[2:])  # without country code
    else:
        variants.append("91" + phone_number)  # with country code
    return variants


async def is_allowlisted(phone_number: str) -> bool:
    """Check if a phone number is on the allowlist (handles country code variants)."""
    db = await get_db()
    try:
        variants = normalize_phone_variants(phone_number)
        placeholders = ",".join("?" for _ in variants)
        cursor = await db.execute(
            f"SELECT id FROM allowlist WHERE phone_number IN ({placeholders})",
            variants,
        )
        row = await cursor.fetchone()
        return row is not None
    finally:
        await db.close()


async def get_system_prompt() -> str:
    """Get the system prompt from settings."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = 'system_prompt'"
        )
        row = await cursor.fetchone()
        if row:
            return row[0]
        return "You are a helpful AI assistant."
    finally:
        await db.close()


async def get_conversation_history(phone_number: str) -> list[dict[str, str]]:
    """Get recent conversation history for a phone number."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT content, direction FROM messages
               WHERE (sender = ? OR receiver = ?)
               ORDER BY timestamp DESC LIMIT 10""",
            (phone_number, phone_number),
        )
        rows = await cursor.fetchall()
        history: list[dict[str, str]] = []
        for row in reversed(rows):
            role = "user" if row[1] == "incoming" else "assistant"
            history.append({"role": role, "content": row[0]})
        return history
    finally:
        await db.close()


async def save_message(
    sender: str,
    receiver: str,
    content: str,
    channel: str,
    direction: str,
    wa_message_id: str = "",
) -> None:
    """Save a message to the database."""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO messages (sender, receiver, content, channel, direction, wa_message_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sender, receiver, content, channel, direction, wa_message_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_message_by_wa_id(wa_message_id: str) -> dict | None:
    """Look up a message by its WhatsApp message ID."""
    if not wa_message_id:
        return None
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT content, sender, receiver, direction, timestamp
               FROM messages WHERE wa_message_id = ? LIMIT 1""",
            (wa_message_id,),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "content": row[0],
                "sender": row[1],
                "receiver": row[2],
                "direction": row[3],
                "timestamp": row[4],
            }
        return None
    finally:
        await db.close()


def _normalize_digits(phone: str) -> str:
    """Strip leading 91 country code to get bare 10-digit number."""
    if phone.startswith("91") and len(phone) > 10:
        return phone[2:]
    return phone


def _teacher_chat_id(phone: str) -> str:
    """Build Green API chat ID for a teacher's personal number."""
    if phone.startswith("91"):
        return f"{phone}@c.us"
    return f"91{phone}@c.us"


async def _download_media_bytes(media_info: dict | None) -> tuple[bytes | None, str]:
    """Download Cloud API media once and return (bytes, mime_type).

    Returns (None, "") if no media or download fails.
    """
    if not media_info:
        return None, ""
    cloud_mid = media_info.get("cloud_media_id", "")
    if not cloud_mid:
        return None, ""
    try:
        from app.services.whatsapp_service import download_cloud_media
        media_bytes, mime_type = await download_cloud_media(cloud_mid)
        if not media_bytes:
            logger.error(f"[MEDIA DL] Could not download media {cloud_mid}")
            return None, ""
        logger.info(f"[MEDIA DL] Downloaded {len(media_bytes)} bytes, mime={mime_type}")
        return media_bytes, mime_type
    except Exception as exc:
        logger.error(f"[MEDIA DL] Failed: {exc}")
        return None, ""


def _make_email_attachments(
    media_info: dict | None,
    media_bytes: bytes | None,
    mime_type: str,
) -> list[tuple[bytes, str, str]]:
    """Build email attachment list from pre-downloaded media bytes.

    Returns a list of (file_bytes, filename, mime_type) tuples ready for
    send_email_async(). Returns an empty list if no media bytes.
    """
    if not media_bytes or not media_info:
        return []
    filename = media_info.get("filename", "")
    if not filename:
        ext_map = {
            "image/jpeg": "image.jpg", "image/png": "image.png",
            "image/webp": "image.webp", "application/pdf": "document.pdf",
            "video/mp4": "video.mp4",
        }
        filename = ext_map.get(mime_type, "attachment")
    return [(media_bytes, filename, mime_type or "application/octet-stream")]


async def save_forwarded_conversation(
    teacher_phone: str,
    teacher_name: str,
    teacher_grade: str,
    original_chat_id: str,
    sender_phone: str,
    original_message: str,
) -> None:
    """Save a forwarded conversation so we can relay teacher replies back."""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO forwarded_conversations
               (teacher_phone, teacher_name, teacher_grade, original_chat_id, sender_phone, original_message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (teacher_phone, teacher_name, teacher_grade, original_chat_id, sender_phone, original_message),
        )
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Pending query management: when bot can't answer, store the query and ask
# for class/section.  When parent replies with class info, email the teacher.
# ---------------------------------------------------------------------------

async def save_pending_query(sender_phone: str, reply_to: str, original_query: str) -> None:
    """Save a query that the bot couldn't answer, pending class/section info from parent."""
    db = await get_db()
    try:
        # Remove any existing pending query for this sender first
        await db.execute("DELETE FROM pending_queries WHERE sender_phone = ?", (sender_phone,))
        await db.execute(
            """INSERT INTO pending_queries (sender_phone, reply_to, original_query)
               VALUES (?, ?, ?)""",
            (sender_phone, reply_to, original_query),
        )
        await db.commit()
    finally:
        await db.close()


async def get_pending_query(sender_phone: str) -> dict | None:
    """Get pending query for a sender (within last 1 hour)."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT original_query, reply_to, created_at
               FROM pending_queries
               WHERE sender_phone = ?
               AND created_at > datetime('now', '-1 hour')
               ORDER BY created_at DESC LIMIT 1""",
            (sender_phone,),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "original_query": row[0],
                "reply_to": row[1],
                "created_at": row[2],
            }
        return None
    finally:
        await db.close()


async def delete_pending_query(sender_phone: str) -> None:
    """Remove pending query after it's been handled."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM pending_queries WHERE sender_phone = ?", (sender_phone,))
        await db.commit()
    finally:
        await db.close()


async def try_handle_pending_query(
    sender: str, message_text: str, reply_to: str
) -> bool:
    """Check if sender has a pending query and current message contains class/section info.
    If so, look up the class teacher and email them the original query.
    Returns True if handled."""
    pending = await get_pending_query(sender)
    if not pending:
        return False

    # Try to match the message to a class/grade
    teacher_entry = find_teacher_by_grade(message_text)
    if not teacher_entry:
        # Message doesn't look like a class/section — might be a new question
        # Delete the pending query and let normal flow handle it
        await delete_pending_query(sender)
        return False

    original_query = pending["original_query"]
    teacher_name = teacher_entry["teacher"].split("/")[0].strip()
    teacher_email = teacher_entry.get("email", "")
    teacher_phone = teacher_entry.get("whatsapp", "")
    grade = teacher_entry["grade"]

    if not teacher_email and not teacher_phone:
        # No contact info for this teacher
        msg = (
            f"I found the class teacher for *{grade}* is *{teacher_name}*, "
            f"but I don't have their contact details on file. "
            f"Please contact the school office for assistance.\n\n"
            f"Phone: 011-45161066 / 64 / 63\n"
            f"Email: info@ppischool.in"
        )
        await send_whatsapp_message(reply_to, msg)
        await delete_pending_query(sender)
        return True

    # Build the forwarded message for the teacher
    notification = (
        f"Dear {teacher_name},\n\n"
        f"A parent has sent the following query via the *PPIS Bot* "
        f"regarding *{grade}*:\n\n"
        f"\U0001f4e9 \"{original_query}\"\n\n"
        f"Please address this query at your earliest convenience.\n\n"
        f"— PPIS Bot"
    )

    # Forward via WhatsApp
    wa_success = False
    if teacher_phone:
        chat_id = _teacher_chat_id(teacher_phone)
        wa_success = await send_whatsapp_message(chat_id, notification)
        if wa_success:
            logger.info(f"Forwarded pending query via WhatsApp to {teacher_name} ({teacher_phone})")

    # Forward via email
    email_success = False
    if teacher_email:
        email_subject = f"PPIS Bot: Parent Query regarding {grade}"
        email_body = (
            f"Dear {teacher_name},\n\n"
            f"A parent has sent the following query via the PPIS WhatsApp Bot "
            f"regarding {grade}:\n\n"
            f"\"{original_query}\"\n\n"
            f"Please address this query at your earliest convenience.\n\n"
            f"Regards,\nPPIS Bot"
        )
        email_success = await send_email_async(teacher_email, email_subject, email_body)
        if email_success:
            logger.info(f"Forwarded pending query via email to {teacher_name} ({teacher_email})")

    # Build confirmation message
    methods = []
    if wa_success:
        methods.append("WhatsApp")
    if email_success:
        methods.append(f"email ({teacher_email})")

    if methods:
        method_str = " and ".join(methods)
        confirm_msg = (
            f"Thank you! Your query has been forwarded to *{teacher_name}* "
            f"(Class Teacher, {grade}) via {method_str}.\n\n"
            f"They will get back to you soon."
        )
    else:
        confirm_msg = (
            f"I tried to forward your query to *{teacher_name}* ({grade}) "
            f"but could not reach them right now. Please contact the school office directly.\n\n"
            f"Phone: 011-45161066 / 64 / 63\n"
            f"Email: info@ppischool.in"
        )

    await send_whatsapp_message(reply_to, confirm_msg)
    await delete_pending_query(sender)
    return True


def _is_unknown_response(ai_response: str) -> bool:
    """Detect if the AI response indicates the bot doesn't have specific info.
    Returns True ONLY if the response is PRIMARILY a 'I don't know' message.
    Does NOT flag responses that contain useful info alongside a contact suggestion."""
    lower = ai_response.lower()

    # Strong indicators that the bot genuinely doesn't know — these almost always
    # mean the response is primarily a "don't know" message.
    strong_unknown_phrases = [
        "i don't have specific information",
        "i don't have that information",
        "i don't have the specific",
        "i do not have specific",
        "i do not have that information",
        "i currently don't have",
        "i currently do not have",
        "i don't have enough information",
        "i'm unable to answer",
        "i am unable to answer",
        "beyond my current knowledge",
        "outside my current knowledge",
        "i don't have access to that",
        "[escalate]",
    ]

    # Weak indicators — these appear in helpful responses too (e.g. "here is the info,
    # please feel free to contact the office for more details").  Only flag these if the
    # response is very short (under 200 chars), meaning the bot didn't actually provide
    # substantive info.
    weak_unknown_phrases = [
        "i'm not sure about",
        "i am not sure about",
        "please contact our school office",
        "please feel free to contact",
        "for detailed and accurate information",
        "for detailed assistance",
    ]

    if any(phrase in lower for phrase in strong_unknown_phrases):
        return True

    # Only flag weak phrases when the response is very short (no real content)
    if len(ai_response) < 200 and any(phrase in lower for phrase in weak_unknown_phrases):
        return True

    return False


async def get_recent_forwarded_conversation(teacher_phone: str) -> dict | None:
    """Get the most recent forwarded conversation for a teacher (within last 24 hours)."""
    db = await get_db()
    try:
        digits = _normalize_digits(teacher_phone)
        cursor = await db.execute(
            """SELECT teacher_name, teacher_grade, original_chat_id, sender_phone, original_message, created_at
               FROM forwarded_conversations
               WHERE (teacher_phone = ? OR teacher_phone = ?)
               AND created_at > datetime('now', '-24 hours')
               ORDER BY created_at DESC LIMIT 1""",
            (digits, f"91{digits}"),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "teacher_name": row[0],
                "teacher_grade": row[1],
                "original_chat_id": row[2],
                "sender_phone": row[3],
                "original_message": row[4],
                "created_at": row[5],
            }
        return None
    finally:
        await db.close()


async def forward_to_teachers_and_confirm(
    sender: str, message_text: str, reply_to: str, media_info: dict | None = None
) -> None:
    """Forward inquiry to mentioned teachers and send confirmation back.
    Also saves the conversation context so teacher replies can be relayed back.
    If media_info is provided, also forwards the media file to the teacher."""
    teachers = find_mentioned_teachers(message_text)
    if not teachers:
        return

    # --- Look up the parent's child name and class from PI Sheet ---
    parent_children = await _lookup_parent_child_class(sender)
    if parent_children:
        # Use the first child (most common case: one child per parent)
        child = parent_children[0]
        parent_label = f"Parent of {child['student_name']} ({child['grade']})"
    else:
        parent_label = f"A parent (phone: ...{sender[-4:]})"

    forwarded_names: list[str] = []

    # Download media ONCE (reused for every teacher's WhatsApp + email)
    _dl_bytes, _dl_mime = await _download_media_bytes(media_info)

    for entry in teachers:
        teacher_phone = entry.get("whatsapp", "")
        teacher_email = entry.get("email", "")
        teacher_display = entry["teacher"].split("/")[0].strip()

        if not teacher_phone and not teacher_email:
            logger.info(f"No WhatsApp or email for {entry['teacher']} ({entry['grade']}), skipping")
            continue

        # Don't forward to the teacher if they sent the message themselves
        if teacher_phone and _normalize_digits(sender) == _normalize_digits(teacher_phone):
            logger.info(f"Sender is {entry['teacher']}, skipping self-forward")
            continue

        wa_success = False
        if teacher_phone:
            import asyncio as _asyncio_relay
            from app.services.whatsapp_service import (
                send_cloud_template_message as _send_tmpl,
                forward_cloud_media_to_recipient,
            )
            chat_id = _teacher_chat_id(teacher_phone)
            t_recipient = teacher_phone if not teacher_phone.startswith("91") else teacher_phone
            if len(t_recipient) == 10:
                t_recipient = "91" + t_recipient

            _query_msg = (
                f"\U0001f4e9 *Query from {parent_label}:*\n\n"
                f"\"{message_text[:500]}\"\n\n"
                f"_Reply to this message \u2014 your response will be forwarded to the parent._"
            )

            # Try sending the query directly first (works if conversation
            # window is already open). This avoids the confusing template.
            wa_success = await send_whatsapp_message(chat_id, _query_msg)
            if wa_success:
                logger.info(f"[FWD] Direct query sent to {entry['teacher']}")
            else:
                # Conversation window closed — send template to open a
                # business-initiated window, then resend the actual query.
                logger.info(f"[FWD] Direct msg failed, sending template for {entry['teacher']}")
                tmpl_ok = await _send_tmpl(
                    t_recipient, "ppis_class_assignment",
                    body_params=[f"Query from {parent_label}", message_text[:400]],
                )
                if tmpl_ok:
                    # Wait for template delivery to open conversation window
                    await _asyncio_relay.sleep(4)
                    wa_success = await send_whatsapp_message(chat_id, _query_msg)
                    if wa_success:
                        logger.info(f"[FWD] Query sent after template to {entry['teacher']}")
                    else:
                        wa_success = True  # template itself was delivered
                        logger.warning(f"[FWD] Query after template failed for {entry['teacher']}, template delivered")
                else:
                    logger.error(f"[FWD] Both direct msg and template failed for {entry['teacher']}")

            # Send media attachment
            if wa_success and media_info:
                await _asyncio_relay.sleep(3)
                _mcap = media_info.get("caption", "")
                _mok = False
                try:
                    _mok = await forward_cloud_media_to_recipient(media_info, chat_id, caption=_mcap)
                except Exception as _me:
                    logger.error(f"[FWD MEDIA] Cloud error: {_me}")
                if _mok:
                    logger.info(f"[FWD MEDIA] Media sent to {entry['teacher']}")
                else:
                    logger.warning(f"[FWD MEDIA] WhatsApp media failed for {entry['teacher']} — email has attachment")

        # Always send email too (reliable channel with full query + attachment)
        email_success = False
        if teacher_email:
            email_body = (
                f"Dear {teacher_display},\n\n"
                f"{parent_label} has sent the following query via the PPIS Bot:\n\n"
                f"\"{message_text[:500]}\"\n\n"
                f"Kindly reply to this email and your response will be forwarded back to the parent.\n\n"
                f"Regards,\nPPIS Bot"
            )
            _email_att = _make_email_attachments(media_info, _dl_bytes, _dl_mime)
            email_success = await send_email_async(
                teacher_email,
                f"PPIS Bot: Query from {parent_label}",
                email_body,
                attachments=_email_att or None,
            )
            if email_success:
                logger.info(f"Forwarded via email to {entry['teacher']} ({teacher_email})")
            else:
                logger.error(f"Failed to forward via email to {entry['teacher']} ({teacher_email})")

        if wa_success or email_success:
            methods = []
            if wa_success:
                methods.append("WhatsApp")
            if email_success:
                methods.append("email")
            method = " & ".join(methods)
            forwarded_names.append(f"{teacher_display} ({entry['grade']}) via {method}")
            # Save conversation context for 2-way relay (only if WhatsApp worked)
            if wa_success:
                await save_forwarded_conversation(
                    teacher_phone=teacher_phone,
                    teacher_name=teacher_display,
                    teacher_grade=entry["grade"],
                    original_chat_id=reply_to,
                    sender_phone=sender,
                    original_message=message_text[:500],
                )
            # --- Relay tracking: log this parent→teacher message ---
            try:
                from app.services.relay_service import (
                    save_relay_message as _save_relay,
                    auto_tag_message as _auto_tag,
                )
                from app.services.attachment_service import (
                    save_attachment_metadata as _save_att,
                    classify_media_type as _classify_att,
                )
                _tags = _auto_tag(message_text, media_info is not None)
                _child_info = await _lookup_parent_child_class(sender)
                _p_label = f"Parent of {_child_info[0]['student_name']}" if _child_info else ""
                _r_id = await _save_relay(
                    sender_phone=sender, sender_role="parent",
                    receiver_phone=teacher_phone or "", receiver_role="teacher",
                    direction="parent_to_teacher",
                    message_text=message_text[:1000],
                    message_type="attachment" if media_info else "text",
                    grade=entry.get("grade", ""),
                    student_name=_p_label,
                    delivery_status="delivered" if (wa_success or email_success) else "failed",
                    email_sent=email_success,
                    tags=_tags,
                )
                if media_info and _r_id:
                    await _save_att(
                        relay_message_id=_r_id,
                        file_type=_classify_att(media_info.get("mime_type", ""), media_info.get("filename", "")),
                        file_name=media_info.get("filename", "attachment"),
                        mime_type=media_info.get("mime_type", "application/octet-stream"),
                        file_size=media_info.get("file_size", 0),
                        cloud_media_id=media_info.get("cloud_media_id", ""),
                    )
            except Exception as _relay_err:
                logger.error(f"Relay tracking error (forward_to_teachers): {_relay_err}")
        else:
            logger.error(f"Failed to forward to {entry['teacher']} (both WhatsApp and email failed)")
            # Log failed delivery to relay system
            try:
                from app.services.relay_service import save_relay_message as _save_relay_fail, auto_tag_message as _auto_tag_fail
                _tags_f = _auto_tag_fail(message_text, media_info is not None)
                await _save_relay_fail(
                    sender_phone=sender, sender_role="parent",
                    receiver_phone=teacher_phone or "", receiver_role="teacher",
                    direction="parent_to_teacher",
                    message_text=message_text[:1000],
                    message_type="attachment" if media_info else "text",
                    grade=entry.get("grade", ""),
                    delivery_status="failed",
                    tags=_tags_f,
                )
            except Exception:
                pass

    # Send confirmation back to the original chat
    if forwarded_names:
        names_str = ", ".join(forwarded_names)
        confirm_msg = (
            f"Your message has been forwarded to: {names_str}.\n\n"
            f"Once they respond, their reply will be shared with you here.\n\n"
            f"Thank you for your cooperation.\n"
            f"Warm regards,\n"
            f"PP International School"
        )
        await send_whatsapp_message(reply_to, confirm_msg)


async def try_relay_teacher_reply(
    sender: str, message_text: str, reply_to: str, media_info: dict | None = None
) -> bool:
    """Check if this is a teacher replying to a forwarded message.
    If so, relay their reply back to the original parent who asked.
    Returns True if the message was relayed (so normal bot response can be skipped).

    IMPORTANT: This must NOT intercept broadcast-style messages (homework,
    summary sheets, etc.) that should be handled by
    detect_and_handle_teacher_homework_broadcast() instead.
    """
    from app.services.whatsapp_service import (
        forward_cloud_media_to_recipient,
        send_cloud_template_message,
    )

    # Only check private (non-group) chats for teacher relay
    if reply_to.endswith("@g.us"):
        return False

    # --- Guard: skip broadcast-style messages ---
    # If the message matches homework/broadcast keywords, let the broadcast
    # handler deal with it instead of intercepting it as a "reply".
    if _TEACHER_BROADCAST_RE.search(message_text):
        teacher_entry = _is_teacher_phone(sender)
        if teacher_entry is not None:
            logger.info(
                f"try_relay_teacher_reply: skipping broadcast-style message "
                f"from teacher {sender}: {message_text[:80]}"
            )
            return False

    conv = await get_recent_forwarded_conversation(sender)
    if conv is None:
        return False

    # This teacher has a recent forwarded conversation — relay their reply
    original_chat_id = conv["original_chat_id"]
    teacher_name = conv["teacher_name"]
    teacher_grade = conv["teacher_grade"]

    # Build the relay text — skip placeholder text like "[Document shared]"
    actual_text = message_text.strip()
    is_placeholder = actual_text in (
        "[Document shared]", "[Image shared]", "[Video shared]",
        "[Audio shared]", "[Sticker shared]",
    )

    import asyncio as _asyncio_relay

    # --- Send text reply to parent (with 24-hour window handling) ---
    # If the parent's conversation window is closed (>24 hours since their
    # last message), direct sends fail. We use a template to re-open it.
    text_success = True
    relay_msg = (
        f"Reply from {teacher_name} (Class Teacher, {teacher_grade}):\n\n"
        f"{actual_text}\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\n"
        f"PP International School"
    )

    # Determine the parent phone for template sending (strip @c.us/@s.whatsapp.net)
    parent_recipient = original_chat_id.split("@")[0]
    if len(parent_recipient) == 10:
        parent_recipient = "91" + parent_recipient

    if not is_placeholder:
        text_success = await send_whatsapp_message(original_chat_id, relay_msg)
        if not text_success:
            # 24-hour window likely closed — send template to re-open
            logger.info(
                f"Relay: Direct msg to parent {original_chat_id} failed, "
                f"sending template to open window"
            )
            tmpl_ok = await send_cloud_template_message(
                parent_recipient, "ppis_class_assignment",
                body_params=[
                    f"Reply from {teacher_name}",
                    actual_text[:400],
                ],
            )
            if tmpl_ok:
                # Wait for template delivery to open conversation window
                await _asyncio_relay.sleep(4)
                text_success = await send_whatsapp_message(original_chat_id, relay_msg)
                if not text_success:
                    text_success = True  # template itself was delivered
                    logger.warning(
                        f"Relay: Direct msg after template also failed for "
                        f"{original_chat_id}, but template was delivered"
                    )
            else:
                logger.error(
                    f"Relay: Both direct msg and template failed for "
                    f"parent {original_chat_id}"
                )

    # If teacher sent media, forward the actual media file
    media_forwarded = False
    if media_info:
        media_caption = f"From {teacher_name} (Class Teacher, {teacher_grade})"
        cloud_media_id = media_info.get("cloud_media_id", "")
        if cloud_media_id:
            # Cloud API path: download and re-upload the media
            logger.info(
                f"Relay: forwarding cloud media {cloud_media_id} "
                f"from {teacher_name} to {original_chat_id}"
            )
            # Wait a moment for window to settle after text/template
            await _asyncio_relay.sleep(3)
            media_forwarded = await forward_cloud_media_to_recipient(
                media_info, original_chat_id, caption=media_caption
            )
            if not media_forwarded:
                logger.warning(
                    f"Relay: Cloud media forwarding failed for {cloud_media_id}, "
                    f"will send text fallback"
                )
        elif media_info.get("url"):
            # URL-based path: forward by URL
            media_forwarded = await forward_file_by_url(
                original_chat_id,
                media_info["url"],
                media_info.get("filename", "file"),
                media_caption,
            )

    success = text_success or media_forwarded

    # --- Relay tracking: log teacher→parent reply ---
    try:
        from app.services.relay_service import (
            save_relay_message as _save_relay_t2p,
            auto_tag_message as _auto_tag_t2p,
        )
        from app.services.attachment_service import (
            save_attachment_metadata as _save_att_t2p,
            classify_media_type as _classify_att_t2p,
        )
        _tags_t = _auto_tag_t2p(message_text, media_info is not None)
        _rid_t = await _save_relay_t2p(
            sender_phone=sender, sender_role="teacher",
            receiver_phone=original_chat_id.replace("@c.us", "").replace("@s.whatsapp.net", ""),
            receiver_role="parent",
            direction="teacher_to_parent",
            message_text=message_text[:1000],
            message_type="attachment" if media_info else "text",
            grade=teacher_grade,
            delivery_status="delivered" if success else "failed",
            tags=_tags_t,
        )
        if media_info and _rid_t:
            await _save_att_t2p(
                relay_message_id=_rid_t,
                file_type=_classify_att_t2p(media_info.get("mime_type", ""), media_info.get("filename", "")),
                file_name=media_info.get("filename", "attachment"),
                mime_type=media_info.get("mime_type", "application/octet-stream"),
                file_size=media_info.get("file_size", 0),
                cloud_media_id=media_info.get("cloud_media_id", ""),
            )
    except Exception as _relay_t2p_err:
        logger.error(f"Relay tracking error (teacher_reply): {_relay_t2p_err}")

    if success:
        logger.info(f"Relayed teacher reply from {teacher_name} to {original_chat_id} (media={media_forwarded})")
        await send_whatsapp_message(
            reply_to,
            "Your reply has been forwarded to the parent. Thank you for your prompt response.\n\nWarm regards,\nPP International School"
        )
    else:
        logger.error(f"Failed to relay teacher reply to {original_chat_id}")
        await send_whatsapp_message(
            reply_to,
            "We were unable to forward your reply at this time. Please try again or contact the school office.\n\nWarm regards,\nPP International School"
        )

    return True


# ---------------------------------------------------------------------------
# Direct messaging: detect "send message to X", "tell X that Y", "message X"
# patterns and deliver the message directly to that person's WhatsApp.
# ---------------------------------------------------------------------------

# Honorifics / titles to strip when looking up a recipient name
_HONORIFICS = re.compile(
    r"\b(?:ms\.?|mrs\.?|mr\.?|miss|ma'?am|sir|dr\.?|teacher|mam)\b",
    re.IGNORECASE,
)

# Prefixes like "the ct of", "class teacher of", "teacher of" to strip from recipient
_RECIPIENT_PREFIXES = re.compile(
    r"^(?:the\s+)?(?:ct|class\s*teacher|teacher)\s+(?:of\s+)?",
    re.IGNORECASE,
)

# Admin / non-teaching staff that can also receive direct messages
ADMIN_STAFF = [
    {"name": "Harpreet Kaur", "role": "Administration Incharge", "whatsapp": "9599488106"},
]

# School helpline / front desk number
SCHOOL_HELPLINE = "8800935552"

# Patterns that indicate a direct-message request.
# Each pattern should have named groups `recipient` and `content`.
# IMPORTANT: recipient uses `.+?` (non-greedy) terminated by a keyword
# (that/about/regarding/saying) or a colon — NOT by whitespace.
_DM_PATTERNS: list[re.Pattern[str]] = [
    # --- keyword-delimited (recipient ends at "that/about/regarding/saying") ---
    # "send message to ms Prabhjot Kaur that homework needs to be checked"
    re.compile(
        r"(?:send|forward)\s+(?:a\s+)?message\s+to\s+(?P<recipient>.+?)\s+(?:that|about|regarding|saying)\s+(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "tell Harnoor Kaur that tomorrow is PTM"
    re.compile(
        r"(?:tell|inform|notify|remind|ask)\s+(?P<recipient>.+?)\s+(?:that|about|to|regarding)\s+(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "convey to 3C teacher that there is no school tomorrow"
    re.compile(
        r"convey\s+to\s+(?P<recipient>.+?)\s+(?:that|about|regarding)\s+(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "ping Harnoor about the PTM schedule"
    re.compile(
        r"ping\s+(?P<recipient>.+?)\s+(?:that|about|to|regarding)\s+(?P<content>.+)",
        re.IGNORECASE,
    ),

    # --- colon-delimited (recipient ends at ":") ---
    # "send message to Harnoor Kaur: tomorrow is PTM"
    re.compile(
        r"(?:send|forward)\s+(?:a\s+)?message\s+to\s+(?P<recipient>.+?)\s*:\s*(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "message Harnoor Kaur: tomorrow is PTM"
    re.compile(
        r"message\s+(?P<recipient>.+?)\s*:\s*(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "send to 9289234659: hello"
    re.compile(
        r"send\s+to\s+(?P<recipient>.+?)\s*:\s*(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "whatsapp Harnoor Kaur: please check the homework"
    re.compile(
        r"whatsapp\s+(?P<recipient>.+?)\s*:\s*(?P<content>.+)",
        re.IGNORECASE,
    ),

    # --- email-specific patterns ---
    # "mail alisha.kanwar@ppischool.in about the meeting with team"
    re.compile(
        r"(?:mail|email|e-mail)\s+(?:at\s+|to\s+)?(?P<recipient>[\w.+-]+@[\w.-]+)\s+(?:that|about|regarding|saying)\s+(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "mail alisha.kanwar@ppischool.in: meeting tomorrow"
    re.compile(
        r"(?:mail|email|e-mail)\s+(?:at\s+|to\s+)?(?P<recipient>[\w.+-]+@[\w.-]+)\s*:\s*(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "send mail to alisha.kanwar@ppischool.in about the meeting"
    re.compile(
        r"send\s+(?:a\s+)?(?:mail|email|e-mail)\s+(?:to\s+|at\s+)?(?P<recipient>[\w.+-]+@[\w.-]+)\s+(?:that|about|regarding|saying)\s+(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "send mail to alisha.kanwar@ppischool.in: meeting tomorrow"
    re.compile(
        r"send\s+(?:a\s+)?(?:mail|email|e-mail)\s+(?:to\s+|at\s+)?(?P<recipient>[\w.+-]+@[\w.-]+)\s*:\s*(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "send an email to Harnoor about the PTM" (name-based, not email address)
    re.compile(
        r"send\s+(?:a\s+)?(?:mail|email|e-mail)\s+to\s+(?P<recipient>.+?)\s+(?:that|about|regarding|saying)\s+(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "email Harnoor: please check homework"
    re.compile(
        r"(?:mail|email|e-mail)\s+(?P<recipient>.+?)\s*:\s*(?P<content>.+)",
        re.IGNORECASE,
    ),
    # "email Harnoor about the PTM"
    re.compile(
        r"(?:mail|email|e-mail)\s+(?P<recipient>.+?)\s+(?:that|about|regarding|saying)\s+(?P<content>.+)",
        re.IGNORECASE,
    ),
]


_CLASS_LIST_RE = re.compile(
    r"(?:class\s*list|student\s*list|students?\s*(?:of|in)|list\s*(?:of\s*)?students?|"
    r"class\s*(?:roll|roster)|who\s*(?:all\s*)?(?:is|are)\s*(?:in|of)\s*(?:class|grade)|"
    r"show\s*(?:me\s*)?(?:the\s*)?(?:class|student)\s*list|"
    r"(?:कक्षा|क्लास)\s*(?:सूची|लिस्ट)|(?:छात्र|विद्यार्थी)\s*(?:सूची|लिस्ट))",
    re.IGNORECASE,
)

_GRADE_EXTRACT_RE = re.compile(
    r"(?:grade|class|कक्षा|क्लास)[\s\-]*(\d{1,2})[\s\-]*([a-cA-C])?|"
    r"(?:nur(?:sery)?|nursery)[\s\-]*(\d)?|"
    r"(?:prep)[\s\-]*(\d)?|"
    r"(?:popsicle)",
    re.IGNORECASE,
)


async def lookup_class_list(message_text: str) -> str | None:
    """If the message asks for a class list, query the PI sheet DB and return the list."""
    if not _CLASS_LIST_RE.search(message_text):
        return None

    msg_low = message_text.lower()

    # Try to extract the grade/class from the message
    grade_filter = None
    m = _GRADE_EXTRACT_RE.search(message_text)
    if m:
        if m.group(1):  # grade N [section]
            grade_num = m.group(1)
            section = (m.group(2) or "").upper()
            if section:
                grade_filter = f"Grade {grade_num}{section}"
            else:
                grade_filter = f"Grade {grade_num}"
        elif m.group(3) is not None:  # nursery N
            grade_filter = f"Nursery" if not m.group(3) else f"NURSERY {m.group(3)}"
        elif m.group(4) is not None:  # prep N
            grade_filter = f"Prep {m.group(4)}" if m.group(4) else "Prep"
        elif "popsicle" in msg_low:
            grade_filter = "POPSICLE"
    elif "nursery" in msg_low or "nur " in msg_low:
        # try to get number after nursery
        nm = re.search(r"(?:nursery|nur)\s*(\d)", msg_low)
        grade_filter = f"NURSERY {nm.group(1)}" if nm else "Nursery"
    elif "prep" in msg_low:
        pm = re.search(r"prep\s*(\d)", msg_low)
        grade_filter = f"Prep {pm.group(1)}" if pm else "Prep"
    elif "popsicle" in msg_low:
        grade_filter = "POPSICLE"

    if not grade_filter:
        return None

    db = await get_db()
    try:
        # Use LIKE for flexible matching
        like_pattern = f"%{grade_filter}%"
        cursor = await db.execute(
            "SELECT student_name, grade FROM pi_sheet_students WHERE grade LIKE ? ORDER BY student_name",
            (like_pattern,),
        )
        rows = await cursor.fetchall()

        if not rows:
            # Try case-insensitive
            cursor2 = await db.execute(
                "SELECT student_name, grade FROM pi_sheet_students WHERE LOWER(grade) LIKE LOWER(?) ORDER BY student_name",
                (like_pattern,),
            )
            rows = await cursor2.fetchall()

        if not rows:
            return None

        grade_label = rows[0][1]
        student_names = [r[0] for r in rows]
        header = f"Class List — {grade_label}\n"
        header += f"Total students: {len(student_names)}\n\n"
        body = "\n".join(f"{i+1}. {name}" for i, name in enumerate(student_names))
        return header + body
    finally:
        await db.close()


def _strip_honorifics(name: str) -> str:
    """Remove honorifics like Ms, Ma'am, Sir etc. and prefixes like 'the ct of' from a name."""
    cleaned = _RECIPIENT_PREFIXES.sub("", name).strip()
    cleaned = _HONORIFICS.sub("", cleaned).strip()
    # collapse multiple spaces
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned if cleaned else name


def _extract_phone_from_text(text: str) -> str | None:
    """Extract a 10+ digit phone number from free text, return bare 10 digits or None."""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        return digits[-10:]
    return None


def _lookup_admin_staff(query: str) -> dict | None:
    """Look up admin staff by name."""
    q = query.lower().strip()
    for staff in ADMIN_STAFF:
        name_lower = staff["name"].lower()
        first_name = name_lower.split()[0]
        if name_lower in q or (len(first_name) > 2 and first_name in q):
            return staff
    return None


async def try_direct_message(
    sender: str, message_text: str, reply_to: str, media_info: dict | None = None
) -> bool:
    """Detect if user is asking to send a direct message to someone.
    If so, deliver it and confirm. Returns True if handled."""
    msg = message_text.strip()

    recipient_raw: str | None = None
    content: str | None = None

    for pattern in _DM_PATTERNS:
        m = pattern.search(msg)
        if m:
            recipient_raw = m.group("recipient").strip().rstrip(":,- ")
            content = m.group("content").strip()
            break

    if not recipient_raw or not content:
        return False

    logger.info(f"Direct message detected — recipient_raw='{recipient_raw}', content='{content}'")

    # Strip honorifics (Ms, Ma'am, Sir, etc.) for better name matching
    recipient_clean = _strip_honorifics(recipient_raw)
    logger.info(f"After stripping honorifics: '{recipient_clean}'")

    # --- Always send email when asking to inform/tell/mail a teacher ---
    # Any direct-message request that resolves to a teacher with an email
    # should send an email, since WhatsApp quota is limited on free tier.
    prefer_email = True  # always email teachers when message is about informing them

    # --- Resolve recipient to phone number and/or email ---
    target_phone: str | None = None
    target_email: str | None = None
    target_name: str = recipient_clean  # fallback display name

    # 0. Check if recipient is already an email address
    if re.match(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", recipient_clean):
        target_email = recipient_clean
        # Try to find a matching name in TEACHER_DATA
        for t in TEACHER_DATA:
            if t.get("email", "").lower() == recipient_clean.lower():
                target_name = t["teacher"].split("/")[0].strip()
                target_phone = t.get("whatsapp", "")
                break
        if target_name == recipient_clean:
            # Use the part before @ as a display name
            target_name = recipient_clean.split("@")[0].replace(".", " ").title()

    # 1. Check if recipient is a raw phone number
    if not target_email:
        phone_digits = _extract_phone_from_text(recipient_clean)
        if phone_digits:
            target_phone = phone_digits
            entry = lookup_person_by_name_or_phone(recipient_clean)
            if entry:
                target_name = entry["teacher"].split("/")[0].strip()
                target_email = entry.get("email", "")

    if not target_phone and not target_email:
        # 2. Look up by name / grade in TEACHER_DATA
        entry = lookup_person_by_name_or_phone(recipient_clean)
        if entry:
            target_phone = entry.get("whatsapp", "")
            target_email = entry.get("email", "")
            target_name = entry["teacher"].split("/")[0].strip()

    if not target_phone and not target_email:
        # 3. Check admin staff (Harpreet Kaur)
        admin = _lookup_admin_staff(recipient_clean)
        if admin:
            target_phone = admin.get("whatsapp", "")
            target_email = admin.get("email", "")
            target_name = admin["name"]

    if not target_phone and not target_email:
        logger.info(f"Could not resolve recipient '{recipient_raw}' (cleaned: '{recipient_clean}') to a phone or email")
        # If user explicitly asked to email/mail, tell them we couldn't find the person
        if prefer_email:
            await send_whatsapp_message(
                reply_to,
                f"Sorry, I couldn't find *{recipient_clean}* in the school directory. "
                f"Please check the name/grade and try again, or provide the full email address "
                f"(e.g. \"mail name@ppischool.in about ...\").",
            )
            return True  # handled — don't fall through to AI
        return False

    logger.info(f"Resolved recipient to {target_name} (phone={target_phone}, email={target_email})")

    # --- Always send email to the teacher if we have their email ---
    wa_success = False
    email_success = False

    if target_email:
        email_body = (
            f"Hello {target_name},\n\n"
            f"You have a message via the PPIS Bot:\n\n"
            f"\"{content}\"\n\n"
            f"Please reply to this email or contact the school for further details.\n\n"
            f"Regards,\nPPIS Bot"
        )
        email_success = await send_email_async(
            target_email,
            f"PPIS Bot: Message for {target_name}",
            email_body,
        )
        logger.info(f"Email {'sent' if email_success else 'FAILED'} to {target_email}")

    # --- Also try WhatsApp for 2-way communication ---
    if target_phone:
        chat_id = _teacher_chat_id(target_phone)
        dm_text = (
            f"Hello {target_name},\n\n"
            f"You have a message via *PPIS Bot*:\n\n"
            f"\"{content}\"\n\n"
            f"You can reply to this message and your response will be forwarded back.\n\n"
            f"— PPIS Bot"
        )
        wa_success = await send_whatsapp_message(chat_id, dm_text)

        if wa_success:
            # Also forward media if present
            if media_info and media_info.get("url"):
                await forward_file_by_url(
                    chat_id,
                    media_info["url"],
                    media_info.get("filename", "file"),
                    media_info.get("caption", ""),
                )

    if not wa_success and not email_success:
        await send_whatsapp_message(reply_to, f"Sorry, I couldn't deliver the message to {target_name}. Please try again.")
        logger.error(f"Failed to send direct message to {target_name} (both WhatsApp and email failed)")
        return True  # still handled — don't fall through to AI

    # Save conversation context so teacher can reply back (WhatsApp only)
    if wa_success and target_phone:
        grade_label = next(
            (e["grade"] for e in TEACHER_DATA if e.get("whatsapp") == target_phone),
            None,
        )
        if not grade_label:
            admin = _lookup_admin_staff(target_name)
            grade_label = admin["role"] if admin else "Staff"

        await save_forwarded_conversation(
            teacher_phone=target_phone,
            teacher_name=target_name,
            teacher_grade=grade_label,
            original_chat_id=reply_to,
            sender_phone=sender,
            original_message=content[:500],
        )

    # Confirm back to the sender / group
    methods: list[str] = []
    if email_success:
        methods.append("email")
    if wa_success:
        methods.append("WhatsApp")
    method_str = " & ".join(methods) if methods else "email"
    confirm = f"Your message has been sent to {target_name} via {method_str}.\n\nThank you for your cooperation.\nWarm regards,\nPP International School"
    if wa_success:
        confirm += "\nThey can reply and the response will be shared here."
    await send_whatsapp_message(reply_to, confirm)
    logger.info(f"Direct message sent to {target_name} (phone={target_phone}, email={target_email}) via {method_str} from {sender}")

    return True


# ---------------------------------------------------------------------------
# Homework Query Detection & Forwarding
# ---------------------------------------------------------------------------

_HOMEWORK_KEYWORDS = [
    "homework", "home work", "hw", "assignment", "classwork", "class work",
    "today's work", "todays work", "project", "worksheet", "work sheet",
    "pending work", "given work", "what was taught", "syllabus covered",
    "गृहकार्य", "होमवर्क", "असाइनमेंट", "काम",
]

_HOMEWORK_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _HOMEWORK_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


# Subject keywords → subject name mapping (for routing to subject teachers)
_SUBJECT_KEYWORDS: dict[str, str] = {
    "hindi": "Hindi",
    "हिंदी": "Hindi",
    "हिन्दी": "Hindi",
    "english": "English",
    "अंग्रेजी": "English",
    "math": "Maths",
    "maths": "Maths",
    "mathematics": "Maths",
    "गणित": "Maths",
    "science": "Science",
    "विज्ञान": "Science",
    "sst": "Social Science",
    "social studies": "Social Science",
    "social science": "Social Science",
    "evs": "EVS",
    "computer": "Computer",
    "computers": "Computer",
    "sanskrit": "Sanskrit",
    "संस्कृत": "Sanskrit",
    "physics": "Physics",
    "chemistry": "Chemistry",
    "biology": "Biology",
    "accounts": "Accounts",
    "accountancy": "Accounts",
    "economics": "Economics",
    "business studies": "Business Studies",
    "geography": "Geography",
    "history": "History",
    "political science": "Political Science",
}


def _extract_subject(message_text: str) -> str:
    """Extract subject name from a homework query message."""
    msg_low = message_text.lower()
    for kw, subject in _SUBJECT_KEYWORDS.items():
        if kw in msg_low:
            return subject
    return ""


async def detect_and_handle_homework_query(
    sender: str, message_text: str, reply_to: str, media_info: dict | None = None
) -> bool:
    """Detect homework-related queries and forward to the class/subject teacher.

    Smart flow:
    1. Detect homework keywords in the message.
    2. Auto-detect parent's child class from phone number (pi_sheet_students).
    3. If grade explicitly mentioned in message, use that instead.
    4. Extract subject from message (e.g. "Hindi homework" -> Hindi).
    5. Forward to subject teacher if available, else class teacher.
    6. Only ask for class if parent has multiple children AND no grade in message.
    Returns True if handled, False otherwise.
    """
    if not _HOMEWORK_RE.search(message_text):
        return False

    logger.info(f"Homework query detected from {sender}: {message_text[:100]}")

    # Step 1: Try to extract grade explicitly from the message
    grade_from_message = None
    grade_match = _GRADE_EXTRACT_RE.search(message_text)
    if grade_match:
        teacher_entry = find_teacher_by_grade(message_text)
        if teacher_entry:
            grade_from_message = teacher_entry["grade"]

    # Step 2: Auto-detect parent's child class from phone number
    children = await _lookup_parent_child_class(sender)
    detected_grade = None

    if grade_from_message:
        # Parent explicitly mentioned a grade — use it
        detected_grade = grade_from_message
    elif len(children) == 1:
        # Exactly one child — auto-detect grade
        detected_grade = children[0]["grade"]
        logger.info(
            f"Auto-detected grade {detected_grade} for parent {sender} "
            f"(child: {children[0]['student_name']})"
        )
    elif len(children) > 1:
        # Multiple children — ask which child (NOT which class)
        await save_pending_query(sender, reply_to, message_text)
        child_list = "\n".join(
            f"- {c['student_name']} ({c['grade']})" for c in children
        )
        ask_msg = (
            f"{_greeting(sender)},\n\n"
            "We have found multiple wards linked to your number:\n"
            f"{child_list}\n\n"
            "Kindly specify which ward's homework you are inquiring about "
            "by replying with their name or class.\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\nPP International School"
        )
        await send_whatsapp_message(reply_to, ask_msg)
        return True

    if not detected_grade:
        # No children found for this number AND no grade in message
        # Fall back: ask for class/section
        await save_pending_query(sender, reply_to, message_text)
        ask_msg = (
            f"{_greeting(sender)},\n\n"
            "Thank you for your query regarding homework. "
            "Could you please share your ward's class and section "
            "(e.g. Grade 5A, Nursery 1, Prep 2) so that I can forward "
            "your query to the class teacher?\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\nPP International School"
        )
        await send_whatsapp_message(reply_to, ask_msg)
        return True

    # Step 3: Find the class teacher for this grade
    teacher_entry = find_teacher_by_grade(detected_grade)
    if not teacher_entry:
        # Try fuzzy match
        for entry in TEACHER_DATA:
            entry_grade_low = entry["grade"].lower().replace(" ", "")
            detected_low = detected_grade.lower().replace(" ", "")
            if detected_low == entry_grade_low or detected_low in entry_grade_low:
                teacher_entry = entry
                break

    if not teacher_entry:
        logger.warning(f"No teacher found for grade {detected_grade}")
        await send_whatsapp_message(
            reply_to,
            f"{_greeting(sender)},\n\n"
            f"We could not find a class teacher for {detected_grade} in our records. "
            "For further assistance, please contact:\n"
            "School Helpline: 8800935552\n"
            "Ms. Harpreet Kaur (Administration Incharge): 9599488106\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\nPP International School"
        )
        return True

    # Step 4: Extract subject from the message
    subject = _extract_subject(message_text)

    # Use class teacher info (subject-specific teachers are not in TEACHER_DATA,
    # but the class teacher can route the query to the subject teacher)
    teacher_name = teacher_entry["teacher"].split("/")[0].strip()
    teacher_grade = teacher_entry["grade"]
    teacher_email = teacher_entry.get("email", "")
    teacher_phone = teacher_entry.get("whatsapp", "")

    subject_label = f" ({subject})" if subject else ""

    # Build parent identity label for the teacher
    hw_parent_label = "A parent"
    if children:
        child = children[0] if len(children) == 1 else next(
            (c for c in children if c["grade"].lower().replace(" ", "") == detected_grade.lower().replace(" ", "")),
            children[0],
        )
        hw_parent_label = f"Parent of {child['student_name']} ({child['grade']})"

    # Compose the forwarding message
    forward_msg = (
        f"Dear {teacher_name},\n\n"
        f"{hw_parent_label} has inquired about{subject_label} homework for "
        f"*{teacher_grade}*.\n\n"
        f"Parent's query: \"{message_text}\"\n\n"
        f"Kindly reply with the homework details so that the same can be "
        f"communicated to the parent.\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )

    wa_success = False
    email_success = False

    # Send via WhatsApp
    if teacher_phone:
        teacher_chat = _teacher_chat_id(teacher_phone)
        wa_success = await send_whatsapp_message(teacher_chat, forward_msg)
        if wa_success:
            # Forward any attached media (document, image, etc.) to the teacher
            if media_info:
                from app.services.whatsapp_service import forward_cloud_media_to_recipient
                cloud_mid = media_info.get("cloud_media_id", "")
                if cloud_mid:
                    await forward_cloud_media_to_recipient(
                        media_info, teacher_chat,
                        caption=media_info.get("caption", ""),
                    )
                elif media_info.get("url"):
                    await forward_file_by_url(
                        teacher_chat,
                        media_info["url"],
                        media_info.get("filename", "file"),
                        media_info.get("caption", ""),
                    )
            await save_forwarded_conversation(
                teacher_phone=teacher_phone,
                teacher_name=teacher_name,
                teacher_grade=teacher_grade,
                original_chat_id=reply_to,
                sender_phone=sender,
                original_message=message_text[:500],
            )

    # Send via email
    if teacher_email:
        email_body = (
            f"Dear {teacher_name},\n\n"
            f"{hw_parent_label} has inquired about{subject_label} homework for "
            f"{teacher_grade}.\n\n"
            f"Parent's query: \"{message_text}\"\n\n"
            f"Kindly reply with the homework details so that the same can be "
            f"communicated to the parent.\n\n"
            f"Thank you for your cooperation.\n"
            f"Warm regards,\nPP International School"
        )
        email_success = await send_email_async(
            teacher_email,
            f"Homework Query from {hw_parent_label}{subject_label} — {teacher_grade}",
            email_body,
            "PP International School",
        )

    # Confirm to the parent
    methods = []
    if wa_success:
        methods.append("WhatsApp")
    if email_success:
        methods.append("email")
    method_str = " and ".join(methods) if methods else "the school"

    confirm_msg = (
        f"{_greeting(sender)},\n\n"
        f"Your{subject_label} homework query for *{teacher_grade}* has been "
        f"forwarded to the class teacher ({teacher_name}) via {method_str}.\n\n"
        f"You will receive the response as soon as the teacher replies.\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )
    await send_whatsapp_message(reply_to, confirm_msg)

    logger.info(
        f"Homework query{subject_label} from {sender} forwarded to {teacher_name} "
        f"({teacher_grade}) via {method_str}"
    )
    return True


# ---------------------------------------------------------------------------
# Leave Application Detection & Handling
# ---------------------------------------------------------------------------

_LEAVE_KEYWORDS = [
    "leave", "absent", "won't come", "wont come", "not coming",
    "will not come", "won't be coming", "wont be coming",
    "off", "sick leave", "medical leave", "casual leave",
    "on leave", "taking leave", "apply leave", "leave application",
    "छुट्टी", "अनुपस्थित", "नहीं आएगा", "नहीं आएगी", "लीव",
]

_LEAVE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _LEAVE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Pattern to extract child name, date, reason from leave messages
_LEAVE_DETAIL_RE = re.compile(
    r"(?:my\s+(?:child|son|daughter|ward|kid)\s+)?(?P<name>[A-Z][a-zA-Z\s]+?)"
    r"\s+(?:will\s+be\s+on\s+leave|(?:won'?t|will\s+not)\s+(?:come|be\s+coming)|"
    r"(?:is|will\s+be)\s+(?:absent|on\s+leave))"
    r"(?:\s+on\s+(?P<date>[^\s,]+(?:\s+[^\s,]+)?))??"
    r"(?:\s+(?:due\s+to|because\s+of|reason|for)\s+(?P<reason>.+))?",
    re.IGNORECASE,
)


async def detect_and_handle_leave_application(
    sender: str, message_text: str, reply_to: str, media_info: dict | None = None
) -> bool:
    """Detect leave application messages and process them.

    Flow:
    1. Detect leave-related keywords.
    2. Try to extract child name, date, reason from the message.
    3. Look up child in PI Sheet to find their class and teacher.
    4. Save to leave_applications table.
    5. Forward to class teacher via WhatsApp + email.
    6. Confirm receipt to parent.
    Returns True if handled, False otherwise.
    """
    if not _LEAVE_RE.search(message_text):
        return False

    logger.info(f"Leave application detected from {sender}: {message_text[:100]}")

    # Try structured extraction first
    detail_match = _LEAVE_DETAIL_RE.search(message_text)
    child_name = detail_match.group("name").strip() if detail_match and detail_match.group("name") else ""
    leave_date = detail_match.group("date").strip() if detail_match and detail_match.group("date") else ""
    reason = detail_match.group("reason").strip() if detail_match and detail_match.group("reason") else ""

    # If structured extraction failed, try to find a student name from the DB
    # by matching any known student name in the message
    grade = ""
    teacher_entry = None

    if child_name:
        # Look up the child in PI Sheet
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT student_name, grade FROM pi_sheet_students WHERE UPPER(student_name) LIKE ?",
                (f"%{child_name.upper()}%",),
            )
            row = await cursor.fetchone()
            if row:
                child_name = row[0]
                grade = row[1]
                teacher_entry = find_teacher_by_grade(grade)
        finally:
            await db.close()

    # If we still don't have essential info, try to find grade from message
    if not teacher_entry:
        teacher_entry = find_teacher_by_grade(message_text)
        if teacher_entry:
            grade = teacher_entry["grade"]

    # If we couldn't find the child or teacher, ask for details
    if not child_name or not teacher_entry:
        await save_pending_query(sender, reply_to, message_text)
        ask_msg = (
            f"{_greeting(sender)},\n\n"
            "Thank you for informing us about the leave. "
            "To process your leave application, could you please provide:\n\n"
            "1. Your ward's *full name*\n"
            "2. *Class and section* (e.g. Grade 5A)\n"
            "3. *Date(s)* of leave\n"
            "4. *Reason* for leave\n\n"
            "For example: \"My child Nitya Gupta of Grade 3C will be on leave "
            "on 5th April due to fever.\"\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\nPP International School"
        )
        await send_whatsapp_message(reply_to, ask_msg)
        return True

    teacher_name = teacher_entry["teacher"].split("/")[0].strip()
    teacher_email = teacher_entry.get("email", "")
    teacher_phone = teacher_entry.get("whatsapp", "")

    # Default date to "as mentioned" if not extracted
    if not leave_date:
        leave_date = "as mentioned by parent"
    if not reason:
        reason = "not specified"

    # Save to leave_applications table
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO leave_applications "
            "(parent_phone, child_name, grade, leave_date, reason, status, "
            "teacher_name, teacher_phone) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (sender, child_name, grade, leave_date, reason,
             teacher_name, teacher_phone),
        )
        await db.commit()
    finally:
        await db.close()

    # Forward to class teacher
    forward_msg = (
        f"Dear {teacher_name},\n\n"
        f"A leave application has been received:\n\n"
        f"Student: *{child_name}*\n"
        f"Class: *{grade}*\n"
        f"Date: *{leave_date}*\n"
        f"Reason: {reason}\n"
        f"Parent Phone: {sender}\n\n"
        f"Please reply with 'Approved' or 'Rejected' to respond to this "
        f"leave application. Your response will be communicated to the parent.\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )

    wa_success = False
    email_success = False

    if teacher_phone:
        teacher_chat = _teacher_chat_id(teacher_phone)
        wa_success = await send_whatsapp_message(teacher_chat, forward_msg)
        if wa_success:
            # Forward any attached media (e.g. medical certificate) to the teacher
            if media_info:
                from app.services.whatsapp_service import forward_cloud_media_to_recipient
                cloud_mid = media_info.get("cloud_media_id", "")
                if cloud_mid:
                    await forward_cloud_media_to_recipient(
                        media_info, teacher_chat,
                        caption=media_info.get("caption", ""),
                    )
                elif media_info.get("url"):
                    await forward_file_by_url(
                        teacher_chat,
                        media_info["url"],
                        media_info.get("filename", "file"),
                        media_info.get("caption", ""),
                    )
            await save_forwarded_conversation(
                teacher_phone=teacher_phone,
                teacher_name=teacher_name,
                teacher_grade=grade,
                original_chat_id=reply_to,
                sender_phone=sender,
                original_message=f"LEAVE APPLICATION: {child_name} - {leave_date} - {reason}",
            )

    if teacher_email:
        email_body = (
            f"Dear {teacher_name},\n\n"
            f"A leave application has been received:\n\n"
            f"Student: {child_name}\n"
            f"Class: {grade}\n"
            f"Date: {leave_date}\n"
            f"Reason: {reason}\n"
            f"Parent Phone: {sender}\n\n"
            f"Please reply to acknowledge this leave application.\n\n"
            f"Thank you for your cooperation.\n"
            f"Warm regards,\nPP International School"
        )
        email_success = await send_email_async(
            teacher_email,
            f"Leave Application — {child_name} ({grade})",
            email_body,
            "PP International School",
        )

    # Confirm to parent
    methods = []
    if wa_success:
        methods.append("WhatsApp")
    if email_success:
        methods.append("email")
    method_str = " and ".join(methods) if methods else "the school"

    confirm_msg = (
        f"{_greeting(sender)},\n\n"
        f"Your leave application has been received and forwarded to "
        f"the class teacher ({teacher_name}) via {method_str}.\n\n"
        f"Details:\n"
        f"Student: *{child_name}*\n"
        f"Class: *{grade}*\n"
        f"Date: *{leave_date}*\n"
        f"Reason: {reason}\n\n"
        f"You will be notified once the teacher responds.\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )
    await send_whatsapp_message(reply_to, confirm_msg)

    logger.info(
        f"Leave application from {sender} for {child_name} ({grade}) "
        f"forwarded to {teacher_name} via {method_str}"
    )
    return True


# ---------------------------------------------------------------------------
# Parent-to-Class-Teacher Routing — auto-route parent queries to the
# assigned class teacher based on student mapping.
# ---------------------------------------------------------------------------

# Messages that should NOT be forwarded to the teacher (greetings, thanks, etc.)
_SKIP_FORWARD_RE = re.compile(
    r"^(?:hi|hello|hey|ok|okay|thanks|thank you|thankyou|thank u|good morning|"
    r"good evening|good night|bye|haan|ji|theek hai|shukriya|dhanyavaad|namaste|"
    r"hmm|yes|no|nahi|haa|accha|👍|🙏|❤️|😊|"
    r"\[reaction received\]|\[sticker received\]|\[unsupported received\])\s*[.!?]*$",
    re.IGNORECASE,
)


async def try_route_parent_to_class_teacher(
    sender: str, message_text: str, reply_to: str, media_info: dict | None = None,
) -> bool:
    """Auto-route a parent's query to the respective class teacher.

    Flow:
    1. Skip if sender is admin, teacher, or message is trivial.
    2. Look up parent's children from PI sheet.
    3. If one child → find class teacher → forward.
    4. If multiple children → ask which child the query is about.
    5. If unknown parent → return False (fall through to GPT).
    6. Save conversation for two-way reply relay.
    Returns True if handled, False otherwise.
    """
    # Skip admin panel numbers — they should use GPT / direct message flow
    if _is_admin_panel(sender):
        return False

    # Skip if the sender IS a teacher (their messages go through broadcast/relay)
    if _is_teacher_phone(sender):
        return False

    # Skip trivial / greeting messages
    stripped = message_text.strip()
    if not stripped or len(stripped) < 3:
        return False
    if _SKIP_FORWARD_RE.match(stripped):
        return False

    # Look up children linked to this phone number
    children = await _lookup_parent_child_class(sender)
    if not children:
        return False  # Not a known parent — fall through to GPT

    logger.info(
        f"[PARENT→TEACHER] Parent {sender} has {len(children)} child(ren), "
        f"routing query: {message_text[:80]}"
    )

    # --- Multiple children: ask which child the query is about ---
    if len(children) > 1:
        # Check if message mentions a specific grade — use it
        grade_teacher = find_teacher_by_grade(message_text)
        if grade_teacher:
            # Message mentions a grade — find the matching child
            matching_child = next(
                (c for c in children
                 if c["grade"].lower().replace(" ", "")
                 == grade_teacher["grade"].lower().replace(" ", "")),
                None,
            )
            if matching_child:
                return await _forward_query_to_class_teacher(
                    sender, message_text, reply_to, matching_child,
                    grade_teacher, media_info,
                )

        # Check if message mentions a child's name
        msg_lower = message_text.lower()
        for child in children:
            name_parts = child["student_name"].lower().split()
            if any(part in msg_lower for part in name_parts if len(part) > 2):
                teacher_entry = find_teacher_by_grade(child["grade"])
                if teacher_entry:
                    return await _forward_query_to_class_teacher(
                        sender, message_text, reply_to, child,
                        teacher_entry, media_info,
                    )

        # Can't determine which child — ask the parent
        await save_pending_query(sender, reply_to, message_text)
        child_list = "\n".join(
            f"- {c['student_name']} ({c['grade']})" for c in children
        )
        ask_msg = (
            f"{_greeting(sender)},\n\n"
            "We have found multiple wards linked to your number:\n"
            f"{child_list}\n\n"
            "Kindly specify which ward this query is regarding "
            "by replying with their name or class.\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\nPP International School"
        )
        await send_whatsapp_message(reply_to, ask_msg)
        return True

    # --- Single child: auto-route to class teacher ---
    child = children[0]
    teacher_entry = find_teacher_by_grade(child["grade"])
    if not teacher_entry:
        # Try fuzzy match
        for entry in TEACHER_DATA:
            entry_grade_low = entry["grade"].lower().replace(" ", "")
            child_grade_low = child["grade"].lower().replace(" ", "")
            if child_grade_low == entry_grade_low or child_grade_low in entry_grade_low:
                teacher_entry = entry
                break

    if not teacher_entry:
        logger.warning(f"[PARENT→TEACHER] No teacher found for grade {child['grade']}")
        return False  # Fall through to GPT

    return await _forward_query_to_class_teacher(
        sender, message_text, reply_to, child, teacher_entry, media_info,
    )


async def _forward_query_to_class_teacher(
    sender: str,
    message_text: str,
    reply_to: str,
    child: dict,
    teacher_entry: dict,
    media_info: dict | None = None,
) -> bool:
    """Forward a parent's query to the class teacher with full context.

    Sends via WhatsApp and email. Saves conversation for reply relay.
    Returns True on success.
    """
    teacher_name = teacher_entry["teacher"].split("/")[0].strip()
    teacher_grade = teacher_entry["grade"]
    teacher_email = teacher_entry.get("email", "")
    teacher_phone = teacher_entry.get("whatsapp", "")

    if not teacher_email and not teacher_phone:
        logger.warning(f"[PARENT→TEACHER] No contact for teacher of {teacher_grade}")
        msg = (
            f"{_greeting(sender)},\n\n"
            f"The class teacher for *{teacher_grade}* is *{teacher_name}*, "
            f"but we don't have their contact details on file. "
            f"Please contact the school office for assistance.\n\n"
            f"Phone: 011-45161066 / 64 / 63\n"
            f"Email: info@ppischool.in\n\n"
            f"Warm regards,\nPP International School"
        )
        await send_whatsapp_message(reply_to, msg)
        return True

    parent_label = f"Parent of {child['student_name']} ({child['grade']})"

    # Build forwarded message for the teacher
    notification = (
        f"Dear {teacher_name},\n\n"
        f"{parent_label} has sent the following query via the *PPIS Bot*:\n\n"
        f"\U0001f4e9 \"{message_text[:500]}\"\n\n"
        f"Kindly reply to this message and your response will be "
        f"forwarded back to the parent.\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )

    wa_success = False
    email_success = False

    # Download media ONCE — reused for WhatsApp and email
    _dl_bytes2, _dl_mime2 = await _download_media_bytes(media_info)

    # Forward via WhatsApp — try direct message first, fall back to template
    if teacher_phone:
        import asyncio as _asyncio
        from app.services.whatsapp_service import (
            send_cloud_template_message,
            forward_cloud_media_to_recipient,
        )
        chat_id = _teacher_chat_id(teacher_phone)
        teacher_recipient = teacher_phone if not teacher_phone.startswith("91") else teacher_phone
        if len(teacher_recipient) == 10:
            teacher_recipient = "91" + teacher_recipient

        query_msg = (
            f"\U0001f4e9 *Query from {parent_label}:*\n\n"
            f"\"{message_text[:500]}\"\n\n"
            f"_Kindly reply to this message and your response will be "
            f"forwarded back to the parent._"
        )

        # Try sending the query directly first (works if conversation
        # window is already open). This avoids the confusing template.
        wa_success = await send_whatsapp_message(chat_id, query_msg)
        if wa_success:
            logger.info(f"[PARENT→TEACHER] Direct query sent to {teacher_name} ({teacher_phone})")
        else:
            # Conversation window closed — send template to open a
            # business-initiated window, then resend the actual query.
            logger.info(f"[PARENT→TEACHER] Direct msg failed, sending template for {teacher_name}")
            tmpl_ok = await send_cloud_template_message(
                teacher_recipient, "ppis_class_assignment",
                body_params=[f"Query from {parent_label}", message_text[:400]],
            )
            if tmpl_ok:
                # Wait for template delivery to open conversation window
                await _asyncio.sleep(4)
                wa_success = await send_whatsapp_message(chat_id, query_msg)
                if wa_success:
                    logger.info(f"[PARENT→TEACHER] Query sent after template to {teacher_name}")
                else:
                    wa_success = True  # template itself was delivered
                    logger.warning(f"[PARENT→TEACHER] Query after template failed for {teacher_name}, template delivered")
            else:
                logger.error(f"[PARENT→TEACHER] Both direct msg and template failed for {teacher_name}")

        # Send media attachment
        if wa_success and media_info:
            await _asyncio.sleep(3)
            media_caption = media_info.get("caption", "")
            media_fwd_ok = False
            try:
                media_fwd_ok = await forward_cloud_media_to_recipient(
                    media_info, chat_id, caption=media_caption,
                )
            except Exception as mf_exc:
                logger.error(f"[PARENT→TEACHER] Media forward error: {mf_exc}")
            if media_fwd_ok:
                logger.info(f"[PARENT→TEACHER] Media sent to {teacher_name}")
            else:
                logger.warning(f"[PARENT→TEACHER] WhatsApp media failed for {teacher_name} — email has attachment")

        if wa_success:
            # Save conversation for reply relay
            await save_forwarded_conversation(
                teacher_phone=teacher_phone,
                teacher_name=teacher_name,
                teacher_grade=teacher_grade,
                original_chat_id=reply_to,
                sender_phone=sender,
                original_message=message_text[:500],
            )

    # Forward via email (reliable channel with full query + attachment)
    if teacher_email:
        email_body = (
            f"Dear {teacher_name},\n\n"
            f"{parent_label} has sent the following query via the PPIS Bot:\n\n"
            f"\"{message_text[:500]}\"\n\n"
            f"Kindly reply to this email and your response will be "
            f"forwarded back to the parent.\n\n"
            f"Regards,\nPPIS Bot"
        )
        _email_att2 = _make_email_attachments(media_info, _dl_bytes2, _dl_mime2)
        email_success = await send_email_async(
            teacher_email,
            f"PPIS Bot: Query from {parent_label}",
            email_body,
            "PP International School",
            attachments=_email_att2 or None,
        )
        if email_success:
            logger.info(
                f"[PARENT→TEACHER] Forwarded to {teacher_name} ({teacher_email}) via email"
            )

    # --- Relay tracking: log parent→teacher class teacher routing ---
    try:
        from app.services.relay_service import (
            save_relay_message as _save_relay_ct,
            auto_tag_message as _auto_tag_ct,
        )
        from app.services.attachment_service import (
            save_attachment_metadata as _save_att_ct,
            classify_media_type as _classify_att_ct,
        )
        _tags_ct = _auto_tag_ct(message_text, media_info is not None)
        _rid_ct = await _save_relay_ct(
            sender_phone=sender, sender_role="parent",
            receiver_phone=teacher_phone or "", receiver_role="teacher",
            direction="parent_to_teacher",
            message_text=message_text[:1000],
            message_type="attachment" if media_info else "text",
            grade=teacher_grade,
            student_name=child.get("student_name", ""),
            delivery_status="delivered" if (wa_success or email_success) else "failed",
            email_sent=email_success,
            tags=_tags_ct,
        )
        if media_info and _rid_ct:
            await _save_att_ct(
                relay_message_id=_rid_ct,
                file_type=_classify_att_ct(media_info.get("mime_type", ""), media_info.get("filename", "")),
                file_name=media_info.get("filename", "attachment"),
                mime_type=media_info.get("mime_type", "application/octet-stream"),
                file_size=media_info.get("file_size", 0),
                cloud_media_id=media_info.get("cloud_media_id", ""),
            )
    except Exception as _relay_ct_err:
        logger.error(f"Relay tracking error (class_teacher_route): {_relay_ct_err}")

    # Confirm to the parent
    methods = []
    if wa_success:
        methods.append("WhatsApp")
    if email_success:
        methods.append("email")
    method_str = " and ".join(methods) if methods else "the school"

    confirm_msg = (
        f"{_greeting(sender)},\n\n"
        f"Your query has been forwarded to *{teacher_name}* "
        f"(Class Teacher, {teacher_grade}) via {method_str}.\n\n"
        f"You will receive the response as soon as the teacher replies.\n\n"
        f"Thank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )
    await send_whatsapp_message(reply_to, confirm_msg)

    logger.info(
        f"[PARENT→TEACHER] Query from {sender} ({parent_label}) → "
        f"{teacher_name} ({teacher_grade}) via {method_str}"
    )
    return True


# ---------------------------------------------------------------------------
# Teacher Homework Broadcast — teacher sends homework, bot relays to parents
# ---------------------------------------------------------------------------

_TEACHER_BROADCAST_KEYWORDS = [
    "homework", "home work", "hw", "assignment", "classwork", "class work",
    "worksheet", "work sheet", "project", "pending work", "revision",
    "test tomorrow", "exam", "syllabus", "chapter", "exercise",
    "complete", "submit", "bring", "prepare", "practice",
    "summary sheet", "summary", "report card", "progress report",
    "circular", "notice", "update for parents", "daily report",
    "गृहकार्य", "होमवर्क", "असाइनमेंट", "काम", "परीक्षा",
]

_TEACHER_BROADCAST_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _TEACHER_BROADCAST_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _is_teacher_phone(sender: str) -> dict | None:
    """Check if sender phone matches a teacher in TEACHER_DATA.

    Returns the TEACHER_DATA entry if matched, else None.
    """
    digits = re.sub(r"\D", "", sender)
    last10 = digits[-10:] if len(digits) >= 10 else digits
    for entry in TEACHER_DATA:
        t_phone = entry.get("whatsapp", "")
        if not t_phone:
            continue
        t_digits = re.sub(r"\D", "", t_phone)
        t_last10 = t_digits[-10:] if len(t_digits) >= 10 else t_digits
        if last10 == t_last10:
            return entry
    return None


async def _get_parents_by_grade(grade: str) -> list[str]:
    """Query pi_sheet_students for all unique parent phone numbers in a grade.

    Returns a de-duplicated list of phone numbers (father + mother).
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT father_mobile, mother_mobile FROM pi_sheet_students WHERE grade = ?",
            (grade,),
        )
        rows = await cursor.fetchall()
        phones: set[str] = set()
        for row in rows:
            for col in ("father_mobile", "mother_mobile"):
                raw = (row[col] or "").strip()
                if not raw:
                    continue
                digits = re.sub(r"\D", "", raw)
                if len(digits) < 10:
                    continue
                last10 = digits[-10:] if len(digits) >= 10 else digits
                phones.add(last10)
        return sorted(phones)
    finally:
        await db.close()


async def _get_parents_by_grade_fuzzy(grade_fragment: str) -> tuple[str, list[str]]:
    """Try to find parents matching a grade fragment (e.g. '3C' -> 'Grade 3C').

    Returns (matched_grade, parent_phones) or ('', []) if not found.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT DISTINCT grade FROM pi_sheet_students",
        )
        all_grades = [row["grade"] for row in await cursor.fetchall()]
    finally:
        await db.close()

    # Normalise fragment for matching
    frag_lower = grade_fragment.lower().replace(" ", "")
    for g in all_grades:
        g_lower = g.lower().replace(" ", "")
        if frag_lower == g_lower:
            parents = await _get_parents_by_grade(g)
            return g, parents

    # Fallback: substring match but require the match to be followed by
    # a non-digit (or end-of-string) so "grade1" doesn't match "grade10a".
    import re
    for g in all_grades:
        g_lower = g.lower().replace(" ", "")
        pattern = re.escape(frag_lower) + r"(?!\d)"
        if re.search(pattern, g_lower):
            parents = await _get_parents_by_grade(g)
            return g, parents

    return "", []


async def detect_and_handle_teacher_homework_broadcast(
    sender: str, message_text: str, reply_to: str, media_info: dict | None = None,
) -> bool:
    """Detect when a teacher sends homework and broadcast it to all parents of that class.

    Flow:
    1. Check if the sender is a teacher (phone in TEACHER_DATA).
    2. Check if the message contains homework/assignment keywords.
    3. Extract the target grade from the message, or default to the teacher's own grade.
    4. Look up all parents of that grade from pi_sheet_students.
    5. Forward the homework message to every parent phone.
    6. Send confirmation back to the teacher.
    Returns True if handled, False otherwise.
    """
    teacher_entry = _is_teacher_phone(sender)
    if teacher_entry is None:
        return False

    # Check for homework-related keywords OR teacher sent media without caption
    # (placeholder text like "[Document shared]" means teacher sent a file with no text)
    is_placeholder = message_text.strip() in (
        "[Document shared]", "[Image shared]", "[Video shared]",
        "[Audio shared]", "[Sticker shared]",
    )
    has_broadcast_keywords = _TEACHER_BROADCAST_RE.search(message_text)
    has_media = media_info is not None and media_info.get("cloud_media_id")

    # Trigger broadcast if: keywords match OR teacher sent media-only (no caption)
    if not has_broadcast_keywords and not (is_placeholder and has_media):
        return False

    teacher_name = teacher_entry["teacher"].split("/")[0].strip()
    teacher_grade = teacher_entry["grade"]

    logger.info(
        f"Homework broadcast detected from teacher {teacher_name} ({sender}): "
        f"{message_text[:120]}"
    )

    # Try to extract explicit grade from the message; fall back to teacher's own grade
    grade_match = _GRADE_EXTRACT_RE.search(message_text)
    target_grade = ""
    if grade_match:
        if grade_match.group(1):  # grade N [section]
            grade_num = grade_match.group(1)
            section = (grade_match.group(2) or "").upper()
            target_grade = f"Grade {grade_num}{section}" if section else f"Grade {grade_num}"
        elif grade_match.group(3) is not None:  # nursery N
            target_grade = f"Nursery {grade_match.group(3)}" if grade_match.group(3) else "Nursery"
        elif grade_match.group(4) is not None:  # prep N
            target_grade = f"Prep {grade_match.group(4)}" if grade_match.group(4) else "Prep"
        else:
            target_grade = "Popsicles"

    if not target_grade:
        target_grade = teacher_grade

    # Look up parents
    matched_grade, parent_phones = await _get_parents_by_grade_fuzzy(target_grade)
    if not parent_phones:
        # Try exact match with teacher's own grade as last resort
        parent_phones = await _get_parents_by_grade(teacher_grade)
        matched_grade = teacher_grade if parent_phones else ""

    if not parent_phones:
        await send_whatsapp_message(
            reply_to,
            f"Dear {teacher_name},\n\n"
            f"No parent contacts were found for {target_grade} in the school records. "
            f"Please check the class name and try again.\n\n"
            f"Thank you for your cooperation.\n"
            f"Warm regards,\nPP International School",
        )
        return True

    # Compose the homework content (truncate if too long for template param)
    # If teacher sent media-only (placeholder text), use a descriptive message instead
    hw_content = message_text.strip()
    if hw_content in (
        "[Document shared]", "[Image shared]", "[Video shared]",
        "[Audio shared]", "[Sticker shared]",
    ):
        media_type_label = {
            "[Document shared]": "document",
            "[Image shared]": "image",
            "[Video shared]": "video",
            "[Audio shared]": "audio",
        }.get(hw_content, "file")
        hw_content = f"Please find the attached {media_type_label} shared by your class teacher."
    if len(hw_content) > 900:
        hw_content = hw_content[:900] + "..."

    # Broadcast to all parents using template messages (required outside 24-hr window)
    from app.services.whatsapp_service import (
        send_cloud_template_message, get_whatsapp_provider,
        forward_cloud_media_to_recipient,
    )
    import asyncio

    # If teacher sent media, download it once and re-upload once for sharing
    # We'll cache the re-uploaded media_id so we don't re-upload per parent.
    cached_media_id: str | None = None
    media_cloud_type = "document"
    media_filename = "file"
    if media_info and media_info.get("cloud_media_id"):
        from app.services.whatsapp_service import download_cloud_media, upload_media_bytes_cloud
        media_bytes, mime_type = await download_cloud_media(media_info["cloud_media_id"])
        if media_bytes:
            media_filename = media_info.get("filename", "file")
            cached_media_id = await upload_media_bytes_cloud(media_bytes, mime_type, media_filename)
            del media_bytes  # free memory
            internal_type = media_info.get("type", "")
            type_map = {"imageMessage": "image", "videoMessage": "video",
                        "documentMessage": "document", "audioMessage": "audio"}
            media_cloud_type = type_map.get(internal_type, "document")
            logger.info(f"Cached re-uploaded media for broadcast: id={cached_media_id}, type={media_cloud_type}")

    sent_count = 0
    fail_count = 0
    for phone in parent_phones:
        recipient = f"91{phone}" if len(phone) == 10 else phone
        if get_whatsapp_provider() == "cloud":
            # Use approved template — try ppis_homework_update first,
            # fall back to ppis_class_assignment if homework template not yet approved
            success = await send_cloud_template_message(
                recipient,
                "ppis_homework_update",
                body_params=[matched_grade, hw_content, teacher_name],
            )
            if not success:
                # Fallback: use ppis_class_assignment (UTILITY, APPROVED)
                fallback_text = f"HW from {teacher_name}"
                success = await send_cloud_template_message(
                    recipient,
                    "ppis_class_assignment",
                    body_params=[fallback_text, matched_grade],
                )
            # Also send the media file if the teacher attached one
            # NOTE: add a delay so the template message opens the 24-hour
            # conversation window before we send a regular media message.
            if success and cached_media_id:
                await asyncio.sleep(3)
                from app.services.whatsapp_service import send_cloud_media
                caption = f"From {teacher_name} ({matched_grade})"
                media_sent = await send_cloud_media(
                    recipient, media_cloud_type,
                    media_id=cached_media_id, caption=caption,
                    filename=media_filename if media_cloud_type == "document" else "",
                )
                if not media_sent:
                    # Retry once after a longer delay
                    logger.warning(f"Media send failed for {recipient}, retrying after 3s...")
                    await asyncio.sleep(3)
                    await send_cloud_media(
                        recipient, media_cloud_type,
                        media_id=cached_media_id, caption=caption,
                        filename=media_filename if media_cloud_type == "document" else "",
                    )
        else:
            # Green API path — plain text is fine (linked device)
            parent_msg = (
                f"Dear Parent,\n\n"  # broadcast to parents; sender is teacher here
                f"The following message has been shared by the class teacher "
                f"of {matched_grade} ({teacher_name}):\n\n"
                f"---\n{hw_content}\n---\n\n"
                f"Thank you for your cooperation.\n"
                f"Warm regards,\nPP International School"
            )
            success = await send_whatsapp_message(recipient, parent_msg)
            # Forward media via Green API
            if success and media_info and media_info.get("url"):
                await forward_file_by_url(
                    recipient, media_info["url"],
                    media_info.get("filename", "file"),
                    f"From {teacher_name} ({matched_grade})",
                )
        if success:
            sent_count += 1
        else:
            fail_count += 1
        # Small delay between messages (0.5s) to avoid rate limits
        await asyncio.sleep(0.5)

    # Send confirmation to the teacher
    confirm_msg = (
        f"Dear {teacher_name},\n\n"
        f"Your homework message for {matched_grade} has been successfully "
        f"shared with {sent_count} parent(s)."
    )
    if fail_count > 0:
        confirm_msg += f" ({fail_count} delivery attempt(s) failed.)"
    confirm_msg += (
        f"\n\nThank you for your cooperation.\n"
        f"Warm regards,\nPP International School"
    )
    await send_whatsapp_message(reply_to, confirm_msg)

    logger.info(
        f"Homework broadcast from {teacher_name} for {matched_grade}: "
        f"{sent_count} sent, {fail_count} failed out of {len(parent_phones)} parents"
    )
    return True


# ---------------------------------------------------------------------------
# Admin Panel — privileged numbers with full camera/media access
# These numbers can request snapshots of ANY camera (classrooms + non-classroom
# locations like library, reception, principal room, etc.).
# Their numbers are NEVER shared by the bot (except Harpreet who is also admin).
# ---------------------------------------------------------------------------

ADMIN_PANEL_NUMBERS: set[str] = {
    "9971166562",   # Mr. Rahul Gupta
    "9910034550",   # Ms. Purnima Gupta
    "9599488106",   # Ms. Harpreet Kaur
    "8076455224",    # Ms. Alisha Ahuja
}


def _is_admin_panel(sender: str) -> bool:
    """Check if the sender is an admin panel number (full camera access)."""
    digits = re.sub(r"\D", "", sender)
    last10 = digits[-10:] if len(digits) >= 10 else digits
    return last10 in ADMIN_PANEL_NUMBERS


def _greeting(sender: str) -> str:
    """Return 'Dear Admin' for admin panel numbers, 'Dear Parent' for others."""
    return "Dear Admin" if _is_admin_panel(sender) else "Dear Parent"


def _extract_location_from_message(message_text: str) -> str | None:
    """Extract any camera location name from message text for admin panel requests.

    Handles both classroom names (Grade 3C, Nursery 1) and non-classroom
    locations (library, reception, principal room, assembly ground, etc.).

    The location keywords MUST match the actual camera mapping entries stored
    in the cloud database (agent_camera_mapping table).
    """
    # First try the standard classroom extraction
    classroom = _extract_classroom_from_message(message_text)
    if classroom:
        return classroom

    # For admin panel: match non-classroom locations from the camera mapping.
    # These MUST match the actual location names in the cloud DB exactly.
    # Source of truth: School Camera Details - 24-04-2026.xls "All Mix" tab
    # (91 unique classroom names, 123 total entries across 3 DVRs).
    msg_upper = message_text.upper()

    # --- Exact location keywords (match cloud DB camera_mapping keys) ---
    # Each tuple is (UPPERCASE_SEARCH_KEY, exact_cloud_db_key).
    # Longest first so multi-word entries match before their substrings.
    location_keywords: list[tuple[str, str]] = [
        # Gallery entries (DVR 1 first-floor & DVR 2 ground-floor)
        ("GALLERY LIB 8", "GALLERY LIB 8"),
        ("GALLERY LIB 7", "GALLERY LIB 7"),
        ("GALLERY LIB 6", "GALLERY LIB 6"),
        ("GALLERY LIB 5", "GALLERY LIB 5"),
        ("GALLERY LIB 4", "GALLERY LIB 4"),
        ("GALLERY LIB 3", "GALLERY LIB 3"),
        ("GALLERY LIB 2", "GALLERY LIB 2"),
        ("GALLERY LIB 1", "GALLERY LIB 1"),
        ("GALLERY MID 6", "GALLERY MID 6"),
        ("GALLERY MID 5", "GALLERY MID 5"),
        ("GALLERY MID 4", "GALLERY MID 4"),
        ("GALLERY MID 3", "GALLERY MID 3"),
        ("GALLERY MID 2", "GALLERY MID 2"),
        ("GALLERY MID 1", "GALLERY MID 1"),
        ("GALLERY MID", "GALLERY MID"),
        # Multi-word non-classroom locations (exact DB keys)
        ("PARK GENERATOR SIDE", "PARK GENERATOR SIDE"),
        ("BUS PARKING SIDE", "BUS PARKING SIDE"),
        ("DANCE ROOM BASEMENT", "DANCE ROOM BASEMENT"),
        ("DRESS ROOM BASEMENT", "Dress Room Basement"),
        ("MINI COMPUTER LAB", "MINI COMPUTER LAB"),
        ("ACADEMIC COORDINATOR", "Academic Coordinator"),
        ("ADMISSION ROOM C1", "Admission Room C1"),
        ("ACTIVITY ROOM C2", "ACTIVITY ROOM C2"),
        ("ACTIVITY ROOM C1", "ACTIVITY ROOM C1"),
        ("ADMIN ROOM C1", "Admin Room C1"),
        ("ACCOUNTS ROOM", "Accounts Room"),
        ("COMPUTER LAB 2", "COMPUTER LAB 2"),
        ("COMPUTER LAB", "COMPUTER LAB"),
        ("TEACHER STAFF 2", "TEACHER STAFF 2"),
        ("TEACHER STAFF 1", "TEACHER STAFF 1"),
        ("SCIENCE LAB 2", "SCIENCE LAB 2"),
        ("SCIENCE LAB 1", "SCIENCE LAB 1"),
        ("LIBRARY LAB 2", "LIBRARY LAB 2"),
        ("LIBRARY LAB 1", "LIBRARY LAB 1"),
        ("MATH LAB 2", "MATH LAB 2"),
        ("MATH LAB 1", "MATH LAB 1"),
        ("DISPERSAL EXIT", "DISPERSAL EXIT"),
        ("PARK GENERATOR", "PARK GENERATOR"),
        ("PRINCIPAL ROOM", "Principal Room"),
        ("EDUCOMP ROOM", "EDUCOMP ROOM"),
        ("GERMAN ROOM", "GERMAN ROOM"),
        ("MUSICE ROOM", "MUSICE ROOM"),
        ("ENTRY GATE-1", "ENTRY GATE-1"),
        ("ENTRY GATE- 2", "ENTRY GATE- 2"),
        ("ENTRY GATE", "ENTRY GATE- 2"),
        ("ASSEMBLY AREA", "ASSEMBLY AREA"),
        ("SPORTS ROOM", "SPORTS ROOM"),
        ("RECEPTION C4", "Reception C4"),
        ("RECEPTION C3", "Reception C3"),
        ("RECEPTION C2", "Reception C2"),
        ("RECEPTION C1", "Reception C1"),
        ("ART ROOM", "ART ROOM"),
        ("PARK SWING", "PARK SWING"),
        ("PARK BACK", "PARK BACK"),
        ("PARK GATE", "PARK GATE"),
        # Short entries (exact DB case)
        ("R 1  3  F", "R 1  3  F"),
        ("R 2 F", "r 2 f"),
        ("R3 M", "r3 m"),
        ("L 3 M", "l 3 m"),
        ("POPSICLES", "Popsicles"),
    ]
    for search_key, db_key in location_keywords:
        if search_key in msg_upper:
            return db_key

    # --- Fuzzy single-word matches for common short names ---
    # Maps user-friendly short words to actual camera mapping keys.
    # Values are the EXACT cloud DB key (case-sensitive).
    short_map: list[tuple[str, str]] = [
        # Multi-word first (longest match wins)
        ("MAIN GATE", "ENTRY GATE- 2"),
        ("ENTRY GATE", "ENTRY GATE- 2"),
        ("STAFF ROOM", "TEACHER STAFF 1"),
        ("STAFFROOM", "TEACHER STAFF 1"),
        ("COMPUTER LAB", "COMPUTER LAB"),
        ("SCIENCE LAB", "SCIENCE LAB 1"),
        ("MATH LAB", "MATH LAB 1"),
        ("BUS PARKING", "BUS PARKING SIDE"),
        ("MUSIC ROOM", "MUSICE ROOM"),
        # Single-word
        ("LIBRARY", "LIBRARY LAB 1"),
        ("RECEPTION", "Reception C1"),
        ("PRINCIPAL", "Principal Room"),
        ("ADMIN", "Admin Room C1"),
        ("ADMISSION", "Admission Room C1"),
        ("ACCOUNTS", "Accounts Room"),
        ("PARK", "PARK GATE"),
        ("GATE", "ENTRY GATE- 2"),
        ("MUSIC", "MUSICE ROOM"),
        ("GERMAN", "GERMAN ROOM"),
        ("GALLERY", "GALLERY MID 1"),
        ("DANCE", "DANCE ROOM BASEMENT"),
        ("DRESS", "Dress Room Basement"),
        ("ACTIVITY", "ACTIVITY ROOM C1"),
        ("SPORTS", "SPORTS ROOM"),
        ("ASSEMBLY", "ASSEMBLY AREA"),
        ("STAFF", "TEACHER STAFF 1"),
        ("ACADEMIC", "Academic Coordinator"),
        ("BUS", "BUS PARKING SIDE"),
        ("PARKING", "BUS PARKING SIDE"),
        ("EDUCOMP", "EDUCOMP ROOM"),
        ("ART", "ART ROOM"),
        ("DISPERSAL", "DISPERSAL EXIT"),
        ("POPSICLE", "Popsicles"),
        ("NURSERY", "NUR-1"),
        ("NUR", "NUR-1"),
        ("PREP", "PREP-1"),
    ]
    for keyword, location in short_map:
        if re.search(r'\b' + re.escape(keyword) + r'\b', msg_upper):
            return location
    return None


def _find_all_matching_locations(message_text: str) -> list[str]:
    """Find ALL camera location keys that match a general area name.

    For example, 'reception' matches Reception C1, C2, C3, C4.
    This queries the cloud DB for all location keys containing the area name.
    """
    msg_upper = message_text.upper()

    # Map general area names to prefixes used in the DB keys
    area_prefixes: list[tuple[str, str]] = [
        ("RECEPTION", "Reception"),
        ("GALLERY LIB", "GALLERY LIB"),
        ("GALLERY MID", "GALLERY MID"),
        ("ACTIVITY ROOM", "Activity Room"),
        ("ADMIN ROOM", "Admin Room"),
        ("ADMISSION ROOM", "Admission Room"),
    ]

    for search_key, db_prefix in area_prefixes:
        if search_key in msg_upper:
            # Return matching keys from SEED data — prefer C1 and C2 only
            from app.seed_data import SEED_CAMERA_MAPPING
            matches = [
                k for k in SEED_CAMERA_MAPPING
                if k.startswith(db_prefix)
            ]
            if len(matches) > 1:
                # RULE: Only share C1 + C2. Filter to C1/C2 entries only.
                c1_c2 = [m for m in matches if m.endswith("C1") or m.endswith("C2")]
                return c1_c2[:2] if c1_c2 else matches[:2]

    return []


# ---------------------------------------------------------------------------
# Classroom Snapshot Request Detection & Handling
# ---------------------------------------------------------------------------

_SNAPSHOT_KEYWORDS = [
    "photo", "picture", "image", "snapshot", "snap", "camera", "pic",
    "media", "footage", "cctv", "live feed",
    "show me", "show my", "see my", "share photo", "share picture",
    "share media", "send photo", "send picture", "send media",
    "child photo", "classroom photo",
    "class photo", "live photo", "current photo", "latest photo",
    "how is my child", "what is my child doing", "show my child",
    "फोटो", "तस्वीर", "कैमरा", "फ़ोटो", "दिखाओ", "दिखा दो",
]

_SNAPSHOT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _SNAPSHOT_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Phrases that indicate the parent is asking for a live classroom snapshot
# (not school gallery photos or meal menu images)
_SNAPSHOT_INTENT_RE = re.compile(
    r"(?:show|send|get|take|capture|see|want|need|give|share)\s+"
    r"(?:me\s+)?(?:a\s+)?(?:the\s+)?(?:live\s+|current\s+|latest\s+|today'?s?\s+)?"
    r"(?:photo|picture|image|snapshot|snap|pic|media|footage)"
    r"(?:\s+of\s+|\s+from\s+|\s+for\s+)?"
    r"(?:my\s+)?(?:child|ward|kid|son|daughter|class|classroom|room|grade|nursery|prep|popsicle)?",
    re.IGNORECASE,
)

_CHILD_PHOTO_RE = re.compile(
    r"(?:show|send|get|see|want|share)\s+(?:me\s+)?(?:my\s+)?(?:child|ward|kid|son|daughter)'?s?\s+"
    r"(?:photo|picture|image|pic|snapshot|media)",
    re.IGNORECASE,
)

# Pattern: "show/share/send + class/grade/location" WITHOUT requiring photo/picture keyword
# E.g. "show class 10 a", "show reception", "show gallery", "show nursery 1",
# "show 12 b", "show 10 a" (bare grade numbers)
_SHOW_LOCATION_RE = re.compile(
    r"(?:show|share|send)\s+(?:me\s+)?(?:the\s+)?(?:live\s+)?"
    r"(?:photo\s+(?:of\s+)?|picture\s+(?:of\s+)?|image\s+(?:of\s+)?|pic\s+(?:of\s+)?)?"
    r"(?:"
    r"(?:class|grade|कक्षा|क्लास)\s*\d{1,2}\s*[a-cA-C]?"
    r"|\d{1,2}\s*[a-cA-C]"           # bare grade: "show 12 b", "show 10 a"
    r"|(?:nursery|nur)\s*\d?"
    r"|(?:prep)\s*\d?"
    r"|popsicle[s]?"
    r"|reception"
    r"|gallery"
    r"|library"
    r"|principal\s*(?:room)?"
    r"|admin\s*(?:room)?"
    r"|admission\s*(?:room)?"
    r"|accounts\s*(?:room)?"
    r"|assembly\s*(?:ground)?"
    r"|art\s*(?:room)?"
    r"|computer\s*(?:lab)?"
    r"|science\s*(?:lab)?"
    r"|math(?:s)?\s*(?:lab)?"
    r"|music\s*(?:room)?"
    r"|sports\s*(?:room)?"
    r"|dress\s*(?:room)?"
    r"|activity\s*(?:room)?"
    r"|german\s*(?:room)?"
    r"|park\s*(?:gate|swing|back|generator)?"
    r"|main\s*gate"
    r"|entry\s*gate"
    r"|gate"
    r"|bus\s*parking"
    r"|educomp"
    r"|teacher\s*staff"
    r"|staff\s*room"
    r"|academic\s*(?:coordinator)?"
    r"|first\s*floor\s*gallery"
    r"|second\s*floor\s*gallery"
    r"|third\s*floor\s*gallery"
    r")",
    re.IGNORECASE,
)


async def _lookup_parent_child_class(sender_phone: str) -> list[dict]:
    """Look up children and their classes for a parent phone number from the PI Sheet DB."""
    phone_digits = re.sub(r"\D", "", sender_phone)
    # Try last 10 digits
    last10 = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits

    db = await get_db()
    try:
        results = []
        # Search both father and mother phone columns
        cursor = await db.execute(
            "SELECT student_name, grade, father_mobile, mother_mobile "
            "FROM pi_sheet_students WHERE father_mobile LIKE ? OR mother_mobile LIKE ?",
            (f"%{last10}%", f"%{last10}%"),
        )
        rows = await cursor.fetchall()
        for row in rows:
            # Collect all parent phones (supports comma-separated numbers)
            parent_phones = []
            for raw in (row[2] or "", row[3] or ""):
                for segment in raw.split(","):
                    digits = re.sub(r"\D", "", segment)
                    if len(digits) >= 10:
                        parent_phones.append(f"91{digits[-10:]}")
            results.append({
                "student_name": row[0],
                "grade": row[1],
                "parent_phones": parent_phones,
            })
        return results
    finally:
        await db.close()


def _grade_to_camera_key(grade: str) -> str:
    """Convert a PI-sheet grade name to a camera mapping key.

    Examples:
      'Grade 9A'   -> 'GRADE 9A'
      'Nursery 2'  -> 'NUR-2'
      'Prep 3'     -> 'PREP-3'
      'Popsicles'  -> 'Popsicles'
    """
    g = grade.strip()
    g_upper = g.upper()
    m = re.match(r"GRADE\s*(\d{1,2})\s*([A-D])?", g_upper)
    if m:
        num, sec = m.group(1), (m.group(2) or "")
        return f"GRADE {num}{sec}"
    m = re.match(r"(?:NURSERY|NUR)[\s-]*(\d+)", g_upper)
    if m:
        return f"NUR-{m.group(1)}"
    m = re.match(r"PREP[\s-]*(\d+)", g_upper)
    if m:
        return f"PREP-{m.group(1)}"
    if "POPSICLE" in g_upper:
        return "Popsicles"
    return g


def _extract_classroom_from_message(message_text: str) -> str | None:
    """Extract classroom name from message text.

    Returns the EXACT cloud DB key as it appears in the Excel 'All Mix' tab
    Classroom column.  Examples:
      'GRADE 3C'   (uppercase with section)
      'GRADE 10'   (uppercase without section)
      'NUR-1'      (hyphenated nursery)
      'PREP-1'     (hyphenated prep)
      'Popsicles'  (mixed case)
    """
    m = _GRADE_EXTRACT_RE.search(message_text)
    if m:
        if m.group(1):  # grade N [section]
            grade_num = m.group(1)
            section = (m.group(2) or "").upper()
            if section:
                return f"GRADE {grade_num}{section}"
            return f"GRADE {grade_num}"
        elif m.group(3) is not None:  # nursery N
            n = m.group(3)
            return f"NUR-{n}" if n else "NUR-1"
        elif m.group(4) is not None:  # prep N
            n = m.group(4)
            return f"PREP-{n}" if n else "PREP-1"
        elif "popsicle" in message_text.lower():
            return "Popsicles"

    # Fallback: bare grade number with optional section after "of/for/show/share/send"
    # e.g. "show photo of 12 b", "send picture of 3 c", "show 10 a", "show 12 b"
    bare_grade = re.search(
        r"(?:of|for|show|share|send)\s+(?:me\s+)?(?:the\s+)?(?:live\s+)?(?:photo\s+of\s+)?(?:picture\s+of\s+)?(?:image\s+of\s+)?(\d{1,2})\s*([a-cA-C])\b",
        message_text, re.IGNORECASE,
    )
    if bare_grade:
        grade_num = bare_grade.group(1)
        section = bare_grade.group(2).upper()
        return f"GRADE {grade_num}{section}"

    # Fallback 2: bare grade number WITHOUT section after "of/for/show/share/send"
    # e.g. "show photo of 10", "show 12"
    bare_grade_no_section = re.search(
        r"(?:of|for|show|share|send)\s+(?:me\s+)?(?:the\s+)?(?:live\s+)?(?:photo\s+of\s+)?(?:picture\s+of\s+)?(?:image\s+of\s+)?(\d{1,2})\b(?!\s*[a-zA-Z])",
        message_text, re.IGNORECASE,
    )
    if bare_grade_no_section:
        grade_num = bare_grade_no_section.group(1)
        return f"GRADE {grade_num}"

    return None


def _is_snapshot_request(message_text: str, is_admin: bool = False) -> bool:
    """Detect if the message is asking for a live camera snapshot.

    RULE: ANY message that starts with 'show' should be treated as a photo
    sharing request. For admin panel members, ALL locations are accessible.
    For regular parents, only their child's classroom is accessible.
    """
    msg_low = message_text.strip().lower()

    # ---- CRITICAL: Any message beginning with "show" = photo request ----
    if msg_low.startswith("show"):
        return True

    # Direct intent patterns ("send me a photo of...", "share picture of...")
    if _SNAPSHOT_INTENT_RE.search(message_text):
        return True
    if _CHILD_PHOTO_RE.search(message_text):
        return True

    # "share class 10 a", "send reception" etc.
    if _SHOW_LOCATION_RE.search(message_text):
        return True

    # Check for snapshot keyword + context
    if _SNAPSHOT_RE.search(message_text):
        # Classroom words (for regular parents)
        classroom_words = [
            "class", "classroom", "grade", "nursery", "prep", "popsicle",
            "child", "ward", "kid", "son", "daughter", "my",
            "cctv", "footage", "media",
            "कक्षा", "बच्चा", "बच्चे",
        ]
        if any(w in msg_low for w in classroom_words):
            return True

        # Admin panel members: also match non-classroom location words
        if is_admin:
            location_words = [
                "library", "reception", "principal", "admin", "admission",
                "accounts", "assembly", "art room", "computer", "science",
                "math lab", "sports", "dress", "educomp", "music",
                "teacher staff", "activity", "german", "park", "gate",
                "floor", "gallery", "strs", "stairs", "bus parking",
                "academic", "main gate", "entry gate",
            ]
            if any(w in msg_low for w in location_words):
                return True

    # Hindi patterns
    hindi_patterns = [
        r"(?:मेरे\s+)?बच्चे?\s+की?\s+(?:फोटो|फ़ोटो|तस्वीर)",
        r"कक्षा\s+की?\s+(?:फोटो|फ़ोटो|तस्वीर)",
        r"(?:कैमरा|कैमेरा)\s+(?:से\s+)?(?:फोटो|फ़ोटो|तस्वीर)",
    ]
    for pat in hindi_patterns:
        if re.search(pat, message_text, re.IGNORECASE):
            return True

    return False


async def _is_pi_sheet_parent(sender: str) -> bool:
    """Check if the sender's phone number is in the PI Sheet (parent database).

    SECURITY: Only PI Sheet parents and admin panel members may receive
    camera snapshots.  Everyone else is denied.
    """
    children = await _lookup_parent_child_class(sender)
    return len(children) > 0


async def _extract_classroom_for_queue(
    message_text: str, sender: str, is_admin: bool
) -> str | None:
    """Best-effort classroom extraction for queuing when agent is offline.

    Tries message text first, then parent's registered child grade.
    """
    # Try explicit classroom from message
    loc = _extract_classroom_from_message(message_text)
    if loc:
        return loc

    # Try location keywords for admin
    if is_admin:
        loc2 = _extract_location_from_message(message_text)
        if loc2:
            return loc2

    # Fall back to parent's child grade
    children = await _lookup_parent_child_class(sender)
    if len(children) == 1:
        return _grade_to_camera_key(children[0]["grade"])

    return None


async def detect_and_handle_snapshot_request(
    sender: str, message_text: str, reply_to: str
) -> bool:
    """Detect if a parent/admin is requesting a camera snapshot and handle it.

    Admin panel numbers can request ANY camera location (library, reception, etc.).
    Regular parents can only request their child's classroom camera.
    UNKNOWN numbers (not admin, not in PI Sheet) are DENIED.

    Returns True if handled, False if not a snapshot request.
    """
    is_admin = _is_admin_panel(sender)

    if not _is_snapshot_request(message_text, is_admin=is_admin):
        return False

    # ---- STRICT ACCESS CONTROL ----
    # Only admin panel members and parents listed in the PI Sheet may
    # receive camera snapshots.  Everyone else is politely refused.
    if not is_admin and not await _is_pi_sheet_parent(sender):
        logger.warning(
            f"BLOCKED snapshot request from unknown number {sender} "
            f"(not admin, not in PI Sheet): {message_text}"
        )
        await send_whatsapp_message(
            reply_to,
            "Dear User,\n\n"
            "We are unable to process your request. "
            "Access to live campus photos is restricted to registered parents "
            "and school administrators only.\n\n"
            "If you believe this is an error, please contact:\n"
            "School Helpline / Front Desk: 8800935552\n"
            "Ms. Harpreet Kaur (Administration Incharge): 9599488106\n\n"
            "Thank you for your cooperation.\n"
            "Warm regards,\nPP International School"
        )
        return True

    from app.routes.agent_ws import (
        is_agent_connected, request_snapshot, wait_for_agent,
        record_snapshot_success, record_snapshot_failure, should_alert_admin,
        get_health_state, queue_snapshot_request,
    )
    from app.services.whatsapp_service import (
        upload_base64_image_cloud,
        send_cloud_media,
    )

    logger.info(f"Snapshot request detected from {sender} (admin={is_admin}): {message_text}")

    # ---------------------------------------------------------------------------
    # ALWAYS-ACTIVE: Smart retry with queued delivery
    # Instead of saying "system offline", queue the request. The agent
    # auto-reconnects (exponential backoff 5-60s) and queued requests are
    # fulfilled automatically — the parent gets their photo without retrying.
    # ---------------------------------------------------------------------------
    agent_ready = await wait_for_agent(max_wait=30.0)
    if not agent_ready:
        record_snapshot_failure()
        health = get_health_state()
        consecutive = health["consecutive_failures"]
        logger.warning(
            f"Agent not ready for snapshot from {sender} "
            f"(consecutive_failures={consecutive})"
        )

        # Determine the classroom for queuing (best-effort extraction)
        _queue_classroom = _extract_classroom_for_queue(message_text, sender, is_admin)

        if _queue_classroom:
            queued = queue_snapshot_request(_queue_classroom, sender, reply_to)
            if queued:
                await send_whatsapp_message(
                    reply_to,
                    f"{_greeting(sender)},\n\n"
                    "Your request is being processed. The system is briefly "
                    "reconnecting to the campus cameras — your photo will be "
                    "delivered automatically within the next 1-2 minutes.\n\n"
                    "No need to send the request again.\n\n"
                    "Thank you for your patience.\n"
                    "Warm regards,\nPP International School"
                )
                return True

        # Queue full or couldn't determine classroom — give polite retry message
        await send_whatsapp_message(
            reply_to,
            f"{_greeting(sender)},\n\n"
            "Your request is being processed. The system is reconnecting "
            "to the campus cameras. Please try again in 1-2 minutes and "
            "your photo will be delivered.\n\n"
            "Thank you for your patience.\n"
            "Warm regards,\nPP International School"
        )

        # Alert admin only after 5+ consecutive failures (silent to parents)
        if consecutive >= 5 and should_alert_admin():
            admin_phones = ["919971166562", "919599488106", "918076455224"]
            for admin_phone in admin_phones:
                try:
                    await send_whatsapp_message(
                        admin_phone,
                        "PPIS Bot — Camera System Alert\n\n"
                        f"The campus camera system has failed {consecutive} "
                        f"consecutive times. The agent auto-restart is in "
                        f"progress.\n\n"
                        "The system should recover automatically. If it "
                        "persists for more than 10 minutes, please check "
                        "the Campus Agent PC."
                    )
                except Exception:
                    pass
        return True

    # --- Determine which location/classroom to capture ---
    location = None
    multi_locations: list[str] = []  # For multi-camera areas like Reception

    if is_admin:
        # Admin panel: check for multi-camera areas first (e.g. Reception -> C1,C2,C3,C4)
        multi_locations = _find_all_matching_locations(message_text)
        # Also try to extract a single specific location
        location = _extract_location_from_message(message_text)
        if not location and not multi_locations:
            # Admin didn't specify a location — try auto-detecting their child's class
            # (admins can also be parents)
            children = await _lookup_parent_child_class(sender)
            if len(children) == 1:
                location = _grade_to_camera_key(children[0]["grade"])
                logger.info(f"Admin {sender} auto-detected as parent of {children[0]['student_name']} ({location})")
            elif len(children) > 1:
                child_list = "\n".join(
                    f"- {c['student_name']} ({c['grade']})" for c in children
                )
                await send_whatsapp_message(
                    reply_to,
                    "Dear Admin,\n\n"
                    "We found multiple wards registered to your number:\n"
                    f"{child_list}\n\n"
                    "Please specify the class/section or location "
                    "(e.g., 'Show photo of Grade 3C' or 'Show photo of Library').\n\n"
                    "Thank you.\n"
                    "Warm regards,\nPP International School"
                )
                return True
            else:
                # Not a parent either — ask for location
                await send_whatsapp_message(
                    reply_to,
                    "Dear Admin,\n\n"
                    "Please specify the location for the camera snapshot. Examples:\n"
                    "- 'Show photo of Grade 3C'\n"
                    "- 'Show photo of Library'\n"
                    "- 'Show photo of Reception'\n"
                    "- 'Show photo of Assembly Ground'\n"
                    "- 'Show photo of Principal Room'\n\n"
                    "Thank you.\n"
                    "Warm regards,\nPP International School"
                )
                return True
    else:
        # Regular parent: try classroom from message first
        location = _extract_classroom_from_message(message_text)

        # Look up the parent's children (needed for fallback and section inference)
        children = await _lookup_parent_child_class(sender)

        # If grade extracted without section (e.g. "GRADE 9"), try to infer
        # section from the parent's registered children.
        if location and re.match(r"^GRADE \d{1,2}$", location) and children:
            grade_num = location.split()[-1]  # e.g. "9"
            matching = [
                c for c in children
                if re.match(rf"Grade {grade_num}[A-D]", c["grade"], re.IGNORECASE)
            ]
            if len(matching) == 1:
                # Convert child grade "Grade 9A" to camera key "GRADE 9A"
                location = matching[0]["grade"].upper()
                logger.info(
                    f"Inferred section: {location} for parent {sender} "
                    f"(child: {matching[0]['student_name']})"
                )

        if not location:
            if len(children) == 1:
                location = _grade_to_camera_key(children[0]["grade"])
                logger.info(f"Auto-detected classroom {location} for parent {sender} (child: {children[0]['student_name']})")
            elif len(children) > 1:
                child_list = "\n".join(
                    f"- {c['student_name']} ({c['grade']})" for c in children
                )
                await send_whatsapp_message(
                    reply_to,
                    f"{_greeting(sender)},\n\n"
                    "We found multiple wards registered to your number:\n"
                    f"{child_list}\n\n"
                    "Please specify the class and section "
                    "(e.g., 'Show photo of Grade 3C') so we can capture the correct classroom.\n\n"
                    "Thank you for your cooperation.\n"
                    "Warm regards,\nPP International School"
                )
                return True
            else:
                await send_whatsapp_message(
                    reply_to,
                    f"{_greeting(sender)},\n\n"
                    "Please specify the class and section for the classroom photo "
                    "(e.g., 'Show photo of Grade 3C' or 'Send picture of Nursery 1').\n\n"
                    "Thank you for your cooperation.\n"
                    "Warm regards,\nPP International School"
                )
                return True

    # --- Handle multi-camera locations (e.g. Reception C1-C4) ---
    # RULE: Only share exactly 2 photos (C1 + C2). Never more.
    if multi_locations and is_admin:
        multi_locations = multi_locations[:2]  # Max 2 cameras (C1 + C2 only)
        label = multi_locations[0].rsplit(" ", 1)[0] if multi_locations else location
        await send_whatsapp_message(
            reply_to,
            f"{_greeting(sender)},\n\n"
            f"Capturing live photo(s) from {label} ({len(multi_locations)} cameras). "
            f"Please wait a moment..."
        )

        total_sent = 0
        for loc in multi_locations:
            try:
                result = await request_snapshot(loc, timeout=60.0)
            except Exception as exc:
                logger.error(f"Snapshot request raised exception for {loc}: {exc}", exc_info=True)
                result = {"success": False, "error": str(exc)}

            if result.get("success"):
                images_list = result.get("images", [])
                if not images_list and result.get("image_base64"):
                    images_list = [{
                        "image_base64": result["image_base64"],
                        "description": result.get("description", loc),
                        "filename": result.get("filename", "snapshot.jpg"),
                    }]
                # RULE: Only take the FIRST image from each camera location.
                # Each location = 1 photo. 2 locations (C1+C2) = exactly 2 photos.
                images_list = images_list[:1]
                for img_data in images_list:
                    image_b64 = img_data.get("image_base64", "")
                    if not image_b64:
                        continue
                    try:
                        media_id = await upload_base64_image_cloud(image_b64)
                        del image_b64
                        img_data.pop("image_base64", None)
                        if media_id:
                            caption = f"Live photo from {loc}\nPP International School"
                            sent = await send_cloud_media(
                                reply_to, "image", media_id=media_id, caption=caption,
                            )
                            if sent:
                                total_sent += 1
                    except Exception as exc:
                        logger.error(f"Error sending snapshot for {loc}: {exc}", exc_info=True)
            else:
                logger.warning(f"Snapshot failed for {loc}: {result.get('error', 'unknown')}")

        if total_sent > 0:
            record_snapshot_success()
            logger.info(f"Multi-location snapshot: sent {total_sent} images for {label}")
            return True

        record_snapshot_failure()
        await send_whatsapp_message(
            reply_to,
            f"{_greeting(sender)},\n\n"
            f"We were unable to capture photos from {label} at this time. "
            f"The cameras may be temporarily unavailable.\n\n"
            f"Please try again later.\n\n"
            "Thank you for your patience.\n"
            "Warm regards,\nPP International School"
        )
        return True

    # Send "processing" message
    label = f"{location} camera" if is_admin else f"{location} classroom camera"
    await send_whatsapp_message(
        reply_to,
        f"{_greeting(sender)},\n\n"
        f"Capturing live photo(s) from the {label}. "
        f"Please wait a moment..."
    )

    # Request snapshot from Campus Agent
    try:
        result = await request_snapshot(location, timeout=60.0)
    except Exception as exc:
        logger.error(f"Snapshot request raised exception for {location}: {exc}", exc_info=True)
        result = {"success": False, "error": str(exc)}

    if result.get("success"):
        # --- HEALTH: Record success, reset failure counter ---
        record_snapshot_success()

        # --- Handle multi-image response (C1 + C2 cameras) ---
        images_list = result.get("images", [])
        # Backward compat: if no images array, use single image_base64
        if not images_list and result.get("image_base64"):
            images_list = [{
                "image_base64": result["image_base64"],
                "description": result.get("description", ""),
                "filename": result.get("filename", "snapshot.jpg"),
            }]

        # RULE: Only share exactly 2 photos (C1 + C2). Never more.
        # For single-location requests, limit to max 2 images.
        images_list = images_list[:2]
        image_count = len(images_list)
        logger.info(f"Snapshot result for {location}: {image_count} image(s) (capped at 2)")

        sent_count = 0
        for idx, img_data in enumerate(images_list):
            image_b64 = img_data.get("image_base64", "")
            desc = img_data.get("description", "")
            if not image_b64:
                logger.warning(f"Empty image_base64 for {location} image {idx}")
                continue

            try:
                logger.info(f"Uploading snapshot image {idx+1}/{image_count} for {location} ({desc})...")
                media_id = await upload_base64_image_cloud(image_b64)
                # Free memory
                del image_b64
                img_data.pop("image_base64", None)

                if media_id:
                    # Build caption with camera angle info
                    if image_count > 1 and desc:
                        caption = f"Live photo from {location} ({desc})\nPP International School"
                    else:
                        caption = f"Live photo from {location}\nPP International School"

                    sent = await send_cloud_media(
                        reply_to,
                        "image",
                        media_id=media_id,
                        caption=caption,
                    )
                    if sent:
                        sent_count += 1
                        logger.info(f"Snapshot {idx+1}/{image_count} for {location} sent ({desc})")
                    else:
                        logger.error(f"send_cloud_media returned False for {location} image {idx+1} ({desc})")
                else:
                    logger.error(f"upload_base64_image_cloud returned None for {location} image {idx+1} ({desc})")

            except Exception as exc:
                logger.error(f"Exception uploading/sending snapshot {idx+1} for {location}: {exc}", exc_info=True)

        # Free any remaining base64 data and force garbage collection
        for img_data in images_list:
            img_data.pop("image_base64", None)
        result.pop("image_base64", None)
        result.pop("images", None)
        import gc; gc.collect()

        if sent_count > 0:
            logger.info(f"Sent {sent_count}/{image_count} snapshot(s) for {location} to {sender}")
            return True

        # All uploads/sends failed
        record_snapshot_failure()
        logger.warning(f"Failed to upload/send any snapshot image for {location}")
        await send_whatsapp_message(
            reply_to,
            f"{_greeting(sender)},\n\n"
            f"The photo(s) from {location} were captured but could not be delivered. "
            f"Please try again in a few minutes.\n\n"
            "Thank you for your patience.\n"
            "Warm regards,\nPP International School"
        )
        return True
    else:
        # --- HEALTH: Record failure ---
        record_snapshot_failure()

        error_msg = result.get("error", "Unknown error")
        logger.error(f"Snapshot request failed for {location}: {error_msg}")
        fail_msg = (
            f"We were unable to capture a photo from {location} at this time. "
            f"The camera may be temporarily unavailable."
        )
        if not is_admin:
            fail_msg += (
                "\n\nPlease try again later or contact:\n"
                "School Helpline / Front Desk: 8800935552\n"
                "Ms. Harpreet Kaur (Administration Incharge): 9599488106"
            )
        else:
            fail_msg += "\n\nPlease try again later."
        await send_whatsapp_message(
            reply_to,
            f"{_greeting(sender)},\n\n"
            f"{fail_msg}\n\n"
            "Thank you for your patience.\n"
            "Warm regards,\nPP International School"
        )
        return True


@router.post("/webhook")
async def receive_whatsapp_message(request: Request):
    """Handle incoming WhatsApp messages from Green API webhook."""
    if not BOT_ENABLED:
        return {"status": "ok", "note": "bot disabled"}
    body = await request.json()
    logger.info(f"Received Green API webhook: {body}")

    # --- Deduplicate retried webhooks from Green API ---
    msg_id = body.get("idMessage", "")
    if msg_id and await _is_duplicate(msg_id):
        logger.info(f"Duplicate webhook for idMessage={msg_id}, skipping.")
        return {"status": "ok"}

    parsed = parse_incoming_message(body)
    if parsed is None:
        return {"status": "ok"}

    sender, message_text, reply_to, media_info = parsed
    logger.info(f"Message from {sender} (reply to {reply_to}): {message_text} | media: {media_info is not None}")

    # --- Voice message transcription ---
    # If the message is an audio/voice note, transcribe it with Whisper
    if media_info and media_info.get("type") == "audioMessage" and media_info.get("url"):
        logger.info(f"Voice message detected from {sender}, transcribing...")
        transcribed_text = await transcribe_audio(media_info["url"])
        if transcribed_text:
            logger.info(f"Voice transcription for {sender}: {transcribed_text[:100]}")
            # Replace the placeholder text with the actual transcription
            message_text = transcribed_text
            # Notify the sender that their voice message was understood
            await send_whatsapp_message(
                reply_to,
                f"Voice message received:\n{transcribed_text}\n\nProcessing your query..."
            )
        else:
            logger.warning(f"Could not transcribe voice message from {sender}")
            await send_whatsapp_message(
                reply_to,
                "I received your voice message but could not understand it clearly. "
                "Could you please type your question or send another voice note?"
            )
            return {"status": "ok"}

    # Bot is publicly accessible — reply to ALL incoming messages (no allowlist check)

    # BULK SEND SAFEGUARD: Pause bulk sending while we handle this incoming message
    # NOTE: pause + save_message are INSIDE the try block so that
    # resume_after_bot_reply() is always called even if save_message raises.
    try:
        await pause_for_bot_reply()

        bot_phone = "bot"
        await save_message(sender, bot_phone, message_text, "whatsapp", "incoming")

        # --- Check for buffered image + name text combo (Green API path) ---
        if not media_info and message_text.strip():
            _buffered_g = _recent_images.pop(sender, None)
            if _buffered_g and (_time_mod.time() - _buffered_g["timestamp"]) < _RECENT_IMAGE_TTL:
                _name_text_g = message_text.strip()
                _name_words_g = [
                    w for w in re.sub(r"[^\w\s]", " ", _name_text_g).split()
                    if len(w) >= 2 and w.isalpha()
                    and w.lower() not in {"the", "of", "is", "at", "in", "for", "and", "as", "my",
                                          "show", "check", "hi", "hello", "help", "please",
                                          "homework", "thanks", "ok", "yes", "no"}
                ]
                if len(_name_words_g) >= 1:
                    logger.info(
                        f"[IMAGE BUFFER GREEN] Linking buffered image + name '{_name_text_g}' from {sender}"
                    )
                    _clean_g = _extract_person_name(_name_text_g)
                    if _clean_g:
                        sender_digits_g = re.sub(r"\D", "", sender)
                        sender_last10_g = sender_digits_g[-10:] if len(sender_digits_g) >= 10 else sender_digits_g
                        try:
                            handled = await _try_register_teacher_face(
                                sender, reply_to, bot_phone,
                                _buffered_g["media_info"],
                                {"teacher": _clean_g, "grade": "", "whatsapp": sender_last10_g},
                            )
                            if handled:
                                return {"status": "ok"}
                        except Exception as e:
                            logger.error(f"[IMAGE BUFFER GREEN] Face reg error: {e}", exc_info=True)

        # --- Photo / image handling (MUST run BEFORE text handlers) ---
        has_image_green = media_info and media_info.get("type") == "imageMessage"
        is_teacher_green = _is_teacher_phone(sender)
        logger.info(f"[GREEN IMAGE CHECK] sender={sender} has_image={has_image_green} is_teacher={is_teacher_green} media_type={media_info.get('type') if media_info else 'None'}")

        # Teacher face registration: if a teacher sends a photo, register for attendance
        if has_image_green and is_teacher_green:
            caption_lower = (media_info.get("caption", "") or "").strip().lower()
            # Register if no caption or caption suggests face/selfie/register
            is_face_photo = (
                not caption_lower
                or any(kw in caption_lower for kw in [
                    "register", "face", "selfie", "photo", "attendance",
                    "my photo", "my face", "register me", "chk",
                ])
            )
            if is_face_photo:
                logger.info(f"[GREEN IMAGE HANDLER] Teacher face registration for {sender}")
                try:
                    handled = await _try_register_teacher_face(
                        sender, reply_to, bot_phone, media_info, is_teacher_green,
                    )
                    if handled:
                        return {"status": "ok"}
                except Exception as e:
                    logger.error(f"[GREEN IMAGE HANDLER] Teacher face reg error: {e}", exc_info=True)

        # Face registration for ANYONE who sends a photo WITH a name (Green API path)
        if has_image_green:
            _gcap_raw = (media_info.get("caption", "") or "").strip()
            _gcap_low = _gcap_raw.lower()
            _G_NON_NAME = [
                "homework", "check", "check this", "please check", "hi", "hello",
                "help", "thanks", "thank you", "ok", "yes", "no", "please",
                "good morning", "good afternoon", "good evening", "show",
                "convey", "forward", "fwd", "relay", "inform",
                "convey to ct", "convey to class teacher",
                "forward to ct", "send to ct", "tell ct",
            ]
            _G_FWD_PHRASES = [
                "convey to", "forward to", "send to", "tell to",
                "relay to", "pass to", "give to", "share with",
                "class teacher", "ct ", " ct.", " ct,",
                "i don't know", "i dont know", "don't know",
                "what is this", "what's this", "kya hai", "pata nahi",
                "please ask", "pls ask", "plz ask", "kindly ask",
                "ask ", "if this is", "if it is", "is this",
                "syllabus", "circular", "timetable", "notice",
                "homework", "schedule",
            ]
            _g_is_non_name = _gcap_low in _G_NON_NAME or not _gcap_raw
            if not _g_is_non_name:
                # Use the unified query detection from _is_query_caption
                if "?" in _gcap_low or _is_query_caption(_gcap_low):
                    _g_is_non_name = True
            if not _g_is_non_name:
                for _gp in _G_FWD_PHRASES:
                    if _gp in _gcap_low:
                        _g_is_non_name = True
                        break
            _g_name_words = [
                w for w in re.sub(r"[^\w\s]", " ", _gcap_raw).split()
                if len(w) >= 2 and w.isalpha()
                and w.lower() not in {"the", "of", "is", "at", "in", "for", "and", "as", "my",
                                       "convey", "forward", "class", "teacher", "know", "what",
                                       "dont", "this", "that"}
            ]
            # Word-count heuristic: >= 5 words is a sentence/query, not a name
            _all_words = [w for w in _gcap_raw.split() if len(w) >= 2]
            if len(_all_words) >= 5:
                _g_is_non_name = True
            _g_has_name = len(_g_name_words) >= 1 and not _g_is_non_name
            _g_has_class = bool(_CAPTION_CLASS_RE.search(_gcap_raw)) if _gcap_raw else False

            if _g_has_name and not _g_has_class:
                _tname = _extract_person_name(_gcap_raw)
                if not _tname:
                    _tname = f"Staff_{sender[-10:]}"
                _sd = re.sub(r"\D", "", sender)
                _sl10 = _sd[-10:] if len(_sd) >= 10 else _sd
                logger.info(f"[GREEN] Face reg from caption name sender={sender} name='{_tname}'")
                try:
                    _handled = await _try_register_teacher_face(
                        sender, reply_to, bot_phone, media_info,
                        {"teacher": _tname, "grade": "", "whatsapp": _sl10},
                    )
                    if _handled:
                        return {"status": "ok"}
                except Exception as _e:
                    logger.error(f"[GREEN] Caption-based face reg error: {_e}", exc_info=True)

            # Buffer image if no caption — wait for follow-up name text
            if not _gcap_raw or _g_is_non_name:
                _recent_images[sender] = {
                    "media_info": media_info,
                    "timestamp": _time_mod.time(),
                    "reply_to": reply_to,
                    "bot_phone": bot_phone,
                }
                logger.info(f"[IMAGE BUFFER GREEN] Buffered image from {sender} (no caption)")

        if has_image_green and not is_teacher_green:
            logger.info(f"[GREEN IMAGE HANDLER] Processing image from {sender}, url={media_info.get('url', '')[:80]}")
            try:
                face_reg_handled = await _try_register_child_face(
                    sender, reply_to, bot_phone, media_info,
                )
                logger.info(f"[GREEN IMAGE HANDLER] face_reg_handled={face_reg_handled} for {sender}")
                if face_reg_handled:
                    return {"status": "ok"}
            except Exception as img_exc:
                logger.error(f"[GREEN IMAGE HANDLER] Exception: {img_exc}", exc_info=True)

            # Homework review: ONLY when caption explicitly requests it
            _hw_caption = (media_info.get("caption", "") or "").strip().lower()
            _is_hw_check = any(
                _hw_caption.startswith(kw) for kw in [
                    "check please", "please check", "check homework",
                    "check hw", "check h.w.", "check h.w",
                ]
            )
            if _is_hw_check:
                try:
                    import httpx as _httpx
                    direct_url = media_info.get("url", "")
                    if direct_url:
                        async with _httpx.AsyncClient(timeout=30) as _client:
                            _resp = await _client.get(direct_url)
                            if _resp.status_code == 200:
                                from app.services.openai_service import generate_homework_review
                                caption = media_info.get("caption", "")
                                children_green = await _lookup_parent_child_class(sender)
                                child_name = children_green[0]["student_name"] if children_green else ""
                                child_grade = children_green[0]["grade"] if children_green else ""
                                ai_response = await generate_homework_review(
                                    _resp.content, _resp.headers.get("content-type", "image/jpeg"),
                                    caption, student_name=child_name, grade=child_grade,
                                )
                                await save_message(bot_phone, sender, ai_response, "whatsapp", "outgoing")
                                await send_whatsapp_message(reply_to, ai_response)
                                return {"status": "ok"}
                except Exception as vis_exc:
                    logger.error(f"[GREEN IMAGE HANDLER] Homework review error: {vis_exc}", exc_info=True)

            # If all image processing fails, acknowledge receipt
            err_msg = "Your image has been received. Please try sending it again if needed."
            await save_message(bot_phone, sender, err_msg, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, err_msg)
            return {"status": "ok"}

        # Check if a teacher is broadcasting homework to parents of their class
        hw_broadcast = await detect_and_handle_teacher_homework_broadcast(
            sender, message_text, reply_to, media_info,
        )
        if hw_broadcast:
            return {"status": "ok"}

        # Check if this is a teacher replying to a forwarded message (text or media)
        relayed = await try_relay_teacher_reply(sender, message_text, reply_to, media_info)
        if relayed:
            return {"status": "ok"}

        # Check for classroom snapshot request (live photo from DVR camera)
        # NOTE: This MUST run BEFORE try_direct_message / GPT so that
        # "show reception", "show class 10 a", "show 12 b" etc. from admins
        # are routed to the camera system, not to GPT or the DM handler.
        snapshot_handled = await detect_and_handle_snapshot_request(sender, message_text, reply_to)
        if snapshot_handled:
            return {"status": "ok"}

        # Check if this is a direct-message request (e.g. "send message to Harnoor: ...")
        dm_handled = await try_direct_message(sender, message_text, reply_to, media_info)
        if dm_handled:
            return {"status": "ok"}

        # Check if sender has a pending query and is now providing class/section info
        pending_handled = await try_handle_pending_query(sender, message_text, reply_to)
        if pending_handled:
            return {"status": "ok"}

        # Check for homework query — forward to class teacher
        hw_handled = await detect_and_handle_homework_query(sender, message_text, reply_to, media_info)
        if hw_handled:
            return {"status": "ok"}

        # Check for leave application — forward to class teacher
        leave_handled = await detect_and_handle_leave_application(sender, message_text, reply_to, media_info)
        if leave_handled:
            return {"status": "ok"}

        # Class list query
        class_list_answer = await lookup_class_list(message_text)
        if class_list_answer:
            await save_message(bot_phone, sender, class_list_answer, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, class_list_answer)
            return {"status": "ok"}

        # Check if this is a transport/route query — answer from structured data
        transport_answer = lookup_transport(message_text)
        if transport_answer:
            await save_message(bot_phone, sender, transport_answer, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, transport_answer)
            return {"status": "ok"}

        # --- Activity-specific contact lookup (use word boundaries to avoid false matches) ---
        from app.services.openai_service import _is_hindi
        _msg_low = message_text.lower()
        _activity_contact = None

        def _activity_word_match(keywords: list[str], text: str) -> bool:
            """Match activity keywords using word boundaries so 'act' doesn't match 'contact'."""
            for kw in keywords:
                if re.search(r'\b' + re.escape(kw) + r'\b', text, re.IGNORECASE):
                    return True
            return False

        _skating_kw = ["skating", "skate", "roller", "\u0938\u094d\u0915\u0947\u0924\u093f\u0902\u0917", "\u0938\u094d\u0915\u0947\u091f"]
        _dance_kw = ["dance", "dancing", "nritya", "\u0928\u0943\u0924\u094d\u092f", "\u0921\u093e\u0902\u0938"]
        _theatre_kw = ["theatre", "theater", "drama", "acting", "natya", "natak", "\u0928\u093e\u091f\u0915", "\u0925\u093f\u090f\u091f\u0930", "\u0928\u093e\u091f\u094d\u092f"]
        _music_kw = ["music", "singing", "song", "instrument", "band", "sangeet", "\u0938\u0902\u0917\u0940\u0924", "\u0917\u093e\u0928\u093e", "\u0917\u0940\u0924"]
        _art_kw = ["art class", "drawing", "painting", "sketch", "craft", "kala", "\u0915\u0932\u093e", "\u091a\u093f\u0924\u094d\u0930", "\u092a\u0947\u0902\u091f\u093f\u0902\u0917"]
        if _activity_word_match(_skating_kw, _msg_low):
            _activity_contact = ("skating competition", "Mr. Gautam", "9910933523")
        elif _activity_word_match(_dance_kw, _msg_low):
            _activity_contact = ("dance", "Mr. Akshay", "8368869135")
        elif _activity_word_match(_theatre_kw, _msg_low):
            _activity_contact = ("theatre", "Mr. Nupin", "8851496687")
        elif _activity_word_match(_music_kw, _msg_low):
            _activity_contact = ("music", "Mr. Ankur", "9319502107")
        elif _activity_word_match(_art_kw, _msg_low):
            _activity_contact = ("art", "Ms. Preity", "9870501952")

        if _activity_contact:
            _act, _name, _phone = _activity_contact
            if _is_hindi(message_text):
                _act_msg = (
                    f"{_act.title()} \u0938\u0947 \u0938\u0902\u092c\u0902\u0927\u093f\u0924 \u0915\u093f\u0938\u0940 \u092d\u0940 \u092a\u094d\u0930\u0936\u094d\u0928 \u0915\u0947 \u0932\u093f\u090f, "
                    f"\u0915\u0943\u092a\u092f\u093e {_name} \u0938\u0947 \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0947\u0902: "
                    f"{_phone}\n\n"
                )
            else:
                _act_msg = (
                    f"For any queries related to {_act}, "
                    f"please contact {_name}:\n"
                    f"{_phone}\n\n"
                )
            await save_message(bot_phone, sender, _act_msg, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, _act_msg)
            return {"status": "ok"}

        system_prompt = await get_system_prompt()
        history = await get_conversation_history(sender)
        ai_response = await generate_response(message_text, system_prompt, history)

        # Check if the bot couldn't answer — ask for class/section and email teacher
        if _is_unknown_response(ai_response):
            await save_pending_query(sender, reply_to, message_text)
            # Detect Hindi for bilingual response
            from app.services.openai_service import _is_hindi

            escalation_contacts = (
                "\n\nFor immediate assistance, please contact:\n"
                "- School Helpline / Front Desk: 8800935552\n"
                "- Ms. Harpreet Kaur (Administration Incharge): 9599488106"
            )
            escalation_contacts_hi = (
                "\n\n\u0924\u0941\u0930\u0902\u0924 \u0938\u0939\u093e\u092f\u0924\u093e \u0915\u0947 \u0932\u093f\u090f \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0947\u0902:\n"
                "- School Helpline / Front Desk: 8800935552\n"
                "- Ms. Harpreet Kaur (Administration Incharge): 9599488106"
            )

            if _is_hindi(message_text):
                ask_class_msg = (
                    "\u092e\u0941\u091d\u0947 \u0916\u0947\u0926 \u0939\u0948, \u092e\u0947\u0930\u0947 \u092a\u093e\u0938 \u0907\u0938 \u092c\u093e\u0930\u0947 \u092e\u0947\u0902 \u091c\u093e\u0928\u0915\u093e\u0930\u0940 \u0928\u0939\u0940\u0902 \u0939\u0948\u0964 "
                    "\u0915\u0943\u092a\u092f\u093e \u0905\u092a\u0928\u0947 \u092c\u091a\u094d\u091a\u0947 \u0915\u0940 *\u0915\u0915\u094d\u0937\u093e \u0914\u0930 \u0938\u0947\u0915\u094d\u0936\u0928* \u092c\u0924\u093e\u090f\u0902 "
                    "(\u091c\u0948\u0938\u0947 Grade 5A, Nursery 1, Prep 2) \u0924\u093e\u0915\u093f \u092e\u0948\u0902 \u0906\u092a\u0915\u093e \u092a\u094d\u0930\u0936\u094d\u0928 "
                    "\u0915\u0915\u094d\u0937\u093e \u0905\u0927\u094d\u092f\u093e\u092a\u0915 \u0915\u094b WhatsApp \u0914\u0930 \u0908\u092e\u0947\u0932 \u0926\u094d\u0935\u093e\u0930\u093e \u092d\u0947\u091c \u0938\u0915\u0942\u0901\u0964"
                    + escalation_contacts_hi
                )
            else:
                ask_class_msg = (
                    "I'm sorry, I don't have specific information about that. "
                    "Could you please share your ward's *class and section* "
                    "(e.g. Grade 5A, Nursery 1, Prep 2) so I can forward your query "
                    "to the class teacher via WhatsApp and email?"
                    + escalation_contacts
                )
            await save_message(bot_phone, sender, ask_class_msg, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, ask_class_msg)
            return {"status": "ok"}

        await save_message(bot_phone, sender, ai_response, "whatsapp", "outgoing")

        # Reply to the correct chat (group or individual)
        await send_whatsapp_message(reply_to, ai_response)

        # Meal menu image sharing — DISABLED per user request
        # Photo gallery sharing — DISABLED per user request

        # Forward to teachers if a class/teacher was mentioned, and confirm delivery
        # Also forward any attached media to the teacher
        await forward_to_teachers_and_confirm(sender, message_text, reply_to, media_info)

        return {"status": "ok"}
    finally:
        # BULK SEND SAFEGUARD: Resume bulk sending after bot reply is done
        await resume_after_bot_reply()


@router.post("/webhook/send-image")
async def send_image_to_chat(request: Request):
    """Send an image to a WhatsApp chat (individual or group)."""
    # Auth check — admin-only endpoint
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            return {"error": "Unauthorized"}
    body = await request.json()
    chat_id = body.get("chatId", "")
    image_url = body.get("imageUrl", "")
    caption = body.get("caption", "")

    if not chat_id or not image_url:
        return {"status": "error", "message": "chatId and imageUrl are required"}

    success = await send_whatsapp_image(chat_id, image_url, caption)
    return {"status": "ok" if success else "error"}


@router.post("/webhook/sms")
async def receive_sms(request: Request):
    """Handle incoming SMS messages (provider-specific webhook)."""
    body = await request.json()
    logger.info(f"Received SMS webhook: {body}")

    parsed = parse_incoming_sms(body)
    if parsed is None:
        return {"status": "ok"}

    sender, message_text = parsed
    logger.info(f"SMS from {sender}: {message_text}")

    if not await is_allowlisted(sender):
        logger.info(f"Number {sender} is not allowlisted. Ignoring.")
        return {"status": "ok"}

    bot_phone = "bot"
    await save_message(sender, bot_phone, message_text, "sms", "incoming")

    system_prompt = await get_system_prompt()
    history = await get_conversation_history(sender)
    ai_response = await generate_response(message_text, system_prompt, history)

    await save_message(bot_phone, sender, ai_response, "sms", "outgoing")

    await send_sms(sender, ai_response)

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Meta Cloud API Webhook — verification (GET) and incoming messages (POST)
# ---------------------------------------------------------------------------

CLOUD_VERIFY_TOKEN = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "ppis_bot_verify_2024")


@router.get("/webhook/cloud")
async def verify_cloud_webhook(request: Request):
    """Meta Cloud API webhook verification (hub.challenge handshake)."""
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == CLOUD_VERIFY_TOKEN:
        logger.info("Cloud API webhook verified successfully")
        # Meta expects the hub.challenge echoed back as an integer
        return Response(content=str(challenge), media_type="text/plain", status_code=200)

    logger.warning(f"Cloud API webhook verification failed: mode={mode}")
    return Response(content="Forbidden", status_code=403)


async def _find_student_by_caption(caption: str) -> dict | None:
    """Search the school DB for a student matching the caption text.

    Parses caption for a student name (and optionally grade/class).
    Returns the matched student dict with student_name, grade, parent_phones
    or None if no match found.
    """
    caption_clean = caption.strip()
    if not caption_clean:
        return None

    # Try to extract grade from caption (e.g. "Grade 3C", "5A", "3 C", "NUR-1")
    grade_match = _GRADE_EXTRACT_RE.search(caption_clean)
    caption_grade = None
    name_part = caption_clean

    if grade_match:
        caption_grade = _extract_classroom_from_message(caption_clean)
        name_part = caption_clean[:grade_match.start()].strip()
        if not name_part:
            name_part = caption_clean[grade_match.end():].strip()

    # Fallback: try flexible regex for "3C", "3 C", "5A" without Grade/Class prefix
    if not caption_grade:
        flex_match = _CAPTION_CLASS_RE.search(caption_clean)
        if flex_match:
            # Extract grade number + section from flexible match
            g5, g6 = flex_match.group(5), flex_match.group(6)  # bare "3C" groups
            if g5 and g6:
                caption_grade = f"Grade {g5}{g6.upper()}"
            elif not caption_grade:
                # Try standard extraction
                caption_grade = _extract_classroom_from_message(caption_clean)
            name_part = caption_clean[:flex_match.start()].strip()
            if not name_part:
                name_part = caption_clean[flex_match.end():].strip()

    # Clean name: remove common prefixes/suffixes and command words
    name_part = re.sub(r"(?i)^(register|add|enroll|enrol|signup|photo|pic|image)(\s+(of|for))?\s*", "", name_part).strip()
    name_part = re.sub(r"[^\w\s]", "", name_part).strip()
    # Also remove any remaining command words from the middle
    name_words_clean = [w for w in name_part.split() if w.lower() not in _NON_NAME_WORDS]
    name_part = " ".join(name_words_clean).strip()

    if not name_part or len(name_part) < 2:
        return None

    db = await get_db()
    try:
        # Search by name (case-insensitive LIKE match)
        name_words = name_part.lower().split()
        # Build search: all name words must appear in student_name
        conditions = []
        params = []
        for word in name_words:
            if len(word) > 1:
                conditions.append("LOWER(student_name) LIKE ?")
                params.append(f"%{word}%")

        if not conditions:
            return None

        where_clause = " AND ".join(conditions)
        query = (
            f"SELECT student_name, grade, father_mobile, mother_mobile "
            f"FROM pi_sheet_students WHERE {where_clause}"
        )
        if caption_grade:
            query += " AND UPPER(grade) = ?"
            params.append(caption_grade.upper())

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        if not rows:
            # Try without grade filter if grade was specified but no match
            if caption_grade:
                cursor = await db.execute(
                    f"SELECT student_name, grade, father_mobile, mother_mobile "
                    f"FROM pi_sheet_students WHERE {where_clause}",
                    params[:-1],
                )
                rows = await cursor.fetchall()

        if not rows:
            return None

        # Pick the best match — prefer exact name match
        best = None
        for row in rows:
            student_name = row[0]
            grade = row[1]
            father_phone = re.sub(r"\D", "", row[2] or "") if row[2] else ""
            mother_phone = re.sub(r"\D", "", row[3] or "") if row[3] else ""
            parent_phones = []
            for p in (father_phone, mother_phone):
                if len(p) >= 10:
                    parent_phones.append(f"91{p[-10:]}")

            candidate = {
                "student_name": student_name,
                "grade": grade,
                "parent_phones": parent_phones,
            }

            # Exact name match (case-insensitive)
            if student_name.lower() == name_part.lower():
                return candidate
            # First/last name match
            if not best:
                best = candidate

        return best
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Image/Media Content Classification — strictly differentiate between
# student registration photos and general files/documents.
#
# RULE: A file/image is a student photo ONLY IF the caption contains
#       Student Name + Class/Section. Otherwise it's a general document.
# ---------------------------------------------------------------------------

# Flexible regex for detecting class/grade in captions — handles formats like:
# "Grade 3C", "Class 5A", "3C", "3 C", "NUR", "Nursery", "Prep 1", "Popsicle"
_CAPTION_CLASS_RE = re.compile(
    r"(?:"
    r"(?:grade|class|कक्षा|क्लास)[\s\-]*(\d{1,2})[\s\-]*([a-cA-C])?"  # "Grade 3C", "Grade 9-A"
    r"|"
    r"(?:nur(?:sery)?)[\s\-]*(\d)?"  # "Nursery", "NUR 1"
    r"|"
    r"(?:prep)[\s\-]*(\d)?"  # "Prep 1"
    r"|"
    r"(?:popsicle)"  # "Popsicle"
    r"|"
    r"\b(\d{1,2})[\s\-]*([a-cA-C])\b"  # "3C", "3 C", "3-C", "9-A", "10B"
    r")",
    re.IGNORECASE,
)

# Words that indicate a document/query/common English — NOT a student name
_NON_NAME_WORDS = {
    # Document keywords
    "homework", "syllabus", "circular", "notice", "timetable", "schedule",
    "report", "marks", "result", "fee", "payment", "receipt", "worksheet",
    "assignment", "question", "paper", "exam", "test", "quiz", "screenshot",
    "forwarded", "whatsapp", "chat", "map", "location", "menu", "list",
    "form", "application", "attendance", "absent", "present", "leave",
    # Action/request words
    "please", "confirm", "check", "send", "share", "forward", "update",
    "final", "teacher", "school", "student", "parent", "child", "ward",
    "correct", "wrong", "right", "new", "old", "same", "other", "both",
    "need", "want", "know", "see", "get", "give", "take", "make", "let",
    "come", "tell", "ask", "say", "said", "done", "today", "tomorrow",
    "yesterday", "daily", "weekly", "monthly", "yearly", "urgent", "asap",
    # Common English words (not names)
    "this", "that", "the", "for", "from", "with", "about", "what", "when",
    "where", "how", "can", "will", "should", "would", "could", "have",
    "has", "had", "been", "being", "are", "was", "were", "not", "but",
    "and", "also", "any", "all", "our", "your", "his", "her", "its",
    "if", "is", "it", "or", "an", "am", "do", "so", "no", "my", "we",
    "me", "us", "at", "on", "in", "up", "by", "to", "of", "be", "he",
    # Media/greeting words
    "image", "photo", "file", "document", "video", "pdf", "attached",
    "attachment", "dear", "hello", "sir", "maam", "madam", "good",
    "morning", "evening", "thank", "thanks", "okay", "yes", "hi",
    # Command words (not student names)
    "register", "add", "enroll", "enrol", "signup",
    # Forwarding/routing words
    "convey", "forward", "fwd", "relay", "inform", "notify", "pass",
    "class", "dont", "doesn", "won",
}


def _caption_has_name_and_class(caption: str) -> bool:
    """Check if the caption contains both a student name AND a class/section.

    Returns True only when both are clearly present.
    Examples that return True:
      "Aarav Sharma Grade 5A", "Grade 3C Nitya Gupta", "Suhaan 3C",
      "Suhaan 3 C", "Karman 1B", "Ishnoor Grade 7B"
    Examples that return False:
      "homework", "syllabus for 3 C", "confirm from teacher if final syllabus 3C",
      "screenshot", "", "hi", "check this",
      "Please ask reva ma'am if this is the final syllabus of grade 3?"
    """
    if not caption or len(caption.strip()) < 3:
        return False

    caption_lower = caption.lower().strip()

    # Question marks indicate a query, not a registration request
    if "?" in caption:
        return False

    # Captions with forwarding/query intent are NOT face registration
    _QUERY_PATTERNS = [
        "please ask", "ask ", "is this", "if this", "is it",
        "kya hai", "kya ye", "confirm", "verify",
        "syllabus", "circular", "timetable", "schedule",
        "homework", "notice", "final",
    ]
    if any(caption_lower.startswith(p) or f" {p}" in f" {caption_lower}" for p in _QUERY_PATTERNS):
        return False

    # Must contain a class/grade indicator
    has_class = bool(_CAPTION_CLASS_RE.search(caption))

    # Must contain at least one proper name word (not a document keyword)
    name_part = _CAPTION_CLASS_RE.sub("", caption).strip()
    name_part = re.sub(r"[^\w\s]", "", name_part).strip()
    name_words = [
        w for w in name_part.split()
        if len(w) >= 2 and w.isalpha() and w.lower() not in _NON_NAME_WORDS
    ]
    has_name = len(name_words) >= 1

    # If the non-class portion is too long (>= 5 words), it's likely a
    # sentence/query rather than just a person's name
    all_words = [w for w in name_part.split() if len(w) >= 2]
    if len(all_words) >= 5:
        return False

    return has_class and has_name


def _is_query_caption(caption_lower: str) -> bool:
    """Return True if the caption is clearly a question or forwarding request."""
    _QUERY_STARTERS = [
        "please ask", "pls ask", "plz ask", "kindly ask",
        "ask ", "convey to", "forward to", "send to", "tell to",
        "relay to", "inform to", "pass to", "give to",
        "share with", "share this with",
        "is this", "is it", "what is", "what's",
        "kya hai", "kya ye", "ye kya", "yeh kya",
    ]
    _QUERY_PHRASES = [
        "if this is", "if it is", "if this the", "if it's",
        "please check", "please confirm", "please verify",
        "is the final", "is this final", "is this the",
        "syllabus", "circular", "timetable", "notice",
    ]
    for starter in _QUERY_STARTERS:
        if caption_lower.startswith(starter):
            return True
    for phrase in _QUERY_PHRASES:
        if phrase in caption_lower:
            return True
    return False


def _classify_media_content(media_info: dict) -> str:
    """Classify incoming media into two strictly separate categories:

    CATEGORY 1 — 'face_registration'
      Triggered ONLY when the image caption is tagged as:
        - name + class (e.g. "Aarav Sharma Grade 5A")
        - name only (e.g. "Alisha Ahuja") — 2+ name words, no forwarding intent
        - name + staff/teacher indicator (e.g. "Alisha Ahuja Staff : PPIS")
        - explicit registration keywords ("register", "face", "selfie", "attendance")
      These images go ONLY to the face-recognition pipeline.
      They are NEVER forwarded as normal media to teachers or parents.

    CATEGORY 2 — 'document'
      Everything else: homework photos, PDFs, worksheets, screenshots,
      voice notes, announcements, any normal parent-teacher shared media.
      These are forwarded normally. Face recognition is NEVER run on them.

    Returns: 'face_registration' or 'document'
    """
    media_type = media_info.get("type", "")
    caption = (media_info.get("caption", "") or "").strip()
    caption_lower = caption.lower()

    # Non-image media types are ALWAYS Category 2 (documents)
    if media_type in ("documentMessage", "videoMessage", "audioMessage"):
        return "document"

    # --- Early exit: captions that are clearly queries/forwarding ---
    # Questions (has "?") or forwarding requests are NEVER face registration
    if caption_lower and ("?" in caption_lower or _is_query_caption(caption_lower)):
        return "document"

    # --- Category 1 checks (face registration) ---

    # Check 1: Caption has Name + Class → student face registration
    if caption and _caption_has_name_and_class(caption):
        return "face_registration"

    # Check 2: Caption has explicit registration keywords
    _REG_KEYWORDS = ["register", "face reg", "selfie", "attendance photo",
                     "my photo", "my face", "register me"]
    if caption_lower and any(kw in caption_lower for kw in _REG_KEYWORDS):
        return "face_registration"

    # Check 3: Caption has staff/designation indicators
    _STAFF_INDICATORS = ["staff", "ppis", "pgt", "tgt", "ntt", "coordinator",
                         "principal", "vice principal", "hod"]
    if caption_lower and any(ind in caption_lower for ind in _STAFF_INDICATORS):
        return "face_registration"

    # Check 4: Caption looks like a person's name (2+ name words, no
    # forwarding intent, no document/action keywords, no sentence structure)
    if caption and _caption_is_name_only(caption):
        return "face_registration"

    # --- Everything else is Category 2 (general media/document) ---
    return "document"


# Forwarding phrases that indicate the caption is a query, NOT a person's name
_FORWARDING_PHRASES = [
    "convey to", "forward to", "send to", "tell to",
    "relay to", "inform to", "pass to", "give to",
    "ask maam", "ask ma'am", "ask sir", "ask teacher",
    "share with", "share this with",
    "class teacher", "ct ", " ct.", " ct,",
    "i don't know", "i dont know", "don't know",
    "what is this", "what's this", "kya hai", "pata nahi", "samajh nahi",
]

# Single-word or short captions that are clearly NOT a name
_NON_NAME_CAPTIONS = {
    "homework", "check", "check this", "please check", "hi", "hello",
    "help", "thanks", "thank you", "ok", "yes", "no", "please",
    "good morning", "good afternoon", "good evening", "show",
    "chk", "chck", "pls", "plz", "plz check", "pls check",
    "hw", "hmwk", "hmw", "see", "look", "dekho", "dekhiye",
    "dekhna", "btao", "bataiye", "verify", "review", "done",
    "send", "share", "shared", "forwarded", "fwd", "correction",
    "correct", "wrong", "right", "marks", "grade", "test", "exam",
    "pic", "photo", "image", "screenshot", "ss", "file", "doc",
    "document", "pdf", "page", "pg", "note", "notes",
    "sir", "mam", "madam", "ma'am", "teacher",
    "kindly", "urgent", "asap", "imp", "important",
    "today", "tomorrow", "kal", "aaj", "abhi",
    "convey", "forward", "relay", "inform",
}

# Basic filler/common words for _caption_is_name_only and child-name scoring
_NAME_FILLER_WORDS = {
    "the", "of", "is", "at", "in", "for", "and", "as", "my",
    "this", "that", "from", "with", "staff", "ppis",
    "teacher", "sir", "mam", "madam", "mrs", "mr", "ms",
    "regards", "good", "morning", "evening", "photo",
    "required", "please", "kindly",
    "ask", "convey", "forward", "class", "know", "what",
    "dont", "doesn", "won", "can", "will", "should",
    "need", "want", "tell", "give", "send", "check",
    "look", "see", "how", "why", "where", "when",
    "not", "but", "also", "just", "only", "very",
    "all", "any", "our", "your", "his", "her",
}


def _caption_is_name_only(caption: str) -> bool:
    """Check if a caption looks like ONLY a person's name (no query/command).

    Returns True for: "Alisha Ahuja", "Aarav Sharma", "Riya Singh"
    Returns False for: "check this", "ask harnoor maam if this is final",
                       "homework", "convey to teacher", sentences with punctuation
    """
    caption_lower = caption.lower().strip()
    if not caption_lower or len(caption_lower) < 3:
        return False

    # Exact match against known non-name captions
    if caption_lower in _NON_NAME_CAPTIONS:
        return False

    # Contains a forwarding/question phrase → it's a query, not a name
    for phrase in _FORWARDING_PHRASES:
        if phrase in caption_lower:
            return False
    if caption_lower.startswith("ask "):
        return False

    # Extract name-like words (alphabetic, 2+ chars, not common filler words)
    name_words = [
        w for w in re.sub(r"[^\w\s]", " ", caption).split()
        if len(w) >= 2 and w.isalpha() and w.lower() not in _NAME_FILLER_WORDS
    ]

    # Must have at least 2 name words (first + last name)
    if len(name_words) < 2:
        return False

    # Long captions with punctuation are sentences/requests, not names
    word_count = len(caption.split())
    has_punctuation = any(c in caption for c in ".?!,;:")
    if word_count >= 5 and has_punctuation:
        return False

    return True


async def _try_register_child_face(
    sender: str, reply_to: str, bot_phone: str,
    media_info: dict,
) -> bool:
    """Auto-register a child's face when a parent sends a photo.

    CRITICAL: Each photo is identified by the caption (Name + Class).
    Different names/classes = different students, even from the same parent.
    Only group photos under the same student when name AND class match exactly.

    Flow:
    1. Classify image content (student photo vs document).
    2. Parse caption for student name and class.
    3. Look up student in pi_sheet_students DB (by name + class, or parent phone).
    4. Download image.
    5. Store with correct student identity.
    6. Confirm to parent.

    Returns True if the image was handled as a face registration.
    """
    caption_raw = (media_info.get("caption", "") or "").strip()
    caption = caption_raw.lower()

    # --- Step 1: Try to find student by caption name ---
    # Search the entire DB for a student matching the caption name
    target_child = None
    caption_matched = False

    if caption_raw:
        target_child = await _find_student_by_caption(caption_raw)
        if target_child:
            caption_matched = True
            logger.info(f"Face registration: caption matched student "
                        f"{target_child['student_name']} ({target_child['grade']})")

    # --- Step 2: If caption didn't match, fall back to parent phone lookup ---
    if not target_child:
        children = await _lookup_parent_child_class(sender)
        logger.info(f"Face registration: parent lookup for {sender} found "
                    f"{len(children)} children: {[c['student_name'] for c in children]}")

        if not children:
            # Not a known parent — can't register
            return False

        if caption_raw:
            # Caption was provided but didn't match DB exactly — try matching
            # against this parent's children by partial name.
            # Use a scoring system: count how many of the child's name words
            # appear in the caption. Pick the child with the MOST matches.
            # This prevents "gupta" in "Aarna Gupta" from matching "Nitya Gupta".
            caption_name_words = [
                w for w in caption.split()
                if len(w) > 2 and w.isalpha() and w not in _NAME_FILLER_WORDS
            ]
            best_score = 0
            for child in children:
                child_name_lower = child["student_name"].lower()
                child_words = [w for w in child_name_lower.split() if len(w) > 1]
                # Score = number of child name words found in caption
                score = sum(1 for cw in child_words if cw in caption)
                # Bonus: check if caption also mentions the child's grade
                child_grade = child.get("grade", "").lower().replace("grade", "").replace("grade ", "").strip()
                if child_grade and child_grade in caption.replace("-", "").replace(" ", ""):
                    score += 2
                if score > best_score:
                    best_score = score
                    target_child = child
                    caption_matched = True

        if not target_child:
            # Caption had name+class but didn't match any student in DB.
            # Ask parent to resend with correct details.
            if children:
                child_list = "\n".join(
                    f"{i+1}. {c['student_name']} ({c['grade']})"
                    for i, c in enumerate(children)
                )
            else:
                child_list = "(No children found linked to your number)"
            ask_msg = (
                "Thank you for sharing the photo!\n\n"
                "We could not match the name/class in your caption to a student in our records.\n"
                "Please resend the photo with the correct caption in this format:\n"
                "*Student Name Grade/Class*\n\n"
                "Example: *Aarav Sharma Grade 5A*\n\n"
                f"Children linked to your number:\n{child_list}"
            )
            await save_message(bot_phone, sender, ask_msg, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, ask_msg)
            return True

    # Download image — supports both Cloud API (cloud_media_id) and Green API (direct url)
    cloud_media_id = media_info.get("cloud_media_id", "")
    direct_url = media_info.get("url", "")
    img_bytes = None
    img_mime = "image/jpeg"

    if cloud_media_id:
        from app.services.whatsapp_service import download_cloud_media
        logger.info(f"Face registration: downloading image via Cloud API {cloud_media_id} from {sender}")
        img_bytes, img_mime = await download_cloud_media(cloud_media_id)
    elif direct_url:
        import httpx
        logger.info(f"Face registration: downloading image via direct URL from {sender}")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(direct_url)
                if resp.status_code == 200:
                    img_bytes = resp.content
                    img_mime = resp.headers.get("content-type", "image/jpeg")
                else:
                    logger.error(f"Face registration: direct URL returned {resp.status_code}")
        except Exception as e:
            logger.error(f"Face registration: direct URL download error: {e}")
    else:
        logger.warning(f"Face registration: no cloud_media_id or url for {sender}")
        return False

    if not img_bytes:
        logger.error(f"Face registration: failed to download image from {sender}")
        return False

    student_name = target_child["student_name"]
    grade = target_child["grade"]
    person_id = re.sub(r"[^a-zA-Z0-9]", "_", student_name.upper()) + "_" + re.sub(r"[^a-zA-Z0-9]", "", grade.upper())

    # Build comma-separated phone list with both parents for notifications
    parent_phones = target_child.get("parent_phones", [])
    if not parent_phones:
        parent_phones = [sender]
    # Ensure sender is included and de-duplicate
    sender_digits = re.sub(r"\D", "", sender)
    sender_10 = sender_digits[-10:] if len(sender_digits) >= 10 else sender_digits
    normalized_phones = set()
    for p in parent_phones:
        p_digits = re.sub(r"\D", "", p)
        p_10 = p_digits[-10:] if len(p_digits) >= 10 else p_digits
        normalized_phones.add(f"91{p_10}")
    if sender_10:
        normalized_phones.add(f"91{sender_10}")
    phone_str = ",".join(sorted(normalized_phones))

    # Store the face image in the cloud DB
    db = await get_db()
    try:
        # Check how many photos are already registered for this child
        cursor = await db.execute(
            "SELECT COUNT(*) FROM agent_registered_faces WHERE person_id = ?",
            (person_id,),
        )
        count_row = await cursor.fetchone()
        existing_count = count_row[0] if count_row else 0

        angle = "front" if existing_count == 0 else f"angle_{existing_count + 1}"

        await db.execute(
            "INSERT INTO agent_registered_faces "
            "(person_id, name, role, phone, angle, image_data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, student_name, "Student", phone_str, angle, img_bytes),
        )
        await db.commit()
        photo_num = existing_count + 1

        logger.info(
            f"Face registered: {student_name} ({grade}) person_id={person_id} "
            f"photo #{photo_num} phones={phone_str}"
        )
    finally:
        await db.close()

    del img_bytes

    notify_count = len(normalized_phones)
    notify_note = (
        "Both parents" if notify_count >= 2
        else "You"
    )

    if photo_num == 1:
        confirm_msg = (
            f"Thank you! Your ward *{student_name}* ({grade}) has been "
            f"registered for automatic attendance tracking.\n\n"
            f"{notify_note} will receive a WhatsApp notification whenever "
            f"{student_name} arrives at school.\n\n"
            f"You can send more photos from different angles to improve "
            f"recognition accuracy."
        )
    else:
        confirm_msg = (
            f"Photo #{photo_num} for *{student_name}* ({grade}) has been "
            f"added successfully.\n\n"
            f"More photos from different angles help improve recognition accuracy."
        )

    await save_message(bot_phone, sender, confirm_msg, "whatsapp", "outgoing")
    await send_whatsapp_message(reply_to, confirm_msg)
    return True


async def _try_register_teacher_face(
    sender: str, reply_to: str, bot_phone: str,
    media_info: dict, teacher_entry: dict,
) -> bool:
    """Register a teacher's face when they send a photo to the bot.

    Teachers send selfies for face recognition attendance via reception
    and entry gate cameras.
    """
    teacher_name = teacher_entry["teacher"].split("/")[0].strip()
    teacher_grade = teacher_entry.get("grade", "")
    teacher_phone = teacher_entry.get("whatsapp", "")

    # Normalize PI Sheet phone
    phone_digits = re.sub(r"\D", "", teacher_phone)
    if len(phone_digits) == 10:
        phone_digits = "91" + phone_digits

    # Always include the sender's WhatsApp number (the number they registered from).
    # This ensures teachers who aren't in the PI Sheet still get notifications.
    sender_digits = re.sub(r"\D", "", sender)
    sender_normalized = "91" + sender_digits[-10:] if len(sender_digits) >= 10 else sender_digits
    if sender_normalized and len(sender_normalized) >= 12:
        if phone_digits and phone_digits != sender_normalized:
            # Combine PI Sheet phone + sender phone (deduped)
            phone_digits = f"{phone_digits},{sender_normalized}"
        elif not phone_digits:
            phone_digits = sender_normalized

    person_id = "TEACHER_" + re.sub(r"[^a-zA-Z0-9]", "_", teacher_name.upper())

    # Download the image
    cloud_media_id = media_info.get("cloud_media_id", "")
    direct_url = media_info.get("url", "")
    img_bytes = None

    if cloud_media_id:
        from app.services.whatsapp_service import download_cloud_media
        img_bytes, _ = await download_cloud_media(cloud_media_id)
    elif direct_url:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(direct_url)
                if resp.status_code == 200:
                    img_bytes = resp.content
        except Exception as e:
            logger.error(f"Teacher face registration: download error: {e}")

    if not img_bytes:
        logger.error(f"Teacher face registration: failed to download image from {sender}")
        return False

    # Store the face image in the cloud DB
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*), phone FROM agent_registered_faces WHERE person_id = ?",
            (person_id,),
        )
        count_row = await cursor.fetchone()
        existing_count = count_row[0] if count_row else 0
        existing_phone = count_row[1] if count_row and count_row[1] else ""

        # Merge sender's phone into existing phone list if not already there
        if existing_phone and sender_normalized not in existing_phone:
            phone_digits = f"{existing_phone},{sender_normalized}"
        elif not existing_phone:
            phone_digits = phone_digits  # use the one we computed above

        angle = "front" if existing_count == 0 else f"angle_{existing_count + 1}"

        await db.execute(
            "INSERT INTO agent_registered_faces "
            "(person_id, name, role, phone, angle, image_data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, teacher_name, "Teacher", phone_digits, angle, img_bytes),
        )

        # Also update phone on ALL existing entries for this person
        if phone_digits:
            await db.execute(
                "UPDATE agent_registered_faces SET phone = ? "
                "WHERE person_id = ? COLLATE NOCASE",
                (phone_digits, person_id),
            )

        await db.commit()
        photo_num = existing_count + 1

        logger.info(
            f"Teacher face registered: {teacher_name} ({teacher_grade}) "
            f"person_id={person_id} photo #{photo_num} phone={phone_digits}"
        )
    finally:
        await db.close()

    del img_bytes

    if photo_num == 1:
        confirm_msg = (
            f"Thank you, *{teacher_name}*! Your face has been registered "
            f"for automatic attendance tracking.\n\n"
            f"You will receive a WhatsApp notification when you arrive "
            f"at school each morning.\n\n"
            f"You can send more photos from different angles to improve "
            f"recognition accuracy."
        )
    else:
        confirm_msg = (
            f"Photo #{photo_num} for *{teacher_name}* added successfully.\n\n"
            f"More photos from different angles help improve recognition accuracy."
        )

    await save_message(bot_phone, sender, confirm_msg, "whatsapp", "outgoing")
    await send_whatsapp_message(reply_to, confirm_msg)
    return True


@router.post("/webhook/cloud")
async def receive_cloud_api_message(request: Request):
    """Handle incoming WhatsApp messages from Meta Cloud API webhook."""
    body = await request.json()
    # Still need to handle webhook verification even when disabled
    if not BOT_ENABLED:
        # But don't skip status updates / verification GETs
        if body.get("object") == "whatsapp_business_account":
            return {"status": "ok", "note": "bot disabled"}
    logger.info(f"Received Cloud API webhook: {body}")

    # Cloud API sends status updates too — ignore those
    if body.get("object") != "whatsapp_business_account":
        return {"status": "ok"}

    parsed = parse_cloud_api_message(body)
    if parsed is None:
        return {"status": "ok"}

    sender, message_text, reply_to, media_info = parsed

    # --- Deduplicate ---
    entry = body.get("entry", [{}])[0]
    changes = entry.get("changes", [{}])[0]
    value = changes.get("value", {})
    msgs = value.get("messages", [{}])
    msg_obj = msgs[0] if msgs else {}
    msg_id = msg_obj.get("id", "")
    if msg_id and await _is_duplicate(msg_id):
        logger.info(f"Duplicate Cloud API webhook for id={msg_id}, skipping.")
        return {"status": "ok"}

    # --- Extract quoted/reply context ---
    # WhatsApp Cloud API includes 'context.id' when user replies to a message
    quoted_msg_id = msg_obj.get("context", {}).get("id", "")
    quoted_context_text = ""
    if quoted_msg_id:
        quoted_msg = await get_message_by_wa_id(quoted_msg_id)
        if quoted_msg:
            quoted_context_text = quoted_msg["content"]
            logger.info(
                f"[REPLY CONTEXT] User replied to msg {quoted_msg_id}: "
                f"{quoted_context_text[:100]}"
            )
        else:
            # Fallback: try to find the original message via Cloud API
            logger.info(f"[REPLY CONTEXT] Quoted msg {quoted_msg_id} not in DB, trying Cloud API lookup")
            try:
                import httpx as _httpx_ctx
                from app.services.whatsapp_service import get_cloud_token, get_cloud_phone_id
                _token = get_cloud_token()
                _phone_id = get_cloud_phone_id()
                if _token and _phone_id:
                    async with _httpx_ctx.AsyncClient() as _client:
                        _resp = await _client.get(
                            f"https://graph.facebook.com/v25.0/{_phone_id}/messages/{quoted_msg_id}",
                            headers={"Authorization": f"Bearer {_token}"},
                            timeout=10.0,
                        )
                        if _resp.status_code == 200:
                            _data = _resp.json()
                            _body = (_data.get("text", {}).get("body", "")
                                     or _data.get("image", {}).get("caption", "")
                                     or _data.get("document", {}).get("caption", ""))
                            if _body:
                                quoted_context_text = _body
                                logger.info(f"[REPLY CONTEXT] Got from Cloud API: {_body[:100]}")
            except Exception as _e:
                logger.warning(f"[REPLY CONTEXT] Cloud API lookup failed: {_e}")

            # Fallback 2: use the last bot message to this user as context
            if not quoted_context_text:
                try:
                    _db = await get_db()
                    try:
                        _cur = await _db.execute(
                            """SELECT content FROM messages
                               WHERE receiver = ? AND direction = 'outgoing'
                               ORDER BY timestamp DESC LIMIT 1""",
                            (sender,),
                        )
                        _row = await _cur.fetchone()
                        if _row:
                            quoted_context_text = _row[0]
                            logger.info(
                                f"[REPLY CONTEXT] Using last bot message as context: "
                                f"{quoted_context_text[:100]}"
                            )
                    finally:
                        await _db.close()
                except Exception as _e2:
                    logger.warning(f"[REPLY CONTEXT] DB fallback failed: {_e2}")

    logger.info(f"Cloud API message from {sender}: {message_text} | media: {media_info is not None}")

    # --- Silently ignore reactions, stickers, and other non-content messages ---
    # These should NOT be forwarded to teachers or processed by GPT.
    _msg_type_cloud = msg_obj.get("type", "")
    if _msg_type_cloud in ("reaction", "sticker", "interactive", "button", "unsupported"):
        logger.info(f"[SKIP] Ignoring {_msg_type_cloud} message from {sender}")
        return {"status": "ok"}

    # --- Handle forwarded messages from parents ---
    # When a parent forwards a message from another app (e.g. Claude, another
    # WhatsApp chat), we should NOT send it to GPT. Instead, buffer it and ask
    # the parent what they want done with it. When they reply, forward their
    # query + the buffered content to the teacher.
    _is_msg_forwarded = msg_obj.get("context", {}).get("forwarded", False)
    if not _is_msg_forwarded:
        # Also check for frequently_forwarded flag
        _is_msg_forwarded = msg_obj.get("context", {}).get("frequently_forwarded", False)

    # Check if this message is a reply to a buffered forwarded message
    _buffered_fwd = _recent_forwarded.pop(sender, None)
    if _buffered_fwd and (_time_mod.time() - _buffered_fwd["timestamp"]) < _FORWARDED_MSG_TTL:
        # Parent is replying with context for a previously forwarded message.
        # Treat their reply as the query and include the forwarded content.
        _fwd_content = _buffered_fwd["content"]
        logger.info(f"[FORWARDED] Parent {sender} provided context for forwarded msg: {message_text[:80]}")
        # Prepend forwarded content as quoted context for the teacher
        _combined_text = (
            f"{message_text}\n\n"
            f"[Forwarded content: \"{_fwd_content[:500]}\"]"
        )
        # Route to class teacher with combined context
        parent_routed = await try_route_parent_to_class_teacher(
            sender, _combined_text, reply_to, _buffered_fwd.get("media_info"),
        )
        if parent_routed:
            return {"status": "ok"}
        # If not routed (parent not recognized), try mentioned teachers
        _mentioned_fwd = find_mentioned_teachers(message_text)
        if _mentioned_fwd:
            await forward_to_teachers_and_confirm(
                sender, _combined_text, reply_to, _buffered_fwd.get("media_info"),
            )
            return {"status": "ok"}
        # Fall through to normal processing with the combined text
        message_text = _combined_text

    # If THIS message is forwarded (from another app), buffer it and ask parent
    if _is_msg_forwarded and not _is_teacher_phone(sender) and not _is_admin_panel(sender):
        # Buffer the forwarded content
        _recent_forwarded[sender] = {
            "content": message_text,
            "media_info": media_info,
            "timestamp": _time_mod.time(),
            "reply_to": reply_to,
        }
        logger.info(f"[FORWARDED] Buffered forwarded message from {sender}: {message_text[:80]}")
        bot_phone = "bot"
        await save_message(sender, bot_phone, message_text, "whatsapp", "incoming")
        _ask_msg = (
            "I received a forwarded message. What would you like me to do with this?\n\n"
            "Please share your query (e.g. _\"Ask Reva ma'am what is this\"_ or "
            "_\"Forward this to class teacher\"_) and I will relay it along with the "
            "forwarded content."
        )
        await save_message(bot_phone, sender, _ask_msg, "whatsapp", "outgoing")
        await send_whatsapp_message(reply_to, _ask_msg)
        return {"status": "ok"}

    # --- Admin holiday commands ---
    # Admins can type: "holiday today", "holiday tomorrow",
    # "holiday 2026-05-09 Summer break", "remove holiday 2026-05-09"
    if message_text and _is_admin_panel(sender):
        _hl = message_text.strip().lower()
        if _hl.startswith("holiday") or _hl.startswith("remove holiday"):
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            from app.database import get_db as _gdb
            _ist = _tz(_td(hours=5, minutes=30))
            _today = _dt.now(_ist)
            _removing = _hl.startswith("remove holiday")
            _rest = message_text.strip()
            if _removing:
                _rest = _rest[len("remove holiday"):].strip()
            else:
                _rest = _rest[len("holiday"):].strip()

            # Parse date
            _hol_date = None
            _reason = "Holiday"
            if not _rest or _rest.lower() == "today":
                _hol_date = _today.strftime("%Y-%m-%d")
            elif _rest.lower() == "tomorrow":
                _hol_date = (_today + _td(days=1)).strftime("%Y-%m-%d")
            else:
                # Try YYYY-MM-DD format
                _parts = _rest.split(None, 1)
                try:
                    _dt.strptime(_parts[0], "%Y-%m-%d")
                    _hol_date = _parts[0]
                    if len(_parts) > 1:
                        _reason = _parts[1]
                except ValueError:
                    # Try DD-MM-YYYY
                    try:
                        _parsed = _dt.strptime(_parts[0], "%d-%m-%Y")
                        _hol_date = _parsed.strftime("%Y-%m-%d")
                        if len(_parts) > 1:
                            _reason = _parts[1]
                    except ValueError:
                        _reason = _rest
                        _hol_date = _today.strftime("%Y-%m-%d")

            if _hol_date:
                _hdb = await _gdb()
                try:
                    if _removing:
                        await _hdb.execute(
                            "DELETE FROM school_holidays WHERE date = ?",
                            (_hol_date,),
                        )
                        await _hdb.commit()
                        await send_whatsapp_message(
                            reply_to,
                            f"Holiday removed for {_hol_date}. Attendance will resume on this date.",
                        )
                    else:
                        await _hdb.execute(
                            "INSERT OR REPLACE INTO school_holidays (date, reason) VALUES (?, ?)",
                            (_hol_date, _reason),
                        )
                        await _hdb.commit()
                        await send_whatsapp_message(
                            reply_to,
                            f"Holiday marked for {_hol_date}: {_reason}\n\n"
                            f"No attendance notifications will be sent on this date.",
                        )
                finally:
                    await _hdb.close()
                return {"status": "ok"}

    # --- Voice message transcription (Cloud API) ---
    if media_info and media_info.get("type") == "audioMessage":
        cloud_media_id = media_info.get("cloud_media_id", "")
        if cloud_media_id:
            # Cloud API CDN URLs require Bearer token auth, so we must
            # download the audio via download_cloud_media() first and
            # pass the raw bytes to transcribe_audio().
            from app.services.whatsapp_service import download_cloud_media
            audio_bytes, audio_mime = await download_cloud_media(cloud_media_id)
            if audio_bytes:
                logger.info(f"Voice message detected from {sender} (Cloud API), transcribing...")
                transcribed_text = await transcribe_audio(
                    audio_bytes=audio_bytes, content_type=audio_mime,
                )
                if transcribed_text:
                    logger.info(f"Voice transcription for {sender}: {transcribed_text[:100]}")
                    message_text = transcribed_text
                    await send_whatsapp_message(
                        reply_to,
                        f"Voice message received:\n{transcribed_text}\n\nProcessing your query..."
                    )
                else:
                    logger.warning(f"Could not transcribe voice message from {sender}")
                    await send_whatsapp_message(
                        reply_to,
                        "I received your voice message but could not understand it clearly. "
                        "Could you please type your question or send another voice note?"
                    )
                    return {"status": "ok"}

    # BULK SEND SAFEGUARD
    # NOTE: pause + save_message are INSIDE the try block so that
    # resume_after_bot_reply() is always called even if save_message raises.
    try:
        await pause_for_bot_reply()

        bot_phone = "bot"
        await save_message(sender, bot_phone, message_text, "whatsapp", "incoming", wa_message_id=msg_id)

        # --- Check for buffered image + follow-up text (face registration prompt) ---
        # When someone sends a photo without caption, the bot asks:
        #   "What is this? Is this photo for face registration?"
        # If the follow-up text is an affirmation (yes/haan/ji) or a person's
        # name, register the buffered image for face recognition.
        # If the follow-up is "no" or a non-name response, discard the buffer.
        if not media_info and message_text.strip():
            _buffered = _recent_images.pop(sender, None)
            if _buffered and (_time_mod.time() - _buffered["timestamp"]) < _RECENT_IMAGE_TTL:
                _reply_text = message_text.strip()
                _reply_lower = _reply_text.lower()

                # Check if reply is an affirmation (yes, haan, ji, etc.)
                _AFFIRMATIVE_WORDS = {
                    "yes", "y", "ya", "yeah", "yep", "yup", "sure",
                    "haan", "haa", "ha", "ji", "ji haan", "haanji",
                    "ok", "okay", "register", "register it",
                    "registration", "face registration", "for registration",
                }
                _NEGATIVE_WORDS = {
                    "no", "nah", "nahi", "na", "nope", "n",
                    "not", "cancel", "skip", "ignore",
                }

                _is_affirmative = _reply_lower in _AFFIRMATIVE_WORDS
                _is_negative = _reply_lower in _NEGATIVE_WORDS

                # Check if reply looks like a person's name
                _name_words_buf = [
                    w for w in re.sub(r"[^\w\s]", " ", _reply_text).split()
                    if len(w) >= 2 and w.isalpha()
                    and w.lower() not in {"the", "of", "is", "at", "in", "for", "and", "as", "my",
                                          "show", "check", "hi", "hello", "help", "please",
                                          "homework", "thanks", "ok", "yes", "no"}
                ]
                _is_name_reply = len(_name_words_buf) >= 2 and not _is_negative

                if _is_negative:
                    logger.info(f"[IMAGE BUFFER] Sender {sender} said '{_reply_text}' — NOT for registration, discarding buffer")
                    _discard_msg = "Okay, the photo will not be registered for face recognition."
                    await save_message(bot_phone, sender, _discard_msg, "whatsapp", "outgoing")
                    await send_whatsapp_message(reply_to, _discard_msg)
                    return {"status": "ok"}

                if _is_affirmative:
                    # User confirmed registration but didn't provide a name yet.
                    # Ask for the name.
                    logger.info(f"[IMAGE BUFFER] Sender {sender} confirmed registration — asking for name")
                    _ask_name_msg = "Please share the name of the person in the photo (e.g. *Firstname Lastname* or *Firstname Lastname Grade 5A*)."
                    await save_message(bot_phone, sender, _ask_name_msg, "whatsapp", "outgoing")
                    await send_whatsapp_message(reply_to, _ask_name_msg)
                    # Re-buffer the image so the next text message (the name) can link to it
                    _recent_images[sender] = _buffered
                    _recent_images[sender]["timestamp"] = _time_mod.time()
                    _recent_images[sender]["confirmed_registration"] = True
                    return {"status": "ok"}

                if _is_name_reply:
                    logger.info(
                        f"[IMAGE BUFFER] Linking buffered image + name text '{_reply_text}' "
                        f"from {sender} for face registration"
                    )
                    _clean_buf_name = _extract_person_name(_reply_text)
                    if _clean_buf_name:
                        sender_digits = re.sub(r"\D", "", sender)
                        sender_last10 = sender_digits[-10:] if len(sender_digits) >= 10 else sender_digits
                        synthetic_entry = {
                            "teacher": _clean_buf_name,
                            "grade": "",
                            "whatsapp": sender_last10,
                        }
                        try:
                            handled = await _try_register_teacher_face(
                                sender, reply_to, bot_phone,
                                _buffered["media_info"], synthetic_entry,
                            )
                            if handled:
                                return {"status": "ok"}
                        except Exception as e:
                            logger.error(f"[IMAGE BUFFER] Face reg error: {e}", exc_info=True)

        # ================================================================
        # MEDIA CLASSIFICATION — TWO STRICTLY SEPARATE PIPELINES
        # ================================================================
        # CATEGORY 1 — Face Registration (name/name+class/name+staff)
        #   → Face-recognition pipeline ONLY. NEVER forwarded as media.
        # CATEGORY 2 — General Media/Documents (homework, PDFs, etc.)
        #   → Forward normally. NEVER triggers face recognition.
        # ================================================================
        has_media = media_info and media_info.get("type") in ("imageMessage", "documentMessage", "videoMessage")
        has_image = media_info and media_info.get("type") == "imageMessage" and media_info.get("cloud_media_id")
        is_teacher = _is_teacher_phone(sender)
        is_non_image_media = media_info and media_info.get("type") in ("documentMessage", "videoMessage")
        logger.info(f"[MEDIA CHECK] sender={sender} has_media={has_media} has_image={has_image} is_teacher={is_teacher} is_non_image={is_non_image_media}")

        # --- STEP 1: Classify ALL media using the unified classifier ---
        if has_media:
            content_class = _classify_media_content(media_info)
            logger.info(f"[MEDIA CLASSIFY] Classified as '{content_class}' for sender={sender} caption='{(media_info.get('caption','') or '')[:80]}'")

            # ==============================================================
            # PIPELINE 1 — FACE REGISTRATION (Category 1)
            # ==============================================================
            # Triggered for: name+class, name-only, name+staff, registration keywords
            # Action: Send to face-recognition module ONLY.
            # NEVER forward as normal media. NEVER store as document.
            if content_class == "face_registration":
                caption_raw = (media_info.get("caption", "") or "").strip()
                has_class_indicator = bool(_CAPTION_CLASS_RE.search(caption_raw)) if caption_raw else False

                if has_class_indicator:
                    # Name + Class → student/child face registration
                    logger.info(f"[FACE REG] Student photo (name+class) from {sender}")
                    try:
                        face_reg_handled = await _try_register_child_face(
                            sender, reply_to, bot_phone, media_info,
                        )
                        if face_reg_handled:
                            return {"status": "ok"}
                    except Exception as img_exc:
                        logger.error(f"[FACE REG] Child face registration error: {img_exc}", exc_info=True)
                    _ack_msg = "Photo received for face registration."
                    await save_message(bot_phone, sender, _ack_msg, "whatsapp", "outgoing")
                    await send_whatsapp_message(reply_to, _ack_msg)
                    return {"status": "ok"}
                else:
                    # Name-only or name+staff → teacher/staff face registration
                    _clean_name = _extract_person_name(caption_raw)
                    if not _clean_name:
                        _clean_name = f"Staff_{sender[-10:]}"
                    logger.info(f"[FACE REG] Staff/teacher photo from {sender} name='{_clean_name}'")
                    sender_digits = re.sub(r"\D", "", sender)
                    sender_last10 = sender_digits[-10:] if len(sender_digits) >= 10 else sender_digits

                    # If sender is a known teacher, use their TEACHER_DATA entry
                    _teacher_entry = is_teacher if isinstance(is_teacher, dict) else None
                    if _teacher_entry:
                        reg_entry = _teacher_entry
                    else:
                        reg_entry = {
                            "teacher": _clean_name,
                            "grade": "",
                            "whatsapp": sender_last10,
                        }
                    try:
                        handled = await _try_register_teacher_face(
                            sender, reply_to, bot_phone, media_info, reg_entry,
                        )
                        if handled:
                            return {"status": "ok"}
                    except Exception as e:
                        logger.error(f"[FACE REG] Teacher/staff face reg error: {e}", exc_info=True)
                    _ack_msg = (
                        f"Photo received for *{_clean_name}*. "
                        f"Face registration is being processed."
                    )
                    await save_message(bot_phone, sender, _ack_msg, "whatsapp", "outgoing")
                    await send_whatsapp_message(reply_to, _ack_msg)
                    return {"status": "ok"}

            # ==============================================================
            # PIPELINE 2 — GENERAL MEDIA / DOCUMENT SHARING (Category 2)
            # ==============================================================
            # Triggered for: homework photos, PDFs, worksheets, screenshots,
            # voice notes, announcements, any normal parent-teacher media.
            # Action: Forward normally. NEVER run face recognition.
            # Teacher media must fall through to homework broadcast / relay handlers.
            if content_class == "document":
                # Buffer captionless images from PARENTS and ask if for registration.
                # Teachers skip this — their captionless images should reach the
                # broadcast/relay handlers below.
                if has_image and not is_teacher:
                    caption_raw = (media_info.get("caption", "") or "").strip()
                    if not caption_raw:
                        _recent_images[sender] = {
                            "media_info": media_info,
                            "timestamp": _time_mod.time(),
                            "reply_to": reply_to,
                            "bot_phone": bot_phone,
                        }
                        logger.info(f"[IMAGE BUFFER] Buffered captionless image from {sender} — asking if for registration")
                        _ask_msg = (
                            "What is this? Is this photo for face registration?\n\n"
                            "If *yes*, please reply with the person's name "
                            "(e.g. *Firstname Lastname* or *Firstname Lastname Grade 5A*).\n"
                            "If *no*, reply *no* and I will not register it."
                        )
                        await save_message(bot_phone, sender, _ask_msg, "whatsapp", "outgoing")
                        await send_whatsapp_message(reply_to, _ask_msg)
                        return {"status": "ok"}

                # Teacher media: let it fall through to homework broadcast
                # and teacher reply relay handlers below
                if is_teacher:
                    logger.info(f"[MEDIA] Teacher document/media from {sender} — passing to broadcast/relay handlers")
                    # Do NOT intercept — skip entire parent forwarding block
                    # and let it reach detect_and_handle_teacher_homework_broadcast
                    # and try_relay_teacher_reply below.
                else:
                    # Parent media: forward to teacher(s)
                    logger.info(f"[MEDIA] General file/document from parent {sender}")
                    caption = (media_info.get("caption", "") or "").strip()
                    forward_text = caption if caption else "Shared a file/image"

                    # --- Homework Review (for images only) ---
                    # ONLY trigger when caption explicitly requests a homework check.
                    # Keywords: "check please", "please check", "check homework",
                    # "check hw", "check h.w."
                    _cap_lower = caption.lower() if caption else ""
                    _is_hw_check_request = any(
                        _cap_lower.startswith(kw) for kw in [
                            "check please", "please check", "check homework",
                            "check hw", "check h.w.", "check h.w",
                        ]
                    )
                    is_image_msg = media_info.get("type") == "imageMessage"
                    cloud_mid = media_info.get("cloud_media_id", "")
                    if is_image_msg and cloud_mid and _is_hw_check_request:
                        logger.info(f"[HOMEWORK REVIEW] Auto-reviewing image from parent {sender}")
                        try:
                            from app.services.whatsapp_service import download_cloud_media
                            from app.services.openai_service import generate_homework_review

                            img_bytes, img_mime = await download_cloud_media(cloud_mid)
                            if img_bytes:
                                children_hw = await _lookup_parent_child_class(sender)
                                child_name = children_hw[0]["student_name"] if children_hw else ""
                                child_grade = children_hw[0]["grade"] if children_hw else ""

                                review = await generate_homework_review(
                                    img_bytes, img_mime, caption,
                                    student_name=child_name, grade=child_grade,
                                )
                                del img_bytes
                                await save_message(bot_phone, sender, review, "whatsapp", "outgoing")
                                await send_whatsapp_message(reply_to, review)
                                logger.info(f"[HOMEWORK REVIEW] Sent review to {sender}")
                                # Do NOT return here — continue to forward image to teacher
                            else:
                                logger.warning(f"[HOMEWORK REVIEW] Could not download image for {sender}")
                        except Exception as hw_exc:
                            logger.error(f"[HOMEWORK REVIEW] Error: {hw_exc}", exc_info=True)

                    # If caption mentions specific teachers by name, forward to
                    # ALL of them (not just the class teacher).
                    _mentioned = find_mentioned_teachers(forward_text)
                    if _mentioned:
                        await forward_to_teachers_and_confirm(
                            sender, forward_text, reply_to, media_info,
                        )
                        return {"status": "ok"}

                    # Forward attachment + text to class teacher (all file types)
                    # Try parent→teacher routing (works for non-admin parents)
                    routed = await try_route_parent_to_class_teacher(
                        sender, forward_text, reply_to, media_info,
                    )
                    if routed:
                        return {"status": "ok"}

                    # For admins who are also parents, or when routing skipped:
                    # Look up children directly and forward to class teacher
                    children = await _lookup_parent_child_class(sender)
                    if children:
                        # If caption mentions a grade, route to that teacher
                        grade_teacher = find_teacher_by_grade(forward_text)
                        if not grade_teacher and len(children) == 1:
                            # Single child → auto-route to their class teacher
                            grade_teacher = find_teacher_by_grade(children[0]["grade"])
                        if not grade_teacher:
                            # Try matching child name in caption
                            msg_lower = forward_text.lower()
                            for child in children:
                                name_parts = child["student_name"].lower().split()
                                if any(part in msg_lower for part in name_parts if len(part) > 2):
                                    grade_teacher = find_teacher_by_grade(child["grade"])
                                    break

                        if grade_teacher:
                            child_for_label = children[0]
                            if len(children) > 1:
                                # Try to match the specific child
                                for c in children:
                                    if c["grade"].lower().replace(" ", "") == grade_teacher["grade"].lower().replace(" ", ""):
                                        child_for_label = c
                                        break

                            teacher_name = grade_teacher["teacher"].split("/")[0].strip()
                            teacher_phone = grade_teacher.get("whatsapp", "")
                            teacher_email = grade_teacher.get("email", "")
                            parent_label = f"Parent of {child_for_label['student_name']} ({child_for_label['grade']})"

                            # Download media ONCE for both WhatsApp and email
                            _dl_bytes3, _dl_mime3 = await _download_media_bytes(media_info)

                            if teacher_phone:
                                import asyncio as _asyncio_fwd
                                from app.services.whatsapp_service import (
                                    send_cloud_template_message as _send_tmpl3,
                                    forward_cloud_media_to_recipient,
                                )
                                chat_id = _teacher_chat_id(teacher_phone)
                                _trec = teacher_phone
                                if len(_trec) == 10:
                                    _trec = "91" + _trec

                                _query_msg = (
                                    f"\U0001f4e9 *File from {parent_label}:*\n\n"
                                    f"\"{forward_text[:500]}\"\n\n"
                                    f"_Reply to this message \u2014 your response will be forwarded to the parent._"
                                )

                                # Try sending the query directly first (works if
                                # conversation window is already open).
                                _fwd_ok = await send_whatsapp_message(chat_id, _query_msg)
                                if _fwd_ok:
                                    logger.info(f"[CLOUD FILE FWD] Direct query sent to {teacher_name}")
                                else:
                                    # Conversation window closed — send template to
                                    # open a business-initiated window, then resend.
                                    logger.info(f"[CLOUD FILE FWD] Direct msg failed, sending template for {teacher_name}")
                                    _tmpl_ok = await _send_tmpl3(
                                        _trec, "ppis_class_assignment",
                                        body_params=[f"File from {parent_label}", forward_text[:400]],
                                    )
                                    if _tmpl_ok:
                                        # Wait for template delivery to open conversation window
                                        await _asyncio_fwd.sleep(4)
                                        _fwd_ok = await send_whatsapp_message(chat_id, _query_msg)
                                        if _fwd_ok:
                                            logger.info(f"[CLOUD FILE FWD] Query sent after template to {teacher_name}")
                                        else:
                                            _fwd_ok = True  # template itself was delivered
                                            logger.warning(f"[CLOUD FILE FWD] Query after template failed for {teacher_name}, template delivered")
                                    else:
                                        logger.error(f"[CLOUD FILE FWD] Both direct msg and template failed for {teacher_name}")

                                # Send media attachment
                                if _fwd_ok:
                                    await _asyncio_fwd.sleep(3)
                                    _mok3 = False
                                    try:
                                        _mok3 = await forward_cloud_media_to_recipient(media_info, chat_id, caption=caption)
                                    except Exception as _me3:
                                        logger.error(f"[CLOUD FILE FWD] Media forward error: {_me3}")
                                    if _mok3:
                                        logger.info(f"[CLOUD FILE FWD] Media sent to {teacher_name}")
                                    else:
                                        logger.warning(f"[CLOUD FILE FWD] WhatsApp media failed for {teacher_name} — email has attachment")

                            # Always send email with attachment (reliable channel)
                            if teacher_email:
                                _email_body3 = (
                                    f"Dear {teacher_name},\n\n"
                                    f"{parent_label} has shared the following file/query "
                                    f"via the PPIS Bot:\n\n"
                                    f"\"{forward_text[:500]}\"\n\n"
                                    f"Kindly reply to this email and your response will be "
                                    f"forwarded back to the parent.\n\n"
                                    f"Regards,\nPPIS Bot"
                                )
                                _ea3 = _make_email_attachments(media_info, _dl_bytes3, _dl_mime3)
                                _eok3 = await send_email_async(
                                    teacher_email,
                                    f"PPIS Bot: File from {parent_label}",
                                    _email_body3,
                                    "PP International School",
                                    attachments=_ea3 or None,
                                )
                                if _eok3:
                                    logger.info(f"[CLOUD FILE FWD] Email with attachment sent to {teacher_name} ({teacher_email})")
                                else:
                                    logger.error(f"[CLOUD FILE FWD] Email FAILED for {teacher_name} ({teacher_email})")

                                # Save conversation for 2-way relay
                                await save_forwarded_conversation(
                                    teacher_phone=teacher_phone,
                                    teacher_name=teacher_name,
                                    teacher_grade=grade_teacher["grade"],
                                    original_chat_id=reply_to,
                                    sender_phone=sender,
                                    original_message=forward_text[:500],
                                )
                                confirm = (
                                    f"Your file has been forwarded to *{teacher_name}* "
                                    f"({grade_teacher['grade']}). "
                                    f"You will receive a reply when the teacher responds."
                                )
                                await save_message(bot_phone, sender, confirm, "whatsapp", "outgoing")
                                await send_whatsapp_message(reply_to, confirm)
                                return {"status": "ok"}

                        elif len(children) > 1:
                            # Multiple children, can't determine which teacher
                            child_list = "\n".join(
                                f"- {c['student_name']} ({c['grade']})" for c in children
                            )
                            ask_msg = (
                                "We have found multiple wards linked to your number:\n"
                                f"{child_list}\n\n"
                                "Kindly specify which ward this query is regarding "
                                "by replying with their name or class."
                            )
                            await save_message(bot_phone, sender, ask_msg, "whatsapp", "outgoing")
                            await send_whatsapp_message(reply_to, ask_msg)
                            return {"status": "ok"}

                    # Fallback — simple ack
                    doc_msg = "File received successfully."
                    await save_message(bot_phone, sender, doc_msg, "whatsapp", "outgoing")
                    await send_whatsapp_message(reply_to, doc_msg)
                    return {"status": "ok"}

        # Check if a teacher is broadcasting homework to parents of their class
        # NOTE: This MUST run before try_relay_teacher_reply() so broadcast
        # messages ("Summary Sheet - Class 3A", homework, etc.) don't get
        # intercepted as a "reply" to a previous forwarded conversation.
        hw_broadcast = await detect_and_handle_teacher_homework_broadcast(
            sender, message_text, reply_to, media_info,
        )
        if hw_broadcast:
            return {"status": "ok"}

        # Check if this is a teacher replying to a forwarded message
        relayed = await try_relay_teacher_reply(sender, message_text, reply_to, media_info)
        if relayed:
            return {"status": "ok"}

        # Check for classroom snapshot request (live photo from DVR camera)
        # NOTE: This MUST run BEFORE try_direct_message / GPT so that
        # "show reception", "show class 10 a", "show 12 b" etc. from admins
        # are routed to the camera system, not to GPT or the DM handler.
        snapshot_handled = await detect_and_handle_snapshot_request(sender, message_text, reply_to)
        if snapshot_handled:
            return {"status": "ok"}

        # Check if this is a direct-message request
        dm_handled = await try_direct_message(sender, message_text, reply_to, media_info)
        if dm_handled:
            return {"status": "ok"}

        # Check if sender has a pending query
        pending_handled = await try_handle_pending_query(sender, message_text, reply_to)
        if pending_handled:
            return {"status": "ok"}

        # Check for homework query — forward to class teacher
        hw_handled = await detect_and_handle_homework_query(sender, message_text, reply_to, media_info)
        if hw_handled:
            return {"status": "ok"}

        # Check for leave application — forward to class teacher
        leave_handled = await detect_and_handle_leave_application(sender, message_text, reply_to, media_info)
        if leave_handled:
            return {"status": "ok"}

        # Class list query
        class_list_answer = await lookup_class_list(message_text)
        if class_list_answer:
            await save_message(bot_phone, sender, class_list_answer, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, class_list_answer)
            return {"status": "ok"}

        # Transport query
        transport_answer = lookup_transport(message_text)
        if transport_answer:
            await save_message(bot_phone, sender, transport_answer, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, transport_answer)
            return {"status": "ok"}

        # --- Activity-specific contact lookup (use word boundaries to avoid false matches) ---
        from app.services.openai_service import _is_hindi
        _msg_low = message_text.lower()
        _activity_contact = None

        def _activity_word_match_cloud(keywords: list[str], text: str) -> bool:
            """Match activity keywords using word boundaries so 'act' doesn't match 'contact'."""
            for kw in keywords:
                if re.search(r'\b' + re.escape(kw) + r'\b', text, re.IGNORECASE):
                    return True
            return False

        _skating_kw = ["skating", "skate", "roller", "\u0938\u094d\u0915\u0947\u0924\u093f\u0902\u0917", "\u0938\u094d\u0915\u0947\u091f"]
        _dance_kw = ["dance", "dancing", "nritya", "\u0928\u0943\u0924\u094d\u092f", "\u0921\u093e\u0902\u0938"]
        _theatre_kw = ["theatre", "theater", "drama", "acting", "natya", "natak", "\u0928\u093e\u091f\u0915", "\u0925\u093f\u090f\u091f\u0930", "\u0928\u093e\u091f\u094d\u092f"]
        _music_kw = ["music", "singing", "song", "instrument", "band", "sangeet", "\u0938\u0902\u0917\u0940\u0924", "\u0917\u093e\u0928\u093e", "\u0917\u0940\u0924"]
        _art_kw = ["art class", "drawing", "painting", "sketch", "craft", "kala", "\u0915\u0932\u093e", "\u091a\u093f\u0924\u094d\u0930", "\u092a\u0947\u0902\u091f\u093f\u0902\u0917"]
        if _activity_word_match_cloud(_skating_kw, _msg_low):
            _activity_contact = ("skating competition", "Mr. Gautam", "9910933523")
        elif _activity_word_match_cloud(_dance_kw, _msg_low):
            _activity_contact = ("dance", "Mr. Akshay", "8368869135")
        elif _activity_word_match_cloud(_theatre_kw, _msg_low):
            _activity_contact = ("theatre", "Mr. Nupin", "8851496687")
        elif _activity_word_match_cloud(_music_kw, _msg_low):
            _activity_contact = ("music", "Mr. Ankur", "9319502107")
        elif _activity_word_match_cloud(_art_kw, _msg_low):
            _activity_contact = ("art", "Ms. Preity", "9870501952")

        if _activity_contact:
            _act, _name, _phone = _activity_contact
            if _is_hindi(message_text):
                _act_msg = (
                    f"{_act.title()} \u0938\u0947 \u0938\u0902\u092c\u0902\u0927\u093f\u0924 \u0915\u093f\u0938\u0940 \u092d\u0940 \u092a\u094d\u0930\u0936\u094d\u0928 \u0915\u0947 \u0932\u093f\u090f, "
                    f"\u0915\u0943\u092a\u092f\u093e {_name} \u0938\u0947 \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0947\u0902: "
                    f"{_phone}\n\n"
                )
            else:
                _act_msg = (
                    f"For any queries related to {_act}, "
                    f"please contact {_name}:\n"
                    f"{_phone}\n\n"
                )
            await save_message(bot_phone, sender, _act_msg, "whatsapp", "outgoing")
            await send_whatsapp_message(reply_to, _act_msg)
            return {"status": "ok"}

        # --- Parent → Class Teacher routing ---
        # If the sender is a known parent, auto-route their query to the
        # respective class teacher (based on child's grade in PI sheet).
        # This ensures controlled parent–teacher communication with strict
        # class-wise boundaries.
        # Include quoted message context so teacher sees the full conversation
        routed_text = message_text
        if quoted_context_text:
            routed_text = (
                f"[Replying to: \"{quoted_context_text[:300]}\"]\n\n"
                f"{message_text}"
            )
        parent_routed = await try_route_parent_to_class_teacher(
            sender, routed_text, reply_to, media_info,
        )
        if parent_routed:
            return {"status": "ok"}

        # --- Admin/teacher → Class Teacher forwarding ---
        # If sender is admin/teacher and message mentions "ct", "class teacher",
        # "ask teacher", "confirm from teacher", etc., forward to the class teacher
        # instead of GPT.
        if _is_admin_panel(sender) or _is_teacher_phone(sender):
            _ct_pattern = re.compile(
                r'\b(?:ct|class\s*teacher|ask\s*(?:the\s*)?(?:ct|teacher)|'
                r'confirm\s*(?:from|with)\s*(?:ct|teacher)|'
                r'forward\s*(?:to\s*)?(?:ct|teacher)|'
                r'send\s*(?:to\s*)?(?:ct|teacher)|'
                r'check\s*(?:from|with)\s*(?:ct|teacher))\b',
                re.IGNORECASE,
            )
            if _ct_pattern.search(message_text):
                # Find the grade in the message or via sender's child
                grade_teacher = find_teacher_by_grade(message_text)
                if not grade_teacher:
                    # Try from quoted context
                    if quoted_context_text:
                        grade_teacher = find_teacher_by_grade(quoted_context_text)
                if not grade_teacher:
                    # Try from sender's children (if admin is also a parent)
                    _admin_children = await _lookup_parent_child_class(sender)
                    if _admin_children and len(_admin_children) == 1:
                        grade_teacher = find_teacher_by_grade(_admin_children[0]["grade"])

                if grade_teacher:
                    teacher_name = grade_teacher["teacher"].split("/")[0].strip()
                    teacher_phone = grade_teacher.get("whatsapp", "")
                    if teacher_phone:
                        chat_id = _teacher_chat_id(teacher_phone)
                        fwd_text = message_text
                        if quoted_context_text:
                            fwd_text = (
                                f"[Regarding: \"{quoted_context_text[:300]}\"]\n\n"
                                f"{message_text}"
                            )
                        admin_label = f"Admin ({sender[-4:]})"
                        fwd_msg = (
                            f"Dear {teacher_name},\n\n"
                            f"{admin_label} has sent a query via the PPIS Bot:\n\n"
                            f"\"{fwd_text[:500]}\"\n\n"
                            f"Kindly reply to this message and your response "
                            f"will be forwarded back.\n\n"
                            f"Warm regards,\nPP International School"
                        )
                        await send_whatsapp_message(chat_id, fwd_msg)
                        # Save conversation for 2-way relay
                        await save_forwarded_conversation(
                            teacher_phone=teacher_phone,
                            teacher_name=teacher_name,
                            teacher_grade=grade_teacher["grade"],
                            original_chat_id=reply_to,
                            sender_phone=sender,
                            original_message=fwd_text[:500],
                        )
                        confirm = (
                            f"Your query has been forwarded to *{teacher_name}* "
                            f"({grade_teacher['grade']}). "
                            f"You will receive a reply when the teacher responds."
                        )
                        await save_message(bot_phone, sender, confirm, "whatsapp", "outgoing")
                        await send_whatsapp_message(reply_to, confirm)
                        return {"status": "ok"}
                else:
                    # Can't determine which teacher — ask for grade
                    ask_grade = (
                        "I understand you want to contact a class teacher. "
                        "Could you please specify the class/grade "
                        "(e.g., Grade 3C, 5A, Nursery 1)?"
                    )
                    await save_message(bot_phone, sender, ask_grade, "whatsapp", "outgoing")
                    await send_whatsapp_message(reply_to, ask_grade)
                    return {"status": "ok"}

        # --- GPT fallback (for admins, teachers, or unknown senders) ---
        system_prompt = await get_system_prompt()
        history = await get_conversation_history(sender)

        # If user replied to a specific message, prepend that context
        gpt_query = message_text
        if quoted_context_text:
            gpt_query = (
                f"[The user is replying to this previous message: "
                f"\"{quoted_context_text[:500]}\"]\n\n"
                f"User's reply: {message_text}"
            )

        ai_response = await generate_response(gpt_query, system_prompt, history)

        # Check if the bot couldn't answer
        if _is_unknown_response(ai_response):
            await save_pending_query(sender, reply_to, message_text)
            from app.services.openai_service import _is_hindi

            escalation_contacts = (
                "\n\nFor immediate assistance, please contact:\n"
                "- School Helpline / Front Desk: 8800935552\n"
                "- Ms. Harpreet Kaur (Administration Incharge): 9599488106"
            )
            escalation_contacts_hi = (
                "\n\n\u0924\u0941\u0930\u0902\u0924 \u0938\u0939\u093e\u092f\u0924\u093e \u0915\u0947 \u0932\u093f\u090f \u0938\u0902\u092a\u0930\u094d\u0915 \u0915\u0930\u0947\u0902:\n"
                "- School Helpline / Front Desk: 8800935552\n"
                "- Ms. Harpreet Kaur (Administration Incharge): 9599488106"
            )

            if _is_hindi(message_text):
                ask_class_msg = (
                    "\u092e\u0941\u091d\u0947 \u0916\u0947\u0926 \u0939\u0948, \u092e\u0947\u0930\u0947 \u092a\u093e\u0938 \u0907\u0938 \u092c\u093e\u0930\u0947 \u092e\u0947\u0902 \u091c\u093e\u0928\u0915\u093e\u0930\u0940 \u0928\u0939\u0940\u0902 \u0939\u0948\u0964 "
                    "\u0915\u0943\u092a\u092f\u093e \u0905\u092a\u0928\u0947 \u092c\u091a\u094d\u091a\u0947 \u0915\u0940 *\u0915\u0915\u094d\u0937\u093e \u0914\u0930 \u0938\u0947\u0915\u094d\u0936\u0928* \u092c\u0924\u093e\u090f\u0902 "
                    "(\u091c\u0948\u0938\u0947 Grade 5A, Nursery 1, Prep 2) \u0924\u093e\u0915\u093f \u092e\u0948\u0902 \u0906\u092a\u0915\u093e \u092a\u094d\u0930\u0936\u094d\u0928 "
                    "\u0915\u0915\u094d\u0937\u093e \u0905\u0927\u094d\u092f\u093e\u092a\u0915 \u0915\u094b WhatsApp \u0914\u0930 \u0908\u092e\u0947\u0932 \u0926\u094d\u0935\u093e\u0930\u093e \u092d\u0947\u091c \u0938\u0915\u0942\u0901\u0964"
                    + escalation_contacts_hi
                )
            else:
                ask_class_msg = (
                    "I'm sorry, I don't have specific information about that. "
                    "Could you please share your ward's *class and section* "
                    "(e.g. Grade 5A, Nursery 1, Prep 2) so I can forward your query "
                    "to the class teacher via WhatsApp and email?"
                    + escalation_contacts
                )
            await send_whatsapp_message(reply_to, ask_class_msg)
            esc_wa_id = getattr(send_whatsapp_message, "last_wa_id", "") or ""
            await save_message(bot_phone, sender, ask_class_msg, "whatsapp", "outgoing", wa_message_id=esc_wa_id)
            return {"status": "ok"}

        await send_whatsapp_message(reply_to, ai_response)
        sent_wa_id = getattr(send_whatsapp_message, "last_wa_id", "") or ""
        await save_message(bot_phone, sender, ai_response, "whatsapp", "outgoing", wa_message_id=sent_wa_id)

        # Forward to teachers if mentioned
        await forward_to_teachers_and_confirm(sender, message_text, reply_to, media_info)

        return {"status": "ok"}
    finally:
        await resume_after_bot_reply()


@router.post("/webhook/send-notification-email")
async def send_notification_email(request: Request):
    """Send a notification email via the server's SMTP (used when caller VM has no SMTP access)."""
    # Auth check — prevent unauthenticated arbitrary email sending
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            return {"success": False, "error": "Unauthorized"}
    body = await request.json()
    to = body.get("to", "")
    subject = body.get("subject", "")
    email_body = body.get("body", "")
    if not to or not subject or not email_body:
        return {"success": False, "error": "Missing to, subject, or body"}
    result = await send_email_async(to, subject, email_body, sender_name="PPIS Bot System")
    return {"success": result}
