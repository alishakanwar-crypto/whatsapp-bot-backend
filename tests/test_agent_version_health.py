import unittest

from app.routes import agent_ws


class AgentVersionHealthTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_code_commit", None)
        agent_ws._health_state.pop("agent_started_at_ist", None)

    def test_health_reports_nothing_before_an_agent_says_hello(self):
        state = agent_ws.get_health_state()

        self.assertEqual(state["agent_code_commit"], "")
        self.assertEqual(state["agent_started_at_ist"], "")

    def test_hello_puts_the_running_commit_in_health(self):
        agent_ws._record_agent_version({
            "code_commit": "19efd01",
            "started_at_ist": "31-08-2026 14:05:12 IST",
        })
        state = agent_ws.get_health_state()

        self.assertEqual(state["agent_code_commit"], "19efd01")
        self.assertEqual(
            state["agent_started_at_ist"], "31-08-2026 14:05:12 IST"
        )

    def test_an_older_agent_that_reports_no_version_is_not_faked(self):
        agent_ws._record_agent_version({})

        state = agent_ws.get_health_state()
        self.assertEqual(state["agent_code_commit"], "")
        self.assertEqual(state["agent_started_at_ist"], "")

    def test_a_reconnecting_agent_replaces_the_previous_commit(self):
        agent_ws._record_agent_version({"code_commit": "b923a53"})
        agent_ws._record_agent_version({"code_commit": "19efd01"})

        self.assertEqual(
            agent_ws.get_health_state()["agent_code_commit"], "19efd01"
        )


if __name__ == "__main__":
    unittest.main()
