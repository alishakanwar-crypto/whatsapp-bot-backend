import asyncio
import unittest

from app.routes import agent_ws


class AgentDiskHealthTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_disk", None)
        agent_ws._health_state.pop("disk_alerted", None)

    def test_nothing_is_claimed_before_an_agent_reports(self):
        self.assertEqual(agent_ws.get_health_state()["agent_disk"], {})

    def test_free_space_is_shown(self):
        agent_ws._record_disk(
            {
                "disk": {
                    "drive": "C:",
                    "free_mb": 40960.0,
                    "total_mb": 476000.0,
                    "low": False,
                    "logs_mb": 12.5,
                    "snapshots_mb": 3.0,
                    "secret": "never asked for",
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_disk"]
        self.assertEqual(kept["free_mb"], 40960.0)
        self.assertFalse(kept["low"])
        self.assertNotIn("secret", kept)

    def test_a_low_drive_is_reported_once_until_it_recovers(self):
        sent: list[tuple[str, str]] = []

        async def fake_send(phone, message):
            sent.append((phone, message))
            return True

        import app.services.whatsapp_service as whatsapp_service

        original = whatsapp_service.send_whatsapp_force
        whatsapp_service.send_whatsapp_force = fake_send
        try:
            low = {
                "disk": {
                    "drive": "C:",
                    "free_mb": 300.0,
                    "total_mb": 476000.0,
                    "low": True,
                }
            }

            async def report_twice():
                agent_ws._record_disk(low)
                agent_ws._record_disk(low)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            asyncio.run(report_twice())
            self.assertEqual(len(sent), len(agent_ws._RECORDER_ALERT_NUMBERS))
            self.assertIn("300.0 MB free", sent[0][1])

            agent_ws._record_disk(
                {"disk": {"drive": "C:", "free_mb": 90000.0, "low": False}}
            )
            self.assertFalse(agent_ws._health_state["disk_alerted"])
        finally:
            whatsapp_service.send_whatsapp_force = original

    def test_an_agent_that_says_nothing_keeps_what_we_had(self):
        agent_ws._record_disk({"disk": {"free_mb": 1000.0, "low": False}})
        agent_ws._record_disk({})
        agent_ws._record_disk({"disk": "none"})
        self.assertEqual(
            agent_ws.get_health_state()["agent_disk"]["free_mb"], 1000.0
        )


if __name__ == "__main__":
    unittest.main()
