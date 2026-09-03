"""Browser voice assistant for parents: /voice-agent page and its API."""

import base64
import logging
import os
import time
from collections import defaultdict, deque

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app.services import voice_agent_service as va

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_AUDIO_BYTES = 8 * 1024 * 1024
RATE_LIMIT_WINDOW = 10 * 60
RATE_LIMIT_MAX = 60
GLOBAL_RATE_LIMIT_MAX = int(os.getenv("VOICE_AGENT_GLOBAL_RATE_LIMIT", "1500"))
_PAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "voice_agent.html")

_hits: dict[str, deque[float]] = defaultdict(deque)


def _rate_limited(request: Request) -> bool:
    ip = request.headers.get("fly-client-ip") or (request.client.host if request.client else "?")
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > RATE_LIMIT_WINDOW:
        q.popleft()
    g = _hits["*"]
    while g and now - g[0] > RATE_LIMIT_WINDOW:
        g.popleft()
    if len(q) >= RATE_LIMIT_MAX or len(g) >= GLOBAL_RATE_LIMIT_MAX:
        return True
    q.append(now)
    g.append(now)
    if len(_hits) > 5000:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
            _hits.pop(k, None)
    return False


def _too_many() -> JSONResponse:
    return JSONResponse({"error": "Too many requests. Please try again in a few minutes."}, status_code=429)


async def _read_capped(upload: UploadFile, cap: int) -> bytes | None:
    buf = bytearray()
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
        if len(buf) > cap:
            return None


@router.get("/voice-agent", response_class=HTMLResponse)
async def voice_agent_page():
    with open(_PAGE_PATH, encoding="utf-8") as f:
        return HTMLResponse(f.read())


async def _speak(text: str) -> str:
    audio = await va.synthesize_speech(text, response_format="mp3")
    return base64.b64encode(audio).decode() if audio else ""


@router.post("/api/voice-agent/start")
async def voice_agent_start(request: Request):
    if _rate_limited(request):
        return _too_many()
    session = va.new_session(channel="web")
    greeting = va.greeting()
    session.history.append({"role": "assistant", "content": greeting})
    return {
        "session_id": session.session_id,
        "reply": greeting,
        "audio_b64": await _speak(greeting),
        "done": False,
    }


@router.post("/api/voice-agent/turn")
async def voice_agent_turn(
    request: Request,
    session_id: str = Form(""),
    text: str = Form(""),
    audio: UploadFile | None = File(None),
):
    if _rate_limited(request):
        return _too_many()
    session = va.get_session(session_id) if session_id else None
    if session is None:
        if session_id:
            return JSONResponse({"error": "This conversation has expired. Please reload the page."}, status_code=404)
        session = va.new_session(channel="web")
    if session.done:
        return JSONResponse({"error": "This conversation has ended. Start a new one."}, status_code=409)

    user_text = text.strip()[:2000]
    if not user_text and audio is not None:
        data = await _read_capped(audio, MAX_AUDIO_BYTES)
        if data is None:
            return JSONResponse({"error": "Recording too long."}, status_code=413)
        user_text = (await va.transcribe_upload(data, audio.content_type or "audio/webm")) or ""
    if not user_text:
        prompt = "Sorry, I could not hear that. Please say it again."
        return {
            "session_id": session.session_id,
            "transcript": "",
            "reply": prompt,
            "audio_b64": await _speak(prompt),
            "done": False,
        }

    reply, done = await va.agent_reply(session, user_text)
    if done:
        va.email_admin_in_background(session)
    return {
        "session_id": session.session_id,
        "transcript": user_text,
        "reply": reply,
        "audio_b64": await _speak(reply),
        "done": done,
    }


@router.post("/api/voice-agent/end")
async def voice_agent_end(request: Request, session_id: str = Form("")):
    """Parent closed the page or pressed End: mail whatever was collected.

    Delivery runs in the background with retries; the response says whether
    a mail was queued, not that it has arrived. Calling again for a session
    whose earlier attempt failed queues it once more.
    """
    if _rate_limited(request):
        return _too_many()
    session = va.get_session(session_id) if session_id else None
    if session is None:
        return JSONResponse({"status": "unknown_session"}, status_code=404)
    has_parent_turn = any(m["role"] == "user" for m in session.history)
    if not has_parent_turn:
        session.done = True
        return {"status": "nothing_to_send"}
    session.done = True
    if session.emailed:
        return {"status": "sent"}
    va.email_admin_in_background(session)
    return {"status": "queued"}
