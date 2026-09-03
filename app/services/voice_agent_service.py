"""Voice agent for parents: talk, collect the query, mail the school admin.

Used by two channels:
  * the browser widget at /voice-agent (mic -> Whisper -> GPT -> TTS)
  * WhatsApp voice notes handled in routes/webhook.py

Each finished conversation is summarised by GPT into a fixed set of fields
and emailed to VOICE_AGENT_ADMIN_EMAIL.
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.services.email_service import send_email_async
from app.services.openai_service import (
    generate_response,
    get_client,
    transcribe_audio,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

ADMIN_EMAIL = os.getenv("VOICE_AGENT_ADMIN_EMAIL", "info@ppischool.in")
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
SARVAM_TTS_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "priya")
SARVAM_MAX_CHARS = 1500
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
TTS_MODEL = os.getenv("VOICE_AGENT_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("VOICE_AGENT_TTS_VOICE", "coral")
TTS_INSTRUCTIONS = os.getenv(
    "VOICE_AGENT_TTS_INSTRUCTIONS",
    "You are a polite, warm Indian woman at the front desk of a school in Delhi. "
    "Speak with a natural Indian English accent, at a calm and clear pace, like a "
    "receptionist talking to a parent. Pronounce Hindi and Indian names the Indian way.",
)
SESSION_TTL_SECONDS = 30 * 60
MAX_SESSIONS = 500
EMAIL_RETRY_DELAYS = (5, 30, 120)
DONE_MARKER = "[[DONE]]"
SUMMARY_KEYS = ["parent_name", "child_name", "child_class", "phone", "query", "category", "urgency", "language"]

SCHOOL_FACTS = """Facts about the school you may state confidently:
- PP International School (PPIS): CBSE affiliated Senior Secondary School, affiliation no. 2730720.
- Address: LD Block, Pitampura, near Kohat Enclave Metro Station (Pillar No. 333), New Delhi 110034.
  Nearest metro: Kohat Enclave, Yellow Line.
- Classes: Pre-nursery (called Popsicles) to Grade 12. English medium. Languages taught: English,
  Hindi, German, French. Senior secondary streams: Science (PCM / PCB), Commerce, Humanities.
- Principal: Ms. Deepi Bector.
- School timings, summer: Pre-primary 7:30 am to 11:30 am; Grade 1 onwards 7:30 am to 1:30 pm.
  Winter: Pre-primary 8:00 am to 12:00 noon; Grade 1 onwards 8:00 am to 2:00 pm.
- Saturdays are off, except one Saturday a month for clubs.
- Transport: air-conditioned buses with GPS tracking, CCTV and a caretaker on every bus;
  23 routes across Delhi NCR.
- Medical room run with Max Healthcare (Shalimar Bagh), full-time nurse on campus.
- Nursery admission 2026-27: child must be above 3 and below 4 years as on 31 March 2026;
  40 seats (30 general, 10 EWS/DG); registration fee Rs 25; forms at the school office or www.ppi.school.
- Contact: front desk / helpline 8800935552; Administration Incharge Ms. Harpreet Kaur 9599488106;
  email info@ppischool.in; website www.ppi.school.
- Fees: paid quarterly; the last date for payment is the 10th of the first month of each quarter
  (April, July, October, January). Online payment at www.ppi.school or at the school office.
- Fee amounts, exam dates, holiday lists, results and anything about a particular
  student or teacher: you do NOT know these. Never guess them.
"""

VOICE_AGENT_PROMPT = """You are the telephone-style voice assistant of PP International School (PPIS),
speaking as a courteous Indian school receptionist. A parent is speaking to you. Your job is to
note down their query accurately for the school office, and answer simple questions about the school.

{facts}
How to run the conversation:
- Listen first. When the caller asks something, answer it (from the facts) before asking for any detail.
  Never repeat a question the caller has already answered or declined.
- Work out who is calling from what they say. Two kinds of callers:
  a) Parent of a current PPIS student: they say their child studies here, or talk about their child's
     class teacher, fees due, bus, attendance, homework or a complaint about something that happened in
     school. Get, one question at a time and only what is still missing: parent's name, child's name and
     class/section, contact number, and the concern in their words.
  b) Prospective parent or general enquirer: anyone asking about admission, seats, registration, school
     tour, curriculum, fee structure, "what is going on", or who says they have no child in the school.
     Admission intent wins: a caller who mentions their child or a class while asking about admission is
     a prospective parent (the class is the one they want admission to, not a current class). Do NOT ask
     for a child's name or current class. Answer what you can, then ask only for their name, a contact
     number so the office can call back, and, only if they are asking about admission, which class they
     are seeking admission for.
  If it is unclear which kind, ask once: "Is your child studying at PPIS, or are you enquiring about
  admission?" and go by the answer.
- If the caller says they are not a parent, declines a detail, or the detail does not apply, accept it,
  treat that detail as done, and move on; never ask the same thing again.

Name rule:
- Speech recognition sometimes mishears Indian names, so after the parent gives the child's name, repeat
  it once in your next question, e.g. "Aarav in class 3B, thank you. What is your contact number?" Do
  NOT spell names letter by letter and do not ask the parent to spell them; parents find that slow.
- Only if the parent corrects the name, or the transcribed name is clearly garbled or not a plausible
  Indian name, ask once: "Sorry, could you say the child's name again?" and use what they say. If the
  parent spells a name themselves, use exactly their spelling.
- Use common Indian spellings for names (Aarav, Ananya, Kabir, Riya, Saanvi, Sharma, Gupta, Singh).
- When you read back the phone number, say it digit by digit.

