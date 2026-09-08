import logging
import unittest

from app.routes import agent_ws


LAG = {"worst_seconds": 21.4, "usual_seconds": 0.004, "samples": 300}


class AgentLagHealthTests(unittest.TestCase):
    def setUp(self):
        self._started = agent_ws._health_state.get("agent_started_at_ist", "")
        agent_ws._health_state.pop("agent_lag", None)
        agent_ws._agent_lag_warned_at_seconds = 0.0

    def tearDown(self):
        agent_ws._health_state.pop("agent_lag", None)
        agent_ws._health_state["agent_started_at_ist"] = self._started
        agent_ws._agent_lag_warned_at_seconds = 0.0

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

    def test_one_bad_minute_is_not_logged_on_every_keepalive(self):
        """The sample count changes each pong; the incident is still one."""
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws._record_agent_lag({"agent_lag": LAG})
            agent_ws._record_agent_lag(
                {"agent_lag": dict(LAG, samples=301)}
            )
            agent_ws._record_agent_lag(
                {"agent_lag": dict(LAG, samples=302, worst_seconds=21.5)}
            )

        said = [
            line for line in logs.output
            if "delayed requests by up to" in line
        ]
        self.assertEqual(len(said), 1, logs.output)

    def test_a_materially_worse_delay_is_logged_again(self):
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws._record_agent_lag({"agent_lag": LAG})
            agent_ws._record_agent_lag(
                {"agent_lag": dict(LAG, worst_seconds=60.0)}
            )

        said = [
            line for line in logs.output
            if "delayed requests by up to" in line
        ]
        self.assertEqual(len(said), 2, logs.output)

    def test_a_fresh_delay_after_recovery_is_logged(self):
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws._record_agent_lag({"agent_lag": LAG})
            agent_ws._record_agent_lag(
                {"agent_lag": dict(LAG, worst_seconds=0.1)}
            )
            agent_ws._record_agent_lag({"agent_lag": LAG})

        said = [
            line for line in logs.output
            if "delayed requests by up to" in line
        ]
        self.assertEqual(len(said), 2, logs.output)

    def test_a_new_agents_first_bad_minute_is_logged(self):
        """A restarted process is not judged by the old one's worst delay."""
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws._record_agent_version(
                {"started_at_ist": "07-09-2026 17:40:05 IST"}
            )
            agent_ws._record_agent_lag({"agent_lag": LAG})
            agent_ws._record_agent_version(
                {"started_at_ist": "07-09-2026 18:05:11 IST"}
            )
            agent_ws._record_agent_lag(
                {"agent_lag": dict(LAG, worst_seconds=4.0)}
            )

        said = [
            line for line in logs.output
            if "delayed requests by up to" in line
        ]
        self.assertEqual(len(said), 2, logs.output)

    def test_a_reconnect_by_the_same_agent_does_not_repeat_the_warning(self):
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws._record_agent_version(
                {"started_at_ist": "07-09-2026 17:40:05 IST"}
            )
            agent_ws._record_agent_lag({"agent_lag": LAG})
            agent_ws._record_agent_version(
                {"started_at_ist": "07-09-2026 17:40:05 IST"}
            )
            agent_ws._record_agent_lag({"agent_lag": dict(LAG, samples=305)})

        said = [
            line for line in logs.output
            if "delayed requests by up to" in line
        ]
        self.assertEqual(len(said), 1, logs.output)

    def test_a_replaced_agents_late_hello_does_not_repeat_the_warning(self):
        """Its hello arrives after the live process has already been heard."""
        live = object()
        previous = agent_ws._agent_ws
        agent_ws._agent_ws = live
        try:
            with self.assertLogs(agent_ws.logger, level=logging.WARNING) as lg:
                agent_ws._record_agent_version(
                    {"started_at_ist": "07-09-2026 18:05:11 IST"}, live
                )
                agent_ws._record_agent_lag({"agent_lag": LAG}, live)
                agent_ws._record_agent_version(
                    {"started_at_ist": "07-09-2026 17:40:05 IST"}, object()
                )
                agent_ws._record_agent_lag({"agent_lag": LAG}, live)
        finally:
            agent_ws._agent_ws = previous

        said = [
            line for line in lg.output if "delayed requests by up to" in line
        ]
        self.assertEqual(len(said), 1, lg.output)
        self.assertEqual(
            agent_ws.get_health_state()["agent_started_at_ist"],
            "07-09-2026 18:05:11 IST",
        )

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
