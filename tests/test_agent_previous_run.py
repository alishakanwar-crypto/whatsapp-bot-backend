import unittest

from app.routes import agent_ws


class AgentPreviousRunTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_previous_run", None)
        self._live = agent_ws._agent_ws

    def tearDown(self):
        agent_ws._agent_ws = self._live

    def test_health_reports_nothing_before_an_agent_says_anything(self):
        self.assertEqual(agent_ws.get_health_state()["agent_previous_run"], {})

    def test_a_crashed_previous_run_is_named_by_its_kind(self):
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
        self.assertEqual(kept["ended_how"], "critical error")

    def test_the_log_line_itself_never_reaches_public_health(self):
        agent_ws._record_previous_run(
            {
                "previous_run": {
                    "ended_at": "",
                    "exit_code": "1",
                    "last_error": (
                        "[ERROR] failed reading C:\\PPIS\\agent\\config.json "
                        "with key abcd1234"
                    ),
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_previous_run"]
        self.assertNotIn("last_error", kept)
        self.assertEqual(kept["ended_how"], "error")
        self.assertNotIn("abcd1234", str(kept))

    def test_a_clean_stop_is_told_apart_from_a_crash(self):
        agent_ws._record_previous_run(
            {
                "previous_run": {
                    "ended_at": "2026-09-11 11:07:30",
                    "exit_code": "0",
                    "last_error": "",
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_previous_run"]
        self.assertEqual(kept["ended_how"], "stopped without an error")

    def test_an_agent_that_says_nothing_is_ignored(self):
        agent_ws._record_previous_run({})
        self.assertEqual(agent_ws.get_health_state()["agent_previous_run"], {})

    def test_a_replaced_process_does_not_describe_the_live_one(self):
        live = object()
        replaced = object()
        agent_ws._agent_ws = live
        agent_ws._record_previous_run(
            {"previous_run": {"exit_code": "0", "last_error": ""}}, live
        )
        agent_ws._record_previous_run(
            {
                "previous_run": {
                    "exit_code": "9",
                    "last_error": "Traceback (most recent call last)",
                }
            },
            replaced,
        )
        kept = agent_ws.get_health_state()["agent_previous_run"]
        self.assertEqual(kept["exit_code"], "0")
        self.assertEqual(kept["ended_how"], "stopped without an error")


if __name__ == "__main__":
    unittest.main()
