"""MarkItDown service — converts documents to Markdown text.

Uses Microsoft's markitdown library to extract text from PDFs, DOCX, XLSX,
PPTX, and other document formats. This avoids sending documents as base64
images to GPT-4o Vision, reducing token usage significantly.
"""

import io
import logging
import tempfile
import os

logger = logging.getLogger(__name__)

_md_instance = None


def _get_markitdown():
    """Lazy-load MarkItDown instance."""
    global _md_instance
    if _md_instance is None:
        try:
            from markitdown import MarkItDown
            _md_instance = MarkItDown()
            logger.info("[MARKITDOWN] Initialized successfully")
        except ImportError:
            logger.warning("[MARKITDOWN] markitdown not installed — document text extraction unavailable")
            return None
    return _md_instance


# MIME types that MarkItDown can handle
SUPPORTED_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/html",
    "text/plain",
    "text/csv",
}

# File extensions MarkItDown supports
SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".html", ".htm", ".txt", ".csv",
}


def is_supported(mime_type: str | None = None, filename: str | None = None) -> bool:
    """Check if MarkItDown can process this file type."""
    if mime_type and mime_type in SUPPORTED_MIMES:
        return True
    if filename:
        _, ext = os.path.splitext(filename.lower())
        return ext in SUPPORTED_EXTENSIONS
    return False


def extract_text(file_bytes: bytes, mime_type: str | None = None,
                 filename: str | None = None) -> str | None:
    """Extract text from a document as Markdown.

    Args:
        file_bytes: Raw file content.
        mime_type: MIME type of the file (for extension guessing).
        filename: Original filename (used for extension).

    Returns:
        Extracted markdown text, or None if extraction fails.
    """
    md = _get_markitdown()
    if md is None:
        return None

    # Determine file extension for temp file
    ext = ".bin"
    if filename:
        _, ext = os.path.splitext(filename.lower())
        if not ext:
            ext = ".bin"
    elif mime_type:
        ext_map = {
            "application/pdf": ".pdf",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-powerpoint": ".ppt",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "text/html": ".html",
            "text/plain": ".txt",
            "text/csv": ".csv",
        }
        ext = ext_map.get(mime_type, ".bin")

    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        result = md.convert(tmp_path)
        text = result.text_content.strip() if result.text_content else ""

        if text:
            logger.info(f"[MARKITDOWN] Extracted {len(text)} chars from {ext} file")
            return text
        else:
            logger.warning(f"[MARKITDOWN] No text extracted from {ext} file")
            return None

    except Exception as e:
        logger.error(f"[MARKITDOWN] Extraction failed for {ext}: {e}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