Rules:
- You are speaking, not writing: reply in one or two short sentences. No lists, no markdown, no emojis.
- Reply in the language the parent uses (English, Hindi or Hinglish). Hindi in Devanagari.
- Answer only from the facts above. If a question has parts, answer the parts the facts cover
  (for example school timings) and for the rest, if not covered or you are not fully sure, do not
  guess and do not give a wrong answer: say politely that you will note the question and the school
  office will call back with the correct information, then continue collecting details.
- If the parent gave several details at once, do not ask for them again.
- When you have the caller's name, a contact number and their query (plus child and class only for a
  current parent), or the caller has declined what is still missing, repeat the query back in one spoken sentence (no list of fields), say the school
  office will receive it by email and get back to them, say goodbye, and end your reply with the
  exact token {marker}
- If the parent says goodbye or that there is nothing else, also end your reply with {marker}
"""

def greeting() -> str:
    hour = datetime.now(IST).hour
    if hour < 12:
        salute = "Good morning"
    elif hour < 17:
        salute = "Good afternoon"
    else:
        salute = "Good evening"
    return (
        f"{salute}, this is the PP International School assistant. "
        "How can I help you today?"
    )

SUMMARY_PROMPT = """You are given a transcript of a conversation between a school voice assistant and a parent.
Extract the details as JSON with exactly these keys:
parent_name, child_name, child_class, phone, query, category, urgency, language.
category is one of: fees, admission, transport, attendance, academics, complaint, leave, other.
urgency is one of: low, normal, high.
language is the language the parent spoke: English, Hindi or Hinglish (judge from the transcript).
Use "not given" for anything missing; for an admission or general enquiry child_name and child_class are
usually "not given" and that is fine. query should be a clear one or two sentence summary in English.
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
    if len(_sessions) >= MAX_SESSIONS:
        for k in sorted(_sessions, key=lambda k: _sessions[k].updated_at)[: len(_sessions) - MAX_SESSIONS + 1]:
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
    prompt = VOICE_AGENT_PROMPT.format(marker=DONE_MARKER, facts=SCHOOL_FACTS)
    if session.contact:
        prompt += f"\nThe parent's phone number is already known: {session.contact}. Do not ask for it."
    prior = list(session.history)
    session.history.append({"role": "user", "content": user_text})
    session.updated_at = time.time()
    raw = await generate_response(user_text, prompt, prior)
    reply, done = _strip_marker(raw)
    session.history.append({"role": "assistant", "content": reply})
    session.updated_at = time.time()
    if done:
        session.done = True
    return reply, done


async def _sarvam_tts(text: str, response_format: str) -> bytes | None:
    """Sarvam AI Bulbul TTS: Indian female voice for English, Hindi and Hinglish.

    Sarvam accepts at most SARVAM_MAX_CHARS per request; longer text returns None so the caller falls back.
    """
    if len(text) > SARVAM_MAX_CHARS:
        return None
    payload = {
        "text": text,
        "target_language_code": "hi-IN" if _DEVANAGARI.search(text) else "en-IN",
        "speaker": SARVAM_TTS_SPEAKER,
        "model": SARVAM_TTS_MODEL,
        "pace": 0.95,
        "speech_sample_rate": 24000,
        "output_audio_codec": "wav" if response_format == "wav" else "mp3",
    }
    headers = {"api-subscription-key": SARVAM_API_KEY}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://api.sarvam.ai/text-to-speech", json=payload, headers=headers)
    if resp.status_code != 200:
        logger.error(f"Sarvam TTS failed: {resp.status_code} {resp.text[:200]}")
        return None
    audios = resp.json().get("audios") or []
    return base64.b64decode(audios[0]) if audios else None


_SPOKEN_ENGLISH_PROMPT = (
    "Rewrite the school chatbot reply below as it should be SPOKEN aloud to a parent, in English only. "
    "Translate any Hindi or Hinglish to natural English. Keep every fact, name, phone number and time exactly. "
    "Do not start with Namaste or any greeting word; begin directly with the answer. "
    "Remove emojis, markdown, bullet symbols, links and 'reply with' menu instructions. "
    "Use short plain sentences, at most three. Output only the rewritten text."
)


async def to_spoken_english(text: str) -> str:
    """Turn a WhatsApp text reply into short spoken English for TTS. Falls back to a cleaned copy."""
    cleaned = re.sub(r"[*_~`#>]", "", text).strip()
    ai_client = get_client()
    if ai_client is None or not cleaned:
        return cleaned
    try:
        resp = await ai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=300,
            messages=[
                {"role": "system", "content": _SPOKEN_ENGLISH_PROMPT},
                {"role": "user", "content": cleaned[:4000]},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or cleaned
    except Exception as e:
        logger.error(f"Spoken-English rewrite failed: {e}")
        return cleaned


async def _openai_tts(text: str, response_format: str) -> bytes | None:
    ai_client = get_client()
    if ai_client is None:
        return None
    kwargs: dict = {
        "model": TTS_MODEL,
        "voice": TTS_VOICE,
        "input": text,
        "response_format": response_format,
    }
    if TTS_INSTRUCTIONS and TTS_MODEL.startswith("gpt-"):
        kwargs["instructions"] = TTS_INSTRUCTIONS
    resp = await ai_client.audio.speech.create(**kwargs)
    return resp.content


async def synthesize_speech(text: str, response_format: str = "mp3") -> bytes | None:
    """Text to speech. Sarvam (Indian voice) when configured, else OpenAI. Returns audio bytes or None."""
    text = text.strip()[:4000]
    if not text:
        return None
    try:
        if SARVAM_API_KEY:
            audio = await _sarvam_tts(text, response_format)
            if audio:
                return audio
        return await _openai_tts(text, response_format)
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
