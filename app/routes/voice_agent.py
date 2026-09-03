"""Browser voice assistant for parents: /voice-agent page and its API."""

import base64
import logging
import os

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app.services import voice_agent_service as va

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_AUDIO_BYTES = 8 * 1024 * 1024
_PAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "voice_agent.html")


@router.get("/voice-agent", response_class=HTMLResponse)
async def voice_agent_page():
    with open(_PAGE_PATH, encoding="utf-8") as f:
        return HTMLResponse(f.read())


async def _speak(text: str) -> str:
    audio = await va.synthesize_speech(text, response_format="mp3")
    return base64.b64encode(audio).decode() if audio else ""


@router.post("/api/voice-agent/start")
async def voice_agent_start():
    session = va.new_session(channel="web")
    session.history.append({"role": "assistant", "content": va.GREETING})
    return {
        "session_id": session.session_id,
        "reply": va.GREETING,
        "audio_b64": await _speak(va.GREETING),
        "done": False,
    }


@router.post("/api/voice-agent/turn")
async def voice_agent_turn(
    session_id: str = Form(""),
    text: str = Form(""),
    audio: UploadFile | None = File(None),
):
    session = va.get_session(session_id) if session_id else None
    if session is None:
        session = va.new_session(channel="web")
    if session.done:
        return JSONResponse({"error": "This conversation has ended. Start a new one."}, status_code=409)

    user_text = text.strip()
    if not user_text and audio is not None:
        data = await audio.read()
        if len(data) > MAX_AUDIO_BYTES:
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
async def voice_agent_end(session_id: str = Form("")):
    """Parent closed the page or pressed End: mail whatever was collected."""
    session = va.get_session(session_id) if session_id else None
    if session is None:
        return {"status": "unknown_session"}
    has_parent_turn = any(m["role"] == "user" for m in session.history)
    if not session.done and has_parent_turn:
        session.done = True
        va.email_admin_in_background(session)
    return {"status": "ok", "emailed": session.emailed or has_parent_turn}
