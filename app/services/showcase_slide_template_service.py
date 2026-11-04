"""Register the WhatsApp template that carries a showcase's slide.

The three-day text reminder uses a text-only template, which Meta will not
accept an image on, so the two-day slide send needs its own template with an
IMAGE header. Meta only creates such a template when the request includes an
uploaded example image, so this module uploads one of the hosted slide
thumbnails and submits the template. It is safe to run on every boot: an
existing template is left alone.
"""
import logging
import os

import httpx

from app.services import whatsapp_service
from app.services.showcase_reminder_service import (
    SHOWCASE_SLIDE_TEMPLATE,
)

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v25.0"
WABA_ID = os.environ.get("WHATSAPP_WABA_ID", "2417647228700804")
EXAMPLE_SLIDE_URL = (
    "https://ppis-whatsapp-bot.fly.dev/static/showcase_slides/"
    "slide_r02_thumb.jpg"
)
TEMPLATE_BODY = (
    "Dear Teacher,\n\n"
    "Here is the reference slide for the upcoming showcase, shared two days "
    "in advance so the slide can be prepared in time.\n\n"
    "Class: {{1}}\n"
    "Item: {{2}}\n"
    "Date: {{3}}\n\n"
    "Please use this as the backdrop reference for the presentation."
)


async def _template_exists(client: httpx.AsyncClient, token: str) -> bool:
    response = await client.get(
        f"{GRAPH}/{WABA_ID}/message_templates",
        params={"name": SHOWCASE_SLIDE_TEMPLATE, "limit": 50},
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return any(
        item.get("name") == SHOWCASE_SLIDE_TEMPLATE
        for item in response.json().get("data", [])
    )


async def _upload_example_image(
    client: httpx.AsyncClient, token: str
) -> str:
    """Upload the example slide and return Meta's header handle."""
    debug = await client.get(
        f"{GRAPH}/debug_token",
        params={"input_token": token, "access_token": token},
    )
    debug.raise_for_status()
    app_id = debug.json()["data"]["app_id"]

    image = (await client.get(EXAMPLE_SLIDE_URL)).content
    session = await client.post(
        f"{GRAPH}/{app_id}/uploads",
        params={"file_length": len(image), "file_type": "image/jpeg"},
        headers={"Authorization": f"Bearer {token}"},
    )
    session.raise_for_status()
    upload_id = session.json()["id"]

    uploaded = await client.post(
        f"{GRAPH}/{upload_id}",
        content=image,
        headers={
            "Authorization": f"OAuth {token}",
            "file_offset": "0",
            "Content-Type": "application/octet-stream",
        },
    )
    uploaded.raise_for_status()
    return uploaded.json()["h"]


async def ensure_slide_template() -> str:
    """Create the slide template if Meta does not have it yet.

    Returns the resulting status: 'exists', 'submitted' or 'failed'.
    """
    token = whatsapp_service.get_cloud_token()
    if not token:
        logger.warning("No WhatsApp cloud token — slide template not checked")
        return "failed"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if await _template_exists(client, token):
                logger.info(
                    "Showcase slide template %s already registered",
                    SHOWCASE_SLIDE_TEMPLATE,
                )
                return "exists"

            handle = await _upload_example_image(client, token)
            response = await client.post(
                f"{GRAPH}/{WABA_ID}/message_templates",
                json={
                    "name": SHOWCASE_SLIDE_TEMPLATE,
                    "language": "en",
                    "category": "UTILITY",
                    "components": [
                        {
                            "type": "HEADER",
                            "format": "IMAGE",
                            "example": {"header_handle": [handle]},
                        },
                        {
                            "type": "BODY",
                            "text": TEMPLATE_BODY,
                            "example": {
                                "body_text": [
                                    [
                                        "Grade V",
                                        "Doha Vachan",
                                        "22 December 2026",
                                    ]
                                ]
                            },
                        },
                        {
                            "type": "FOOTER",
                            "text": "PP International School",
                        },
                    ],
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code >= 400:
                logger.error(
                    "Showcase slide template rejected (%s): %s",
                    response.status_code,
                    response.text[:500],
                )
                return "failed"
            logger.info(
                "Showcase slide template submitted: %s", response.json()
            )
            return "submitted"
    except Exception:
        logger.exception("Could not register the showcase slide template")
        return "failed"


def ensure_slide_template_sync() -> None:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(ensure_slide_template())
    finally:
        loop.close()
