import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.routes import trueface

MONDAY = datetime(2026, 8, 10, 9, 0, 0, tzinfo=trueface.IST)
SUNDAY = datetime(2026, 8, 9, 9, 0, 0, tzinfo=trueface.IST)


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeDb:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _frozen_clock(now):
    """Patch trueface.datetime so only .now() is frozen."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    return patch.object(trueface, "datetime", FrozenDatetime)


class TrueFacePollerWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trueface._poller_alert_sent_date = ""
        trueface._last_poller_contact.clear()
        trueface._poller_diag.clear()

    def _patches(self, now, attendance, template, text):
        return (
            _frozen_clock(now),
            patch.object(trueface, "_get_db", new=AsyncMock(return_value=FakeDb())),
            patch.object(
                trueface,
                "_get_all_attendance",
                new=AsyncMock(return_value=attendance),
            ),
            patch("app.services.whatsapp_service.send_cloud_template_message", new=template),
            patch("app.services.whatsapp_service.send_whatsapp_message", new=text),
            patch.object(trueface, "ALERT_PHONES", ["918076455224"]),
        )

    async def test_alerts_once_via_template_when_no_arrival_recorded(self):
        template = AsyncMock(return_value=True)
        text = AsyncMock(return_value=True)
        patches = self._patches(
            MONDAY, [{"pin": "7", "arrival_time": None}], template, text
        )

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            self.assertTrue(await trueface.check_poller_silence())
            # Same day: no repeat alert
            self.assertFalse(await trueface.check_poller_silence())

        template.assert_awaited_once()
        text.assert_not_awaited()
        self.assertEqual(template.await_args.args[1], trueface.POLLER_ALERT_TEMPLATE)
        detail = template.await_args.kwargs["body_params"][3]
        self.assertIn("has not reached the cloud at all today", detail)
        self.assertIn("restart_all_admin.vbs", detail)

    async def test_falls_back_to_text_when_template_fails(self):
        template = AsyncMock(return_value=False)
        text = AsyncMock(return_value=True)
        patches = self._patches(MONDAY, [], template, text)

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            self.assertTrue(await trueface.check_poller_silence())

        text.assert_awaited_once()
        self.assertIn("TrueFace attendance alert", text.await_args.args[1])

    async def test_no_alert_when_an_arrival_exists(self):
        template = AsyncMock(return_value=True)
        text = AsyncMock(return_value=True)
        patches = self._patches(
            MONDAY, [{"pin": "7", "arrival_time": "07:04:00"}], template, text
        )

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            self.assertFalse(await trueface.check_poller_silence())

        template.assert_not_awaited()
        text.assert_not_awaited()

    async def test_no_alert_on_sunday(self):
        template = AsyncMock(return_value=True)
        text = AsyncMock(return_value=True)
        patches = self._patches(SUNDAY, [], template, text)

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            self.assertFalse(await trueface.check_poller_silence())

        template.assert_not_awaited()

    async def test_heartbeat_records_poller_contact(self):
        result = await trueface.trueface_heartbeat(FakeRequest(None))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(trueface._last_poller_contact["source"], "heartbeat")
        self.assertEqual(result["at"], trueface._last_poller_contact["at"])
        self.assertTrue(result["at"].endswith("IST"))

    async def test_alert_reports_last_contact_when_poller_checked_in(self):
        trueface._record_poller_contact("heartbeat")
        template = AsyncMock(return_value=True)
        text = AsyncMock(return_value=True)
        patches = self._patches(MONDAY, [], template, text)

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            self.assertTrue(await trueface.check_poller_silence())

        detail = template.await_args.kwargs["body_params"][3]
        self.assertIn("Poller last reached the cloud at", detail)

    async def test_heartbeat_keeps_what_the_last_poll_saw(self):
        await trueface.trueface_heartbeat(
            FakeRequest({"login_ok": True, "table_found": False, "rows_seen": 0})
        )

        self.assertIs(trueface._poller_diag["table_found"], False)

    async def test_alert_names_a_lost_browser_session(self):
        trueface._record_poller_contact("heartbeat")
        trueface._poller_diag.update(
            {"login_ok": True, "table_found": False, "rows_seen": 0}
        )
        template = AsyncMock(return_value=True)
        text = AsyncMock(return_value=True)
        patches = self._patches(MONDAY, [], template, text)

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            self.assertTrue(await trueface.check_poller_silence())

        detail = template.await_args.kwargs["body_params"][3]
        self.assertIn("browser session is lost", detail)

    async def test_alert_names_a_failed_device_login(self):
        trueface._record_poller_contact("heartbeat")
        trueface._poller_diag.update({"login_ok": False})
        template = AsyncMock(return_value=True)
        text = AsyncMock(return_value=True)
        patches = self._patches(MONDAY, [], template, text)

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            self.assertTrue(await trueface.check_poller_silence())

        detail = template.await_args.kwargs["body_params"][3]
        self.assertIn("login to the TrueFace device failed", detail)

    async def test_heartbeat_keeps_only_the_fields_the_poller_reports(self):
        await trueface.trueface_heartbeat(
            FakeRequest({"login_ok": True, "device_password": "secret"})
        )

        self.assertIs(trueface._poller_diag["login_ok"], True)
        self.assertNotIn("device_password", trueface._poller_diag)

    async def test_heartbeat_keeps_an_allowed_field_only_in_its_own_shape(self):
        await trueface.trueface_heartbeat(
            FakeRequest(
                {
                    "login_ok": "PPIS admin password is hunter2",
                    "rows_seen": "call 1800-000-000 to claim",
                    "session_recycles": -7,
                    "last_event_ist": "x" * 400,
                }
            )
        )

        self.assertIs(trueface._poller_diag["login_ok"], True)
        self.assertNotIn("rows_seen", trueface._poller_diag)
        self.assertEqual(trueface._poller_diag["session_recycles"], 0)
        self.assertEqual(len(trueface._poller_diag["last_event_ist"]), 32)

    async def test_heartbeat_survives_a_count_that_is_not_a_finite_number(self):
        await trueface.trueface_heartbeat(
            FakeRequest({"login_ok": True, "rows_seen": float("inf")})
        )

        self.assertIs(trueface._poller_diag["login_ok"], True)
        self.assertNotIn("rows_seen", trueface._poller_diag)

    async def test_a_bodyless_heartbeat_drops_a_stale_failure(self):
        await trueface.trueface_heartbeat(
            FakeRequest({"login_ok": True, "table_found": False})
        )
        await trueface.trueface_heartbeat(FakeRequest(None))

        self.assertEqual(trueface._poller_diag, {})
        self.assertEqual(trueface._poller_diagnosis(), "")


if __name__ == "__main__":
    unittest.main()
