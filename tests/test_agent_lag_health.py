import logging
import unittest

from app.routes import agent_ws


LAG = {"worst_seconds": 21.4, "usual_seconds": 0.004, "samples": 300}


class AgentLagHealthTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_lag", None)

    def tearDown(self):
        agent_ws._health_state.pop("agent_lag", None)

    def test_health_starts_with_no_lag_reported(self):
        self.assertEqual(agent_ws.get_health_state()["agent_lag"], {})

    def test_the_delay_the_agent_causes_is_named_in_health(self):
        """So a slow morning is not blamed on the cameras without evidence."""
        agent_ws._record_recorder_health({"agent_lag": LAG})

        self.assertEqual(
            agent_ws.get_health_state()["agent_lag"]["worst_seconds"], 21.4
        )

    def test_an_older_agent_that_says_nothing_leaves_the_reading_alone(self):
        agent_ws._record_recorder_health({"agent_lag": LAG})
        agent_ws._record_recorder_health({"dvr_health": []})

        self.assertEqual(agent_ws.get_health_state()["agent_lag"], LAG)

    def test_a_replaced_agents_reading_is_ignored(self):
        live = object()
        previous = agent_ws._agent_ws
        agent_ws._agent_ws = live
        try:
            agent_ws._record_recorder_health({"agent_lag": LAG}, live)
            agent_ws._record_recorder_health(
                {"agent_lag": {"worst_seconds": 0.0}}, object()
            )
        finally:
            agent_ws._agent_ws = previous

        self.assertEqual(agent_ws.get_health_state()["agent_lag"], LAG)

    def test_a_long_delay_is_logged_once_per_change(self):
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws._record_agent_lag({"agent_lag": LAG})
            agent_ws._record_agent_lag({"agent_lag": LAG})

        said = [
            line for line in logs.output
            if "delayed requests by up to" in line
        ]
        self.assertEqual(len(said), 1, logs.output)

    def test_a_healthy_reading_is_not_logged(self):
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws.logger.warning("something else")
            agent_ws._record_agent_lag(
                {"agent_lag": {"worst_seconds": 0.3, "usual_seconds": 0.001}}
            )

        said = [
            line for line in logs.output
            if "delayed requests by up to" in line
        ]
        self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
