import contextlib
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import internal_reminder_service as reminders


class InternalReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE internal_reminder_deliveries ("
                "reminder_key TEXT NOT NULL, reminder_date TEXT NOT NULL, "
                "recipient TEXT NOT NULL, status TEXT NOT NULL, "
                "claimed_at TEXT NOT NULL, "
                "status_updated_at TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY (reminder_key, reminder_date, recipient))"
            )
        self.workshop = reminders.Reminder(
            key="gk_workshop_teachers",
            on=date(2026, 10, 1),
            subject="subject",
            detail="detail",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @contextlib.contextmanager
    def _patched(self, send, phones=("919999995224",)):
        patches = (
            patch.object(reminders, "DB_PATH", str(self.db_path)),
            patch.object(reminders, "INTERNAL_REMINDER_PHONES", phones),
            patch.object(
                reminders.whatsapp_service,
                "send_cloud_template_message",
                send,
            ),
        )
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            yield

    def test_the_gk_workshop_falls_on_the_first_of_october(self):
        due = reminders.due_reminders(date(2026, 10, 1))
        self.assertEqual([item.key for item in due], ["gk_workshop_teachers"])
        self.assertEqual(reminders.due_reminders(date(2026, 9, 30)), ())
        self.assertEqual(reminders.due_reminders(date(2026, 10, 2)), ())

    def test_the_wording_carries_place_time_and_what_to_bring(self):
        workshop = reminders.due_reminders(date(2026, 10, 1))[0]
        words = f"{workshop.subject} {workshop.detail}".lower()
        for expected in ("basement", "2:00 pm", "notepad", "pen", "phone"):
            self.assertIn(expected, words)

    async def test_the_day_sends_once_and_no_second_time(self):
        send = AsyncMock(return_value=True)
        now = datetime(2026, 10, 1, 9, 0, tzinfo=reminders.IST)

        with self._patched(send):
            first = await reminders.send_internal_reminders(now)
            second = await reminders.send_internal_reminders(now)

        self.assertEqual((first, second), (1, 0))
        send.assert_awaited_once()
        call = send.await_args.kwargs
        self.assertEqual(call["to"], "919999995224")
        self.assertEqual(call["template_name"], "ppis_internal_reminder_v2")
        workshop = reminders.due_reminders(date(2026, 10, 1))[0]
        self.assertEqual(
            call["body_params"], [workshop.subject, workshop.detail],
        )

    async def test_a_restart_before_nine_waits_for_nine(self):
        send = AsyncMock(return_value=True)
        early = datetime(2026, 10, 1, 7, 31, tzinfo=reminders.IST)

        with self._patched(send):
            waited = await reminders.send_internal_reminders(early)
            at_nine = await reminders.send_internal_reminders(
                datetime(2026, 10, 1, 9, 0, tzinfo=reminders.IST),
            )

        self.assertEqual((waited, at_nine), (0, 1))
        send.assert_awaited_once()

    async def test_no_other_day_sends_anything(self):
        send = AsyncMock(return_value=True)
        now = datetime(2026, 10, 2, 9, 0, tzinfo=reminders.IST)

        with self._patched(send):
            sent = await reminders.send_internal_reminders(now)

        self.assertEqual(sent, 0)
        send.assert_not_awaited()

    async def test_a_send_that_died_mid_claim_is_tried_again(self):
        now = datetime(2026, 10, 1, 9, 0, tzinfo=reminders.IST)
        stale = now - reminders.CLAIM_LEASE - timedelta(minutes=1)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO internal_reminder_deliveries "
                "(reminder_key, reminder_date, recipient, status, claimed_at) "
                "VALUES ('gk_workshop_teachers', '2026-10-01', "
                "'919999995224', 'generated', ?)",
                (stale.isoformat(),),
            )
        send = AsyncMock(return_value=True)

        with self._patched(send):
            sent = await reminders.send_internal_reminders(now)

        self.assertEqual(sent, 1)
        send.assert_awaited_once()

    async def test_a_claim_still_in_flight_is_left_alone(self):
        now = datetime(2026, 10, 1, 9, 0, tzinfo=reminders.IST)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO internal_reminder_deliveries "
                "(reminder_key, reminder_date, recipient, status, claimed_at) "
                "VALUES ('gk_workshop_teachers', '2026-10-01', "
                "'919999995224', 'generated', ?)",
                ((now - timedelta(minutes=1)).isoformat(),),
            )
        send = AsyncMock(return_value=True)

        with self._patched(send):
            sent = await reminders.send_internal_reminders(now)

        self.assertEqual(sent, 0)
        send.assert_not_awaited()

    def test_an_overtaken_sender_cannot_overwrite_the_newer_result(self):
        first_claim = datetime(2026, 10, 1, 9, 0, tzinfo=reminders.IST)
        takeover = first_claim + reminders.CLAIM_LEASE + timedelta(minutes=1)
        with patch.object(reminders, "DB_PATH", str(self.db_path)):
            stalled = reminders._claim(
                self.workshop, "919999995224", first_claim,
            )
            live = reminders._claim(self.workshop, "919999995224", takeover)
            reminders._finish(
                self.workshop, "919999995224", True, takeover, live,
            )
            reminders._finish(
                self.workshop, "919999995224", False, takeover, stalled,
            )

        self.assertIsNotNone(stalled)
        self.assertIsNotNone(live)
        with sqlite3.connect(self.db_path) as db:
            status = db.execute(
                "SELECT status FROM internal_reminder_deliveries"
            ).fetchone()[0]
        self.assertEqual(status, "accepted")

    def test_two_reminders_on_one_day_are_claimed_apart(self):
        """Reminders share a day without sharing a claim, so one being sent
        cannot mark another as delivered."""
        now = datetime(2026, 10, 1, 9, 0, tzinfo=reminders.IST)
        other = reminders.Reminder(
            key="another_thing",
            on=date(2026, 10, 1),
            subject="subject",
            detail="detail",
        )
        with patch.object(reminders, "DB_PATH", str(self.db_path)):
            self.assertIsNotNone(
                reminders._claim(self.workshop, "919999995224", now),
            )
            self.assertIsNotNone(reminders._claim(other, "919999995224", now))

    async def test_a_failed_send_is_tried_again(self):
        send = AsyncMock(side_effect=[False, True])
        now = datetime(2026, 10, 1, 9, 0, tzinfo=reminders.IST)

        with self._patched(send):
            first = await reminders.send_internal_reminders(now)
            second = await reminders.send_internal_reminders(now)

        self.assertEqual((first, second), (0, 1))
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT reminder_date, status "
                "FROM internal_reminder_deliveries"
            ).fetchone()
        self.assertEqual(row, ("2026-10-01", "accepted"))


if __name__ == "__main__":
    unittest.main()
