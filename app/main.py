import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv

# load_dotenv() MUST run before app module imports that read env vars at module level
# (e.g. AGENT_SECRET in agent_config.py and agent_ws.py, IMAP/SMTP creds in email services)
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import init_db
from app.routes import webhook, allowlist, messages, settings, bulk, agent_ws, agent_config, face, dashboard
from app.services.scheduler_service import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


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
    from app.services.sheet_refresh_service import populate_parent_phones, sync_pi_sheet_phones_to_face_db
    try:
        result = await populate_parent_phones()
        logger.info(f"STARTUP: populate_parent_phones returned {result}")
    except Exception as e:
        logger.error(f"STARTUP: parent phone population failed: {e}", exc_info=True)
    try:
        synced = await sync_pi_sheet_phones_to_face_db()
        logger.info(f"STARTUP: synced {synced} teacher phone numbers from PI Sheet to face DB")
    except Exception as e:
        logger.error(f"STARTUP: face-phone sync failed: {e}", exc_info=True)
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
app.include_router(dashboard.router)


# Serve static files (school images)
import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Serve dashboard frontend (SPA)
dashboard_dir = os.path.join(os.path.dirname(__file__), "dashboard_dist")
if os.path.isdir(dashboard_dir):
    from fastapi.responses import FileResponse

    @app.get("/dashboard/{path:path}")
    async def serve_dashboard(path: str):
        file_path = os.path.join(dashboard_dir, path)
        if path and os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(dashboard_dir, "index.html"))

    @app.get("/dashboard")
    async def serve_dashboard_root():
        return FileResponse(os.path.join(dashboard_dir, "index.html"))


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


@app.get("/debug/templates")
async def list_templates(request: Request):
    """List approved WhatsApp message templates."""
    _check_debug_auth(request)
    import httpx
    from app.services.whatsapp_service import get_cloud_token
    token = get_cloud_token()
    if not token:
        return {"error": "No cloud token configured"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://graph.facebook.com/v25.0/2417647228700804/message_templates",
                params={"limit": 100},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            return r.json()
    except Exception as e:
        return {"error": str(e)}


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


@app.post("/debug/add-student")
async def debug_add_student(request: Request):
    """Add a student to pi_sheet_students for testing."""
    _check_debug_auth(request)
    body = await request.json()
    name = body.get("name", "").strip()
    grade = body.get("grade", "").strip()
    father = body.get("father_mobile", "").strip()
    mother = body.get("mother_mobile", "").strip()
    if not name or not grade:
        return {"error": "name and grade required"}
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO pi_sheet_students (student_name, grade, father_mobile, mother_mobile) "
            "VALUES (?, ?, ?, ?)",
            (name, grade, father, mother),
        )
        await db.commit()
        return {"status": "ok", "name": name, "grade": grade}
    finally:
        await db.close()


