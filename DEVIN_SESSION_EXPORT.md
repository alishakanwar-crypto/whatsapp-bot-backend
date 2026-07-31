# Devin Session Export: "Connect Phone 919311258016 for Allowlist Responses"

**Session URL:** https://app.devin.ai/sessions/90ee4e1a5a2a4ba3ad8ca2e85f667101
**Status:** Running (active)

---

## Project Summary

A **WhatsApp & SMS bot for PP International School (PPIS)**. The bot:
- Responds to WhatsApp messages from allowlisted phone numbers with AI-powered replies (OpenAI GPT)
- Acts as a school administrator/facilitator — answering queries about the school
- Works in both individual chats and group chats
- Sends images (school photos, circulars) on request
- Has a daily lunch reminder at 2:00 PM IST to the PPIS BOT WhatsApp group
- Has teacher data for 36 class teachers stored in memory (names, emails, WhatsApp numbers)

---

## Tech Stack

- **Backend:** Python, FastAPI, SQLite (aiosqlite), OpenAI GPT (gpt-4o-mini), Green API (WhatsApp)
- **Frontend:** Admin dashboard (React-based, deployed on devinapps.com)
- **Hosting:** Backend on Fly.io, also deployed on Hostinger server (62.72.12.208)
- **WhatsApp Integration:** Green API (QR-code linked, not Meta Business API)
- **Database:** SQLite with aiosqlite for async operations

---

## GitHub Repositories

1. **whatsapp-bot-backend** — Main backend code
   - Repo: https://github.com/alishakanwar-crypto/whatsapp-bot-backend
   - PR: https://github.com/alishakanwar-crypto/whatsapp-bot-backend/pull/1

2. **ppis-messenger** — Messaging frontend/components
   - Repo: https://github.com/alishakanwar-crypto/ppis-messenger
   - PR: https://github.com/alishakanwar-crypto/ppis-messenger/pull/1

3. **ppis-campus-agent** — Campus surveillance agent (Hikvision cameras)
   - Repo: https://github.com/alishakanwar-crypto/ppis-campus-agent
   - PR: https://github.com/alishakanwar-crypto/ppis-campus-agent/pull/1

---

## Live Deployment URLs

| Resource | URL |
|---|---|
| Admin Dashboard (old) | https://phone-allowlist-app-gkhdmfv8.devinapps.com |
| Backend API (Fly.io, current) | https://app-reykyihf.fly.dev |
| Backend API (Fly.io, old) | https://app-ukmjfzku.fly.dev |
| Backend API (Hostinger) | http://62.72.12.208:5000 |
| WhatsApp Webhook | https://app-reykyihf.fly.dev/webhook/cloud |
| API Docs (Swagger) | http://62.72.12.208:5000/docs |

---

## Hostinger Server Details

- **IP:** 62.72.12.208
- **Code path:** /opt/whatsapp-bot-api/
- **Service:** whatsapp-bot-api.service (systemd, auto-restart)
- **Port:** 5000 (via nginx proxy to uvicorn on 8002)

---

## Key Code Structure (whatsapp-bot-backend)

```
app/
├── main.py                    # FastAPI app entrypoint, middleware, lifespan
├── database.py                # SQLite schema, init_db(), get_db()
├── seed_data.py               # Auto-seed 3 DVRs + 91 camera mappings
├── models/
│   └── schemas.py             # Pydantic data validation schemas
├── routes/
│   ├── webhook.py             # WhatsApp webhook (incoming messages)
│   ├── allowlist.py           # Manage allowlisted phone numbers
│   ├── messages.py            # Message history API
│   ├── settings.py            # Bot settings (system prompt, etc.)
│   ├── bulk.py                # Bulk messaging
│   ├── agent_ws.py            # WebSocket for campus agent
│   └── agent_config.py        # Camera/DVR configuration API
├── services/
│   ├── whatsapp_service.py    # Green API / Meta WhatsApp sending
│   ├── openai_service.py      # OpenAI GPT integration
│   ├── email_service.py       # SMTP email sending
│   ├── email_polling_service.py # IMAP email polling
│   ├── sms_service.py         # SMS integration (pluggable)
│   ├── bulk_service.py        # Bulk message logic
│   ├── scheduler_service.py   # APScheduler for scheduled tasks
│   └── sheet_refresh_service.py # Google Sheets sync for student data
├── static/                    # School images and assets
├── personalized_parents.json  # Student-parent contact mappings
pyproject.toml                 # Poetry dependencies
pi_sheet_data.json             # PI sheet student data
```

