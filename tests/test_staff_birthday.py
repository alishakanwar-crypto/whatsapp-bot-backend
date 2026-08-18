import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import database
from app.services import staff_birthday_service as birthdays

IST = ZoneInfo("Asia/Kolkata")


class StaffBirthdayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "birthdays.db"
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE staff_birthday_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    staff_name TEXT NOT NULL,
                    wish_date TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'claimed',
                    wa_message_id TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT NOT NULL DEFAULT '',
                    status_updated_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(staff_name, wish_date)
                )
                """
            )

        self.poster_dir = root / "posters"
        self.poster_dir.mkdir()
        (self.poster_dir / "harnoor_kaur.jpg").write_bytes(b"jpeg")
        (self.poster_dir / "shreya_sikka.jpg").write_bytes(b"jpeg")

        self.data_path = root / "staff_birthdays.json"
        self.data_path.write_text(
            json.dumps(
                [
                    {
                        "name": "Harnoor Kaur",
                        "display_name": "Ms. Harnoor Kaur",
                        "dob": "02-20",
                        "phone": "9289234659",
                        "poster": "harnoor_kaur.jpg",
                        "needs_review": "",
                    },
                    {
                        "name": "Shreya Sikka",
                        "display_name": "Ms. Shreya Sikka",
                        "dob": "02-20",
                        "phone": "919289236072",
                        "poster": "shreya_sikka.jpg",
                        "needs_review": "number is shared with Ritika Dhamija",
                    },
                    {
                        "name": "Mridul Pilani",
                        "display_name": "Mridul Pilani",
                        "dob": "02-20",
                        "phone": "919289236055",
                        "poster": "",
                        "needs_review": "no birthday poster available",
                    },
                    {
                        "name": "Gone Missing",
                        "display_name": "Ms. Gone Missing",
                        "dob": "02-21",
                        "phone": "919000000001",
                        "poster": "absent.jpg",
                        "needs_review": "",
                    },
                ]
            ),
            encoding="utf-8",
        )
        self.now = datetime(2026, 2, 20, 9, 0, tzinfo=IST)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def asyncSetUp(self):
        self.db_patch = patch.object(database, "DB_PATH", str(self.db_path))
        self.db_patch.start()
        self.env_patch = patch.dict(
            "os.environ", {"STAFF_BIRTHDAY_FILE": str(self.data_path)}
        )
        self.env_patch.start()
        self.poster_patch = patch.object(birthdays, "_POSTER_DIR", self.poster_dir)
        self.poster_patch.start()
        self.enabled_patch = patch.object(birthdays, "STAFF_BIRTHDAY_ENABLED", True)
        self.enabled_patch.start()
        self.base_patch = patch.object(
            birthdays, "POSTER_BASE_URL", "https://example.test/birthday_posters"
        )
        self.base_patch.start()

    async def asyncTearDown(self):
        for patcher in (
            self.base_patch,
            self.enabled_patch,
            self.poster_patch,
            self.env_patch,
            self.db_patch,
        ):
            patcher.stop()

    def test_phone_is_normalised_to_country_code(self):
        staff = {s["name"]: s for s in birthdays.load_staff()}
        self.assertEqual(staff["Harnoor Kaur"]["phone"], "919289234659")

    def test_birthdays_on_matches_month_and_day_only(self):
        names = [s["name"] for s in birthdays.birthdays_on(date(2031, 2, 20))]
        self.assertEqual(names, ["Harnoor Kaur", "Shreya Sikka", "Mridul Pilani"])

    def test_missing_poster_file_blocks_the_send(self):
        staff = {s["name"]: s for s in birthdays.load_staff()}
        self.assertIn("absent.jpg", birthdays.blocking_reason(staff["Gone Missing"]))

    async def test_wish_is_sent_once_with_poster_header(self):
        send = AsyncMock(return_value=True)
        with patch.object(
            birthdays.whatsapp_service, "send_cloud_template_message", send
        ), patch.object(
            birthdays.whatsapp_service, "send_cloud_text", AsyncMock(return_value=True)
        ), patch.object(
            birthdays.whatsapp_service, "last_cloud_template_message_id", "wamid.1"
        ):
            first = await birthdays.send_birthday_wishes(now=self.now)
            second = await birthdays.send_birthday_wishes(now=self.now)

        self.assertEqual([e["name"] for e in first["sent"]], ["Harnoor Kaur"])
        self.assertEqual([e["name"] for e in second["already_sent"]], ["Harnoor Kaur"])
        self.assertEqual(send.await_count, 1)
        kwargs = send.await_args.kwargs
        self.assertEqual(kwargs["to"], "919289234659")
        self.assertEqual(kwargs["body_params"], ["Ms. Harnoor Kaur"])
        self.assertEqual(
            kwargs["header_image_url"],
            "https://example.test/birthday_posters/harnoor_kaur.jpg",
        )

        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT staff_name, status, wa_message_id FROM staff_birthday_log"
            ).fetchall()
        self.assertEqual(rows, [("Harnoor Kaur", "sent", "wamid.1")])

    async def test_staff_needing_review_are_skipped_and_admin_is_told(self):
        alert = AsyncMock(return_value=True)
        with patch.object(
            birthdays.whatsapp_service,
            "send_cloud_template_message",
            AsyncMock(return_value=True),
        ), patch.object(birthdays.whatsapp_service, "send_cloud_text", alert):
            summary = await birthdays.send_birthday_wishes(now=self.now)

        self.assertEqual(
            sorted(e["name"] for e in summary["skipped"]),
            ["Mridul Pilani", "Shreya Sikka"],
        )
        alert.assert_awaited_once()
        body = alert.await_args.args[1]
        self.assertIn("Shreya Sikka", body)
        self.assertIn("no birthday poster available", body)

    async def test_failed_send_is_retried_on_the_next_run(self):
        send = AsyncMock(side_effect=[False, True])
        with patch.object(
            birthdays.whatsapp_service, "send_cloud_template_message", send
        ), patch.object(
            birthdays.whatsapp_service, "send_cloud_text", AsyncMock(return_value=True)
        ):
            failed = await birthdays.send_birthday_wishes(now=self.now)
            retried = await birthdays.send_birthday_wishes(now=self.now)

        self.assertEqual([e["name"] for e in failed["failed"]], ["Harnoor Kaur"])
        self.assertEqual([e["name"] for e in retried["sent"]], ["Harnoor Kaur"])

    async def test_dry_run_sends_nothing(self):
        send = AsyncMock(return_value=True)
        with patch.object(
            birthdays.whatsapp_service, "send_cloud_template_message", send
        ):
            summary = await birthdays.send_birthday_wishes(now=self.now, dry_run=True)

        send.assert_not_awaited()
        self.assertEqual([e["name"] for e in summary["sent"]], ["Harnoor Kaur"])
        with sqlite3.connect(self.db_path) as db:
            count = db.execute("SELECT COUNT(*) FROM staff_birthday_log").fetchone()[0]
        self.assertEqual(count, 0)

    def test_upcoming_lists_birthdays_in_calendar_order(self):
        entries = birthdays.upcoming(days=2, now=self.now)
        self.assertEqual(
            [(e["name"], e["on"]) for e in entries],
            [
                ("Harnoor Kaur", "20-02-2026"),
                ("Shreya Sikka", "20-02-2026"),
                ("Mridul Pilani", "20-02-2026"),
                ("Gone Missing", "21-02-2026"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
