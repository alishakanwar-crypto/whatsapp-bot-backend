import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv

# load_dotenv() MUST run before app module imports that read env vars at module level
# (e.g. AGENT_SECRET in agent_config.py and agent_ws.py, IMAP/SMTP creds in email services)
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import init_db
from app.routes import webhook, allowlist, messages, settings, bulk, agent_ws, agent_config, face
from app.services.scheduler_service import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


class LowercaseURLMiddleware(BaseHTTPMiddleware):
    """Middleware to normalize URL paths to lowercase for case-insensitive routing."""

    async def dispatch(self, request: Request, call_next):
        path = request.scope["path"]
        method = request.method
        # Log all webhook-related requests for debugging
        if "webhook" in path.lower():
            logging.getLogger("app.main").info(
                f"[REQUEST] {method} {path} from {request.client.host if request.client else 'unknown'}"
            )
        request.scope["path"] = path.lower()
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    # Populate parent phone numbers in pi_sheet_students on startup
    logger = logging.getLogger(__name__)
    logger.info("STARTUP: About to populate parent phone numbers...")
    from app.services.sheet_refresh_service import populate_parent_phones
    try:
        result = await populate_parent_phones()
        logger.info(f"STARTUP: populate_parent_phones returned {result}")
    except Exception as e:
        logger.error(f"STARTUP: parent phone population failed: {e}", exc_info=True)
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="WhatsApp & SMS Bot", version="1.0.0", lifespan=lifespan)

# Disable CORS. Do not remove this for full-stack development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

app.add_middleware(LowercaseURLMiddleware)

app.include_router(webhook.router)
app.include_router(allowlist.router)
app.include_router(messages.router)
app.include_router(settings.router)
app.include_router(bulk.router)
app.include_router(agent_ws.router)
app.include_router(agent_config.router)
app.include_router(face.router)


# Serve static files (school images)
import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


def _check_debug_auth(request: Request) -> None:
    """Verify X-Agent-Secret header on debug endpoints.

    Raises 401 when AGENT_SECRET is set and the header is missing/wrong.
    Skips the check when the env var is not configured so local dev
    keeps working without extra setup.
    """
    from fastapi import HTTPException
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/debug/version")
async def debug_version(request: Request):
    """Check deployed code version."""
    _check_debug_auth(request)
    from app.services.whatsapp_service import get_whatsapp_provider, get_id_instance, get_api_url
    return {
        "version": "2026-04-28-v6-caption-identity-fix",
        "image_handler_cloud": True,
        "image_handler_green": True,
        "whatsapp_provider": get_whatsapp_provider(),
        "green_api_instance": get_id_instance()[:10] + "..." if get_id_instance() else "not_set",
        "green_api_url": get_api_url(),
    }