@app.post("/api/send-face-reminder")
async def send_face_photo_reminder(request: Request, background_tasks: BackgroundTasks):
    """Send WhatsApp reminder to parents who haven't registered face photos."""
    _check_debug_auth(request)
    import json as _json
    import asyncio

    # 1. Get registered student names from face API
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://app-itszlsnn.fly.dev/api/face/images", timeout=30)
        face_data = resp.json()

    registered_ids = {item["person_id"].upper() for item in face_data}
    # Extract just the name part (e.g. "SUHAAN_AHUJA_GRADE3C" -> "SUHAAN AHUJA")
    registered_names = set()
    for pid in registered_ids:
        parts = pid.split("_")
        # Remove grade suffix
        name_parts = []
        for p in parts:
            if p.startswith("GRADE") or p.startswith("NUR") or p.startswith("PREP") or p == "NURSERY":
                break
            name_parts.append(p)
        if name_parts:
            registered_names.add(" ".join(name_parts))

    # 2. Load all parents
    json_path = os.path.join(os.path.dirname(__file__), "personalized_parents.json")
    with open(json_path) as f:
        parents = _json.load(f)

    # 3. Group by student, collect phones for unregistered students only
    phones_to_message: dict[str, set[str]] = {}  # phone -> set of child names
    for entry in parents:
        name = (entry.get("student_name") or "").strip().upper()
        phone = (entry.get("phone") or "").strip()
        grade = (entry.get("sheet") or entry.get("grade") or "").strip()
        if not name or not phone:
            continue
        # Check if registered (fuzzy)
        is_reg = False
        for rn in registered_names:
            if rn in name or name in rn:
                is_reg = True
                break
        if is_reg:
            continue
        # Skip test/admin entries
        if "ALISHA" in name or "HARPREET" in name or "ARPIT" in name:
            continue
        child_label = f"{name} ({grade})" if grade else name
        phones_to_message.setdefault(phone, set()).add(child_label)

    unique_phones = list(phones_to_message.keys())

    # 4. Send in background using approved template
    async def _send_reminders():
        from app.services.whatsapp_service import send_cloud_template_message
        sent = 0
        failed = 0
        for phone in unique_phones:
            try:
                ok = await send_cloud_template_message(
                    phone,
                    "face_registration_reminder",
                    language_code="en",
                )
                if ok:
                    sent += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.error(f"Reminder send error for {phone}: {exc}")
                failed += 1
            # Rate limit: ~10 msgs/sec to stay within Meta limits
            await asyncio.sleep(0.1)
            if (sent + failed) % 50 == 0 and (sent + failed) > 0:
                logger.info(f"Face reminder progress: {sent} sent, {failed} failed of {len(unique_phones)}")
        logger.info(f"Face reminder complete: {sent} sent, {failed} failed of {len(unique_phones)}")

    background_tasks.add_task(_send_reminders)

    return {
        "status": "sending",
        "total_phones": len(unique_phones),
        "registered_students": len(registered_names),
        "message": f"Sending reminders to {len(unique_phones)} parent phones (excluding {len(registered_names)} already registered students)",
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


async def _log_attendance_audit(phone: str, student_name: str,
                                status: str, details: str = ""):
    """Log attendance notification to the notification_log table for auditing."""
    try:
        from app.database import get_db
        adb = await get_db()
        try:
            await adb.execute(
                """INSERT INTO notification_log
                   (phone, message_type, student_name, status, wa_message_id)
                   VALUES (?, 'attendance', ?, ?, ?)""",
                (phone, student_name, status, details),
            )
            await adb.commit()
        finally:
            await adb.close()
    except Exception:
        pass


@app.post("/api/send-whatsapp")
async def api_send_whatsapp(request: Request):
    """Send a WhatsApp message (used by Campus Agent for attendance notifications).

    Requires X-Agent-Secret header when AGENT_SECRET is configured.
    Blocks attendance notifications on school holidays, Saturdays, and Sundays.
    """
    import os
    from datetime import datetime, timezone, timedelta
    from fastapi import HTTPException
    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            raise HTTPException(status_code=401, detail="Invalid or missing agent secret")

    from app.services.whatsapp_service import send_whatsapp_message, send_whatsapp_force, send_cloud_template_message, upload_media_bytes_cloud
    from app.database import get_db
    body = await request.json()
    phone = body.get("phone", "")
    message = body.get("message", "")
    template_name = body.get("template_name", "")
    template_params = body.get("template_params", [])
    language_code = body.get("language_code", "en")
    header_image_base64 = body.get("header_image_base64", "")

    # --- Block attendance notifications on holidays and Sundays ---
    ist = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(ist).strftime("%Y-%m-%d")
    today_day = datetime.now(ist).strftime("%A")
    is_attendance_msg = (
        template_name in ("ppis_attendance_alert", "ppis_teacher_present")
        or "marked present" in message.lower()
    )
    # Extract student name from template params for audit logging
    _student_name = template_params[0] if template_params else ""

    if is_attendance_msg:
        # Block on Sundays always; block on 2nd Saturday only
        _block_day = False
        if today_day == "Sunday":
            _block_day = True
        elif today_day == "Saturday":
            _today_date = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
            _sat_number = (_today_date.day - 1) // 7 + 1
            if _sat_number == 2:
                _block_day = True
        if _block_day:
            await _log_attendance_audit(
                phone, _student_name, "blocked", f"{today_day} — school closed"
            )
            return {"status": "blocked", "reason": f"{today_day} — school is closed"}
        # Block on holidays in the school_holidays table
        try:
            db = await get_db()
            try:
                cur = await db.execute(
                    "SELECT reason FROM school_holidays WHERE date = ?",
                    (today_ist,),
                )
                row = await cur.fetchone()
                if row:
                    await _log_attendance_audit(
                        phone, _student_name, "blocked", f"Holiday: {row[0]}"
                    )
                    return {
                        "status": "blocked",
                        "reason": f"Holiday: {row[0]}",
                    }
            finally:
                await db.close()
        except Exception:
            pass  # If DB fails, allow the message through

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
            # Upload header image if provided (teacher attendance snapshots)
            _header_image_id = None
            if header_image_base64:
                import base64 as _b64
                try:
                    img_bytes = _b64.b64decode(header_image_base64)
                    logger.info(f"Uploading header image: {len(img_bytes)} bytes")
                    _header_image_id = await upload_media_bytes_cloud(
                        img_bytes, "image/jpeg", "attendance_snapshot.jpg"
                    )
                    logger.info(f"Header image uploaded: media_id={_header_image_id}")
                except Exception as _img_err:
                    logger.warning(f"Failed to upload header image: {_img_err}")
            success = await send_cloud_template_message(
                digits, template_name,
                language_code=language_code,
                body_params=template_params or None,
                header_image_id=_header_image_id,
            )
        elif message:
            success = await send_whatsapp_force(digits, message)
        else:
            results.append({"phone": digits, "status": "error", "error": "no message"})
            continue
        results.append({"phone": digits, "status": "ok" if success else "error"})

        # Audit log for attendance notifications
        if is_attendance_msg:
            await _log_attendance_audit(
                digits, _student_name,
                "sent" if success else "failed",
                f"template={template_name}" if template_name else "direct_message",
            )

    if not results:
        return {"status": "error", "error": "No valid phone numbers to send to", "results": []}
    all_ok = all(r["status"] == "ok" for r in results)
    return {"status": "ok" if all_ok else "partial", "results": results}


_broadcast_status: dict = {}


# ---- Summer Camp Student APIs ----

@app.get("/api/summer-camp/students")
async def list_summer_camp_students():
    """List all summer camp enrolled students."""
    from app.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, student_name, grade, school_name, parent_name, "
            "contact_no, email, address, is_outsider, created_at "
            "FROM summer_camp_students ORDER BY id"
        )
        rows = await cur.fetchall()
        return {
            "status": "ok",
            "count": len(rows),
            "students": [
                {
                    "id": r[0], "student_name": r[1], "grade": r[2],
                    "school_name": r[3], "parent_name": r[4],
                    "contact_no": r[5], "email": r[6], "address": r[7],
                    "is_outsider": bool(r[8]), "created_at": r[9],
                }
                for r in rows
            ],
        }
    finally:
        await db.close()


