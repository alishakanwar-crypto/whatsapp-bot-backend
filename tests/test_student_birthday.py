import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import database
from app.services import student_birthday_service as birthdays

IST = ZoneInfo("Asia/Kolkata")


class DobParsingTests(unittest.TestCase):
    def test_reads_every_shape_the_pi_sheet_holds(self):
        cases = {
            "24.02.2018": "2018-02-24",
            "3.10.2017": "2017-10-03",
            "1.08.2017": "2017-08-01",
            "2017-04-01": "2017-04-01",
            "11/09/2009": "2009-09-11",
            "18-09-2008": "2008-09-18",
            "29-Dec-15": "2015-12-29",
            "17-Jun-13": "2013-06-17",
            "11/Jul/09": "2009-07-11",
            "4-Sept-12": "2012-09-04",
            " 7.7.2016 ": "2016-07-07",
            "02.04.2016 ( New)": "2016-04-02",
            "08May 2015": "2015-05-08",
        }
        for raw, expected in cases.items():
            self.assertEqual(birthdays.parse_dob(raw), expected, raw)

    def test_month_first_when_day_first_cannot_be_a_date(self):
        self.assertEqual(birthdays.parse_dob("7/25/2016"), "2016-07-25")

    def test_unreadable_dates_are_left_alone(self):
        for raw in (
            "",
            "   ",
            "-",
            "NA",
            "to be given",
            "32.13.2018",
            "2018",
            "12/2018",
            "24.02.1901",
            "29.02.2017",
            "11/Foo/09",
        ):
            self.assertEqual(birthdays.parse_dob(raw), "", raw)


class PhoneAndNameTests(unittest.TestCase):
    def test_numbers_become_dialable_or_nothing(self):
        self.assertEqual(birthdays.normalize_phone("9582281605"), "919582281605")
        self.assertEqual(birthdays.normalize_phone("09582281605"), "919582281605")
        self.assertEqual(birthdays.normalize_phone("+91 95822 81605"), "919582281605")
        self.assertEqual(
            birthdays.normalize_phone("9582281605 / 9560179996"), "919582281605"
        )
        self.assertEqual(birthdays.normalize_phone("12345"), "")
        self.assertEqual(birthdays.normalize_phone(""), "")

    def test_both_parents_once_each(self):
        self.assertEqual(
            birthdays.recipients(
                {"father_phone": "9582281605", "mother_phone": "9560179996"}
            ),
            ["919582281605", "919560179996"],
        )
        self.assertEqual(
            birthdays.recipients(
                {"father_phone": "9582281605", "mother_phone": "919582281605"}
            ),
            ["919582281605"],
        )
        self.assertEqual(
            birthdays.recipients({"father_phone": "", "mother_phone": "bad"}), []
        )

    def test_shouting_names_are_written_as_a_parent_reads_them(self):
        self.assertEqual(birthdays.display_name("NITYA GUPTA"), "Nitya Gupta")
        self.assertEqual(birthdays.display_name("Seerat  Sethi"), "Seerat Sethi")


class StudentBirthdaySendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "birthdays.db"
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE student_birthdays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_name TEXT NOT NULL,
                    grade TEXT NOT NULL,
                    dob TEXT NOT NULL,
                    father_phone TEXT DEFAULT '',
                    mother_phone TEXT DEFAULT '',
                    last_wish_sent TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE student_birthday_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_name TEXT NOT NULL,
                    grade TEXT NOT NULL DEFAULT '',
                    wish_date TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'claimed',
                    wa_message_id TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT NOT NULL DEFAULT '',
                    status_updated_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(student_name, grade, wish_date, phone)
                );
                INSERT INTO student_birthdays
                    (student_name, grade, dob, father_phone, mother_phone)
                VALUES
                    ('NITYA GUPTA', 'Grade 1A', '2018-02-20',
                     '9582281605', '9560179996'),
                    ('SEERAT SETHI', 'Grade 2B', '2017-02-20', '', ''),
                    ('ARJUN RAO', 'Grade 3C', '2016-02-21',
                     '9000000001', '')
                """
            )
        self.now = datetime(2026, 2, 20, 8, 0, tzinfo=IST)

    def tearDown(self):
        self.temp_dir.cleanup()

    async def asyncSetUp(self):
        self.db_patch = patch.object(database, "DB_PATH", str(self.db_path))
        self.db_patch.start()
        self.enabled_patch = patch.object(
            birthdays, "STUDENT_BIRTHDAY_ENABLED", True
        )
        self.enabled_patch.start()
        self.gap_patch = patch.object(birthdays, "SEND_GAP_SECONDS", 0.0)
        self.gap_patch.start()

    async def asyncTearDown(self):
        self.gap_patch.stop()
        self.enabled_patch.stop()
        self.db_patch.stop()

    def _log(self):
        with sqlite3.connect(self.db_path) as db:
            return db.execute(
                "SELECT student_name, phone, status FROM student_birthday_log "
                "ORDER BY phone"
            ).fetchall()

    async def test_only_todays_children_are_matched(self):
        found = await birthdays.birthdays_on(self.now.date())
        self.assertEqual(
            [s["student_name"] for s in found], ["NITYA GUPTA", "SEERAT SETHI"]
        )

    async def test_wish_goes_to_both_parents_with_name_and_grade(self):
        send = AsyncMock(return_value=True)
        with patch.object(
            birthdays.whatsapp_service, "send_cloud_template_message", send
        ), patch.object(
            birthdays.whatsapp_service, "send_cloud_text", AsyncMock(return_value=True)
        ) as admin:
            summary = await birthdays.send_birthday_wishes(now=self.now)

        self.assertEqual(len(summary["sent"]), 2)
        self.assertEqual(
            [call.kwargs["to"] for call in send.await_args_list],
            ["919582281605", "919560179996"],
        )
        self.assertEqual(
            send.await_args_list[0].kwargs["body_params"],
            ["Nitya Gupta", "Grade 1A"],
        )
        self.assertEqual(
            send.await_args_list[0].kwargs["template_name"],
            birthdays.WISH_TEMPLATE,
        )
        self.assertEqual(
            [row[2] for row in self._log()], ["sent", "sent"]
        )
        # The child with no number on file is reported, not wished.
        self.assertEqual(
            [s["name"] for s in summary["no_number"]], ["Seerat Sethi"]
        )
        self.assertIn("Seerat Sethi", admin.await_args.args[1])

    async def test_a_second_run_the_same_day_wishes_nobody_again(self):
        send = AsyncMock(return_value=True)
        with patch.object(
            birthdays.whatsapp_service, "send_cloud_template_message", send
        ), patch.object(
            birthdays.whatsapp_service, "send_cloud_text", AsyncMock(return_value=True)
        ):
            await birthdays.send_birthday_wishes(now=self.now)
            again = await birthdays.send_birthday_wishes(now=self.now)

        self.assertEqual(send.await_count, 2)
        self.assertEqual(len(again["sent"]), 0)
        self.assertEqual(len(again["already_sent"]), 2)

    async def test_a_refused_wish_is_retried_on_the_next_run(self):
        send = AsyncMock(side_effect=[False, False, True, True])
        with patch.object(
            birthdays.whatsapp_service, "send_cloud_template_message", send
        ), patch.object(
            birthdays.whatsapp_service, "send_cloud_text", AsyncMock(return_value=True)
        ):
            first = await birthdays.send_birthday_wishes(now=self.now)
            self.assertEqual(len(first["sent"]), 0)
            self.assertEqual([f["name"] for f in first["failed"]], ["Nitya Gupta"])
            self.assertEqual(self._log(), [])

            second = await birthdays.send_birthday_wishes(now=self.now)

        self.assertEqual(len(second["sent"]), 2)
        self.assertEqual(send.await_count, 4)

    async def test_nothing_is_sent_while_the_feature_is_off(self):
        send = AsyncMock(return_value=True)
        with patch.object(birthdays, "STUDENT_BIRTHDAY_ENABLED", False), patch.object(
            birthdays.whatsapp_service, "send_cloud_template_message", send
        ):
            summary = await birthdays.send_birthday_wishes(now=self.now)
        self.assertTrue(summary["disabled"])
        send.assert_not_awaited()

    async def test_a_dry_run_touches_nobody(self):
        send = AsyncMock(return_value=True)
        with patch.object(
            birthdays.whatsapp_service, "send_cloud_template_message", send
        ):
            summary = await birthdays.send_birthday_wishes(
                now=self.now, dry_run=True
            )
        send.assert_not_awaited()
        self.assertEqual(len(summary["sent"]), 2)
        self.assertEqual(self._log(), [])


class SchedulerRegistrationTests(unittest.TestCase):
    def test_student_wishes_run_at_eight_in_the_morning_ist(self):
        from app.services import scheduler_service

        self.assertIs(scheduler_service.STUDENT_BIRTHDAY_IST, birthdays.IST)
        source = Path(scheduler_service.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "CronTrigger(hour=8, minute=0, timezone=STUDENT_BIRTHDAY_IST)", source
        )
        self.assertIn('id="student_birthday_wishes"', source)


if __name__ == "__main__":
    unittest.main()
