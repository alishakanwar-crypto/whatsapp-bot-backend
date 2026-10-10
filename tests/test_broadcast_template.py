import pytest

from app import main


@pytest.mark.asyncio
async def test_broadcast_sends_the_named_template(monkeypatch):
    sent = []

    async def fake_template(to, name, body_params=None, **kwargs):
        sent.append((to, name, body_params))
        return True

    async def fail_text(to, message):
        raise AssertionError("plain text must not be used for a template run")

    monkeypatch.setattr(
        "app.services.whatsapp_service.send_cloud_template_message",
        fake_template,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_service.send_whatsapp_force", fail_text
    )
    main._broadcast_status = {
        "running": False, "done": False,
        "total": 2, "progress": 0, "sent": 0, "failed": 0,
    }

    await main._run_broadcast(
        ["919899641369", "918076508220"], "", 0,
        template="ppis_parent_services_notice",
    )

    assert sent == [
        ("919899641369", "ppis_parent_services_notice", None),
        ("918076508220", "ppis_parent_services_notice", None),
    ]
    assert main._broadcast_status["sent"] == 2
    assert main._broadcast_status["failed"] == 0


@pytest.mark.asyncio
async def test_broadcast_without_a_template_still_sends_text(monkeypatch):
    sent = []

    async def fake_text(to, message):
        sent.append((to, message))
        return True

    monkeypatch.setattr(
        "app.services.whatsapp_service.send_whatsapp_force", fake_text
    )
    main._broadcast_status = {
        "running": False, "done": False,
        "total": 1, "progress": 0, "sent": 0, "failed": 0,
    }

    await main._run_broadcast(["919899641369"], "hello", 0)

    assert sent == [("919899641369", "hello")]
