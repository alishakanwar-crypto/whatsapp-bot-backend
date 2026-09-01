import unittest
from unittest.mock import AsyncMock, patch

from app.routes import agent_ws


class LockedRecorderAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        agent_ws._health_state.pop("recorders_alerted", None)

    async def _alert(self, health):
        sent = []

        async def send(number, message):
            sent.append((number, message))

        with patch.object(
            agent_ws,
            "_classrooms_on_recorder",
            AsyncMock(return_value=["GRADE 1B", "GRADE 3C", "PREP 1"]),
        ), patch(
            "app.services.whatsapp_service.send_whatsapp_force", send
        ):
            await agent_ws._alert_refused_recorders(health)
        return sent

    async def test_a_refused_login_names_the_classrooms_it_blocks(self):
        sent = await self._alert([
            {"ip": "192.168.0.12", "reason": "credentials refused"}
        ])

        self.assertEqual(len(sent), len(agent_ws._RECORDER_ALERT_NUMBERS))
        _, message = sent[0]
        self.assertIn("192.168.0.12", message)
        self.assertIn("GRADE 3C", message)
        self.assertIn("3 classroom(s)", message)
        self.assertIn("IST", message)

    async def test_the_same_lockout_is_not_alerted_twice(self):
        health = [{"ip": "192.168.0.12", "reason": "credentials refused"}]
        await self._alert(health)
        again = await self._alert(health)

        self.assertEqual(again, [])

    async def test_a_recorder_that_recovers_can_alert_again_later(self):
        health = [{"ip": "192.168.0.12", "reason": "credentials refused"}]
        await self._alert(health)
        await self._alert([])
        after_recovery = await self._alert(health)

        self.assertTrue(after_recovery)

    async def test_a_slow_recorder_is_not_reported_as_a_lockout(self):
        sent = await self._alert([
            {"ip": "192.168.0.12", "reason": "not answering"}
        ])

        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