@app.get("/debug/webhook-config")
async def debug_webhook_config(request: Request):
    """Check webhook and WhatsApp provider configuration."""
    _check_debug_auth(request)
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('GREEN_API_ID_INSTANCE','GREEN_API_TOKEN','GREEN_API_URL',"
            "'WHATSAPP_CLOUD_TOKEN','WHATSAPP_PHONE_ID')"
        )
        rows = await cursor.fetchall()
        config = {}
        for row in rows:
            k, v = row[0], row[1]
            if "TOKEN" in k and v and len(v) > 10:
                config[k] = v[:10] + "..." + v[-5:]
            else:
                config[k] = v
        return {"settings_keys": config, "note": "Tokens are masked"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        await db.close()


@app.get("/debug/check-meta-webhook")
async def check_meta_webhook(request: Request):
    """Server-side check of Meta webhook subscription status."""
    _check_debug_auth(request)
    import httpx
    from app.services.whatsapp_service import get_cloud_token
    token = get_cloud_token()
    if not token:
        return {"error": "No cloud token configured"}

    results = {}
    async with httpx.AsyncClient(timeout=15) as client:
        # Get phone info
        try:
            r = await client.get(
                "https://graph.facebook.com/v25.0/1143087072210203",
                params={"fields": "display_phone_number,verified_name"},
                headers={"Authorization": f"Bearer {token}"},
            )
            results["phone_info"] = r.json()
        except Exception as e:
            results["phone_info_error"] = str(e)

        # Get WABA ID from phone number
        try:
            r2 = await client.get(
                "https://graph.facebook.com/v25.0/1143087072210203",
                params={"fields": "whatsapp_business_account"},
                headers={"Authorization": f"Bearer {token}"},
            )
            waba_data = r2.json()
            results["waba_info"] = waba_data
            waba_id = waba_data.get("whatsapp_business_account", {}).get("id")
            if waba_id:
                # Check WABA subscribed apps
                r3 = await client.get(
                    f"https://graph.facebook.com/v25.0/{waba_id}/subscribed_apps",
                    headers={"Authorization": f"Bearer {token}"},
                )
                results["subscribed_apps"] = r3.json()

                # Check WABA message templates
                r4 = await client.get(
                    f"https://graph.facebook.com/v25.0/{waba_id}",
                    params={"fields": "name,timezone_id,message_template_namespace"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                results["waba_details"] = r4.json()
        except Exception as e:
            results["waba_error"] = str(e)

    return results


@app.get("/debug/update-webhook-url")
async def update_webhook_url(request: Request):
    """Try to update Meta webhook callback URL to this server."""
    _check_debug_auth(request)
    import httpx
    from app.services.whatsapp_service import get_cloud_token
    token = get_cloud_token()
    if not token:
        return {"error": "No cloud token configured"}

    app_id = "1250990343758441"
    new_callback = "https://app-itszlsnn.fly.dev/webhook/cloud"
    verify_token = "ppis_bot_verify_2024"

    results = {"target_url": new_callback}
    async with httpx.AsyncClient(timeout=30) as client:
        # Method 1: Try POST to /app/subscriptions with system user token
        try:
            r = await client.post(
                f"https://graph.facebook.com/v25.0/{app_id}/subscriptions",
                headers={"Authorization": f"Bearer {token}"},
                data={
                    "object": "whatsapp_business_account",
                    "callback_url": new_callback,
                    "verify_token": verify_token,
                    "fields": "messages",
                },
            )
            results["subscription_result"] = r.json()
        except Exception as e:
            results["subscription_error"] = str(e)

        # Method 2: Try subscribing WABA to get webhooks
        # First get WABA ID
        try:
            r2 = await client.get(
                f"https://graph.facebook.com/v25.0/1143087072210203",
                params={"fields": "id"},
                headers={"Authorization": f"Bearer {token}"},
            )
            results["phone_check"] = r2.json()
        except Exception as e:
            results["phone_check_error"] = str(e)

        # Method 3: Try overriding callback_url at WABA level
        try:
            r3 = await client.post(
                f"https://graph.facebook.com/v25.0/{app_id}/subscriptions",
                params={"access_token": token},
                json={
                    "object": "whatsapp_business_account",
                    "callback_url": new_callback,
                    "verify_token": verify_token,
                    "fields": ["messages"],
                },
            )
            results["subscription_json_result"] = r3.json()
        except Exception as e:
            results["subscription_json_error"] = str(e)

    return results


@app.get("/debug/parent-phones")
async def debug_parent_phones(request: Request):
    """Debug endpoint to verify parent phone data is loaded."""
    _check_debug_auth(request)
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM pi_sheet_students WHERE father_mobile != '' OR mother_mobile != ''"
        )
        row = await cursor.fetchone()
        count_with_phones = row[0] if row else 0

        cursor2 = await db.execute("SELECT COUNT(*) FROM pi_sheet_students")
        row2 = await cursor2.fetchone()
        total_count = row2[0] if row2 else 0

        # Sample a few records
        cursor3 = await db.execute(
            "SELECT student_name, grade, father_mobile, mother_mobile "
            "FROM pi_sheet_students WHERE father_mobile != '' LIMIT 3"
        )
        samples = [
            {"name": r[0], "grade": r[1], "father": r[2][:4] + "****", "mother": (r[3] or "")[:4] + "****"}
            for r in await cursor3.fetchall()
        ]

        return {
            "total_students": total_count,
            "students_with_phones": count_with_phones,
            "samples": samples,
        }
    finally:
        await db.close()


@app.get("/debug/parent-search")
async def debug_parent_search(request: Request, phone: str = ""):
    """Search for a parent by phone number (last 10 digits)."""
    _check_debug_auth(request)
    from app.database import get_db
    if not phone:
        return {"error": "Provide ?phone=DIGITS"}
    digits = "".join(c for c in phone if c.isdigit())
    last10 = digits[-10:] if len(digits) >= 10 else digits
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT student_name, grade, father_mobile, mother_mobile "
            "FROM pi_sheet_students WHERE father_mobile LIKE ? OR mother_mobile LIKE ?",
            (f"%{last10}%", f"%{last10}%"),
        )
        rows = await cursor.fetchall()
        return {
            "phone_searched": last10,
            "results": [
                {"name": r[0], "grade": r[1], "father": r[2], "mother": r[3]}
                for r in rows
            ],
        }
    finally:
        await db.close()


@app.get("/debug/simulate-snapshot")
async def debug_simulate_snapshot(request: Request, phone: str = "", message: str = "show my child"):
    """Simulate the snapshot request flow for debugging."""
    _check_debug_auth(request)
    from app.routes.webhook import (
        _is_snapshot_request, _is_admin_panel, _is_pi_sheet_parent,
        _extract_classroom_from_message, _lookup_parent_child_class,
    )
    is_admin = _is_admin_panel(phone)
    is_snapshot = _is_snapshot_request(message, is_admin=is_admin)
    is_parent = await _is_pi_sheet_parent(phone) if phone else False
    classroom = _extract_classroom_from_message(message)
    children = await _lookup_parent_child_class(phone) if phone else []
    return {
        "phone": phone,
        "message": message,
        "is_admin": is_admin,
        "is_snapshot_request": is_snapshot,
        "is_pi_sheet_parent": is_parent,
        "extracted_classroom": classroom,
        "children_found": children,
    }


@app.post("/api/send-email")
async def api_send_email(request: Request):
    """Send an email via the server's SMTP config (for admin use).

    Requires X-Agent-Secret header when AGENT_SECRET is configured.
    """
    import os
    from fastapi import HTTPException
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            raise HTTPException(status_code=401, detail="Invalid or missing agent secret")

    from app.services.email_service import send_email_async
    body = await request.json()
    to = body.get("to", "")
    subject = body.get("subject", "")
    text = body.get("body", "")
    if not to or not subject or not text:
        return {"status": "error", "error": "Missing to, subject, or body"}
    success = await send_email_async(to, subject, text, "PP International School")
    return {"status": "ok" if success else "error", "sent_to": to}


@app.post("/api/send-whatsapp")
async def api_send_whatsapp(request: Request):
    """Send a WhatsApp message (used by Campus Agent for attendance notifications).

    Requires X-Agent-Secret header when AGENT_SECRET is configured.
    """
    import os
    from fastapi import HTTPException
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            raise HTTPException(status_code=401, detail="Invalid or missing agent secret")

    from app.services.whatsapp_service import send_whatsapp_message, send_cloud_template_message
    body = await request.json()
    phone = body.get("phone", "")
    message = body.get("message", "")
    template_name = body.get("template_name", "")
    template_params = body.get("template_params", [])

    if not phone:
        return {"status": "error", "error": "Missing phone"}

    # Handle comma-separated phone numbers (e.g. "91XXXXXXXXXX,91YYYYYYYYYY")
    phone_list = [p.strip() for p in phone.split(",") if p.strip()]
    results = []
    for single_phone in phone_list:
        # Normalize: strip non-digits, ensure country code
        digits = "".join(c for c in single_phone if c.isdigit())
        if len(digits) == 10:
            digits = "91" + digits
        elif len(digits) < 10:
            results.append({"phone": single_phone, "status": "error", "error": "too short"})
            continue

        if template_name:
            success = await send_cloud_template_message(
                digits, template_name, body_params=template_params or None,
            )
        elif message:
            success = await send_whatsapp_message(digits, message)
        else:
            results.append({"phone": digits, "status": "error", "error": "no message"})
            continue
        results.append({"phone": digits, "status": "ok" if success else "error"})

    if not results:
        return {"status": "error", "error": "No valid phone numbers to send to", "results": []}
    all_ok = all(r["status"] == "ok" for r in results)
    return {"status": "ok" if all_ok else "partial", "results": results}


_broadcast_status: dict = {}


async def _run_broadcast(phone_list: list[str], message: str, batch_delay: float):
    """Background task to send broadcast messages."""
    import asyncio
    from app.services.whatsapp_service import send_whatsapp_message

    global _broadcast_status
    _broadcast_status["running"] = True

    for i, phone in enumerate(phone_list):
        try:
            success = await send_whatsapp_message(phone, message)
            if success:
                _broadcast_status["sent"] += 1
            else:
                _broadcast_status["failed"] += 1
        except Exception:
            _broadcast_status["failed"] += 1

        _broadcast_status["progress"] = i + 1
        # Rate limiting: pause every 10 messages
        if (i + 1) % 10 == 0:
            await asyncio.sleep(batch_delay)

    _broadcast_status["running"] = False
    _broadcast_status["done"] = True


@app.post("/api/broadcast")
async def api_broadcast(request: Request):
    """Broadcast a message to all parent phone numbers (runs in background).

    Body JSON:
      - message: The text message to send
      - dry_run: If true, just return the phone list without sending
      - batch_delay: Seconds between batches (default 1)
    """
    import os, asyncio, re
    from fastapi import HTTPException

    global _broadcast_status

    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            raise HTTPException(status_code=401, detail="Invalid or missing agent secret")

    from app.database import get_db

    body = await request.json()
    message = body.get("message", "")
    dry_run = body.get("dry_run", False)
    batch_delay = body.get("batch_delay", 1)

    if not message:
        return {"status": "error", "error": "Missing message"}

    # Check if a broadcast is already running
    if _broadcast_status.get("running"):
        return {
            "status": "already_running",
            "progress": _broadcast_status.get("progress", 0),
            "total": _broadcast_status.get("total", 0),
            "sent": _broadcast_status.get("sent", 0),
            "failed": _broadcast_status.get("failed", 0),
        }

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT DISTINCT father_mobile, mother_mobile FROM pi_sheet_students"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    phones = set()
    for row in rows:
        for raw_phone in (row[0], row[1]):
            if not raw_phone:
                continue
            digits = re.sub(r"\D", "", raw_phone)
            if len(digits) >= 10:
                normalized = f"91{digits[-10:]}"
                phones.add(normalized)

    phone_list = sorted(phones)

    if dry_run:
        return {"status": "dry_run", "total_phones": len(phone_list), "sample": phone_list[:10]}

    # Initialize status and start background task
    _broadcast_status = {
        "running": True, "done": False,
        "total": len(phone_list), "progress": 0,
        "sent": 0, "failed": 0,
    }
    asyncio.create_task(_run_broadcast(phone_list, message, batch_delay))

    return {"status": "started", "total": len(phone_list)}


@app.get("/api/broadcast/status")
async def api_broadcast_status():
    """Check the status of an ongoing broadcast."""
    if not _broadcast_status:
        return {"status": "no_broadcast"}
    return _broadcast_status


@app.post("/api/whatsapp-creds")
async def set_whatsapp_creds(request: Request):
    """Save WhatsApp API credentials to the database.

    Accepts JSON with any of: GREEN_API_ID_INSTANCE, GREEN_API_TOKEN,
    GREEN_API_URL, WHATSAPP_CLOUD_TOKEN, WHATSAPP_PHONE_ID.

    Requires X-Agent-Secret header when AGENT_SECRET is configured.
    """
    import os
    from fastapi import HTTPException
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            raise HTTPException(status_code=401, detail="Invalid or missing agent secret")

    from app.database import get_db
    from app.services.whatsapp_service import refresh_creds_cache

    allowed_keys = {
        "GREEN_API_ID_INSTANCE", "GREEN_API_TOKEN", "GREEN_API_URL",
        "WHATSAPP_CLOUD_TOKEN", "WHATSAPP_PHONE_ID", "system_prompt",
        "OPENAI_API_KEY",
    }
    body = await request.json()
    db = await get_db()
    saved = []
    try:
        for key, value in body.items():
            if key in allowed_keys and value:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
                saved.append(key)
        await db.commit()
    finally:
        await db.close()
    refresh_creds_cache()
    return {"status": "ok", "saved": saved}


@app.get("/privacy-policy")
async def privacy_policy():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(
        "<html><head><title>Privacy Policy - PP International School Bot</title></head>"
        "<body><h1>Privacy Policy</h1>"
        "<p>PP International School WhatsApp Bot collects only the information "
        "necessary to respond to parent queries: phone numbers and message content. "
        "Data is stored securely and used solely for school communication purposes. "
        "We do not share personal data with third parties. "
        "For questions, contact info@ppischool.in.</p>"
        "</body></html>"
    )
