"""
Law Minister Office Attendance Bot — auto-response handler.

Routes incoming WhatsApp messages for the Law Minister phone number
(+91 84489 43232, Phone Number ID: 1168433719678061) and sends
automated responses based on greeting/unrelated/office-topic classification.

Uses the same WABA token as the PPIS bot but a different phone number ID.
"""

import logging
import os
import re

import httpx

logger = logging.getLogger("law_minister_bot")

# Law Minister WhatsApp Phone Number ID
LAW_MINISTER_PHONE_ID = os.getenv(
    "LAW_MINISTER_PHONE_ID", "1168433719678061"
)

# Token is shared across the WABA — reuse the same Cloud token
def _get_token() -> str:
    return os.getenv("WHATSAPP_CLOUD_TOKEN", "")


# ---------- Language Detection ----------

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def detect_language(text: str) -> str:
    """Detect Hindi, English, or Hinglish from text."""
    has_devanagari = bool(_DEVANAGARI_RE.search(text))
    has_latin = bool(re.search(r"[a-zA-Z]", text))
    if has_devanagari and has_latin:
        return "hinglish"
    if has_devanagari:
        return "hindi"
    return "english"


# ---------- Greeting Detection ----------

GREETING_KEYWORDS = {
    "hi", "hello", "hey", "namaste", "namashkar", "namaskar",
    "good morning", "good afternoon", "good evening",
    "shubh prabhat", "pranam", "jai hind",
    "नमस्ते", "नमस्कार", "प्रणाम", "शुभ प्रभात",
    "जय हिन्द", "जय हिंद",
}

ALLOWED_TOPICS = {
    "attendance", "registration", "face", "photo", "photograph",
    "present", "absent", "check-in", "checkin", "office", "timing",
    "time", "leave", "help", "register", "status", "camera",
    "name", "report", "summary",
    "उपस्थिति", "रजिस्ट्रेशन", "फोटो", "कार्यालय", "समय",
    "छुट्टी", "मदद", "स्थिति", "रिपोर्ट",
}


def _is_greeting(text: str) -> bool:
    normalised = text.strip().lower()
    if normalised in GREETING_KEYWORDS:
        return True
    for kw in GREETING_KEYWORDS:
        if normalised.startswith(kw):
            return True
    return False


# ---------- Response Templates ----------

GREETING_EN = (
    "Hello,\n\n"
    "Welcome to the Office Attendance Assistance System of "
    "Shri Arjun Ram Meghwal Ji.\n\n"
    "I am an automated attendance bot designed only for office "
    "attendance and work-related communication.\n\n"
    "For face registration, kindly share:\n"
    "• 2 clear front-face photographs with name\n\n"
    "Thankyou"
)

GREETING_HI = (
    "नमस्कार,\n\n"
    "श्री अर्जुन राम मेघवाल जी के कार्यालय उपस्थिति सहायता प्रणाली "
    "में आपका स्वागत है।\n\n"
    "मैं आपके कार्यालय उपस्थिति (Attendance) हेतु एक स्वचालित बॉट हूँ।\n\n"
    "कृपया फेस रजिस्ट्रेशन के लिए अपने नाम के साथ "
    "2 स्पष्ट फ्रंट फेस फोटो साझा करें।\n\n"
    "धन्यवाद"
)

GREETING_MIXED = (
    "Hello / Namashkar,\n\n"
    "Welcome to the Office Attendance Assistance System of "
    "Shri Arjun Ram Meghwal Ji.\n\n"
    "I am an automated attendance bot designed only for office "
    "attendance and work-related communication.\n\n"
    "For face registration, kindly share:\n"
    "• 2 clear front-face photographs with name\n\n"
    "Thankyou / Dhanyawad"
)

UNRELATED_EN = (
    "Kindly note that this automated system is restricted to official "
    "office attendance and work-related communication only."
)

