"""
English Answer Checker: mark a photographed page of a student's handwritten English
=================================================================================
The phone page (alishakanwar-crypto.github.io/ppis-question-papers/checker.html) sends one
photo. OpenCV boxes every handwritten word and numbers it; ChatGPT gets the clean photo and
the numbered copy, reads every box, and lists the errors by box number. The boxes come back
as fractions of the photo so the page can draw them in red. Photos are not stored.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
from collections import defaultdict, deque

import cv2
import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from PIL import Image

logger = logging.getLogger("answer_check")
router = APIRouter()

MAX_BODY = 9_000_000
MAX_SIDE = 2000
MAX_BOXES = 450
MAX_PIXELS = 40_000_000
RATE = 30
DAY_CAP = 1500
MODELS = ("gpt-5.4", "gpt-4.1")
TYPES = ("Spelling", "Punctuation", "Capitalisation", "Grammar", "Usage")
ALLOWED_ORIGIN = "https://alishakanwar-crypto.github.io"
LOCAL_ORIGIN = re.compile(r"^http://(?:localhost|127\.0\.0\.1)(?::\d+)?$")
_hits: dict[str, deque] = defaultdict(deque)
_day: deque = deque()

PROMPT = """You are an experienced English teacher at an Indian CBSE school marking a student's
handwritten work. Image 1 is the clean photo. Image 2 is the same photo with every handwritten
word boxed and numbered in red (word ids).{level}{question}

1. "words": for EVERY numbered box in image 2, in order, give [id, the exact text inside that box
   as the student wrote it], keeping their mistakes, capitals and punctuation. Use "" for a box
   with no writing (a smudge, a ruled line, a margin mark).
2. "lines": the student's text line by line exactly as written.
3. "errors": every DEFINITE error in the student's writing:
   - Spelling (incl. words wrongly joined or split, e.g. "alot")
   - Punctuation (missing full stop at the end of a sentence, wrong or missing apostrophe,
     missing question mark, comma clearly required)
   - Capitalisation (sentence start, "I", names of people, places, days, months, festivals)
   - Grammar (tense, subject-verb agreement, articles, prepositions, plurals, pronouns, word order)
   - Usage (wrong word, e.g. there/their, cloths/clothes)
   Only real errors: never style suggestions, "better" words or rewording of correct sentences
   (e.g. "like" before examples is fine). Report each mistake once. British and
   Indian English spellings (colour, favourite, programme) are correct. Ignore crossed-out
   words, printed text, the teacher's own marks and the question itself.
   For each: "ids": the word id(s) of the wrong word(s) (for a missing word or mark, the id of
   the word just before the gap), "wrong": text as written ("" if something is missing),
   "correct": the corrected text, "type": one of Spelling|Punctuation|Capitalisation|Grammar|Usage,
   "why": a reason of at most 8 words a student understands.
