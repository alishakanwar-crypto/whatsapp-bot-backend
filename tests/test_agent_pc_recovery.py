import unittest

from app.routes import agent_ws


class AgentPcRecoveryTests(unittest.TestCase):
    def setUp(self):
        agent_ws._health_state.pop("agent_pc_recovery", None)
        self._live = agent_ws._agent_ws

    def tearDown(self):
        agent_ws._agent_ws = self._live

    def test_nothing_is_claimed_before_an_agent_reports(self):
        self.assertEqual(agent_ws.get_health_state()["agent_pc_recovery"], {})

    def test_a_pc_that_cannot_recover_without_a_logon_is_shown(self):
        agent_ws._record_pc_recovery(
            {
                "pc_recovery": {
                    "boot_at_ist": "15-09-2026 01:59:00 IST",
                    "recovers_without_logon": False,
                    "tasks_missing": ["PPIS Campus Agent Watchdog (System)"],
                    "tasks_unreadable": [],
                    "tasks_disabled": [],
                    "tasks_need_logon": ["PPIS Campus Agent Watchdog"],
                    "tasks": {
                        "PPIS Campus Agent Watchdog": {
                            "exists": True,
                            "enabled": True,
                            "needs_logon": True,
                            "last_run": "15-09-2026 01:55:00",
                            "last_result": "0",
                            "next_run": "15-09-2026 07:20:00",
                        }
                    },
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_pc_recovery"]
        self.assertFalse(kept["recovers_without_logon"])
        self.assertEqual(
            kept["tasks_missing"], ["PPIS Campus Agent Watchdog (System)"]
        )
        task = kept["tasks"]["PPIS Campus Agent Watchdog"]
        self.assertTrue(task["needs_logon"])
        self.assertEqual(task["last_result"], "0")

    def test_an_agent_that_says_nothing_keeps_what_we_had(self):
        agent_ws._record_pc_recovery(
            {"pc_recovery": {"recovers_without_logon": True}}
        )
        agent_ws._record_pc_recovery({"pc_recovery": {}})
        agent_ws._record_pc_recovery({"pc_recovery": "none"})
        kept = agent_ws.get_health_state()["agent_pc_recovery"]
        self.assertTrue(kept["recovers_without_logon"])

    def test_only_the_fields_we_asked_for_reach_a_public_endpoint(self):
        agent_ws._record_pc_recovery(
            {
                "pc_recovery": {
                    "boot_at_ist": "x" * 200,
                    "tasks_need_logon": ["a" * 200] + [str(n) for n in range(30)],
                    "tasks": {
                        "PPIS Nightly Restart": {
                            "exists": True,
                            "enabled": True,
                            "needs_logon": False,
                            "account": "PPIS\\principal",
                            "path": "C:\\Users\\principal\\ppis-campus-agent",
                        },
                        "junk": "not a task",
                    },
                }
            }
        )
        kept = agent_ws.get_health_state()["agent_pc_recovery"]
        self.assertEqual(len(kept["boot_at_ist"]), 40)
        self.assertEqual(len(kept["tasks_need_logon"]), 10)
        self.assertEqual(len(kept["tasks_need_logon"][0]), 60)
        task = kept["tasks"]["PPIS Nightly Restart"]
        self.assertNotIn("account", task)
        self.assertNotIn("path", task)
        self.assertNotIn("junk", kept["tasks"])

    def test_a_replaced_process_does_not_describe_this_pc(self):
        live = object()
        replaced = object()
        agent_ws._agent_ws = live
        agent_ws._record_pc_recovery(
            {"pc_recovery": {"recovers_without_logon": True}}, live
        )
        agent_ws._record_pc_recovery(
            {"pc_recovery": {"recovers_without_logon": False}}, replaced
        )
        kept = agent_ws.get_health_state()["agent_pc_recovery"]
        self.assertTrue(kept["recovers_without_logon"])

    def test_an_agent_that_cannot_report_is_not_credited_with_recovery(self):
        # A PC rolled back to an older agent says nothing about its tasks;
        # holding on to the last answer would promise a recovery nobody has.
        agent_ws._record_pc_recovery(
            {"pc_recovery": {"recovers_without_logon": True}}
        )
        agent_ws._record_pc_recovery({"agent_id": "campus"}, hello=True)
        self.assertEqual(agent_ws.get_health_state()["agent_pc_recovery"], {})

    def test_one_silent_pong_does_not_erase_the_last_reading(self):
        agent_ws._record_pc_recovery(
            {"pc_recovery": {"recovers_without_logon": True}}
        )
        agent_ws._record_pc_recovery({})
        kept = agent_ws.get_health_state()["agent_pc_recovery"]
        self.assertTrue(kept["recovers_without_logon"])


if __name__ == "__main__":
    unittest.main()