---

## Allowlisted Numbers (39 total)

**Original 6:**
1. PPIS BOT number (main bot number)
2. 5 admin numbers

**+ 33 teacher numbers** (added from teacher list image shared in session)

Admin panel numbers are flagged so the bot never addresses them as "Dear Parent".

---

## Configuration & Credentials Needed

The `.env` file at `/opt/whatsapp-bot-api/.env` needs:

```
WHATSAPP_ACCESS_TOKEN=<Meta WhatsApp token OR Green API token>
WHATSAPP_PHONE_NUMBER_ID=<Meta phone number ID>
OPENAI_API_KEY=<OpenAI API key>
AGENT_SECRET=<Campus agent WebSocket auth token>
VERIFY_TOKEN=<your webhook verify token>
```

- **Green API** was set up via QR code linking (phone 919311258016)
- **OpenAI** uses GPT-4o-mini model (~$5 credit lasts months)
- Meta webhook URL: `https://app-reykyihf.fly.dev/webhook/cloud`

---

## Bot Behavior / System Prompt

The bot is configured as:
- **Role:** School administrator/facilitator for PP International School
- **Tone:** Polite, helpful
- **Knowledge:** PPIS school info (CBSE affiliation, location in Pitampura Delhi, campus details, sports facilities, admissions), 36 class teachers with contact info
- **Responds to:** All allowlisted numbers in individual chats AND group chats
- **Group behavior:** Replies in groups where bot is added
- **Image support:** Can send school photos when asked
- **Scheduled tasks:** Daily lunch reminder at 2:00 PM IST to PPIS BOT group
- **Fallback:** If OpenAI credits run out, sends smart fixed replies based on stored school data

---

## Known Issues / Pending Work

1. **Image sending** — Was fixed to use direct file upload but may need further testing
2. **OpenAI credits** — Bot falls back to fixed replies if credits run out; needs $5+ credit on OpenAI
3. **WhatsApp re-linking** — Green API QR link may need periodic re-scanning if WhatsApp disconnects
4. **Google Workspace scanning** — School VC account 2FA blocked full scan; a teacher account was partially scanned
5. **Meta Business API** — Never completed setup (CAPTCHA blocked); using Green API instead
6. **Security:** Phone number matching was fixed to handle country code variants (e.g. with/without 91 prefix)
7. **Access control:** Only admin panel + PI Sheet parents get camera photos; unknown numbers denied

---

## Session Timeline (Key Events)

1. Built WhatsApp + SMS bot with FastAPI, Meta API, OpenAI, SQLite
2. Deployed to Fly.io + devinapps.com dashboard
3. Meta Business API setup failed (Facebook CAPTCHA blocked)
4. Switched to **Green API** (QR code based — much simpler)
5. Green API account registered with the school admin email
6. WhatsApp 919311258016 linked via QR code
7. OpenAI configured — bot giving AI-powered replies
8. Phone number matching bug fixed (country code handling)
9. Group message support added
10. 33 teacher numbers added to allowlist (39 total)
11. Teacher data (36 class teachers) stored in bot memory
12. Image sending capability added
13. Daily lunch reminder at 2:00 PM IST configured
14. School website content scanned and added to bot knowledge
15. Campus agent (Hikvision cameras) WebSocket support added
16. Backend also deployed to Hostinger server at 62.72.12.208:5000

---

## Full Conversation Log

The full conversation log is available in the Devin session (not committed to the repo for security reasons):
https://app.devin.ai/sessions/90ee4e1a5a2a4ba3ad8ca2e85f667101