If there is no handwritten English on the page, return empty lists.
Return ONLY JSON: {"words":[[1,"..."]],"lines":["..."],"errors":[{"ids":[1],"wrong":"","correct":"","type":"","why":""}]}"""


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


def decode_image(data_url: str) -> np.ndarray | None:
    b64 = data_url.split(",", 1)[1] if data_url.startswith("data:") else data_url
    try:
        buf = np.frombuffer(base64.b64decode(b64, validate=False), np.uint8)
    except (ValueError, TypeError):
        return None
    try:
        with Image.open(io.BytesIO(buf.tobytes())) as head:
            w, h = head.size
    except Exception:  # noqa: BLE001
        return None
    if w * h > MAX_PIXELS:
        return None
    im = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if im is None:
        return None
    h, w = im.shape[:2]
    s = MAX_SIDE / max(h, w)
    if s < 1:
        im = cv2.resize(im, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    return im


def find_words(im: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Box each handwritten word, in reading order (top line first, left to right)."""
    H, W = im.shape[:2]
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
                               max(15, (W // 40) | 1), 15)
    hl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, W // 30), 1)))
    vl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, H // 30))))
    lines = cv2.bitwise_or(cv2.dilate(hl, np.ones((3, 1), np.uint8)), cv2.dilate(vl, np.ones((1, 3), np.uint8)))
    ink = cv2.morphologyEx(cv2.subtract(bw, lines), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    _, _, st, _ = cv2.connectedComponentsWithStats(ink, 8)
    hs = [s[3] for s in st[1:] if s[4] > 15]
    if not hs:
        return []
    xh = float(np.median(hs))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(xh * 0.6)), max(1, int(xh * 0.15))))
    _, _, st, _ = cv2.connectedComponentsWithStats(cv2.dilate(ink, k), 8)
    boxes = [(int(x), int(y), int(w), int(h)) for x, y, w, h, a in st[1:]
             if a > xh * xh * 0.5 and h > xh * 0.4 and w < W * 0.6 and h < H * 0.25]
    boxes.sort(key=lambda b: b[1] + b[3] / 2)
    rows: list[dict] = []
    for b in boxes:
        cy = b[1] + b[3] / 2
        if rows and abs(cy - rows[-1]["cy"]) < xh * 1.2:
            rows[-1]["b"].append(b)
            rows[-1]["cy"] = float(np.mean([q[1] + q[3] / 2 for q in rows[-1]["b"]]))
        else:
            rows.append({"cy": cy, "b": [b]})
    out: list[tuple[int, int, int, int]] = []
    for r in rows:
        out += sorted(r["b"], key=lambda b: b[0])
    return out[:MAX_BOXES]


def numbered_copy(im: np.ndarray, boxes) -> np.ndarray:
    a = im.copy()
    hs = sorted(b[3] for b in boxes) or [20]
    scale = max(0.45, min(1.1, hs[len(hs) // 2] / 55))
    for i, (x, y, w, h) in enumerate(boxes, 1):
        cv2.rectangle(a, (x, y), (x + w, y + h), (0, 160, 0), 1)
        org = (x, max(12, y - 3))
        cv2.putText(a, str(i), org, cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 4)
        cv2.putText(a, str(i), org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 230), 2)
    return a


def _jpeg(im: np.ndarray) -> str:
    _, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


STYLE_WHY = re.compile(r"\b(better|clearer|more (?:formal|natural|appropriate|precise)|preferred|preferable|sounds|smoother|improves?)\b", re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"[^\w']", "", str(s).replace("\u2019", "'")).lower()


def place_errors(raw: dict, boxes, W: int, H: int) -> list[dict]:
    """Turn ChatGPT's errors into red boxes; move an id to the box whose text matches."""
    words = {}
    for item in raw.get("words") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                words[int(item[0])] = str(item[1])
            except (TypeError, ValueError):
                pass
    out = []
    for e in raw.get("errors") or []:
        if not isinstance(e, dict):
            continue
        ids = []
        for i in e.get("ids") or []:
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            if 1 <= i <= len(boxes):
                ids.append(i)
        wrong = str(e.get("wrong", "") or "")[:200]
        correct = str(e.get("correct", "") or "")[:200]
        why = str(e.get("why", "") or "")[:120]
        if STYLE_WHY.search(why):
            continue
        if not wrong.strip() and ids and correct.strip() and not correct.strip()[0].isalnum():
            if words.get(ids[-1], "").rstrip().endswith(correct.strip()[-1]):
                continue
        first = _norm(wrong.split()[0]) if wrong.split() else ""
        if first and ids and _norm((words.get(ids[0], "").split() or [""])[0]) != first:
            cands = [i for i, t in words.items() if 1 <= i <= len(boxes) and _norm(t) == first]
            if cands:
                best = min(cands, key=lambda i: abs(i - ids[0]))
                ids = [best + (j - ids[0]) for j in ids if 1 <= best + (j - ids[0]) <= len(boxes)]
        box = None
        if ids:
            xs = [boxes[i - 1] for i in ids]
            x0 = min(b[0] for b in xs); y0 = min(b[1] for b in xs)
            x1 = max(b[0] + b[2] for b in xs); y1 = max(b[1] + b[3] for b in xs)
            if x1 - x0 < W * 0.7:
                box = [round(x0 / W, 4), round(y0 / H, 4), round((x1 - x0) / W, 4), round((y1 - y0) / H, 4)]
        t = str(e.get("type", "")).split("|")[0].strip().capitalize()
        if t.startswith("Capital"):
            t = "Capitalisation"
        out.append({
            "box": box,
            "wrong": wrong,
            "correct": correct,
            "type": t if t in TYPES else "Grammar",
            "why": why,
        })
    seen, uniq = set(), []
    for e in out:
        k = (tuple(e["box"] or ()), e["correct"].strip().lower())
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    out = uniq
    out.sort(key=lambda e: (e["box"][1] + e["box"][3] / 2, e["box"][0]) if e["box"] else (2, 2))
    return out[:200]


async def _ask(client, prompt: str, clean: str, marked: str) -> dict:
    last = None
    for model in MODELS:
        kw = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": clean, "detail": "high"}},
                {"type": "image_url", "image_url": {"url": marked, "detail": "high"}},
            ]}],
            "timeout": 110,
        }
        if model.startswith("gpt-4"):
            kw["temperature"] = 0
        try:
            resp = await client.chat.completions.create(**kw)
            content = resp.choices[0].message.content
            if not content:
                raise ValueError("empty reply")
            return json.loads(content)
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("answer-check %s failed: %s", model, e)
    raise RuntimeError(str(last))