@app.post("/api/summer-camp/students/bulk")
async def bulk_add_summer_camp_students(request: Request):
    """Bulk-add summer camp students. Body: {"students": [...]}"""
    from app.database import get_db
    body = await request.json()
    students = body.get("students", [])
    if not students:
        return {"status": "error", "error": "No students provided"}

    db = await get_db()
    try:
        # Clear existing data and re-insert
        await db.execute("DELETE FROM summer_camp_students")
        inserted = 0
        for s in students:
            await db.execute(
                "INSERT INTO summer_camp_students "
                "(student_name, grade, school_name, parent_name, contact_no, "
                "email, address, is_outsider) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s.get("student_name", ""),
                    s.get("grade", ""),
                    s.get("school_name", ""),
                    s.get("parent_name", ""),
                    s.get("contact_no", ""),
                    s.get("email", ""),
                    s.get("address", ""),
                    1 if s.get("is_outsider") else 0,
                ),
            )
            inserted += 1
        await db.commit()
        return {"status": "ok", "inserted": inserted}
    finally:
        await db.close()


@app.get("/api/summer-camp/student-phone/{student_name}")
async def get_summer_camp_student_phone(student_name: str):
    """Look up a summer camp student's parent contact number by name."""
    from app.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT contact_no, parent_name FROM summer_camp_students "
            "WHERE UPPER(student_name) = UPPER(?) AND contact_no != '' LIMIT 1",
            (student_name,),
        )
        row = await cur.fetchone()
        if row:
            return {"status": "ok", "contact_no": row[0], "parent_name": row[1]}
        return {"status": "not_found"}
    finally:
        await db.close()


