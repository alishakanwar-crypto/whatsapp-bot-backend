import unittest

from app.routes import agent_ws


class RecorderFallbackHealthTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("recorders_on_fallback", None)
        agent_ws._health_state.pop("recorders_reported_at_ist", None)

    def test_health_starts_with_no_recorder_on_fallback(self):
        state = agent_ws.get_health_state()

        self.assertEqual(state["recorders_on_fallback"], [])
        self.assertEqual(state["recorders_reported_at_ist"], "")

    def test_agent_report_shows_up_in_health_with_an_ist_stamp(self):
        agent_ws._record_recorder_health({
            "dvr_health": [
                {
                    "ip": "192.168.0.12",
                    "reason": "credentials refused",
                    "seconds_remaining": 900.0,
                }
            ]
        })
        state = agent_ws.get_health_state()

        self.assertEqual(len(state["recorders_on_fallback"]), 1)
        self.assertEqual(
            state["recorders_on_fallback"][0]["ip"], "192.168.0.12"
        )
        self.assertTrue(state["recorders_reported_at_ist"].endswith("IST"))

    def test_a_recovered_recorder_clears_the_list(self):
        agent_ws._record_recorder_health({
            "dvr_health": [{"ip": "192.168.0.12", "reason": "not answering"}]
        })
        agent_ws._record_recorder_health({"dvr_health": []})

        self.assertEqual(
            agent_ws.get_health_state()["recorders_on_fallback"], []
        )

    def test_an_agent_that_reports_nothing_leaves_the_last_state_alone(self):
        agent_ws._record_recorder_health({
            "dvr_health": [{"ip": "192.168.0.12", "reason": "not answering"}]
        })
        agent_ws._record_recorder_health({})

        self.assertEqual(
            len(agent_ws.get_health_state()["recorders_on_fallback"]), 1
        )


if __name__ == "__main__":
    unittest.main()
