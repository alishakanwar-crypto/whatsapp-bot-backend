import asyncio

from fastapi.testclient import TestClient

from app.services import voice_agent_service as va


def test_strip_marker_detects_done():
    text, done = va._strip_marker("Thank you, goodbye. " + va.DONE_MARKER)
    assert done is True
    assert text == "Thank you, goodbye."
    text, done = va._strip_marker("What is your child's class?")
    assert done is False


def test_agent_reply_marks_session_done_and_keeps_history(monkeypatch):
    async def fake_generate(user_message, system_prompt, history=None):
        assert va.DONE_MARKER in system_prompt
        assert "9876543210" in system_prompt
        return "Noted, the office will call you. Goodbye. " + va.DONE_MARKER

    monkeypatch.setattr(va, "generate_response", fake_generate)
    session = va.new_session("whatsapp", contact="9876543210")
    reply, done = asyncio.run(va.agent_reply(session, "Fees kab tak bharni hai?"))
    assert done and session.done
    assert va.DONE_MARKER not in reply
    assert session.history[-1] == {"role": "assistant", "content": reply}


def test_email_admin_sends_once_with_summary(monkeypatch):
    sent = []

    async def fake_summary(history):
        return {
            "parent_name": "Rita", "child_name": "Aarav", "child_class": "Grade 3B",
            "phone": "not given", "query": "Wants the fee due date.",
            "category": "fees", "urgency": "normal", "language": "Hinglish",
        }

    async def fake_send(to, subject, body, sender_name="PPIS Bot", attachments=None):
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(va, "summarise_conversation", fake_summary)
    monkeypatch.setattr(va, "send_email_async", fake_send)
    session = va.new_session("web", contact="9876543210")
    session.history = [
        {"role": "user", "content": "Fees kab tak bharni hai?"},
        {"role": "assistant", "content": "Noted."},
    ]
    assert asyncio.run(va.email_admin(session)) is True
    assert asyncio.run(va.email_admin(session)) is False
    to, subject, body = sent[0]
    assert to == va.ADMIN_EMAIL
    assert "fees" in subject and "Aarav" in subject
    assert "Phone: 9876543210" in body
    assert "Website voice assistant" in body
    assert "Parent: Rita" in body
    assert "IST" in body


def test_web_turn_and_end_endpoints(monkeypatch):
    from app.main import app

    emailed = []

    async def fake_generate(user_message, system_prompt, history=None):
        return "Which class is your child in?"

    async def fake_speech(text, response_format="mp3"):
        return b"ID3fake"

    async def fake_email(session):
        emailed.append(session.session_id)
        return True

    monkeypatch.setattr(va, "generate_response", fake_generate)
    monkeypatch.setattr(va, "synthesize_speech", fake_speech)
    monkeypatch.setattr(va, "email_admin", fake_email)

    with TestClient(app) as client:
        assert client.get("/voice-agent").status_code == 200
        start = client.post("/api/voice-agent/start").json()
        sid = start["session_id"]
        assert start["reply"] == va.GREETING and start["audio_b64"]

        turn = client.post("/api/voice-agent/turn", data={"session_id": sid, "text": "Hello, I am Rita"}).json()
        assert turn["transcript"] == "Hello, I am Rita"
        assert turn["reply"] == "Which class is your child in?"
        assert turn["done"] is False

        end = client.post("/api/voice-agent/end", data={"session_id": sid}).json()
        assert end["status"] == "ok"
    assert emailed == [sid]
