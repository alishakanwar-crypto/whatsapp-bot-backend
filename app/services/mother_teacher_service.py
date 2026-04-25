"""Class Teacher routing system for all classes (Popsicles to Grade 12).

Only the assigned Class Teacher is authorized to communicate with parents.
Messages are never forwarded to other teachers; access to chat history
is restricted; and every blocked or unauthorized attempt is logged.
"""

import logging
import re

from app.database import get_db
from app.services.openai_service import TEACHER_DATA

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Grades that follow the Class Teacher routing system.
# ALL grades from Popsicles to Grade 12 are included — only the assigned
# class teacher may communicate with parents of these grades.
# The set is derived dynamically from TEACHER_DATA so it stays in sync.
# ---------------------------------------------------------------------------
MOTHER_TEACHER_GRADES: set[str] = {entry["grade"] for entry in TEACHER_DATA}


def is_mother_teacher_grade(grade: str) -> bool:
    """Return True if *grade* follows the Mother Teacher routing rule."""
    return grade in MOTHER_TEACHER_GRADES


def get_class_teacher_for_grade(grade: str) -> dict | None:
    """Return the TEACHER_DATA entry for the assigned class teacher of *grade*."""
    for entry in TEACHER_DATA:
        if entry["grade"] == grade:
            return entry
    return None


def _normalize_phone(phone: str) -> str:
    """Strip to last 10 digits for comparison."""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def is_assigned_class_teacher(teacher_entry: dict, child_grade: str) -> bool:
    """Return True if *teacher_entry* is the assigned class teacher for *child_grade*."""
    assigned = get_class_teacher_for_grade(child_grade)
    if assigned is None:
        return False
    return (
        _normalize_phone(assigned.get("whatsapp", ""))
        == _normalize_phone(teacher_entry.get("whatsapp", ""))
    )


def is_teacher_phone_assigned_for_grade(teacher_phone: str, child_grade: str) -> bool:
    """Return True if *teacher_phone* belongs to the assigned class teacher."""
    assigned = get_class_teacher_for_grade(child_grade)
    if assigned is None:
        return False
    assigned_phone = assigned.get("whatsapp", "")
    if not assigned_phone:
        return False
    return _normalize_phone(teacher_phone) == _normalize_phone(assigned_phone)


# ---------------------------------------------------------------------------
# Logging helpers — persist blocked / unauthorized attempts to the database
# ---------------------------------------------------------------------------

async def log_blocked_message(
    sender_phone: str,
    child_grade: str,
    target_teacher_name: str,
    target_teacher_phone: str,
    message_snippet: str,
    reason: str = "mother_teacher_routing",
) -> None:
    """Log a blocked parent message attempt."""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO mother_teacher_blocked_messages
               (sender_phone, child_grade, target_teacher_name,
                target_teacher_phone, message_snippet, reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                sender_phone,
                child_grade,
                target_teacher_name,
                target_teacher_phone,
                message_snippet[:500],
                reason,
            ),
        )
        await db.commit()
        logger.info(
            "Mother Teacher: blocked message from %s (grade %s) to %s — %s",
            sender_phone, child_grade, target_teacher_name, reason,
        )
    finally:
        await db.close()


async def log_unauthorized_access(
    accessor_phone: str,
    accessor_role: str,
    attempted_resource: str,
    child_grade: str,
    reason: str = "unauthorized_access",
) -> None:
    """Log an unauthorized access attempt on Mother Teacher grade data."""
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO mother_teacher_access_logs
               (accessor_phone, accessor_role, attempted_resource,
                child_grade, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (accessor_phone, accessor_role, attempted_resource, child_grade, reason),
        )
        await db.commit()
        logger.info(
            "Mother Teacher: unauthorized access by %s (%s) on %s for grade %s",
            accessor_phone, accessor_role, attempted_resource, child_grade,
        )
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Auto-reply text sent to parents when their message is blocked
# ---------------------------------------------------------------------------

MOTHER_TEACHER_AUTO_REPLY_EN = (
    "For your child's class, "
    "please contact only the assigned Class Teacher for all queries.\n\n"
    "Your assigned Class Teacher is *{teacher_name}* ({grade}).\n"
    "All your queries will be routed to them directly.\n\n"
    "Thank you for your cooperation.\n"
    "Warm regards,\nPP International School"
)

MOTHER_TEACHER_AUTO_REPLY_HI = (
    "आपके बच्चे की कक्षा के लिए, "
    "कृपया सभी प्रश्नों के लिए केवल नियुक्त Class Teacher से संपर्क करें।\n\n"
    "आपकी नियुक्त Class Teacher हैं *{teacher_name}* ({grade}).\n"
    "आपके सभी प्रश्न सीधे उन्हें भेजे जाएंगे।\n\n"
    "आपके सहयोग के लिए धन्यवाद।\n"
    "सादर,\nPP International School"
)


def build_auto_reply(teacher_name: str, grade: str, is_hindi: bool = False) -> str:
    """Build the auto-reply message for blocked Mother Teacher routing."""
    template = MOTHER_TEACHER_AUTO_REPLY_HI if is_hindi else MOTHER_TEACHER_AUTO_REPLY_EN
    return template.format(teacher_name=teacher_name, grade=grade)


# ---------------------------------------------------------------------------
# Filter helpers used by webhook forwarding logic
# ---------------------------------------------------------------------------

def filter_teachers_for_mother_teacher(
    teachers: list[dict], child_grade: str
) -> list[dict]:
    """Given a list of teacher entries to forward a message to, filter to only
    the assigned class teacher if the child's grade follows Mother Teacher rules.

    If the grade is NOT a Mother Teacher grade, return the list unmodified.
    """
    if not is_mother_teacher_grade(child_grade):
        return teachers

    assigned = get_class_teacher_for_grade(child_grade)
    if assigned is None:
        return teachers

    assigned_phone = _normalize_phone(assigned.get("whatsapp", ""))
    return [
        t for t in teachers
        if _normalize_phone(t.get("whatsapp", "")) == assigned_phone
    ]
