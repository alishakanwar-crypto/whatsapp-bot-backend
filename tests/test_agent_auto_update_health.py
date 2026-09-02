"""Merged fixes sat unused on the campus PC while everything read healthy.

A PC that cannot reach GitHub looks exactly like one that is already up to
date, so agent health now carries what the last update check actually saw.
"""
import unittest

from app.routes import agent_ws


class AgentAutoUpdateHealthTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_auto_update", None)

    def test_health_reports_nothing_before_an_agent_says_hello(self):
        self.assertEqual(agent_ws.get_health_state()["agent_auto_update"], {})

    def test_a_failed_fetch_reaches_health(self):
        agent_ws._record_auto_update({
            "auto_update": {
                "enabled": True,
                "wrapper": True,
                "origin_commit": "",
                "last_error": "fatal: could not read Username",
                "checked_at_ist": "02-09-2026 12:40:11 IST",
            }
        })

        state = agent_ws.get_health_state()["agent_auto_update"]
        self.assertEqual(state["last_error"], "fatal: could not read Username")
        self.assertEqual(state["checked_at_ist"], "02-09-2026 12:40:11 IST")

    def test_an_agent_nothing_would_restart_is_visible(self):
        agent_ws._record_auto_update({
            "auto_update": {"enabled": True, "wrapper": False}
        })

        self.assertFalse(
            agent_ws.get_health_state()["agent_auto_update"]["wrapper"]
        )

    def test_an_older_agent_that_reports_nothing_is_not_faked(self):
        agent_ws._record_auto_update({})

        self.assertEqual(agent_ws.get_health_state()["agent_auto_update"], {})

    def test_a_new_agent_that_says_nothing_drops_the_old_report(self):
        agent_ws._record_auto_update({"auto_update": {"last_error": "boom"}})
        agent_ws._record_auto_update({}, hello=True)

        self.assertEqual(agent_ws.get_health_state()["agent_auto_update"], {})

    def test_a_git_error_reaches_health_without_urls_or_paths(self):
        agent_ws._record_auto_update({
            "auto_update": {
                "last_error": (
                    "fatal: unable to access "
                    "'https://token123@github.com/school/agent.git': "
                    "cwd C:\\ppis\\ppis-campus-agent"
                )
            }
        })

        reported = agent_ws.get_health_state()["agent_auto_update"]["last_error"]
        self.assertNotIn("token123", reported)
        self.assertNotIn("github.com", reported)
        self.assertNotIn("C:\\ppis", reported)
        self.assertIn("unable to access", reported)

    def test_a_later_check_replaces_the_previous_one(self):
        agent_ws._record_auto_update({"auto_update": {"last_error": "boom"}})
        agent_ws._record_auto_update({
            "auto_update": {"last_error": "", "origin_commit": "5cf0311"}
        })

        state = agent_ws.get_health_state()["agent_auto_update"]
        self.assertEqual(state["last_error"], "")
        self.assertEqual(state["origin_commit"], "5cf0311")


if __name__ == "__main__":
    unittest.main()
