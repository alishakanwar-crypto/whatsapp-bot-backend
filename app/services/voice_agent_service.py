"""Voice agent for parents: talk, collect the query, mail the school admin.

Used by two channels:
  * the browser widget at /voice-agent (mic -> Whisper -> GPT -> TTS)
  * WhatsApp voice notes handled in routes/webhook.py

Each finished conversation is summarised by GPT into a fixed set of fields
and emailed to VOICE_AGENT_ADMIN_EMAIL.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.email_service import send_email_async
from app.services.openai_service import (
    generate_response,
    get_client,
    transcribe_audio,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

ADMIN_EMAIL = os.getenv("VOICE_AGENT_ADMIN_EMAIL", "info@ppischool.in")
TTS_MODEL = os.getenv("VOICE_AGENT_TTS_MODEL", "tts-1")
TTS_VOICE = os.getenv("VOICE_AGENT_TTS_VOICE", "nova")
SESSION_TTL_SECONDS = 30 * 60
MAX_SESSIONS = 500
EMAIL_RETRY_DELAYS = (5, 30, 120)
DONE_MARKER = "[[DONE]]"
SUMMARY_KEYS = ["parent_name", "child_name", "child_class", "phone", "query", "category", "urgency", "language"]

VOICE_AGENT_PROMPT = """You are the telephone-style voice assistant of PP International School (PPIS).
A parent is speaking to you. Your job is to note down their query for the school office.

Collect, one question at a time, in this order:
1. the parent's name
2. the child's name and class/section
3. a contact phone number (skip if you already know it)
4. the query or concern, in the parent's own words

Rules:
- You are speaking, not writing: reply in one or two short sentences. No lists, no markdown, no emojis.
- Reply in the language the parent uses (English, Hindi or Hinglish). Hindi in Devanagari.
- Do not invent school information. If asked something you do not know, say the office will call back.
- If the parent gave several details at once, do not ask for them again.
- When you have all four details, repeat the query back in one line, say the school office will
  receive it by email and get back to them, say goodbye, and end your reply with the exact token {marker}