# ---- Holiday Management APIs ----

@app.get("/api/holidays")
async def list_holidays():
    """List all school holidays."""
    from app.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id, date, reason, created_at FROM school_holidays ORDER BY date"
        )
        rows = await cur.fetchall()
        return {"holidays": [dict(r) for r in rows]}
    finally:
        await db.close()


@app.post("/api/holidays")
async def add_holiday(request: Request):
    """Add a school holiday. Body: {date: 'YYYY-MM-DD', reason: 'Holiday name'}"""
    from app.database import get_db
    body = await request.json()
    date = body.get("date", "")
    reason = body.get("reason", "Holiday")
    if not date:
        return {"status": "error", "error": "Missing date (format: YYYY-MM-DD)"}
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO school_holidays (date, reason) VALUES (?, ?)",
            (date, reason),
        )
        await db.commit()
        return {"status": "ok", "date": date, "reason": reason}
    finally:
        await db.close()


@app.delete("/api/holidays/{date}")
async def remove_holiday(date: str):
    """Remove a school holiday by date."""
    from app.database import get_db
    db = await get_db()
    try:
        await db.execute("DELETE FROM school_holidays WHERE date = ?", (date,))
        await db.commit()
        return {"status": "ok", "removed": date}
    finally:
        await db.close()


@app.get("/api/attendance-audit")
async def attendance_audit(limit: int = 50):
    """View attendance notification audit log."""
    from app.database import get_db
    adb = await get_db()
    try:
        cur = await adb.execute(
            """SELECT id, phone, student_name, status, wa_message_id as details,
                      created_at
               FROM notification_log
               WHERE message_type = 'attendance'
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        return {"audit": [dict(r) for r in rows]}
    finally:
        await adb.close()


async def _run_broadcast(phone_list: list[str], message: str, batch_delay: float):
    """Background task to send broadcast messages."""
    import asyncio
    from app.services.whatsapp_service import send_whatsapp_force

    global _broadcast_status
    _broadcast_status["running"] = True

    for i, phone in enumerate(phone_list):
        try:
            success = await send_whatsapp_force(phone, message)
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


# ---------------------------------------------------------------------------
# Meal Monitoring API
# ---------------------------------------------------------------------------

_meal_monitoring_status: dict = {}


@app.post("/api/meal-monitoring/trigger")
async def trigger_meal_monitoring(request: Request):
    """Manually trigger meal monitoring for testing.

    Body JSON:
      - break_type: "short_break" or "lunch" (default "lunch")
      - grade: (optional) Only process this specific grade, e.g. "Grade 3C"
      - phones: (optional) Only send to these specific phone numbers
    """
    import os
    from fastapi import HTTPException

    agent_secret = os.environ.get("AGENT_SECRET", "")
    if agent_secret:
        header_secret = request.headers.get("x-agent-secret", "")
        if header_secret != agent_secret:
            raise HTTPException(status_code=401, detail="Invalid or missing agent secret")

    global _meal_monitoring_status
    if _meal_monitoring_status.get("running"):
        return {"status": "already_running", **_meal_monitoring_status}

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    break_type = body.get("break_type", "lunch")
    target_grade = body.get("grade")
    target_phones = body.get("phones")

    from app.services.meal_monitoring_service import run_meal_monitoring

    _meal_monitoring_status = {"running": True, "break_type": break_type}
    try:
        result = await run_meal_monitoring(
            break_type,
            target_grade=target_grade,
            target_phones=target_phones,
        )
        _meal_monitoring_status = {"running": False, **result}
        return result
    except Exception as e:
        _meal_monitoring_status = {"running": False, "error": str(e)}
        return {"status": "error", "error": str(e)}


@app.get("/api/meal-monitoring/logs")
async def meal_monitoring_logs(
    date: str | None = None,
    limit: int = 100,
):
    """Get meal monitoring logs.

    Query params:
      - date: YYYY-MM-DD (default today IST)
      - limit: max rows (default 100)
    """
    from app.database import get_db

    db = await get_db()
    try:
        if not date:
            date = "date('now', '+5 hours', '+30 minutes')"
            cursor = await db.execute(
                f"SELECT * FROM meal_monitoring_logs "
                f"WHERE date(created_at) = {date} "
                f"ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM meal_monitoring_logs "
                "WHERE date(created_at) = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (date, limit),
            )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return {
            "status": "ok",
            "count": len(rows),
            "logs": [dict(zip(cols, r)) for r in rows],
        }
    finally:
        await db.close()


@app.get("/api/meal-monitoring/status")
async def meal_monitoring_status():
    """Get current meal monitoring run status."""
    return _meal_monitoring_status or {"running": False}


@app.get("/api/meal-monitoring/config")
async def meal_monitoring_config():
    """Get meal monitoring configuration including schedule and enabled state."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = 'meal_monitoring_enabled'"
        )
        row = await cursor.fetchone()
        enabled = row[0] == "1" if row else False
    except Exception:
        enabled = False
    finally:
        await db.close()

    return {
        "enabled": enabled,
        "schedule": {
            "short_break": {"time_ist": "08:50 AM", "time_utc": "03:20"},
            "lunch": {"time_ist": "11:20 AM", "time_utc": "05:50"},
        },
    }


