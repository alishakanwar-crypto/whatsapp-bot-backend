import contextlib
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import gk_olympiad_reminder_service as reminders


class GkOlympiadReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE gk_olympiad_reminder_deliveries ("
                "reminder_date TEXT NOT NULL, recipient TEXT NOT NULL, "
                "status TEXT NOT NULL, claimed_at TEXT NOT NULL, "
                "status_updated_at TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY (reminder_date, recipient))"
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    @contextlib.contextmanager
    def _patched(self, send, phones=("919999995224",)):
        patches = (
            patch.object(reminders, "DB_PATH", str(self.db_path)),
            patch.object(reminders, "GK_OLYMPIAD_REMINDER_PHONES", phones),
            patch.object(
                reminders.whatsapp_service, "send_cloud_template_message", send,
            ),
        )
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            yield

    def test_the_three_asked_for_days_are_the_reminder_days(self):
        self.assertEqual(
            reminders.GK_OLYMPIAD_REMINDER_DATES,
            (date(2026, 9, 25), date(2026, 9, 30), date(2026, 10, 3)),
        )
        self.assertTrue(reminders.is_reminder_due(date(2026, 9, 30)))
        self.assertFalse(reminders.is_reminder_due(date(2026, 9, 29)))
        self.assertFalse(reminders.is_reminder_due(date(2026, 10, 6)))

    def test_the_announcement_wording_is_one_line_meta_accepts(self):
        text = reminders.fallback_announcement(date(2026, 9, 25))

        self.assertNotIn("\n", text)
        self.assertNotIn("\t", text)
        self.assertNotIn("    ", text)
        self.assertIn("11 day(s) away", text)
        self.assertIn(reminders.GK_OLYMPIAD_STUDY_LINK, text)

    async def test_a_reminder_day_sends_once_and_no_second_time(self):
        send = AsyncMock(return_value=True)
        now = datetime(2026, 9, 25, 9, 0, tzinfo=reminders.IST)

        with self._patched(send):
            first = await reminders.send_gk_olympiad_reminders(now)
            second = await reminders.send_gk_olympiad_reminders(now)

        self.assertEqual((first, second), (1, 0))
        send.assert_awaited_once()
        call = send.await_args.kwargs
        self.assertEqual(call["to"], "919999995224")
        self.assertEqual(call["template_name"], "ppis_gk_olympiad_reminder")
        self.assertEqual(call["body_params"][0], "Monday, 6th October 2026")
        self.assertEqual(call["body_params"][2], reminders.GK_OLYMPIAD_STUDY_LINK)

    async def test_no_other_day_sends_anything(self):
        send = AsyncMock(return_value=True)
        now = datetime(2026, 9, 26, 9, 0, tzinfo=reminders.IST)

        with self._patched(send):
            sent = await reminders.send_gk_olympiad_reminders(now)

        self.assertEqual(sent, 0)
        send.assert_not_awaited()

    async def test_the_announcement_carries_it_until_meta_approves(self):
        """The reminder must not wait on a wording that is still pending."""
        send = AsyncMock(side_effect=[False, True])
        now = datetime(2026, 10, 3, 9, 0, tzinfo=reminders.IST)

        with self._patched(send):
            sent = await reminders.send_gk_olympiad_reminders(now)

        self.assertEqual(sent, 1)
        self.assertEqual(send.await_count, 2)
        fallback = send.await_args.kwargs
        self.assertEqual(fallback["template_name"], "ppis_school_announcement")
        self.assertIn("GK Olympiad", fallback["body_params"][0])

    async def test_a_failed_day_is_tried_again(self):
        send = AsyncMock(side_effect=[False, False, True])
        now = datetime(2026, 9, 30, 9, 0, tzinfo=reminders.IST)

        with self._patched(send):
            first = await reminders.send_gk_olympiad_reminders(now)
            second = await reminders.send_gk_olympiad_reminders(now)

        self.assertEqual((first, second), (0, 1))
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT reminder_date, status FROM gk_olympiad_reminder_deliveries"
            ).fetchone()
        self.assertEqual(row, ("2026-09-30", "accepted"))


if __name__ == "__main__":
    unittest.main()
