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


if __name__ == "__main__":
    unittest.main()
