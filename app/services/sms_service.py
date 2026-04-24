"""
Pluggable SMS Service Module.

This module provides an abstract interface for SMS providers.
To integrate a specific SMS provider (MSG91, TextLocal, Fast2SMS, etc.),
implement the send_sms function with the provider's API.

Example providers for India:
- MSG91: https://msg91.com/
- TextLocal: https://www.textlocal.in/
- Fast2SMS: https://www.fast2sms.com/
"""

import os
import logging
import httpx

logger = logging.getLogger(__name__)

SMS_PROVIDER = os.getenv("SMS_PROVIDER", "none")


async def send_sms(to: str, message: str) -> bool:
    """
    Send an SMS message.

    Configure SMS_PROVIDER env var to enable:
    - "none" (default): SMS disabled
    - "fast2sms": Use Fast2SMS API (set FAST2SMS_API_KEY)
    - "msg91": Use MSG91 API (set MSG91_AUTH_KEY, MSG91_SENDER_ID)
    - "textlocal": Use TextLocal API (set TEXTLOCAL_API_KEY, TEXTLOCAL_SENDER)
    """
    if SMS_PROVIDER == "none":
        logger.warning("SMS provider not configured. Message not sent.")
        return False

    if SMS_PROVIDER == "fast2sms":
        return await _send_via_fast2sms(to, message)
    elif SMS_PROVIDER == "msg91":
        return await _send_via_msg91(to, message)
    elif SMS_PROVIDER == "textlocal":
        return await _send_via_textlocal(to, message)
    else:
        logger.error(f"Unknown SMS provider: {SMS_PROVIDER}")
        return False


async def _send_via_fast2sms(to: str, message: str) -> bool:
    """Send SMS via Fast2SMS API."""
    api_key = os.getenv("FAST2SMS_API_KEY", "")
    if not api_key:
        logger.error("FAST2SMS_API_KEY not set")
        return False

    url = "https://www.fast2sms.com/dev/bulkV2"
    headers = {"authorization": api_key}
    payload = {
        "route": "q",
        "message": message,
        "language": "english",
        "flash": 0,
        "numbers": to,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                logger.info(f"SMS sent to {to} via Fast2SMS")
                return True
            else:
                logger.error(f"Fast2SMS error: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Fast2SMS error: {e}")
        return False


async def _send_via_msg91(to: str, message: str) -> bool:
    """Send SMS via MSG91 API (placeholder - implement with your MSG91 template)."""
    auth_key = os.getenv("MSG91_AUTH_KEY", "")
    if not auth_key:
        logger.error("MSG91_AUTH_KEY not set")
        return False

    logger.warning("MSG91 integration requires template-based setup. Please configure.")
    return False


async def _send_via_textlocal(to: str, message: str) -> bool:
    """Send SMS via TextLocal API."""
    api_key = os.getenv("TEXTLOCAL_API_KEY", "")
    sender = os.getenv("TEXTLOCAL_SENDER", "TXTLCL")
    if not api_key:
        logger.error("TEXTLOCAL_API_KEY not set")
        return False

    url = "https://api.textlocal.in/send/"
    payload = {
        "apikey": api_key,
        "numbers": to,
        "message": message,
        "sender": sender,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, data=payload)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    logger.info(f"SMS sent to {to} via TextLocal")
                    return True
            logger.error(f"TextLocal error: {response.text}")
            return False
    except Exception as e:
        logger.error(f"TextLocal error: {e}")
        return False


def parse_incoming_sms(body: dict) -> tuple[str, str] | None:
    """
    Parse incoming SMS webhook payload.
    This is provider-specific. Override for your SMS provider.

    Returns (sender_phone, message_text) or None.
    """
    sender = body.get("from", body.get("sender", ""))
    message = body.get("message", body.get("text", body.get("body", "")))

    if sender and message:
        return (sender, message)
    return None
