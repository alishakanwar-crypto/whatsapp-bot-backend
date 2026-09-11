import unittest

from app.routes import agent_ws


class AgentWsLinkTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_ws_link", None)

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
        self.assertEqual(kept["recycles"], 4)
        self.assertEqual(kept["library_version"], "13.1")

    def test_an_agent_that_says_nothing_is_ignored(self):
        agent_ws._record_ws_link({"ws_link": "none"})
        self.assertEqual(agent_ws.get_health_state()["agent_ws_link"], {})


if __name__ == "__main__":
    unittest.main()
