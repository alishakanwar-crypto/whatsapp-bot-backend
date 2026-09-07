import logging
import unittest

from app.routes import agent_ws


CAMERA = {
    "ip": "192.168.0.12",
    "channel": 12,
    "camera": "G1A  C2",
    "classroom": "GRADE 1A",
    "failures_in_a_row": 6,
    "last_served_ist": "",
    "last_failed_ist": "07-09-2026 14:41:02 IST",
    "reason": "timed out: TimeoutError",
}


class DeadCameraHealthTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("cameras_not_answering", None)

    def test_health_starts_with_no_camera_reported(self):
        self.assertEqual(
            agent_ws.get_health_state()["cameras_not_answering"], []
        )

    def test_a_camera_giving_nothing_is_named_in_health(self):
        """So the class whose angle is missing can be found from the cloud."""
        agent_ws._record_recorder_health({"camera_health": [CAMERA]})

        reported = agent_ws.get_health_state()["cameras_not_answering"]

        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["camera"], "G1A  C2")
        self.assertEqual(reported[0]["classroom"], "GRADE 1A")
        self.assertEqual(reported[0]["failures_in_a_row"], 6)

    def test_a_camera_that_serves_again_clears_the_list(self):
        agent_ws._record_recorder_health({"camera_health": [CAMERA]})
        agent_ws._record_recorder_health({"camera_health": []})

        self.assertEqual(
            agent_ws.get_health_state()["cameras_not_answering"], []
        )

    def test_an_older_agent_that_says_nothing_leaves_the_list_alone(self):
        agent_ws._record_recorder_health({"camera_health": [CAMERA]})
        agent_ws._record_recorder_health({"dvr_health": []})

        self.assertEqual(
            len(agent_ws.get_health_state()["cameras_not_answering"]), 1
        )


class ReplacedAgentTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("cameras_not_answering", None)
        self._live = agent_ws._agent_ws

    def tearDown(self):
        agent_ws._agent_ws = self._live
        agent_ws._health_state.pop("cameras_not_answering", None)

    def test_a_replaced_agents_last_pong_is_ignored(self):
        """Its failure counts stopped being added to when it was replaced."""
        live = object()
        replaced = object()
        agent_ws._agent_ws = live
        agent_ws._record_recorder_health({"camera_health": []}, live)

        agent_ws._record_recorder_health({"camera_health": [CAMERA]}, replaced)

        self.assertEqual(
            agent_ws.get_health_state()["cameras_not_answering"], []
        )

    def test_the_live_agents_report_is_kept(self):
        live = object()
        agent_ws._agent_ws = live

        agent_ws._record_recorder_health({"camera_health": [CAMERA]}, live)

        self.assertEqual(
            len(agent_ws.get_health_state()["cameras_not_answering"]), 1
        )


class RepeatedWarningTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("cameras_not_answering", None)

    def tearDown(self):
        agent_ws._health_state.pop("cameras_not_answering", None)

    def test_the_same_camera_is_not_logged_on_every_keepalive(self):
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws._record_camera_health({"camera_health": [CAMERA]})
            agent_ws._record_camera_health({"camera_health": [CAMERA]})
            agent_ws._record_camera_health({"camera_health": [CAMERA]})

        said = [
            line for line in logs.output
            if "Classroom cameras giving no picture" in line
        ]
        self.assertEqual(len(said), 1, logs.output)

    def test_a_changed_report_is_logged_again(self):
        worse = dict(CAMERA, failures_in_a_row=9)
        with self.assertLogs(agent_ws.logger, level=logging.WARNING) as logs:
            agent_ws._record_camera_health({"camera_health": [CAMERA]})
            agent_ws._record_camera_health({"camera_health": [worse]})

        said = [
            line for line in logs.output
            if "Classroom cameras giving no picture" in line
        ]
        self.assertEqual(len(said), 2, logs.output)


if __name__ == "__main__":
    unittest.main()
