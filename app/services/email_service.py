"""Email service for sending reminder/notification emails to teachers via SMTP."""

import html as html_module
import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# SMTP configuration (Google Workspace)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "info@ppischool.in")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")


def send_email(
    to_email: str,
    subject: str,
    body: str,
    sender_name: str = "PPIS Bot",
    attachments: Optional[List[Tuple[bytes, str, str]]] = None,
) -> bool:
    """Send an email via SMTP.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        sender_name: Display name of the sender.
        attachments: Optional list of (file_bytes, filename, mime_type) tuples.

    Returns:
        True if the email was sent successfully, False otherwise.
    """
    smtp_user = SMTP_USER
    smtp_password = SMTP_PASSWORD or os.getenv("SMTP_PASSWORD", "")

    if not smtp_password:
        logger.error("SMTP_PASSWORD not configured — cannot send email")
        return False

    # Use "mixed" when we have attachments so both text and files are included
    msg = MIMEMultipart("mixed" if attachments else "alternative")
    msg["From"] = f"{sender_name} <{smtp_user}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    # Text content (wrapped in "alternative" sub-part when attachments exist)
    text_part = MIMEMultipart("alternative") if attachments else msg
    text_part.attach(MIMEText(body, "plain", "utf-8"))

    # Simple HTML version
    html_body = html_module.escape(body).replace("\n", "<br>")
    html = f"""<html><body style="font-family: Arial, sans-serif; line-height: 1.6;">
{html_body}
<br><br>
<small style="color: #888;">— Sent via PPIS Bot</small>
</body></html>"""
    text_part.attach(MIMEText(html, "html", "utf-8"))

    if attachments:
        msg.attach(text_part)
        for file_bytes, filename, mime_type in attachments:
            maintype, _, subtype = mime_type.partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(file_bytes)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", "attachment", filename=filename,
            )
            msg.attach(part)

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        try:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [to_email], msg.as_string())
            server.quit()
            logger.info(f"Email sent to {to_email}: {subject}")
            return True
        except Exception:
            server.close()
            raise
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP auth failed (may need App Password): {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False


async def send_email_async(
    to_email: str,
    subject: str,
    body: str,
    sender_name: str = "PPIS Bot",
    attachments: Optional[List[Tuple[bytes, str, str]]] = None,
) -> bool:
    """Async wrapper around send_email (runs SMTP in thread to avoid blocking)."""
    import asyncio
    import functools
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(
            send_email, to_email, subject, body, sender_name,
            attachments=attachments,
        ),
    )
