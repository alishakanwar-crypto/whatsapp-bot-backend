import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import database
from app.services import daily_work_report_service as report


class DailyWorkReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "daily-work-report.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_disabled_does_not_send(self):
        sender = AsyncMock(return_value=True)
        with (
            patch.object(report, "DAILY_WORK_REPORT_ENABLED", False),
            patch.object(report.whatsapp_service, "send_cloud_template_message", sender),
        ):
            sent = await report.send_daily_work_report(
                datetime(2026, 8, 3, 15, 0, tzinfo=report.IST)
            )
        self.assertFalse(sent)
        sender.assert_not_awaited()

    async def test_sunday_does_not_send(self):
        sender = AsyncMock(return_value=True)
        with (
            patch.object(report, "DAILY_WORK_REPORT_ENABLED", True),
            patch.object(report.whatsapp_service, "send_cloud_template_message", sender),
        ):
            sent = await report.send_daily_work_report(
                datetime(2026, 8, 2, 15, 0, tzinfo=report.IST)
            )
        self.assertFalse(sent)
        sender.assert_not_awaited()

    async def test_sends_ist_date_line_and_is_idempotent(self):
        sender = AsyncMock(return_value=True)
        now = datetime(2026, 8, 4, 9, 30, tzinfo=ZoneInfo("UTC"))
        with (
            patch.object(database, "DB_PATH", str(self.db_path)),
            patch.object(report, "DAILY_WORK_REPORT_ENABLED", True),
            patch.object(
                report.whatsapp_service, "send_cloud_template_message", sender,
            ),
        ):
            first = await report.send_daily_work_report(now)
            second = await report.send_daily_work_report(now)

        self.assertTrue(first)
        self.assertFalse(second)
        sender.assert_awaited_once_with(
            to="918076455224",
            template_name="ppis_daily_work_report",
            language_code="en",
            body_params=["Tuesday, 04-08-2026"],
        )

    async def test_next_ist_date_can_send_again(self):
        sender = AsyncMock(return_value=True)
        with (
            patch.object(database, "DB_PATH", str(self.db_path)),
            patch.object(report, "DAILY_WORK_REPORT_ENABLED", True),
            patch.object(
                report.whatsapp_service, "send_cloud_template_message", sender,
            ),
        ):
            first = await report.send_daily_work_report(
                datetime(2026, 8, 3, 15, 0, tzinfo=report.IST)
            )
            second = await report.send_daily_work_report(
                datetime(2026, 8, 4, 15, 0, tzinfo=report.IST)
            )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(sender.await_count, 2)
