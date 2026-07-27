import base64
import os
import logging
import sqlite3
import httpx

logger = logging.getLogger(__name__)
last_cloud_template_message_id = ""

# ---------------------------------------------------------------------------
# Credential cache from DB (avoids async DB call on every message)
# ---------------------------------------------------------------------------
_db_creds_cache: dict[str, str] = {}
_db_creds_loaded: bool = False


def _load_db_creds():
    """Load WhatsApp credentials from the settings table (sync, cached)."""
    global _db_creds_loaded
    if _db_creds_loaded:
        return
    db_path = os.getenv("DB_PATH", "/data/app.db")
    if not os.path.exists(os.path.dirname(db_path) if os.path.dirname(db_path) else "."):
        db_path = os.path.join(os.path.dirname(__file__), "..", "..", "app.db")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('GREEN_API_ID_INSTANCE','GREEN_API_TOKEN','GREEN_API_URL',"
            "'WHATSAPP_CLOUD_TOKEN','WHATSAPP_PHONE_ID')"
        )
        for row in cur.fetchall():
            _db_creds_cache[row[0]] = row[1]
        conn.close()
    except Exception:
        pass
    _db_creds_loaded = True


def _get_cred(env_key: str, default: str = "") -> str:
    """Read from env var first, then fall back to settings table."""
    val = os.getenv(env_key, "")
    if val:
        return val
    _load_db_creds()
    return _db_creds_cache.get(env_key, default)


def refresh_creds_cache():
    """Clear the cached credentials so they are re-read from DB."""
    global _db_creds_loaded
    _db_creds_cache.clear()
    _db_creds_loaded = False


# ---------------------------------------------------------------------------
# Provider detection: "cloud" uses Meta Cloud API, "green" uses Green API
# ---------------------------------------------------------------------------

def get_whatsapp_provider() -> str:
    """Return 'cloud' if Cloud API credentials are set, else 'green'."""
    if _get_cred("WHATSAPP_CLOUD_TOKEN") and _get_cred("WHATSAPP_PHONE_ID"):
        return "cloud"
    return "green"


def get_id_instance() -> str:
    return _get_cred("GREEN_API_ID_INSTANCE")


def get_api_token() -> str:
    return _get_cred("GREEN_API_TOKEN")


def get_api_url() -> str:
    return _get_cred("GREEN_API_URL", "https://api.green-api.com")


def get_cloud_token() -> str:
    return _get_cred("WHATSAPP_CLOUD_TOKEN")


def get_cloud_phone_id() -> str:
    return _get_cred("WHATSAPP_PHONE_ID")


async def send_whatsapp_image(to: str, image_url: str, caption: str = "") -> bool:
    """Send an image via WhatsApp using Green API's sendFileByUrl."""
    id_instance = get_id_instance()
    api_token = get_api_token()
    api_url = get_api_url()

    if not id_instance or not api_token:
        logger.error("Green API credentials not configured")
        return False

    chat_id = to if "@" in to else f"{to}@c.us"
    url = f"{api_url}/waInstance{id_instance}/sendFileByUrl/{api_token}"
    payload = {
        "chatId": chat_id,
        "urlFile": image_url,
        "fileName": "image.jpg",
        "caption": caption,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=30.0)
            if response.status_code == 200:
                data = response.json()
                logger.info(f"WhatsApp image sent to {to}, id: {data.get('idMessage', 'unknown')}")
                return True
            else:
                logger.error(f"Failed to send WhatsApp image: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Error sending WhatsApp image: {e}")
        return False


