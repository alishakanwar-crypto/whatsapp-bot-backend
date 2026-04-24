import logging
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
from app.routes import webhook, allowlist, messages, settings, bulk, agent_ws, agent_config
from app.services.scheduler_service import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


class LowercaseURLMiddleware(BaseHTTPMiddleware):
    """Middleware to normalize URL paths to lowercase for case-insensitive routing."""

    async def dispatch(self, request: Request, call_next):
        request.scope["path"] = request.scope["path"].lower()
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


# Serve static files (school images)
import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/debug/parent-phones")
async def debug_parent_phones():
    """Debug endpoint to verify parent phone data is loaded."""
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


@app.post("/api/send-email")
async def api_send_email(request: Request):
    """Send an email via the server's SMTP config (for admin use)."""
    from app.services.email_service import send_email_async
    body = await request.json()
    to = body.get("to", "")
    subject = body.get("subject", "")
    text = body.get("body", "")
    if not to or not subject or not text:
        return {"status": "error", "error": "Missing to, subject, or body"}
    success = await send_email_async(to, subject, text, "PP International School")
    return {"status": "ok" if success else "error", "sent_to": to}


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
