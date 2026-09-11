import unittest

from app.routes import agent_ws


class AgentPreviousRunTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_previous_run", None)

    def test_health_reports_nothing_before_an_agent_says_anything(self):
        self.assertEqual(agent_ws.get_health_state()["agent_previous_run"], {})

    def test_a_crashed_previous_run_is_kept(self):
        agent_ws._record_previous_run(
            {
                "previous_run": {
                    "ended_at": "2026-09-11 11:07:30",
                    "exit_code": "3",
                    "last_error": "[CRITICAL] out of memory",
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_previous_run"]
        self.assertEqual(kept["exit_code"], "3")
        self.assertIn("out of memory", kept["last_error"])

    def test_a_campus_path_never_reaches_public_health(self):
        agent_ws._record_previous_run(
            {
                "previous_run": {
                    "ended_at": "",
                    "exit_code": "1",
                    "last_error": "failed reading C:\\PPIS\\agent\\config.json",
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_previous_run"]
        self.assertNotIn("C:\\PPIS", kept["last_error"])

    def test_an_agent_that_says_nothing_is_ignored(self):
        agent_ws._record_previous_run({})
        self.assertEqual(agent_ws.get_health_state()["agent_previous_run"], {})


if __name__ == "__main__":
    unittest.main()
