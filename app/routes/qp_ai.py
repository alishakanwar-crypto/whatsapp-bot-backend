"""
Question Paper Maker: ChatGPT for the "Tell the paper maker what to fix" box
============================================================================
The static paper maker (alishakanwar-crypto.github.io/ppis-question-papers)
sends the teacher's command plus the paper's text (never the Word file) and
gets back a list of edit operations that the page applies itself, so boxes,
tables and pictures in the Word layout are never rebuilt.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("qp_ai")
router = APIRouter()

MAX_BODY = 200_000
MAX_PARAS = 900
RATE = 20  # requests per IP per minute
DAY_CAP = 1500  # ChatGPT calls per day for the whole service
ALLOWED_ORIGIN = "https://alishakanwar-crypto.github.io"
LOCAL_ORIGIN = re.compile(r"^http://(?:localhost|127\.0\.0\.1)(?::\d+)?$")
_hits: dict[str, deque] = defaultdict(deque)
_day: deque = deque()

SYSTEM = """You fix school question papers for P.P. International School. A teacher types a
plain-English (or Hindi/Hinglish) instruction; you return the exact edits as JSON.

You receive JSON with:
- "command": the teacher's instruction.
- "mode": "word" (the teacher's own Word layout is shown; edit it paragraph by paragraph) or
  "maker" (the paper is the plain text in "body", in the maker's syntax, which you may rewrite).
- "paragraphs" (word mode): [{"i": index, "t": text, "a": alignment, "b": true if bold,
  "tbl": true if inside a table cell, "pic": true if it holds a picture}].
- "body" (maker mode, and also in word mode for reference): the paper text in the maker's syntax:
  "SECTION A ..." headings, "1. question (2)" numbered questions with marks in brackets at the
  end, "(a) part" or "a) part" parts, "(i)" sub-parts, "____" answer lines, blank lines between.
- "settings": current font, size, line spacing, indent, whether question lines are bold.
- "meta": title, grade, subject, duration, max marks, session, general instructions.

Return ONLY a JSON object: {"plan": ["each separate change the teacher asked for, and the exact
lines/words it applies to"], "ops": [...], "message": "one short sentence telling the teacher
what you changed", "cannot": "" or a short reason if part of the command is impossible}.
Allowed ops (use the fewest that do the job; never touch anything the teacher did not ask about):
- {"op":"run","command":"<built-in command>"} for settings, metadata and simple fixes. Built-in
  commands: "font size 13" (any pt), "bigger font", "smaller font", "font Times New Roman",
  "line spacing tight|normal|roomy|double", "no indent", "indent wide", "questions bold",
  "questions not bold", "hide name line", "show name line", "Q3 marks 2", "add 4 lines after Q5",
  "remove lines from Q2", "add a gap after Q6", "delete Q7", "renumber", "title Unit Test 1",
  "grade VII", "subject English", "duration 2 hours", "max marks 40", "session 2026-27",
  "fix errors", "check", "pdf".
- Word mode only (i = paragraph index from "paragraphs"; "is" = list of indexes):
  {"op":"align","is":[..],"how":"left|center|right|justify"}
  {"op":"bold","is":[..],"on":true|false,"text":"optional"}   {"op":"italic","is":[..],"on":true|false,"text":"optional"}
  {"op":"underline","is":[..],"on":true|false,"text":"optional"}   {"op":"size","is":[..],"pt":12,"text":"optional"} (pt = the printed size the teacher wants)
  "text" limits the style to that exact text inside the paragraphs (e.g. a heading such as
  "Situation Vacant:" that starts a longer paragraph, or one word); omit it to style whole paragraphs.   {"op":"indent","is":[..],"mm":5}  (left indent; 0 removes it)
  {"op":"space","is":[..],"before_mm":2,"after_mm":2}
  {"op":"replace","find":"exact text","with":"new text","is":[..] (optional, limits where)}
  {"op":"set_text","i":3,"text":"whole new paragraph text"}  (never on a paragraph with "pic")
  {"op":"insert_after","i":3,"text":"new paragraph text","lines":0}  (lines = blank answer lines to add after it; for answer lines only, omit "text" and just set "lines")
  {"op":"delete","is":[..]}   {"op":"page_break_before","i":12}
- Maker mode only: {"op":"set_body","text":"the whole corrected body"} keeping the syntax,
  every question, part, mark, picture marker and table line exactly unless asked to change it.
Rules: each style applies ONLY to the target named with it. "make the Or lines italic and underline
the heading" = italic on the Or lines, underline on the heading text only, nothing else; a heading,
word or label inside a longer paragraph is styled with "text", not the whole paragraph; never invent or delete questions, marks or content unless asked; keep spelling as the
teacher wrote it unless asked to correct it; "or"/"OR"/"अथवा" choice lines are centred; marks
stay at the right. If the command is unclear, make the most likely sensible edit and say what
you assumed in "message"."""


def _origin_ok(request: Request) -> bool:
    o = request.headers.get("origin", "")
    return o == ALLOWED_ORIGIN or bool(LOCAL_ORIGIN.match(o))


def _rate_ok(ip: str) -> bool:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE:
        return False
    q.append(now)
    return True


def _day_ok() -> bool:
    now = time.time()
    while _day and now - _day[0] > 86400:
        _day.popleft()
    if len(_day) >= DAY_CAP:
        return False
    _day.append(now)
    return True


def _clean(payload: dict) -> dict:
    paras = payload.get("paragraphs") or []
    paras = [
        {k: p.get(k) for k in ("i", "t", "a", "b", "tbl", "pic") if p.get(k) is not None and p.get(k) is not False and p.get(k) != ""}
        for p in paras[:MAX_PARAS] if isinstance(p, dict)
    ]
    for p in paras:
        if isinstance(p.get("t"), str):
            p["t"] = p["t"][:400]
    return {
        "command": str(payload.get("command", ""))[:2000],
        "mode": "word" if payload.get("mode") == "word" else "maker",
        "paragraphs": paras,
        "body": str(payload.get("body", ""))[:60_000],
        "settings": payload.get("settings") or {},
        "meta": payload.get("meta") or {},
    }


@router.post("/api/qp/ai-fix")
async def qp_ai_fix(request: Request):
    if not _origin_ok(request):
        return JSONResponse({"error": "This service is only for the PPIS Question Paper Maker."}, status_code=403)
    ip = (request.headers.get("fly-client-ip") or (request.client.host if request.client else "")) or "?"
    if not _rate_ok(ip):
        return JSONResponse({"error": "Too many commands in a minute. Please wait a moment."}, status_code=429)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({"error": "The paper is too long to send in one go."}, status_code=413)
    try:
        data = _clean(json.loads(raw or b"{}"))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Bad request."}, status_code=400)
    if not data["command"].strip():
        return JSONResponse({"error": "Type a command first."}, status_code=400)

    from app.services.openai_service import get_client
    client = get_client()
    if client is None:
        return JSONResponse({"error": "ChatGPT is not set up on the school server."}, status_code=503)
    if not _day_ok():
        return JSONResponse({"error": "ChatGPT has done all its paper fixes for today. Please try again tomorrow."}, status_code=429)
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            timeout=60,
        )
        out = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:  # noqa: BLE001
        logger.warning("qp ai-fix failed: %s", e)
        return JSONResponse({"error": "ChatGPT could not be reached. Please try again."}, status_code=502)
    ops = out.get("ops") if isinstance(out.get("ops"), list) else []
    return {
        "ops": [o for o in ops if isinstance(o, dict) and isinstance(o.get("op"), str)][:200],
        "message": str(out.get("message", ""))[:500],
        "cannot": str(out.get("cannot", "") or "")[:500],
    }
