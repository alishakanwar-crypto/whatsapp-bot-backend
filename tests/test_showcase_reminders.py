import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import database
from app.routes import webhook
from app.services import showcase_reminder_service as reminders


class ShowcaseReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE showcase_reminder_deliveries ("
                "event_date TEXT NOT NULL, recipient TEXT NOT NULL, "
                "status TEXT NOT NULL, claimed_at TEXT NOT NULL, "
                "accepted_at TEXT NOT NULL DEFAULT '', "
                "status_updated_at TEXT NOT NULL DEFAULT '', "
                "wa_message_id TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY (event_date, recipient))"
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_plan_preserves_all_46_final_source_rows(self):
        self.assertEqual(len(reminders.SHOWCASES), 46)
        self.assertEqual(len({item.event_date for item in reminders.SHOWCASES}), 20)
        self.assertTrue(
            any(
                item.grade_class == "Nursery"
                and item.event_date == date(2026, 9, 11)
                for item in reminders.SHOWCASES
            )
        )

    def test_selects_exactly_three_day_lead_time_and_groups_same_date(self):
        due = reminders.due_showcases(date(2026, 7, 27))

        self.assertEqual(set(due), {date(2026, 7, 30)})
        self.assertEqual(len(due[date(2026, 7, 30)]), 2)

    def test_formats_all_same_day_showcases_in_one_message(self):
        due = reminders.due_showcases(date(2026, 7, 28))

        details = reminders.format_showcase_details(due[date(2026, 7, 31)])

        self.assertIn("Grade I — Friendship Day: Harmony of Hearts", details)
        self.assertIn("Grade II — Theme of the Month: We Are a Family Song", details)
        self.assertIn("Grade VIII — Poetry: Kavita Prastuti", details)

    async def test_sends_one_template_and_deduplicates_event_date(self):
        send = AsyncMock(return_value=True)
        now = datetime(2026, 7, 20, 9, 0, tzinfo=reminders.IST)

        with (
            patch.object(reminders, "DB_PATH", str(self.db_path)),
            patch.object(reminders, "SHOWCASE_REMINDER_PHONES", ("919999995224",)),
            patch.object(
                reminders.whatsapp_service, "send_cloud_template_message", send,
            ),
            patch.object(
                reminders.whatsapp_service,
                "last_cloud_template_message_id",
                "wamid-showcase-test",
            ),
        ):
            first = await reminders.send_showcase_reminders(now)
            second = await reminders.send_showcase_reminders(now)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        send.assert_awaited_once()
        call = send.await_args.kwargs
        self.assertEqual(call["to"], "919999995224")
        self.assertEqual(call["template_name"], "ppis_musical_showcase_reminder")
        self.assertEqual(call["body_params"][0], "23 July 2026")
        self.assertIn("Grade V", call["body_params"][1])
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status, wa_message_id FROM showcase_reminder_deliveries"
            ).fetchone()
        self.assertEqual(row, ("accepted", "wamid-showcase-test"))

    async def test_sends_only_missing_recipient_for_claimed_event_date(self):
        first_recipient = "919999995224"
        second_recipient = "919999998106"
        now = datetime(2026, 7, 20, 9, 0, tzinfo=reminders.IST)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO showcase_reminder_deliveries "
                "(event_date, recipient, status, claimed_at) "
                "VALUES ('2026-07-23', ?, 'delivered', "
                "'21-07-2026 09:00:00 IST')",
                (first_recipient,),
            )
        send = AsyncMock(return_value=True)

        with (
            patch.object(reminders, "DB_PATH", str(self.db_path)),
            patch.object(
                reminders,
                "SHOWCASE_REMINDER_PHONES",
                (first_recipient, second_recipient),
            ),
            patch.object(
                reminders.whatsapp_service, "send_cloud_template_message", send,
            ),
            patch.object(
                reminders.whatsapp_service,
                "last_cloud_template_message_id",
                "wamid-second-recipient",
            ),
        ):
            sent = await reminders.send_showcase_reminders(now)

        self.assertEqual(sent, 1)
        self.assertEqual(send.await_args.kwargs["to"], second_recipient)
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT recipient, status FROM showcase_reminder_deliveries "
                "ORDER BY recipient"
            ).fetchall()
        self.assertEqual(
            rows,
            [(first_recipient, "delivered"), (second_recipient, "accepted")],
        )

    async def test_migrates_existing_delivery_to_first_recipient(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as db:
            db.execute(
                "CREATE TABLE showcase_reminder_deliveries ("
                "event_date TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "claimed_at TEXT NOT NULL, accepted_at TEXT NOT NULL DEFAULT '', "
                "status_updated_at TEXT NOT NULL DEFAULT '', "
                "wa_message_id TEXT NOT NULL DEFAULT '')"
            )
            db.execute(
                "INSERT INTO showcase_reminder_deliveries "
                "(event_date, status, claimed_at) "
                "VALUES ('2026-07-23', 'delivered', "
                "'21-07-2026 09:00:00 IST')"
            )

        with (
            patch.object(database, "DB_PATH", str(legacy_path)),
            patch.dict(
                os.environ,
                {"SHOWCASE_REMINDER_PHONE": "919999995224"},
            ),
        ):
            await database.init_db()

        with sqlite3.connect(legacy_path) as db:
            columns = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(showcase_reminder_deliveries)"
                ).fetchall()
            }
            row = db.execute(
                "SELECT event_date, recipient, status "
                "FROM showcase_reminder_deliveries"
            ).fetchone()
        self.assertIn("recipient", columns)
        self.assertEqual(row, ("2026-07-23", "919999995224", "delivered"))

    async def test_failed_send_is_released_for_retry(self):
        send = AsyncMock(side_effect=[False, True])
        now = datetime(2026, 7, 20, 9, 0, tzinfo=reminders.IST)

        with (
            patch.object(reminders, "DB_PATH", str(self.db_path)),
            patch.object(reminders, "SHOWCASE_REMINDER_PHONES", ("919999995224",)),
            patch.object(
                reminders.whatsapp_service, "send_cloud_template_message", send,
            ),
            patch.object(
                reminders.whatsapp_service,
                "last_cloud_template_message_id",
                "wamid-showcase-test",
            ),
        ):
            first = await reminders.send_showcase_reminders(now)
            second = await reminders.send_showcase_reminders(now)

        self.assertEqual(first, 0)
        self.assertEqual(second, 1)
        self.assertEqual(send.await_count, 2)

    async def test_webhook_forwards_meta_delivery_status(self):
        record = AsyncMock(return_value=True)
        body = {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "value": {
                        "statuses": [{
                            "id": "wamid-showcase-test",
                            "status": "read",
                            "timestamp": "1784518260",
                        }]
                    }
                }]
            }],
        }

        with patch.object(webhook, "record_showcase_delivery_status", record):
            await webhook._record_showcase_statuses(body)

        record.assert_awaited_once()
        args = record.await_args.args
        self.assertEqual(args[:2], ("wamid-showcase-test", "read"))
        self.assertEqual(args[2].tzinfo, reminders.IST)

    async def test_records_meta_delivery_status_by_message_id(self):
        now = datetime(2026, 7, 20, 9, 1, tzinfo=reminders.IST)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO showcase_reminder_deliveries "
                "(event_date, recipient, status, claimed_at, wa_message_id) "
                "VALUES ('2026-07-23', '919999995224', 'accepted', "
                "'21-07-2026 09:00:00 IST', ?)",
                ("wamid-showcase-test",),
            )

        with patch("app.database.DB_PATH", str(self.db_path)):
            updated = await reminders.record_showcase_delivery_status(
                "wamid-showcase-test", "delivered", now,
            )

        self.assertTrue(updated)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status, status_updated_at "
                "FROM showcase_reminder_deliveries"
            ).fetchone()
        self.assertEqual(row, ("delivered", "20-07-2026 09:01:00 IST"))


if __name__ == "__main__":
    unittest.main()