async def send_whatsapp_image_file(to: str, file_path: str, caption: str = "") -> bool:
    """Send an image via WhatsApp using Green API's sendFileByUpload (multipart upload)."""
    id_instance = get_id_instance()
    api_token = get_api_token()
    api_url = get_api_url()

    if not id_instance or not api_token:
        logger.error("Green API credentials not configured")
        return False

    if not os.path.isfile(file_path):
        logger.error(f"Image file not found: {file_path}")
        return False

    chat_id = to if "@" in to else f"{to}@c.us"
    url = f"{api_url}/waInstance{id_instance}/sendFileByUpload/{api_token}"

    try:
        filename = os.path.basename(file_path)
        async with httpx.AsyncClient() as client:
            with open(file_path, "rb") as f:
                files = {"file": (filename, f, "image/jpeg")}
                data = {"chatId": chat_id, "caption": caption}
                response = await client.post(url, data=data, files=files, timeout=60.0)
            if response.status_code == 200:
                resp_data = response.json()
                logger.info(f"WhatsApp image file sent to {to}, id: {resp_data.get('idMessage', 'unknown')}")
                return True
            else:
                logger.error(f"Failed to send WhatsApp image file: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Error sending WhatsApp image file: {e}")
        return False


async def send_cloud_media(to: str, media_type: str, media_url: str = "", media_id: str = "", caption: str = "", filename: str = "") -> bool:
    """Send a media message (image/video/document/audio) via Meta Cloud API.

    Either media_url (link) or media_id (uploaded media ID) must be provided.
    media_type: 'image', 'video', 'document', 'audio'
    """
    token = get_cloud_token()
    phone_id = get_cloud_phone_id()
    if not token or not phone_id:
        logger.error("Cloud API credentials not configured for media send")
        return False

    recipient = to.split("@")[0] if "@" in to else to
    if len(recipient) == 10:
        recipient = "91" + recipient

    url = f"https://graph.facebook.com/v25.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    media_obj: dict = {}
    if media_id:
        media_obj["id"] = media_id
    elif media_url:
        media_obj["link"] = media_url
    else:
        logger.error("send_cloud_media: neither media_url nor media_id provided")
        return False

    if caption:
        media_obj["caption"] = caption
    if filename and media_type == "document":
        media_obj["filename"] = filename

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": media_type,
        media_type: media_obj,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            data = response.json()
            if "messages" in data:
                logger.info(f"Cloud API {media_type} sent to {recipient}, id: {data['messages'][0]['id'][:30]}")
                return True
            logger.error(f"Cloud API {media_type} send failed: {data}")
            return False
    except Exception as e:
        logger.error(f"Error sending Cloud API {media_type}: {e}")
        return False


async def upload_base64_image_cloud(image_base64: str, mime_type: str = "image/jpeg") -> str | None:
    """Upload a base64-encoded image to Meta Cloud API and return the media ID.

    Meta Cloud API requires media to be uploaded first before it can be sent.
    Returns the media_id string on success, or None on failure.
    """
    token = get_cloud_token()
    phone_id = get_cloud_phone_id()
    if not token or not phone_id:
        logger.error("Cloud API credentials not configured for media upload")
        return None

    image_bytes = base64.b64decode(image_base64)
    url = f"https://graph.facebook.com/v25.0/{phone_id}/media"
    headers = {"Authorization": f"Bearer {token}"}

    ext = "jpg" if "jpeg" in mime_type or "jpg" in mime_type else "png"
    files = {
        "file": (f"snapshot.{ext}", image_bytes, mime_type),
        "type": (None, mime_type),
        "messaging_product": (None, "whatsapp"),
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, files=files, timeout=60.0)
            data = response.json()
            media_id = data.get("id")
            if media_id:
                logger.info(f"Cloud API media uploaded, id: {media_id}")
                return media_id
            logger.error(f"Cloud API media upload failed: {data}")
            return None
    except Exception as e:
        logger.error(f"Error uploading media to Cloud API: {e}")
        return None


async def send_whatsapp_media(to: str, media_type: str, media_url: str = "", media_id: str = "", caption: str = "", filename: str = "") -> bool:
    """Send media via the active provider (Cloud API or Green API)."""
    provider = get_whatsapp_provider()
    if provider == "cloud":
        return await send_cloud_media(to, media_type, media_url=media_url, media_id=media_id, caption=caption, filename=filename)
    # Green API fallback
    return await forward_file_by_url(to, media_url, filename or f"{media_type}.bin", caption)


