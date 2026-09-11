"""Health must say when the campus PC's key is being refused for config.

A refused key leaves the agent on its cached config.json: it keeps serving
photos from yesterday's recorder list while every change made in the cloud is
ignored, and nothing in health said so.
"""

import unittest

from app.routes import agent_ws


class ConfigKeyHealthTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_config_key_refused", None)

    def test_default_is_not_refused(self):
        self.assertFalse(
            agent_ws.get_health_state()["agent_config_key_refused"]
        )

    def test_refusal_is_reported(self):
        agent_ws._health_state["agent_config_key_refused"] = True
        self.assertTrue(
            agent_ws.get_health_state()["agent_config_key_refused"]
        )


if __name__ == "__main__":
    unittest.main()