@app.post("/api/meal-monitoring/toggle")
async def toggle_meal_monitoring(request: Request):
    """Enable or disable automatic meal monitoring.

    Body JSON:
      - enabled: true/false
    """
    body = await request.json()
    enabled = body.get("enabled", False)

    from app.database import get_db
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('meal_monitoring_enabled', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if enabled else "0",),
        )
        await db.commit()
    finally:
        await db.close()

    # Update the scheduler
    from app.services.scheduler_service import scheduler
    from apscheduler.triggers.cron import CronTrigger

    if enabled:
        from app.services.meal_monitoring_service import run_meal_monitoring_sync
        scheduler.add_job(
            run_meal_monitoring_sync,
            trigger=CronTrigger(hour=3, minute=20, second=0),
            args=["short_break"],
            id="meal_monitoring_short_break",
            replace_existing=True,
        )
        scheduler.add_job(
            run_meal_monitoring_sync,
            trigger=CronTrigger(hour=5, minute=50, second=0),
            args=["lunch"],
            id="meal_monitoring_lunch",
            replace_existing=True,
        )
    else:
        try:
            scheduler.remove_job("meal_monitoring_short_break")
        except Exception:
            pass
        try:
            scheduler.remove_job("meal_monitoring_lunch")
        except Exception:
            pass

    return {"status": "ok", "enabled": enabled}