async def send_cloud_template_message(
    to: str,
    template_name: str,
    language_code: str = "en",
    body_params: list[str] | None = None,
    header_image_id: str | None = None,
    header_image_url: str | None = None,
    header_document_id: str | None = None,
    header_document_url: str | None = None,
    header_document_filename: str | None = None,
) -> bool:
    """Send a template message via Meta Cloud API.

    Template messages can be sent outside the 24-hour conversation window,
    unlike plain text messages.  ``body_params`` is a list of positional
    parameter values ({{1}}, {{2}}, …) for the template body.

    For templates with IMAGE headers, pass either ``header_image_id``
    (uploaded media ID) or ``header_image_url`` (public URL).

    For templates with DOCUMENT headers, pass ``header_document_id``
    (uploaded media ID) or ``header_document_url`` (public URL), and
    optionally ``header_document_filename``.
    """
    global last_cloud_template_message_id
    last_cloud_template_message_id = ""
    token = get_cloud_token()
    phone_id = get_cloud_phone_id()
    if not token or not phone_id:
        logger.error("Cloud API credentials not configured for template send")
        return False

    recipient = to.split("@")[0] if "@" in to else to
    if len(recipient) == 10:
        recipient = "91" + recipient

    url = f"https://graph.facebook.com/v25.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    components: list[dict] = []

    # Image header component (for templates with IMAGE header)
    if header_image_id or header_image_url:
        img_param: dict = {"type": "image"}
        if header_image_id:
            img_param["image"] = {"id": header_image_id}
        else:
            img_param["image"] = {"link": header_image_url}
        components.append({
            "type": "header",
            "parameters": [img_param],
        })

    # Document header component (for templates with DOCUMENT header)
    if header_document_id or header_document_url:
        doc_param: dict = {"type": "document"}
        doc_obj: dict = {}
        if header_document_id:
            doc_obj["id"] = header_document_id
        else:
            doc_obj["link"] = header_document_url
        if header_document_filename:
            doc_obj["filename"] = header_document_filename
        doc_param["document"] = doc_obj
        components.append({
            "type": "header",
            "parameters": [doc_param],
        })

    if body_params:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "text": p} for p in body_params
            ],
        })

    template_obj: dict = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template_obj["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "template",
        "template": template_obj,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            data = response.json()
            if "messages" in data:
                last_cloud_template_message_id = data["messages"][0]["id"]
                logger.info(
                    f"Cloud API template '{template_name}' sent to {recipient}, "
                    f"id: {last_cloud_template_message_id[:30]}"
                )
                return True
            err_code = ""
            err_msg = ""
            if "error" in data:
                err_code = data["error"].get("code", "")
                err_msg = data["error"].get("message", "")
            logger.error(
                f"Cloud API template '{template_name}' to {recipient} failed: "
                f"HTTP {response.status_code}, code={err_code}, "
                f"msg={err_msg}, full={data}"
            )
            return False
    except Exception as e:
        logger.error(
            f"Cloud API template '{template_name}' to {recipient} exception: {e}"
        )
        return False


async def send_whatsapp_message(to: str, message: str) -> bool:
    """Send a WhatsApp message via the active provider (Cloud API or Green API).

    Returns True on success. The sent message's WhatsApp ID (if available)
    is stored in send_whatsapp_message.last_wa_id after each call.

    NOTE: This only works within the 24-hour conversation window.
    For proactive/outbound messages, use ``send_whatsapp_force()`` instead.
    """
    provider = get_whatsapp_provider()
    if provider == "cloud":
        return await _send_cloud_text(to, message)
    return await _send_green_text(to, message)


# Attribute to store the last sent message ID (set by _send_cloud_text)
send_whatsapp_message.last_wa_id = ""  # type: ignore[attr-defined]


async def send_whatsapp_force(to: str, message: str) -> bool:
    """Send a WhatsApp text message (best-effort).

    NOTE: Freeform text messages only work within the 24-hour conversation
    window.  For media queries to teachers, use template-based sending
    (ppis_parent_query_1 / ppis_parent_query_2) instead of this function.

    This function is kept for text-only notifications (attendance, relay)
    where delivery is best-effort and email serves as the reliable backup.
    """
    return await send_whatsapp_message(to, message)


async def _send_cloud_text(to: str, message: str) -> bool:
    """Send a text message via Meta Cloud API."""
    send_whatsapp_message.last_wa_id = ""  # type: ignore[attr-defined]
    token = get_cloud_token()
    phone_id = get_cloud_phone_id()
    if not token or not phone_id:
        logger.error("Cloud API credentials not configured")
        return False

    # Strip Green-API chat-id suffix if present
    recipient = to.split("@")[0] if "@" in to else to
    # Ensure country code
    if len(recipient) == 10:
        recipient = "91" + recipient

    url = f"https://graph.facebook.com/v25.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"body": message},
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            data = response.json()
            if "messages" in data:
                wa_id = data["messages"][0]["id"]
                send_whatsapp_message.last_wa_id = wa_id  # type: ignore[attr-defined]
                logger.info(
                    "Cloud API message sent to recipient ending %s, id: %s",
                    recipient[-4:],
                    wa_id[:30],
                )
                return True
            logger.error(f"Cloud API send failed: {data}")
            return False
    except Exception as e:
        logger.error(f"Error sending Cloud API message: {e}")
        return False


async def send_cloud_text(to: str, message: str) -> bool:
    """Send freeform text through Meta Cloud API only."""
    return await _send_cloud_text(to, message)


async def _send_green_text(to: str, message: str) -> bool:
    """Send a WhatsApp message using Green API."""
    id_instance = get_id_instance()
    api_token = get_api_token()
    api_url = get_api_url()

    if not id_instance or not api_token:
        logger.error("Green API credentials not configured")
        return False

    # Green API expects chatId in format: 79876543210@c.us
    chat_id = to if "@" in to else f"{to}@c.us"

    url = f"{api_url}/waInstance{id_instance}/sendMessage/{api_token}"
    payload = {
        "chatId": chat_id,
        "message": message,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=30.0)
            if response.status_code == 200:
                data = response.json()
                logger.info(f"WhatsApp message sent to {to}, id: {data.get('idMessage', 'unknown')}")
                return True
            else:
                logger.error(
                    f"Failed to send WhatsApp message: {response.status_code} - {response.text}"
                )
                return False
    except Exception as e:
        logger.error(f"Error sending WhatsApp message: {e}")
        return False


async def forward_file_by_url(to: str, file_url: str, file_name: str, caption: str = "") -> bool:
    """Forward a file to a WhatsApp chat by URL (works for images, videos, documents, audio)."""
    id_instance = get_id_instance()
    api_token = get_api_token()
    api_url = get_api_url()

    if not id_instance or not api_token:
        logger.error("Green API credentials not configured")
        return False

    chat_id = to if "@" in to else f"{to}@c.us"
    url = f"{api_url}/waInstance{id_instance}/sendFileByUrl/{api_token}"
    payload = {
        "chatId": chat_id,
        "urlFile": file_url,
        "fileName": file_name,
        "caption": caption,
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=60.0)
            if response.status_code == 200:
                data = response.json()
                logger.info(f"File forwarded to {to}: {file_name}, id: {data.get('idMessage', 'unknown')}")
                return True
            else:
                logger.error(f"Failed to forward file: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Error forwarding file: {e}")
        return False


def _extract_media_info(message_data: dict, type_message: str) -> dict | None:
    """Extract media URL, filename, and caption from various media message types."""
    media_info: dict = {"url": "", "filename": "", "caption": "", "type": type_message}

    if type_message == "imageMessage":
        img_data = message_data.get("fileMessageData", {}) or message_data.get("imageMessage", {})
        media_info["url"] = img_data.get("downloadUrl", "") or img_data.get("url", "")
        media_info["filename"] = img_data.get("fileName", "image.jpg")
        media_info["caption"] = img_data.get("caption", "")
    elif type_message == "videoMessage":
        vid_data = message_data.get("fileMessageData", {}) or message_data.get("videoMessage", {})
        media_info["url"] = vid_data.get("downloadUrl", "") or vid_data.get("url", "")
        media_info["filename"] = vid_data.get("fileName", "video.mp4")
        media_info["caption"] = vid_data.get("caption", "")
    elif type_message == "documentMessage":
        doc_data = message_data.get("fileMessageData", {}) or message_data.get("documentMessage", {})
        media_info["url"] = doc_data.get("downloadUrl", "") or doc_data.get("url", "")
        media_info["filename"] = doc_data.get("fileName", "document")
        media_info["caption"] = doc_data.get("caption", "")
    elif type_message == "audioMessage":
        aud_data = message_data.get("fileMessageData", {}) or message_data.get("audioMessage", {})
        media_info["url"] = aud_data.get("downloadUrl", "") or aud_data.get("url", "")
        media_info["filename"] = aud_data.get("fileName", "audio.ogg")
        media_info["caption"] = ""
    elif type_message == "stickerMessage":
        sticker_data = message_data.get("fileMessageData", {}) or message_data.get("stickerMessage", {})
        media_info["url"] = sticker_data.get("downloadUrl", "") or sticker_data.get("url", "")
        media_info["filename"] = sticker_data.get("fileName", "sticker.webp")
        media_info["caption"] = ""
    else:
        return None

    if media_info["url"]:
        return media_info
    return None


# Extended return type: (sender, text, reply_to, media_info_or_None)
ParsedMessage = tuple[str, str, str, dict | None]


def parse_cloud_api_message(body: dict) -> ParsedMessage | None:
    """
    Parse incoming Meta Cloud API webhook payload.
    Returns (sender_phone, message_text, reply_to_phone, media_info) or None.
    """
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        if "messages" not in value:
            # Status update or other non-message event
            return None

        msg = value["messages"][0]
        sender = msg.get("from", "")
        msg_type = msg.get("type", "")
        message_id = msg.get("id", "")

        # Extract sender's WhatsApp display name from contacts array
        contacts = value.get("contacts", [])
        sender_name = ""
        if contacts:
            sender_name = contacts[0].get("profile", {}).get("name", "")

        # For Cloud API, reply_to is just the sender phone (no @c.us suffix)
        reply_to = sender

        if msg_type == "text":
            text = msg.get("text", {}).get("body", "")
            return (sender, text, reply_to, None)
        elif msg_type == "image":
            img = msg.get("image", {})
            media_info = {
                "type": "imageMessage",
                "cloud_media_id": img.get("id", ""),
                "mime_type": img.get("mime_type", "image/jpeg"),
                "caption": img.get("caption", ""),
                "url": "",  # Need to fetch via media endpoint
                "filename": "image.jpg",
                "sender_name": sender_name,
            }
            text = img.get("caption", "") or "[Image shared]"
            return (sender, text, reply_to, media_info)
        elif msg_type == "audio":
            aud = msg.get("audio", {})
            media_info = {
                "type": "audioMessage",
                "cloud_media_id": aud.get("id", ""),
                "mime_type": aud.get("mime_type", "audio/ogg"),
                "url": "",  # Need to fetch via media endpoint
                "filename": "audio.ogg",
                "caption": "",
            }
            return (sender, "[Audio shared]", reply_to, media_info)
        elif msg_type == "document":
            doc = msg.get("document", {})
            media_info = {
                "type": "documentMessage",
                "cloud_media_id": doc.get("id", ""),
                "mime_type": doc.get("mime_type", ""),
                "caption": doc.get("caption", ""),
                "url": "",
                "filename": doc.get("filename", "document"),
            }
            text = doc.get("caption", "") or "[Document shared]"
            return (sender, text, reply_to, media_info)
        elif msg_type == "video":
            vid = msg.get("video", {})
            media_info = {
                "type": "videoMessage",
                "cloud_media_id": vid.get("id", ""),
                "mime_type": vid.get("mime_type", ""),
                "caption": vid.get("caption", ""),
                "url": "",
                "filename": "video.mp4",
            }
            text = vid.get("caption", "") or "[Video shared]"
            return (sender, text, reply_to, media_info)
        elif msg_type in ("interactive", "button", "reaction", "sticker"):
            return (sender, f"[{msg_type} received]", reply_to, None)
        else:
            return (sender, f"[{msg_type} received]", reply_to, None)

    except (IndexError, KeyError, TypeError) as e:
        logger.error(f"Error parsing Cloud API message: {e}")
        return None


async def get_cloud_media_url(media_id: str) -> str:
    """Fetch the download URL for a Cloud API media object."""
    token = get_cloud_token()
    if not token or not media_id:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://graph.facebook.com/v25.0/{media_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            data = resp.json()
            return data.get("url", "")
    except Exception as e:
        logger.error(f"Error fetching Cloud API media URL: {e}")
        return ""


async def download_cloud_media(media_id: str) -> tuple[bytes | None, str]:
    """Download media bytes from Cloud API given a media ID.

    Returns (bytes, mime_type) on success, or (None, "") on failure.
    The Cloud API download URL requires the access token as Bearer auth.
    """
    token = get_cloud_token()
    if not token or not media_id:
        return None, ""
    try:
        # Step 1: get the download URL
        download_url = await get_cloud_media_url(media_id)
        if not download_url:
            logger.error(f"Could not get download URL for media {media_id}")
            return None, ""

        # Step 2: download the actual bytes (requires Bearer token)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                download_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60.0,
            )
            if resp.status_code == 200:
                mime = resp.headers.get("content-type", "application/octet-stream")
                logger.info(f"Downloaded cloud media {media_id}: {len(resp.content)} bytes, mime={mime}")
                return resp.content, mime
            logger.error(f"Failed to download cloud media {media_id}: HTTP {resp.status_code}")
            return None, ""
    except Exception as e:
        logger.error(f"Error downloading cloud media {media_id}: {e}")
        return None, ""


