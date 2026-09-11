import unittest

from app.routes import agent_ws


class AgentWsLinkTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_ws_link", None)
        self._live = agent_ws._agent_ws

    def tearDown(self):
        agent_ws._agent_ws = self._live

    def test_nothing_is_claimed_before_an_agent_reports(self):
        self.assertEqual(agent_ws.get_health_state()["agent_ws_link"], {})

    def test_the_agents_own_view_is_kept(self):
        agent_ws._record_ws_link(
            {
                "ws_link": {
                    "connected": True,
                    "liveness_basis": "state=OPEN",
                    "silent_seconds": 3.2,
                    "recycles": 4,
                    "offline_seconds": 0.0,
                    "library_version": "13.1",
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_ws_link"]
        self.assertTrue(kept["connected"])
        self.assertEqual(kept["liveness_basis"], "state=OPEN")
        self.assertEqual(kept["recycles"], 4.0)
        self.assertEqual(kept["library_version"], "13.1")

    def test_an_agent_that_says_nothing_is_ignored(self):
        agent_ws._record_ws_link({"ws_link": "none"})
        self.assertEqual(agent_ws.get_health_state()["agent_ws_link"], {})

    def test_a_reading_that_is_not_a_number_is_dropped(self):
        agent_ws._record_ws_link(
            {
                "ws_link": {
                    "connected": True,
                    "silent_seconds": {"nested": "x" * 5000},
                    "recycles": "many",
                    "offline_seconds": True,
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_ws_link"]
        self.assertIsNone(kept["silent_seconds"])
        self.assertIsNone(kept["recycles"])
        self.assertIsNone(kept["offline_seconds"])

    def test_a_replaced_process_does_not_describe_the_live_link(self):
        live = object()
        replaced = object()
        agent_ws._agent_ws = live
        agent_ws._record_ws_link(
            {"ws_link": {"connected": True, "recycles": 0}}, live
        )
        agent_ws._record_ws_link(
            {"ws_link": {"connected": False, "recycles": 9}}, replaced
        )
        kept = agent_ws.get_health_state()["agent_ws_link"]
        self.assertTrue(kept["connected"])
        self.assertEqual(kept["recycles"], 0.0)


if __name__ == "__main__":
    unittest.main()