@app.get("/api/meal-monitoring/grades")
async def meal_monitoring_grades():
    """List all grades with their camera mapping status."""
    from app.services.meal_monitoring_service import (
        _get_all_classroom_grades,
        _pi_grade_to_camera_key,
    )
    from app.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT DISTINCT grade FROM pi_sheet_students ORDER BY grade"
        )
        all_grades = [r[0] for r in await cursor.fetchall()]
    finally:
        await db.close()

    result = []
    for g in all_grades:
        cam = _pi_grade_to_camera_key(g)
        result.append({"grade": g, "camera": cam or "", "has_camera": cam is not None})

    return {"grades": result, "total": len(result), "with_camera": sum(1 for r in result if r["has_camera"])}


# ---------------------------------------------------------------------------
# Homework Delivery API
# ---------------------------------------------------------------------------

_homework_delivery_status: dict = {}


@app.post("/api/homework/trigger")
async def trigger_homework_delivery(request: Request):
    """Manually trigger homework delivery check for a specific period.

    Body JSON:
      - period: 1-8 (required)
    """
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    period = body.get("period", 0)
    if not period or period < 1 or period > 8:
        return {"status": "error", "error": "period must be 1-8"}

    global _homework_delivery_status
    if _homework_delivery_status.get("running"):
        return {"status": "already_running", **_homework_delivery_status}

    from app.services.homework_delivery_service import run_homework_delivery

    _homework_delivery_status = {"running": True, "period": period}
    try:
        result = await run_homework_delivery(period)
        _homework_delivery_status = {"running": False, **result}
        return result
    except Exception as e:
        _homework_delivery_status = {"running": False, "error": str(e)}
        return {"status": "error", "error": str(e)}


@app.get("/api/homework/logs")
async def homework_delivery_logs(limit: int = 50):
    """Get recent homework delivery logs."""
    from app.services.homework_delivery_service import get_homework_logs
    logs = await get_homework_logs(limit)
    return {"status": "ok", "count": len(logs), "logs": logs}


@app.get("/api/homework/docs")
async def list_homework_docs():
    """List all registered homework Google Docs."""
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT grade, doc_id, doc_url, created_at FROM homework_docs ORDER BY grade"
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return {
            "status": "ok",
            "count": len(rows),
            "docs": [dict(zip(cols, r)) for r in rows],
        }
    finally:
        await db.close()


@app.post("/api/homework/register-doc")
async def register_homework_doc_endpoint(request: Request):
    """Register a Google Doc for a class's homework.

    Body JSON:
      - grade: e.g. "Grade 3A"
      - doc_id: Google Doc ID
      - doc_url: (optional) full URL
    """
    body = await request.json()
    grade = body.get("grade", "")
    doc_id = body.get("doc_id", "")
    doc_url = body.get("doc_url", "")
    if not grade or not doc_id:
        return {"status": "error", "error": "grade and doc_id required"}

    from app.services.homework_delivery_service import register_homework_doc
    ok = await register_homework_doc(grade, doc_id, doc_url)
    return {"status": "ok" if ok else "error", "grade": grade, "doc_id": doc_id}


@app.post("/api/homework/register-docs-bulk")
async def register_homework_docs_bulk(request: Request):
    """Bulk register homework docs.

    Body JSON:
      - docs: [{grade, doc_id, doc_url}, ...]
    """
    body = await request.json()
    docs = body.get("docs", [])
    if not docs:
        return {"status": "error", "error": "docs list required"}

    from app.services.homework_delivery_service import register_homework_doc
    results = []
    for d in docs:
        ok = await register_homework_doc(d["grade"], d["doc_id"], d.get("doc_url", ""))
        results.append({"grade": d["grade"], "ok": ok})

    return {"status": "ok", "registered": len([r for r in results if r["ok"]]),
            "results": results}


@app.get("/api/homework/status")
async def homework_delivery_status():
    """Get current homework delivery status."""
    return _homework_delivery_status or {"running": False}


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
