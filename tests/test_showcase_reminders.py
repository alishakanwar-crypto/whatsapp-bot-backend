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
            db.execute(
                "CREATE TABLE showcase_slide_deliveries ("
                "slide_key TEXT NOT NULL, recipient TEXT NOT NULL, "
                "status TEXT NOT NULL, claimed_at TEXT NOT NULL, "
                "status_updated_at TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY (slide_key, recipient))"
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_plan_holds_only_the_final_november_december_sheet(self):
        self.assertEqual(len(reminders.SHOWCASES), 62)
        self.assertEqual(
            sorted(
                item.event_date
                for item in reminders.SHOWCASES
                if item.grade_class == "Grade III"
            ),
            [
                date(2026, 11, 5),
                date(2026, 11, 13),
                date(2026, 11, 30),
                date(2026, 12, 3),
                date(2026, 12, 4),
                date(2026, 12, 11),
                date(2026, 12, 19),
                date(2026, 12, 23),
            ],
        )
        # Alisha asked for the earlier sheets to stop being synced, so nothing
        # before this plan's first date can raise a reminder any more.
        self.assertEqual(
            min(item.event_date for item in reminders.SHOWCASES),
            date(2026, 11, 5),
        )
        self.assertTrue(
            any(
                item.grade_class == "Grade V"
                and item.event_date == date(2026, 12, 17)
                and "Doha" in item.presentation_item
                for item in reminders.SHOWCASES
            )
        )

    def test_guru_nanak_reminder_names_both_classes_presenting(self):
        due = reminders.due_showcases(date(2026, 11, 21))

        self.assertIn(date(2026, 11, 23), due)
        self.assertEqual(
            [item.grade_class for item in due[date(2026, 11, 23)]],
            ["Nursery", "Prep", "Grade II"],
        )

    def test_diwali_reminder_names_every_class_presenting_that_day(self):
        due = reminders.due_showcases(date(2026, 11, 3))

        self.assertEqual(set(due), {date(2026, 11, 5)})
        self.assertEqual(
            [item.grade_class for item in due[date(2026, 11, 5)]],
            ["Grade I", "Grade II", "Grade III"],
        )

    def test_christmas_carol_reminder_goes_out_two_days_before(self):
        due = reminders.due_showcases(date(2026, 12, 8))

        self.assertIn(date(2026, 12, 10), due)
        self.assertIn(
            "Nursery — Christmas: Christmas Carols",
            reminders.format_showcase_details(due[date(2026, 12, 10)]),
        )

    def test_reminds_up_to_the_lead_time_so_a_revised_sheet_still_lands(self):
        due = reminders.due_showcases(date(2026, 11, 14))

        self.assertEqual(len(due[date(2026, 11, 16)]), 2)
        # Nothing beyond the two-day window, and nothing in the past.
        self.assertEqual(max(due), date(2026, 11, 16))
        self.assertEqual(min(due), date(2026, 11, 16))
        # An event only one day away still gets its reminder once.
        self.assertIn(date(2026, 12, 9), reminders.due_showcases(date(2026, 12, 8)))

    def test_formats_all_same_day_showcases_in_one_message(self):
        due = reminders.due_showcases(date(2026, 12, 21))

        details = reminders.format_showcase_details(due[date(2026, 12, 23)])

        self.assertIn("Grade III — Quiz: GK Quiz", details)
        self.assertIn("Grade IV — Christmas - French: Musical Showcase", details)
        self.assertIn("Grade VIII — Quiz: GK Quiz", details)
        self.assertNotIn("\n", details)
        self.assertNotIn("\t", details)

    def test_slides_cover_every_non_musical_showcase_with_a_prepared_slide(self):
        for showcase in reminders.SHOWCASES:
            if "musical showcase" in (
                showcase.theme_occasion + " " + showcase.presentation_item
            ).lower():
                self.assertEqual(reminders.slide_for(showcase), "")

        slides_dir = (
            Path(reminders.__file__).resolve().parents[1]
            / "static"
            / "showcase_slides"
        )
        for name in set(reminders.SHOWCASE_SLIDES.values()):
            self.assertTrue((slides_dir / f"{name}_thumb.jpg").is_file(), name)

    def test_slide_goes_out_two_days_before_the_event(self):
        due = reminders.due_showcase_slides(date(2026, 12, 21))

        self.assertEqual(
            {item.event_date for item in due},
            {date(2026, 12, 21), date(2026, 12, 22), date(2026, 12, 23)},
        )
        self.assertEqual(reminders.due_showcase_slides(date(2026, 12, 25)), [])
        self.assertEqual(
            [
                (item.grade_class, item.event_date)
                for item in reminders.due_showcase_slides(date(2026, 12, 20))
            ],
            [
                ("Nursery", date(2026, 12, 22)),
                ("Grade IV", date(2026, 12, 21)),
                ("Grade V", date(2026, 12, 22)),
                ("Grade VII", date(2026, 12, 21)),
            ],
        )

    async def test_sends_one_slide_message_per_showcase_and_deduplicates(self):
        send = AsyncMock(return_value=True)
        now = datetime(2026, 12, 20, 9, 5, tzinfo=reminders.IST)

        with (
            patch.object(reminders, "DB_PATH", str(self.db_path)),
            patch.object(reminders, "SHOWCASE_REMINDER_PHONES", ("919999995224",)),
            patch.object(
                reminders.whatsapp_service, "send_cloud_template_message", send,
            ),
        ):
            first = await reminders.send_showcase_slides(now)
            second = await reminders.send_showcase_slides(now)

        self.assertEqual((first, second), (4, 0))
        calls = [call.kwargs for call in send.await_args_list[:first]]
        self.assertEqual(
            {call["template_name"] for call in calls},
            {"ppis_showcase_slide_preview"},
        )
        doha = next(
            call for call in calls
            if call["body_params"][1] == "Doha Vachan"
        )
        self.assertEqual(
            doha["header_image_url"],
            "https://ppis-whatsapp-bot.fly.dev/static/showcase_slides/"
            "slide_r48_thumb.jpg",
        )
        self.assertEqual(
            doha["body_params"],
            ["Grade V", "Doha Vachan", "22 December 2026"],
        )

    async def test_retries_a_slide_whose_send_failed_the_day_before(self):
        send = AsyncMock(return_value=False)

        with (
            patch.object(reminders, "DB_PATH", str(self.db_path)),
            patch.object(reminders, "SHOWCASE_REMINDER_PHONES", ("919999995224",)),
            patch.object(
                reminders.whatsapp_service, "send_cloud_template_message", send,
            ),
        ):
            # Template not approved yet on the first attempt.
            failed = await reminders.send_showcase_slides(
                datetime(2026, 12, 15, 9, 5, tzinfo=reminders.IST)
            )
            send.side_effect = None
            send.return_value = True
            # Still due the next day, and it goes out.
            retried = await reminders.send_showcase_slides(
                datetime(2026, 12, 16, 9, 5, tzinfo=reminders.IST)
            )

        self.assertEqual(failed, 0)
        self.assertGreater(retried, 0)
        self.assertIn(
            "https://ppis-whatsapp-bot.fly.dev/static/showcase_slides/"
            "slide_r52_thumb.jpg",
            [call.kwargs["header_image_url"] for call in send.await_args_list],
        )

    async def test_reclaims_a_slide_left_mid_send_by_a_restart(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO showcase_slide_deliveries "
                "(slide_key, recipient, status, claimed_at) VALUES "
                "('2026-12-22|Grade V|Speaking Activity Hindi - Round 2', "
                "'919999995224', 'generated', 'x')"
            )
        send = AsyncMock(return_value=True)

        with (
            patch.object(reminders, "DB_PATH", str(self.db_path)),
            patch.object(reminders, "SHOWCASE_REMINDER_PHONES", ("919999995224",)),
            patch.object(
                reminders.whatsapp_service, "send_cloud_template_message", send,
            ),
        ):
            sent = await reminders.send_showcase_slides(
                datetime(2026, 12, 20, 9, 5, tzinfo=reminders.IST)
            )

        self.assertEqual(sent, 4)

    async def test_sends_one_template_and_deduplicates_event_date(self):
        send = AsyncMock(return_value=True)
        now = datetime(2026, 11, 3, 9, 0, tzinfo=reminders.IST)

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
        self.assertEqual(call["body_params"][0], "05 November 2026")
        self.assertIn("Grade I", call["body_params"][1])
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status, wa_message_id FROM showcase_reminder_deliveries"
            ).fetchone()
        self.assertEqual(row, ("accepted", "wamid-showcase-test"))

    async def test_sends_only_missing_recipient_for_claimed_event_date(self):
        first_recipient = "919999995224"
        second_recipient = "919999998106"
        now = datetime(2026, 11, 3, 9, 0, tzinfo=reminders.IST)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO showcase_reminder_deliveries "
                "(event_date, recipient, status, claimed_at) "
                "VALUES ('2026-11-05', ?, 'delivered', "
                "'03-11-2026 09:00:00 IST')",
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
        now = datetime(2026, 11, 3, 9, 0, tzinfo=reminders.IST)

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