UNRELATED_HI = (
    "यह स्वचालित प्रणाली केवल कार्यालय उपस्थिति एवं "
    "आधिकारिक कार्य संबंधी संवाद हेतु सीमित है।"
)

UNRELATED_MIXED = (
    "यह स्वचालित प्रणाली केवल कार्यालय उपस्थिति एवं "
    "आधिकारिक कार्य संबंधी संवाद हेतु सीमित है। / "
    "This automated system is restricted to official office "
    "attendance and work-related communication only."
)


def _get_response(text: str) -> str | None:
    """Return auto-response for the message, or None for allowed topics."""
    lang = detect_language(text)

    if _is_greeting(text):
        if lang == "hindi":
            return GREETING_HI
        if lang == "hinglish":
            return GREETING_MIXED
        return GREETING_EN

    normalised = text.strip().lower()
    for topic in ALLOWED_TOPICS:
        if topic in normalised:
            return None  # Allowed topic — no auto-reply needed

    # Unrelated topic
    if lang == "hindi":
        return UNRELATED_HI
    if lang == "hinglish":
        return UNRELATED_MIXED
    return UNRELATED_EN


# ---------- Send via Law Minister Phone Number ----------

async def _send_text(to: str, body: str) -> bool:
    """Send a text message from the Law Minister phone number."""
    token = _get_token()
    if not token:
        logger.error("WHATSAPP_CLOUD_TOKEN not set")
        return False

    recipient = to.split("@")[0] if "@" in to else to
    if len(recipient) == 10:
        recipient = "91" + recipient

    url = f"https://graph.facebook.com/v21.0/{LAW_MINISTER_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"body": body},
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, json=payload, headers=headers, timeout=15.0
            )
            if resp.status_code == 200:
                logger.info(
                    f"Law Minister bot replied to {recipient}: {body[:50]}..."
                )
                return True
            logger.error(
                f"Law Minister bot send error {resp.status_code}: {resp.text}"
            )
            return False
    except Exception as e:
        logger.error(f"Law Minister bot send failed: {e}")
        return False


# ---------- Deduplication ----------

# In-memory set of recently processed message IDs (TTL-based cleanup)
_processed_ids: dict[str, float] = {}
_DEDUP_TTL = 300  # 5 minutes


def _is_duplicate(msg_id: str) -> bool:
    """Check if a message ID was already processed (prevents retry duplicates)."""
    import time
    now = time.time()

    # Clean old entries
    expired = [k for k, t in _processed_ids.items() if now - t > _DEDUP_TTL]
    for k in expired:
        del _processed_ids[k]

    if msg_id in _processed_ids:
        return True
    _processed_ids[msg_id] = now
    return False


# ---------- Main Handler ----------

async def handle_webhook(body: dict) -> dict:
    """Process an incoming webhook payload for the Law Minister phone number.

    Returns a dict with status and actions taken.
    """
    actions = []

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                if message.get("type") != "text":
                    continue

                sender = message.get("from", "")
                text = message.get("text", {}).get("body", "")
                msg_id = message.get("id", "")

                if msg_id and _is_duplicate(msg_id):
                    logger.info(f"Duplicate message {msg_id}, skipping.")
                    continue

                auto_reply = _get_response(text)
                if auto_reply:
                    sent = await _send_text(sender, auto_reply)
                    lang = detect_language(text)
                    category = "greeting" if _is_greeting(text) else "unrelated"
                    actions.append({
                        "from": sender,
                        "text": text,
                        "category": category,
                        "language": lang,
                        "response_sent": sent,
                    })
                    logger.info(
                        f"Auto-replied to {sender} ({category}/{lang}): "
                        f"{text[:40]}"
                    )
                else:
                    actions.append({
                        "from": sender,
                        "text": text,
                        "category": "allowed",
                        "language": detect_language(text),
                        "response_sent": False,
                        "note": "Office topic — no auto-reply",
                    })

    return {"status": "ok", "bot": "law_minister", "actions": actions}