@router.post("/api/answer-check")
async def answer_check(request: Request):
    if not _origin_ok(request):
        return JSONResponse({"error": "This service is only for the PPIS English Answer Checker."}, status_code=403)
    ip = (request.headers.get("fly-client-ip") or (request.client.host if request.client else "")) or "?"
    if not _rate_ok(ip):
        return JSONResponse({"error": "Too many pages in a minute. Please wait a moment."}, status_code=429)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY:
        return JSONResponse({"error": "The photo is too large."}, status_code=413)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            return JSONResponse({"error": "The photo is too large."}, status_code=413)
    if len(raw) > MAX_BODY:
        return JSONResponse({"error": "The photo is too large."}, status_code=413)
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    im = await asyncio.to_thread(decode_image, str(data.get("image", "")))
    if im is None:
        return JSONResponse({"error": "That photo could not be opened. Please take it again."}, status_code=400)
    H, W = im.shape[:2]
    boxes = await asyncio.to_thread(find_words, im)
    if not boxes:
        return {"lines": [], "errors": [], "words": 0, "note": "No handwriting was found on this photo."}

    from app.services.openai_service import get_client
    client = get_client()
    if client is None:
        return JSONResponse({"error": "ChatGPT is not set up on the school server."}, status_code=503)
    if not _day_ok():
        return JSONResponse({"error": "The checker has marked all its pages for today. Please try again tomorrow."}, status_code=429)
    grade = re.sub(r"[^\w ]", "", str(data.get("grade", "")))[:20].strip()
    question = str(data.get("question", "") or "")[:600].strip()
    prompt = PROMPT.replace(
        "{level}", f"\nThe student is in Class {grade}; flag every real error, whatever the class." if grade else ""
    ).replace("{question}", f"\nThe question or topic was: {question}" if question else "")
    clean, marked = await asyncio.to_thread(lambda: (_jpeg(im), _jpeg(numbered_copy(im, boxes))))
    try:
        out = await _ask(client, prompt, clean, marked)
    except RuntimeError:
        return JSONResponse({"error": "ChatGPT could not be reached. Please try again."}, status_code=502)
    errors = place_errors(out, boxes, W, H)
    lines = [str(x)[:300] for x in (out.get("lines") or []) if isinstance(x, (str, int, float))][:120]
    return {"lines": lines, "errors": errors, "words": len(boxes)}
