"""Attachment handling service for the Two-Way Relay Messaging System.

Validates file types, checks file sizes, blocks unsafe extensions,
and manages attachment metadata in the relay_attachments table.
"""

import logging
import os
import re

from app.database import get_db

logger = logging.getLogger(__name__)

# Maximum file size: 16 MB (WhatsApp Cloud API limit)
MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024

# Allowed MIME types grouped by category
ALLOWED_MIME_TYPES: dict[str, list[str]] = {
    "image": [
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
    ],
    "video": [
        "video/mp4", "video/3gpp", "video/quicktime", "video/x-msvideo",
    ],
    "audio": [
        "audio/aac", "audio/ogg", "audio/mpeg", "audio/amr", "audio/mp4",
        "audio/opus", "audio/wav", "audio/x-wav",
    ],
    "document": [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "text/csv",
    ],
}

# Flat set for quick lookup
_ALL_ALLOWED_MIMES: set[str] = set()
for _mimes in ALLOWED_MIME_TYPES.values():
    _ALL_ALLOWED_MIMES.update(_mimes)


def classify_media_type(mime_type: str, filename: str = "") -> str:
    """Return the media category: image, video, audio, document, or unknown."""
    mime_lower = mime_type.lower()
    for category, mimes in ALLOWED_MIME_TYPES.items():
        if mime_lower in mimes:
            return category
    # Fallback: guess from MIME prefix
    if mime_lower.startswith("image/"):
        return "image"
    if mime_lower.startswith("video/"):
        return "video"
    if mime_lower.startswith("audio/"):
        return "audio"
    if mime_lower.startswith("application/") or mime_lower.startswith("text/"):
        return "document"
    return "unknown"


def get_extension(filename: str) -> str:
    """Extract lowercase file extension without the dot."""
    _, ext = os.path.splitext(filename)
    return ext.lstrip(".").lower()


async def is_blocked_extension(filename: str) -> tuple[bool, str]:
    """Check if a file extension is blocked. Returns (is_blocked, reason)."""
    ext = get_extension(filename)
    if not ext:
        return False, ""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT reason FROM relay_blocked_file_types WHERE extension = ?",
            (ext,),
        )
        row = await cursor.fetchone()
        if row:
            return True, row["reason"]
        return False, ""
    finally:
        await db.close()


def is_allowed_mime(mime_type: str) -> bool:
    """Check if a MIME type is in the allowed list."""
    return mime_type.lower() in _ALL_ALLOWED_MIMES


def validate_file_size(size_bytes: int) -> tuple[bool, str]:
    """Check if file size is within limits. Returns (is_valid, error_msg)."""
    if size_bytes <= 0:
        return False, "File is empty"
    if size_bytes > MAX_FILE_SIZE_BYTES:
        max_mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
        actual_mb = round(size_bytes / (1024 * 1024), 1)
        return False, f"File too large ({actual_mb} MB). Maximum is {max_mb} MB."
    return True, ""


async def validate_attachment(
    filename: str, mime_type: str, file_size: int
) -> tuple[bool, str]:
    """Full validation of an attachment. Returns (is_valid, error_message)."""
    # Check blocked extensions
    blocked, reason = await is_blocked_extension(filename)
    if blocked:
        return False, f"File type '.{get_extension(filename)}' is not allowed: {reason}"

    # Check file size
    size_ok, size_err = validate_file_size(file_size)
    if not size_ok:
        return False, size_err

    # Check MIME type (warn but don't block unknown MIMEs for flexibility)
    if not is_allowed_mime(mime_type):
        logger.warning(
            f"Attachment '{filename}' has uncommon MIME type: {mime_type}"
        )

    return True, ""


async def save_attachment_metadata(
    relay_message_id: int,
    file_type: str,
    file_name: str,
    mime_type: str,
    file_size: int,
    cloud_media_id: str = "",
    storage_path: str = "",
) -> int:
    """Save attachment metadata to DB. Returns the attachment ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO relay_attachments "
            "(relay_message_id, file_type, file_name, mime_type, file_size, "
            "cloud_media_id, storage_path, validation_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'valid')",
            (relay_message_id, file_type, file_name, mime_type, file_size,
             cloud_media_id, storage_path),
        )
        await db.commit()
        return cursor.lastrowid or 0
    finally:
        await db.close()


async def get_attachments_for_message(relay_message_id: int) -> list[dict]:
    """Get all attachments for a relay message."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, file_type, file_name, mime_type, file_size, "
            "cloud_media_id, storage_path, validation_status, created_at "
            "FROM relay_attachments WHERE relay_message_id = ?",
            (relay_message_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "file_type": r["file_type"],
                "file_name": r["file_name"],
                "mime_type": r["mime_type"],
                "file_size": r["file_size"],
                "cloud_media_id": r["cloud_media_id"],
                "storage_path": r["storage_path"],
                "validation_status": r["validation_status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        await db.close()


def friendly_file_type(mime_type: str, filename: str = "") -> str:
    """Return a user-friendly file type label."""
    ext = get_extension(filename)
    labels = {
        "pdf": "PDF", "doc": "Word Document", "docx": "Word Document",
        "xls": "Excel Spreadsheet", "xlsx": "Excel Spreadsheet",
        "ppt": "Presentation", "pptx": "Presentation",
        "jpg": "Image", "jpeg": "Image", "png": "Image",
        "gif": "Image", "webp": "Image",
        "mp4": "Video", "3gp": "Video", "mov": "Video",
        "aac": "Voice Note", "ogg": "Voice Note", "mp3": "Audio",
        "amr": "Voice Note", "opus": "Voice Note", "wav": "Audio",
        "txt": "Text File", "csv": "CSV File",
    }
    if ext in labels:
        return labels[ext]
    category = classify_media_type(mime_type, filename)
    return category.capitalize() if category != "unknown" else "File"