- If the parent says goodbye or that there is nothing else, also end your reply with {marker}
"""

GREETING = (
    "Namaste, this is the PP International School assistant. "
    "Please tell me your name and your child's name and class, and how we can help you."
)

SUMMARY_PROMPT = """You are given a transcript of a conversation between a school voice assistant and a parent.
Extract the details as JSON with exactly these keys:
parent_name, child_name, child_class, phone, query, category, urgency, language.
category is one of: fees, admission, transport, attendance, academics, complaint, leave, other.
urgency is one of: low, normal, high.
Use "not given" for anything missing. query should be a clear one or two sentence summary in English.
Return only the JSON object."""


@dataclass
class VoiceSession:
    session_id: str
    channel: str
    contact: str = ""
    history: list[dict[str, str]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    done: bool = False
    emailed: bool = False
    emailing: bool = False


_sessions: dict[str, VoiceSession] = {}


def _purge_sessions() -> None:
    now = time.time()
    stale = [k for k, s in _sessions.items() if now - s.updated_at > SESSION_TTL_SECONDS]
    for k in stale:
        _sessions.pop(k, None)
    if len(_sessions) > MAX_SESSIONS:
        for k in sorted(_sessions, key=lambda k: _sessions[k].updated_at)[: len(_sessions) - MAX_SESSIONS]:
            _sessions.pop(k, None)


def new_session(channel: str, contact: str = "") -> VoiceSession:
    _purge_sessions()
    sid = secrets.token_urlsafe(16)
    session = VoiceSession(session_id=sid, channel=channel, contact=contact)
    _sessions[sid] = session
    return session


def get_session(session_id: str) -> VoiceSession | None:
    _purge_sessions()
    return _sessions.get(session_id)


def _strip_marker(text: str) -> tuple[str, bool]:
    done = DONE_MARKER in text
    cleaned = text.replace(DONE_MARKER, "").strip()
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, done


async def agent_reply(session: VoiceSession, user_text: str) -> tuple[str, bool]:
    """Run one turn. Returns (spoken reply, conversation finished)."""
    prompt = VOICE_AGENT_PROMPT.format(marker=DONE_MARKER)
    if session.contact:
        prompt += f"\nThe parent's phone number is already known: {session.contact}. Do not ask for it."
    raw = await generate_response(user_text, prompt, session.history)
    reply, done = _strip_marker(raw)
    session.history.append({"role": "user", "content": user_text})
    session.history.append({"role": "assistant", "content": reply})
    session.updated_at = time.time()
    if done:
        session.done = True
    return reply, done


async def synthesize_speech(text: str, response_format: str = "mp3") -> bytes | None:
    """Text to speech with OpenAI. Returns audio bytes or None."""
    ai_client = get_client()
    if ai_client is None or not text.strip():
        return None
    try:
        resp = await ai_client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text[:4000],
            response_format=response_format,
        )
        return resp.content
    except Exception as e:
        logger.error(f"TTS failed: {e}")
        return None


def _transcript_text(history: list[dict[str, str]]) -> str:
    lines = []
    for m in history:
        who = "Parent" if m["role"] == "user" else "Assistant"
        lines.append(f"{who}: {m['content']}")
    return "\n".join(lines)


async def summarise_conversation(history: list[dict[str, str]]) -> dict[str, str]:
    keys = SUMMARY_KEYS
    summary = {k: "not given" for k in keys}
    ai_client = get_client()
    transcript = _transcript_text(history)
    if ai_client is not None and transcript:
        try:
            resp = await ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                max_tokens=400,
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or "{}"
            data = json.loads(content)
            for k in keys:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    summary[k] = v.strip()
        except Exception as e:
            logger.error(f"Voice agent summary failed: {e}")
    if summary["query"] == "not given":
        user_lines = [m["content"] for m in history if m["role"] == "user"]
        summary["query"] = " ".join(user_lines)[:1000] or "not given"
    return summary


def build_admin_email(session: VoiceSession, summary: dict[str, str]) -> tuple[str, str]:
    when = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST")
    channel = {"web": "Website voice assistant", "whatsapp": "WhatsApp voice note"}.get(
        session.channel, session.channel
    )
    phone = summary["phone"]
    if phone == "not given" and session.contact:
        phone = session.contact
    subject = f"Parent query ({summary['category']}): {summary['child_name']} - {summary['child_class']}"
    body = (
        f"A parent left a query through the {channel}.\n\n"
        f"Time: {when}\n"
        f"Parent: {summary['parent_name']}\n"
        f"Child: {summary['child_name']}\n"
        f"Class: {summary['child_class']}\n"
        f"Phone: {phone}\n"
        f"Language: {summary['language']}\n"
        f"Category: {summary['category']}\n"
        f"Urgency: {summary['urgency']}\n\n"
        f"Query:\n{summary['query']}\n\n"
        f"Full transcript:\n{_transcript_text(session.history)}\n"
    )
    return subject, body


async def email_admin(session: VoiceSession) -> bool:
    """Summarise the session and mail it to the admin, retrying on SMTP failure.

    Sends at most one email per session; concurrent callers are no-ops.
    """
    if session.emailed or session.emailing or not session.history:
        return False
    session.emailing = True
    try:
        summary = await summarise_conversation(session.history)
        subject, body = build_admin_email(session, summary)
        for attempt, delay in enumerate((0,) + EMAIL_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            if await send_email_async(ADMIN_EMAIL, subject, body, sender_name="PPIS Voice Agent"):
                session.emailed = True
                return True
            logger.error(
                f"Voice agent: admin email attempt {attempt + 1} failed for session {session.session_id}"
            )
        return False
    finally:
        session.emailing = False


def email_admin_in_background(session: VoiceSession) -> None:
    asyncio.ensure_future(email_admin(session))


async def transcribe_upload(audio_bytes: bytes, content_type: str) -> str | None:
    return await transcribe_audio(audio_bytes=audio_bytes, content_type=content_type)


async def whatsapp_voice_note_to_admin(sender: str, transcript: str, reply: str) -> bool:
    """Mail the admin one WhatsApp voice note and the bot's reply."""
    session = VoiceSession(session_id=f"wa-{sender}-{int(time.time())}", channel="whatsapp", contact=sender)
    session.history = [
        {"role": "user", "content": transcript},
        {"role": "assistant", "content": reply},
    ]
    return await email_admin(session)
