import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import database
from app.services import route_duty_service as route_duty


class RouteDutyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "route-duty.db"
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE school_holidays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT UNIQUE,
                    reason TEXT DEFAULT ''
                );
                CREATE TABLE processed_messages (
                    message_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE trueface_teachers (
                    pin TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT DEFAULT ''
                );
                CREATE TABLE route_duty_reminders (
                    duty_date TEXT, route TEXT, teacher_label TEXT,
                    recipient TEXT, status TEXT DEFAULT 'generated',
                    claimed_at TEXT DEFAULT '', accepted_at TEXT DEFAULT '',
                    acknowledged_at TEXT DEFAULT '', status_updated_at TEXT DEFAULT '',
                    wa_message_id TEXT DEFAULT '',
                    UNIQUE(duty_date, route, teacher_label, recipient)
                );
                CREATE TABLE route_duty_leave_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_label TEXT, teacher_name TEXT, leave_date TEXT,
                    route TEXT, source_message_id TEXT DEFAULT '',
                    detected_at TEXT, alerted_at TEXT DEFAULT '',
                    alert_status TEXT DEFAULT 'pending',
                    UNIQUE(teacher_label, leave_date, route)
                );
                CREATE TABLE route_duty_missed (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_name TEXT, duty_date TEXT, route TEXT,
                    reason TEXT DEFAULT 'Leave',
                    compensation_status TEXT DEFAULT 'Pending',
                    comp_date TEXT DEFAULT '', created_at TEXT,
                    UNIQUE(teacher_name, duty_date, route)
                );
                CREATE TABLE route_duty_teachers (
                    label TEXT PRIMARY KEY,
                    canonical_name TEXT DEFAULT '',
                    phone TEXT DEFAULT ''
                );
                CREATE TABLE route_duty_report_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_type TEXT, period_key TEXT, recipient TEXT,
                    status TEXT DEFAULT 'generated', claimed_at TEXT DEFAULT '',
                    wa_message_id TEXT DEFAULT '', status_updated_at TEXT DEFAULT '',
                    UNIQUE(report_type, period_key, recipient)
                );
                """
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    async def asyncSetUp(self):
        self.db_patch = patch.object(database, "DB_PATH", str(self.db_path))
        self.db_patch.start()

    async def asyncTearDown(self):
        self.db_patch.stop()

    async def test_working_day_skips_weekends_and_holidays(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO school_holidays (date, reason) VALUES (?, ?)",
                ("2026-07-30", "School holiday"),
            )
        self.assertFalse(await route_duty._is_working_day(date(2026, 7, 25)))
        self.assertFalse(await route_duty._is_working_day(date(2026, 7, 30)))
        self.assertEqual(
            await route_duty._next_working_day(date(2026, 7, 24)),
            date(2026, 7, 27),
        )
        self.assertEqual(
            await route_duty._next_working_day(date(2026, 7, 29)),
            date(2026, 7, 31),
        )

    async def test_duties_for_date_are_sorted_by_route(self):
        duties = await route_duty.duties_for_date(date(2026, 7, 28))
        self.assertEqual(
            [duty["route"] for duty in duties],
            ["Route 2", "Route 3", "Route 6", "Route 17", "Route 18"],
        )
        self.assertEqual(duties[0]["teacher_label"], "Ms Lipi")

    async def test_leave_date_parser_supports_requested_formats(self):
        dates = route_duty._parse_leave_dates(
            "28/07/2026, 29-07-2026, 30/07, 31 July, August 1"
        )
        self.assertEqual(
            dates,
            [
                date(2026, 7, 28),
                date(2026, 7, 29),
                date(2026, 7, 30),
                date(2026, 7, 31),
                date(2026, 8, 1),
            ],
        )

    async def test_extra_label_requires_runtime_override(self):
        self.assertEqual(await route_duty._resolve_recipients("Ms Yamini"), [])
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO route_duty_teachers (label, phone) VALUES (?, ?)",
                ("Ms Yamini", "9876543210"),
            )
        self.assertEqual(
            await route_duty._resolve_recipients("Ms Yamini"), ["919876543210"]
        )

    async def test_leave_conflict_is_deduplicated(self):
        duty = {
            "date": "2026-07-28",
            "route": "Route 17",
            "teacher_label": "Ms Surbhi",
        }
        first = await route_duty._create_leave_conflict(
            duty, "leave-1", datetime(2026, 7, 20, tzinfo=route_duty.IST)
        )
        second = await route_duty._create_leave_conflict(
            duty, "leave-2", datetime(2026, 7, 20, tzinfo=route_duty.IST)
        )
        self.assertTrue(first)
        self.assertFalse(second)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM route_duty_leave_conflicts").fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM route_duty_missed").fetchone()[0], 1
            )

    async def test_raw_schedule_leave_match_uses_normalized_duty_shape(self):
        matches = await route_duty._schedule_teacher_matches(
            "Approved leave for Ms Surbhi on 28/07/2026"
        )
        duty = next(match for match in matches if match["date"] == "2026-07-28")
        self.assertEqual(
            duty,
            {
                "date": "2026-07-28",
                "route": "Route 17",
                "teacher_label": "Ms Surbhi",
                "report_time": "13:30",
            },
        )
        self.assertTrue(
            await route_duty._create_leave_conflict(
                duty,
                "leave-raw-schedule",
                datetime(2026, 7, 20, tzinfo=route_duty.IST),
            )
        )

    async def test_monthly_report_uses_previous_calendar_month(self):
        with (
            patch.object(route_duty, "ROUTE_DUTY_ENABLED", True),
            patch.object(
                route_duty, "_send_period_report", AsyncMock(return_value=True)
            ) as send_report,
        ):
            self.assertTrue(
                await route_duty.send_monthly_report(
                    datetime(2026, 8, 1, 17, 0, tzinfo=route_duty.IST)
                )
            )
        send_report.assert_awaited_once_with(
            "monthly",
            "2026-07",
            date(2026, 7, 1),
            date(2026, 7, 31),
            datetime(2026, 8, 1, 17, 0, tzinfo=route_duty.IST),
        )

    async def test_acknowledgement_uses_next_working_day_and_latest_row(self):
        with sqlite3.connect(self.db_path) as db:
            db.executemany(
                "INSERT INTO route_duty_reminders "
                "(duty_date, route, teacher_label, recipient, claimed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        "2026-07-27",
                        "Route 2",
                        "Ms Lipi",
                        "919876543210",
                        "31-07-2026 16:00:00 IST",
                    ),
                    (
                        "2026-07-27",
                        "Route 3",
                        "Ms Surbhi",
                        "919876543210",
                        "01-08-2026 16:00:00 IST",
                    ),
                ],
            )
        self.assertTrue(
            await route_duty.mark_reminder_acknowledged(
                "9876543210",
                datetime(2026, 7, 24, 17, 0, tzinfo=route_duty.IST),
            )
        )
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT route, acknowledged_at FROM route_duty_reminders "
                "ORDER BY rowid"
            ).fetchall()
        self.assertEqual(rows[0][1], "")
        self.assertTrue(rows[1][1])

    async def test_reminders_claim_once_and_send_with_cloud_template(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO route_duty_teachers (label, phone) VALUES (?, ?)",
                ("Ms Lipi", "9876543210"),
            )
        sender = AsyncMock(return_value=True)
        now = datetime(2026, 7, 27, 9, 0, tzinfo=route_duty.IST)
        with (
            patch.object(route_duty, "ROUTE_DUTY_ENABLED", True),
            patch.object(
                route_duty.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
            patch.object(
                route_duty.whatsapp_service,
                "last_cloud_template_message_id",
                "wamid-route-duty",
            ),
        ):
            first = await route_duty.send_duty_reminders(now)
            second = await route_duty.send_duty_reminders(now)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        sender.assert_awaited_once()
        self.assertEqual(sender.await_args.kwargs["body_params"], ["28/07/2026", "Route 2"])

    async def test_disabled_send_and_poll_are_noops(self):
        with (
            patch.object(route_duty, "ROUTE_DUTY_ENABLED", False),
            patch.object(route_duty, "whatsapp_service") as whatsapp,
            patch("app.services.route_duty_service.imaplib.IMAP4_SSL") as imap,
        ):
            self.assertEqual(await route_duty.send_duty_reminders(), 0)
            self.assertFalse(await route_duty.send_harpreet_daily_report())
            self.assertEqual(await route_duty.poll_leave_mailbox(), 0)
        whatsapp.send_cloud_template_message.assert_not_called()
        imap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