async def upload_media_bytes_cloud(data: bytes, mime_type: str, filename: str = "file") -> str | None:
    """Upload raw bytes to Cloud API and return the media ID.

    This is used to re-upload media that was downloaded from a teacher's
    message so it can be forwarded to parents.
    """
    token = get_cloud_token()
    phone_id = get_cloud_phone_id()
    if not token or not phone_id:
        logger.error("Cloud API credentials not configured for media upload")
        return None

    url = f"https://graph.facebook.com/v25.0/{phone_id}/media"
    headers = {"Authorization": f"Bearer {token}"}

    files = {
        "file": (filename, data, mime_type),
        "type": (None, mime_type),
        "messaging_product": (None, "whatsapp"),
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, files=files, timeout=60.0)
            resp_data = response.json()
            media_id = resp_data.get("id")
            if media_id:
                logger.info(f"Cloud API media re-uploaded: id={media_id}, mime={mime_type}")
                return media_id
            logger.error(f"Cloud API media re-upload failed: {resp_data}")
            return None
    except Exception as e:
        logger.error(f"Error re-uploading media to Cloud API: {e}")
        return None


async def forward_cloud_media_to_recipient(
    media_info: dict, recipient: str, caption: str = ""
) -> bool:
    """Download media from Cloud API (using cloud_media_id) and re-send it to a recipient.

    This is the key function that fixes the "[Document shared]" problem.
    When a teacher sends a document/image to the bot via Cloud API, we need to:
    1. Download the media using the cloud_media_id
    2. Re-upload it to get a new media_id
    3. Send it to the recipient(s)

    Includes retry logic for robustness.
    """
    import asyncio

    cloud_media_id = media_info.get("cloud_media_id", "")
    if not cloud_media_id:
        # Fall back to URL-based forwarding (Green API path)
        url = media_info.get("url", "")
        if url:
            return await forward_file_by_url(
                recipient, url, media_info.get("filename", "file"), caption
            )
        logger.warning("forward_cloud_media_to_recipient: no cloud_media_id or url")
        return False

    # Determine the Cloud API media type from the internal type
    internal_type = media_info.get("type", "")
    type_map = {
        "imageMessage": "image",
        "videoMessage": "video",
        "documentMessage": "document",
        "audioMessage": "audio",
    }
    cloud_type = type_map.get(internal_type, "document")
    filename = media_info.get("filename", "file")
    logger.info(
        f"forward_cloud_media_to_recipient: cloud_media_id={cloud_media_id}, "
        f"internal_type={internal_type}, cloud_type={cloud_type}, "
        f"filename={filename}, recipient={recipient}"
    )

    # Retry up to 2 times for robustness
    for attempt in range(2):
        if attempt > 0:
            logger.info(f"Retry attempt {attempt + 1} for media forwarding to {recipient}")
            await asyncio.sleep(2)

        # Download the media bytes
        media_bytes, mime_type = await download_cloud_media(cloud_media_id)
        if not media_bytes:
            logger.error(f"Attempt {attempt + 1}: Could not download media {cloud_media_id}")
            continue

        logger.info(
            f"Downloaded media {cloud_media_id}: {len(media_bytes)} bytes, mime={mime_type}"
        )

        # Re-upload to get a fresh media ID
        new_media_id = await upload_media_bytes_cloud(media_bytes, mime_type, filename)
        del media_bytes  # free memory

        if not new_media_id:
            logger.error(f"Attempt {attempt + 1}: Could not re-upload media for {recipient}")
            continue

        logger.info(f"Re-uploaded media, new_media_id={new_media_id}. Sending to {recipient}...")

        # Send the media to the recipient
        success = await send_cloud_media(
            recipient,
            cloud_type,
            media_id=new_media_id,
            caption=caption,
            filename=filename if cloud_type == "document" else "",
        )
        if success:
            logger.info(f"Forwarded {cloud_type} to {recipient} (original media_id={cloud_media_id})")
            return True
        else:
            logger.error(f"Attempt {attempt + 1}: send_cloud_media failed for {recipient}")

    logger.error(f"All attempts failed to forward media {cloud_media_id} to {recipient}")
    return False


