"""Only the 5 PM full-day head count goes out; interim reports stay off."""

import unittest
from unittest.mock import AsyncMock, patch

from app.routes import gate
from app.services import scheduler_service


class DailyHeadcountReportOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_interim_verified_correction_is_withheld_by_default(self):
        recount = {
            "date": "2026-08-27",
            "hour_start": "2026-08-27 09:00:00",
            "hour_end": "2026-08-27 10:00:00",
            "in_count": 21,
            "processed_frames": 0,
            "source": "camera_native_counter",
            "verified_at": "2026-08-27 10:05:00",
        }
        open_db = AsyncMock()
        send = AsyncMock()

        with (
            patch.object(gate, "GATE_INTERIM_REPORTS_ENABLED", False),
            patch.object(gate, "GATE_REPORT_WHATSAPP_PHONES", ["918882127171"]),
            patch.object(gate, "_get_db", open_db),
            patch.object(gate, "send_cloud_template_message", send),
        ):
            await gate._send_verified_cpplus_correction(recount)

        open_db.assert_not_awaited()
        send.assert_not_awaited()

    def test_only_the_five_pm_report_is_scheduled_with_recipients(self):
        jobs: list[str] = []

        class FakeScheduler:
            def add_job(self, func, **kwargs):
                jobs.append(kwargs.get("id", ""))

        with (
            patch.object(gate, "GATE_INTERIM_REPORTS_ENABLED", False),
            patch.object(gate, "GATE_REPORT_WHATSAPP_PHONES", ["918882127171"]),
        ):
            scheduler_service._schedule_gate_headcount_reports(FakeScheduler())

        self.assertIn("gate_event_id_final_report", jobs)
        self.assertNotIn("gate_event_id_two_hour_report", jobs)

    def test_interim_reports_can_be_switched_back_on(self):
        jobs: list[str] = []

        class FakeScheduler:
            def add_job(self, func, **kwargs):
                jobs.append(kwargs.get("id", ""))

        with (
            patch.object(gate, "GATE_INTERIM_REPORTS_ENABLED", True),
            patch.object(gate, "GATE_REPORT_WHATSAPP_PHONES", ["918882127171"]),
        ):
            scheduler_service._schedule_gate_headcount_reports(FakeScheduler())

        self.assertIn("gate_event_id_two_hour_report", jobs)

    def test_no_report_jobs_without_recipients(self):
        jobs: list[str] = []

        class FakeScheduler:
            def add_job(self, func, **kwargs):
                jobs.append(kwargs.get("id", ""))

        with patch.object(gate, "GATE_REPORT_WHATSAPP_PHONES", []):
            scheduler_service._schedule_gate_headcount_reports(FakeScheduler())

        self.assertEqual(jobs, [])


if __name__ == "__main__":
    unittest.main()
