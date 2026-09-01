"""The campus watch has to find a fault before a parent does, and must never
probe a recorder that is refusing our login."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.services import campus_watch_service as watch


class CampusWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        watch._recorder_state.clear()
        watch._next_classroom_index.clear()
        watch._link_alert_sent = False
        watch._link_down_since = None
        self.sent: list[str] = []

    def _capture_alerts(self, delivered=True):
        async def record(message):
            self.sent.append(message)
            return delivered

        return patch.object(watch, "_alert", side_effect=record)

    def _health(self, **overrides):
        state = {
            "connected": True,
            "disconnected_seconds": 0.0,
            "agent_code_commit": "abc1234",
            "agent_started_at_ist": "01-09-2026 07:00:00 IST",
            "recorders_on_fallback": [],
        }
        state.update(overrides)
        return state

    async def test_offline_campus_pc_is_alerted_once_then_recovery(self):
        health = self._health(connected=False, disconnected_seconds=600.0)
        with self._capture_alerts(), \
                patch.object(watch, "is_working_day", AsyncMock(return_value=True)), \
                patch.object(watch, "within_school_hours", return_value=True), \
                patch("app.routes.agent_ws.get_health_state", return_value=health):
            await watch.check_campus_link()
            await watch.check_campus_link()
            self.assertEqual(len(self.sent), 1)
            self.assertIn("Campus PC Offline", self.sent[0])

            health["connected"] = True
            await watch.check_campus_link()

        self.assertEqual(len(self.sent), 2)
        self.assertIn("Back Online", self.sent[1])

    async def test_a_brief_disconnect_is_not_alerted(self):
        health = self._health(connected=False, disconnected_seconds=30.0)
        with self._capture_alerts(), \
                patch.object(watch, "is_working_day", AsyncMock(return_value=True)), \
                patch.object(watch, "within_school_hours", return_value=True), \
                patch("app.routes.agent_ws.get_health_state", return_value=health):
            await watch.check_campus_link()

        self.assertEqual(self.sent, [])

    async def test_a_failing_recorder_is_alerted_once_and_on_recovery(self):
        rooms = {"192.0.2.11": ["Grade 5A", "Grade 4A"]}
        failure = {"success": False, "error": "camera offline"}
        with self._capture_alerts(), \
                patch.object(watch, "_classrooms_by_recorder",
                             AsyncMock(return_value=rooms)), \
                patch("app.routes.agent_ws.is_agent_connected",
                      return_value=True), \
                patch("app.routes.agent_ws.get_health_state",
                      return_value=self._health()), \
                patch("app.routes.agent_ws.request_snapshot",
                      AsyncMock(return_value=failure)):
            await watch.sweep_cameras()
            await watch.sweep_cameras()
            self.assertEqual(len(self.sent), 1)
            self.assertIn("Not Capturing", self.sent[0])
            self.assertIn("Grade 5A", self.sent[0])

            with patch("app.routes.agent_ws.request_snapshot",
                       AsyncMock(return_value={"success": True, "image_count": 2})):
                await watch.sweep_cameras()   # Grade 5A well again
                await watch.sweep_cameras()   # Grade 4A too, so recorder is ok

        self.assertEqual(len(self.sent), 2)
        self.assertIn("Recovered", self.sent[1])
        self.assertEqual(
            watch.watch_state()["cameras"]["192.0.2.11 Grade 5A"]["verdict"],
            "ok",
        )

    async def test_an_outage_that_predates_startup_is_still_alerted(self):
        # A deploy while the campus PC is off leaves the connection state with
        # no disconnect of its own to measure from.
        health = self._health(connected=False, disconnected_seconds=0.0)
        clock = [0.0]
        with self._capture_alerts(), \
                patch.object(watch, "is_working_day", AsyncMock(return_value=True)), \
                patch.object(watch, "within_school_hours", return_value=True), \
                patch.object(watch.time, "monotonic", lambda: clock[0]), \
                patch("app.routes.agent_ws.get_health_state", return_value=health):
            await watch.check_campus_link()
            self.assertEqual(self.sent, [])
            clock[0] = watch.LINK_DOWN_ALERT_SECONDS + 1
            await watch.check_campus_link()

        self.assertEqual(len(self.sent), 1)
        self.assertIn("Campus PC Offline", self.sent[0])

    async def test_an_undelivered_alert_is_retried(self):
        health = self._health(connected=False, disconnected_seconds=600.0)
        with self._capture_alerts(delivered=False), \
                patch.object(watch, "is_working_day", AsyncMock(return_value=True)), \
                patch.object(watch, "within_school_hours", return_value=True), \
                patch("app.routes.agent_ws.get_health_state", return_value=health):
            await watch.check_campus_link()
            await watch.check_campus_link()

        self.assertEqual(len(self.sent), 2)

    async def test_a_healthy_room_does_not_clear_a_broken_rooms_fault(self):
        rooms = {"192.0.2.11": ["Grade 5A", "Grade 4A"]}

        async def probe(classroom, timeout=0):
            if classroom == "Grade 5A":
                return {"success": False, "error": "camera offline"}
            return {"success": True, "image_count": 2}

        with self._capture_alerts(), \
                patch.object(watch, "_classrooms_by_recorder",
                             AsyncMock(return_value=rooms)), \
                patch("app.routes.agent_ws.is_agent_connected",
                      return_value=True), \
                patch("app.routes.agent_ws.get_health_state",
                      return_value=self._health()), \
                patch("app.routes.agent_ws.request_snapshot", probe):
            await watch.sweep_cameras()   # Grade 5A fails
            await watch.sweep_cameras()   # Grade 4A is fine

        self.assertEqual(len(self.sent), 1)
        self.assertIn("Not Capturing", self.sent[0])
        self.assertEqual(
            watch.watch_state()["cameras"]["192.0.2.11 Grade 5A"]["verdict"],
            "failed",
        )

    async def test_the_morning_check_never_claims_ready_without_a_capture(self):
        with self._capture_alerts(), \
                patch.object(watch, "is_working_day", AsyncMock(return_value=True)), \
                patch.object(watch, "_classrooms_by_recorder",
                             AsyncMock(return_value={})), \
                patch("app.routes.agent_ws.is_agent_connected",
                      return_value=True), \
                patch("app.routes.agent_ws.get_health_state",
                      return_value=self._health()):
            message = await watch.morning_readiness()

        self.assertNotIn("Everything is ready", message)
        self.assertIn("COULD NOT BE CHECKED", message)

    async def test_a_recorder_refusing_our_login_is_never_probed(self):
        rooms = {"192.0.2.12": ["Grade 3C"]}
        health = self._health(
            recorders_on_fallback=[
                {"ip": "192.0.2.12", "reason": "credentials refused"}
            ]
        )
        probe = AsyncMock(return_value={"success": True, "image_count": 2})
        with self._capture_alerts(), \
                patch.object(watch, "_classrooms_by_recorder",
                             AsyncMock(return_value=rooms)), \
                patch("app.routes.agent_ws.is_agent_connected",
                      return_value=True), \
                patch("app.routes.agent_ws.get_health_state",
                      return_value=health), \
                patch("app.routes.agent_ws.request_snapshot", probe):
            findings = await watch.sweep_cameras()

        probe.assert_not_awaited()
        self.assertEqual(findings, [])

    async def test_a_slow_capture_is_reported_as_slow(self):
        rooms = {"192.0.2.11": ["Grade 5A"]}

        async def slow(classroom, timeout=0):
            await asyncio.sleep(0)
            return {"success": True, "image_count": 1}

        with self._capture_alerts(), \
                patch.object(watch, "SLOW_CAPTURE_SECONDS", -1.0), \
                patch.object(watch, "_classrooms_by_recorder",
                             AsyncMock(return_value=rooms)), \
                patch("app.routes.agent_ws.is_agent_connected",
                      return_value=True), \
                patch("app.routes.agent_ws.get_health_state",
                      return_value=self._health()), \
                patch("app.routes.agent_ws.request_snapshot", slow):
            findings = await watch.sweep_cameras()

        self.assertEqual(findings[0]["verdict"], "slow")
        self.assertIn("Cameras Slow", self.sent[0])

    async def test_the_sweep_rotates_through_a_recorders_classrooms(self):
        rooms = {"192.0.2.11": ["Grade 5A", "Grade 4A", "Grade 3C"]}
        asked = []

        async def probe(classroom, timeout=0):
            asked.append(classroom)
            return {"success": True, "image_count": 1}

        with self._capture_alerts(), \
                patch.object(watch, "_classrooms_by_recorder",
                             AsyncMock(return_value=rooms)), \
                patch("app.routes.agent_ws.is_agent_connected",
                      return_value=True), \
                patch("app.routes.agent_ws.get_health_state",
                      return_value=self._health()), \
                patch("app.routes.agent_ws.request_snapshot", probe):
            for _ in range(4):
                await watch.sweep_cameras()

        self.assertEqual(
            asked, ["Grade 5A", "Grade 4A", "Grade 3C", "Grade 5A"]
        )

    async def test_the_morning_check_states_the_running_commit_and_recorders(self):
        rooms = {"192.0.2.11": ["Grade 5A"], "192.0.2.12": ["Grade 3C"]}
        health = self._health(
            recorders_on_fallback=[
                {"ip": "192.0.2.12", "reason": "credentials refused"}
            ]
        )
        with self._capture_alerts(), \
                patch.object(watch, "is_working_day", AsyncMock(return_value=True)), \
                patch.object(watch, "_classrooms_by_recorder",
                             AsyncMock(return_value=rooms)), \
                patch("app.routes.agent_ws.is_agent_connected",
                      return_value=True), \
                patch("app.routes.agent_ws.get_health_state",
                      return_value=health), \
                patch("app.routes.agent_ws.request_snapshot",
                      AsyncMock(return_value={"success": True, "image_count": 2})):
            message = await watch.morning_readiness()

        self.assertIn("abc1234", message)
        self.assertIn("192.0.2.11", message)
        self.assertIn("login refused", message)
        self.assertEqual(self.sent, [message])

    async def test_the_morning_check_leads_with_an_offline_campus_pc(self):
        with self._capture_alerts(), \
                patch.object(watch, "is_working_day", AsyncMock(return_value=True)), \
                patch("app.routes.agent_ws.get_health_state",
                      return_value=self._health(connected=False)):
            message = await watch.morning_readiness()

        self.assertIn("OFFLINE", message)

    async def test_nothing_is_checked_on_a_holiday(self):
        with self._capture_alerts(), \
                patch.object(watch, "is_working_day", AsyncMock(return_value=False)):
            self.assertEqual(await watch.morning_readiness(), "")

        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