def parse_incoming_message(body: dict) -> ParsedMessage | None:
    """
    Parse incoming Green API webhook payload.
    Returns (sender_phone, message_text, reply_to_chat_id, media_info) or None.

    media_info is a dict with keys: url, filename, caption, type — or None for text-only messages.

    For individual chats, reply_to_chat_id is the sender's phone (e.g. 918076455224@c.us).
    For group chats, reply_to_chat_id is the group chat ID (e.g. 120363427415804526@g.us).
    """
    try:
        type_webhook = body.get("typeWebhook", "")

        if type_webhook != "incomingMessageReceived":
            logger.info(f"Ignoring webhook type: {type_webhook}")
            return None

        sender_data = body.get("senderData", {})
        message_data = body.get("messageData", {})

        chat_id = sender_data.get("chatId", "")
        is_group = chat_id.endswith("@g.us")

        # Get sender phone number (remove @c.us suffix)
        sender = sender_data.get("sender", "") or chat_id
        if "@" in sender:
            sender = sender.split("@")[0]

        if not sender:
            logger.warning("No sender found in webhook payload")
            return None

        type_message = message_data.get("typeMessage", "")

        # For group chats, reply to the group; for individual chats, reply to the sender
        reply_to = chat_id if is_group else f"{sender}@c.us"

        # Text messages
        if type_message == "textMessage":
            text_data = message_data.get("textMessageData", {})
            text = text_data.get("textMessage", "")
            return (sender, text, reply_to, None)
        elif type_message == "extendedTextMessage":
            ext_data = message_data.get("extendedTextMessageData", {})
            text = ext_data.get("text", "")
            return (sender, text, reply_to, None)
        elif type_message == "quotedMessage":
            ext_data = message_data.get("extendedTextMessageData", {})
            text = ext_data.get("text", "")
            if not text:
                quoted = message_data.get("quotedMessage", {})
                text = quoted.get("textMessage", "")
            if text:
                return (sender, text, reply_to, None)
            return (sender, "", reply_to, None)

        # Media messages (image, video, document, audio, sticker)
        media_types = ["imageMessage", "videoMessage", "documentMessage", "audioMessage", "stickerMessage"]
        if type_message in media_types:
            media_info = _extract_media_info(message_data, type_message)
            caption = media_info.get("caption", "") if media_info else ""
            text = caption if caption else f"[{type_message.replace('Message', '').capitalize()} shared]"
            return (sender, text, reply_to, media_info)

        # Unknown type — still return with text so bot can respond
        logger.info(f"Unknown message type: {type_message}")
        return (sender, f"[{type_message} received]", reply_to, None)

    except (IndexError, KeyError, TypeError) as e:
        logger.error(f"Error parsing Green API message: {e}")
        return None
