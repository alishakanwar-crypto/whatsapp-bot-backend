import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

from app.services import sheet_refresh_service as sheets
from app.services import snapshot_audit_service as audit

SCHEMA = """
CREATE TABLE snapshot_access_students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_name TEXT NOT NULL,
    grade TEXT NOT NULL,
    father_mobile TEXT DEFAULT '',
    mother_mobile TEXT DEFAULT ''
);
CREATE TABLE pi_sheet_students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_name TEXT NOT NULL,
    grade TEXT NOT NULL,
    father_name TEXT DEFAULT '',
    mother_name TEXT DEFAULT '',
    father_mobile TEXT DEFAULT '',
    mother_mobile TEXT DEFAULT '',
    address TEXT DEFAULT '',
    transport TEXT DEFAULT ''
);
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
CREATE TABLE snapshot_request_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_date TEXT NOT NULL,
    requested_at_ist TEXT NOT NULL,
    sender_phone TEXT NOT NULL,
    message_text TEXT DEFAULT '',
    is_admin INTEGER NOT NULL DEFAULT 0,
    student_name TEXT DEFAULT '',
    grade TEXT DEFAULT '',
    location TEXT DEFAULT '',
    outcome TEXT NOT NULL,
    reason TEXT DEFAULT '',
    in_pi_sheet_cache INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE snapshot_audit_report_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL UNIQUE,
    failed_count INTEGER NOT NULL DEFAULT 0,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE manual_students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_name TEXT NOT NULL,
    grade TEXT NOT NULL,
    father_name TEXT DEFAULT '',
    mother_name TEXT DEFAULT '',
    father_mobile TEXT DEFAULT '',
    mother_mobile TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE TABLE allowlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number TEXT NOT NULL,
    label TEXT DEFAULT ''
);
"""


class SnapshotAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.executescript(SCHEMA)
            await db.commit()
        finally:
            await db.close()

    async def asyncTearDown(self):
        os.unlink(self.db_path)

    def _open_db(self):
        async def open_db():
            return await aiosqlite.connect(self.db_path)

        return open_db

    async def _exec(self, sql, params=()):
        db = await aiosqlite.connect(self.db_path)
        try:
            cur = await db.execute(sql, params)
            rows = await cur.fetchall()
            await db.commit()
            return rows
        finally:
            await db.close()

    async def test_recovers_access_for_parent_missing_from_cache(self):
        await self._exec(
            "INSERT INTO student_birthdays "
            "(student_name, grade, dob, father_phone, mother_phone) "
            "VALUES (?, ?, ?, ?, ?)",
            ("SNAISHA JAIN", "Nur 2", "09-01", "9718884500", "9899347270"),
        )

        with (
            patch("app.database.get_db", new=self._open_db()),
            patch.object(audit, "_schedule_full_resync", new=AsyncMock()),
        ):
            recovered = await audit.recover_snapshot_access("919899347270")

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["grade"], "Nursery 2")
        rows = await self._exec(
            "SELECT student_name, grade, father_mobile, mother_mobile "
            "FROM snapshot_access_students"
        )
        self.assertEqual(
            rows,
            [("SNAISHA JAIN", "Nursery 2", "919718884500", "919899347270")],
        )

    async def test_unknown_number_is_not_recovered(self):
        with (
            patch("app.database.get_db", new=self._open_db()),
            patch.object(audit, "_schedule_full_resync", new=AsyncMock()),
        ):
            recovered = await audit.recover_snapshot_access("919000000000")
        self.assertEqual(recovered, [])
        self.assertEqual(
            await self._exec("SELECT COUNT(*) FROM snapshot_access_students"),
            [(0,)],
        )

    async def test_daily_audit_alerts_once_per_day(self):
        with patch("app.database.get_db", new=self._open_db()):
            await audit.log_snapshot_request(
                "919899347270",
                "Show my child",
                audit.OUTCOME_BLOCKED_UNAUTHORIZED,
                reason="number not found in saved parent data",
            )
            await audit.log_snapshot_request(
                "919876543210",
                "Show",
                audit.OUTCOME_DELIVERED,
                location="NUR-2",
            )

            sender = AsyncMock(return_value=True)
            with patch(
                "app.services.whatsapp_service.send_whatsapp_force", new=sender
            ):
                first = await audit.run_daily_snapshot_audit()
                second = await audit.run_daily_snapshot_audit()

        self.assertEqual(first["failed"], 1)
        self.assertTrue(first["alerted"])
        self.assertFalse(second["alerted"])
        body = sender.await_args_list[0].args[1]
        self.assertIn("919899347270", body)
        self.assertIn("Unresolved after auto-repair: 1", body)

    async def test_live_sheet_reread_repairs_access_during_the_request(self):
        async def fake_refresh():
            await self._exec(
                "INSERT INTO snapshot_access_students "
                "(student_name, grade, father_mobile, mother_mobile) "
                "VALUES (?, ?, ?, ?)",
                ("SNAISHA JAIN", "Nursery 2", "919718884500", "919899347270"),
            )

        with (
            patch("app.database.get_db", new=self._open_db()),
            patch.object(audit, "_last_resync_at", None),
            patch.object(
                sheets, "fetch_all_pi_sheet_tabs", new=AsyncMock(wraps=fake_refresh)
            ),
        ):
            wards = await audit.repair_access_now("919899347270")

        self.assertEqual(
            [(w["student_name"], w["grade"]) for w in wards],
            [("SNAISHA JAIN", "Nursery 2")],
        )

    async def test_unknown_number_stays_refused_after_live_reread(self):
        with (
            patch("app.database.get_db", new=self._open_db()),
            patch.object(audit, "_last_resync_at", None),
            patch.object(
                sheets, "fetch_all_pi_sheet_tabs", new=AsyncMock(return_value=True)
            ),
        ):
            self.assertEqual(await audit.repair_access_now("919000000000"), [])

    async def test_failure_is_resolved_once_the_photo_is_delivered(self):
        with patch("app.database.get_db", new=self._open_db()):
            await audit.log_snapshot_request(
                "919718884500",
                "Show my child",
                audit.OUTCOME_CAPTURE_FAILED,
                reason="camera stream did not start",
                location="NUR-2",
            )
            await audit.log_snapshot_request(
                "919718884500",
                "Show my child",
                audit.OUTCOME_DELIVERED,
                reason="delivered on automatic retry",
                location="NUR-2",
            )
            summary = await audit.resolve_open_failures()

        self.assertEqual((summary["open"], summary["repaired"]), (1, 1))
        self.assertEqual(
            await self._exec(
                "SELECT COUNT(*) FROM snapshot_request_audit "
                "WHERE resolved = 0 AND outcome = ?",
                (audit.OUTCOME_CAPTURE_FAILED,),
            ),
            [(0,)],
        )

    async def test_undelivered_failure_stays_open_for_the_admins(self):
        with patch("app.database.get_db", new=self._open_db()):
            await audit.log_snapshot_request(
                "919718884501",
                "Show my child",
                audit.OUTCOME_CAPTURE_FAILED,
                reason="camera stream did not start",
                location="NUR-2",
            )
            summary = await audit.resolve_open_failures()

        self.assertEqual((summary["open"], summary["repaired"]), (1, 0))


class PartialSheetRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.executescript(SCHEMA)
            await db.executemany(
                "INSERT INTO snapshot_access_students "
                "(student_name, grade, father_mobile, mother_mobile) "
                "VALUES (?, ?, ?, ?)",
                [
                    ("SNAISHA JAIN", "Nursery 2", "919718884500", ""),
                    ("OLD NUR2", "Nursery 2", "919718884501", ""),
                    ("PREP KID", "Prep 1", "919718884502", ""),
                ],
            )
            await db.commit()
        finally:
            await db.close()

    async def asyncTearDown(self):
        os.unlink(self.db_path)

    def _open_db(self):
        async def open_db():
            return await aiosqlite.connect(self.db_path)

        return open_db

    def _csv(self, grade: str, *names: str) -> str:
        rows = "".join(
            f"{grade},{name},981111111{i},982222222{i}\n"
            for i, name in enumerate(names)
        )
        return "GRADE,STUDENT NAME,FATHER MOBILE NO.,MOTHER MOBILE NO.\n" + rows

    async def _refresh_with(self, responses: dict[str, object]) -> bool:
        class _Resp:
            def __init__(self, status_code: int, text: str):
                self.status_code = status_code
                self.text = text

        class _Client:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def get(self_inner, url, timeout=0, follow_redirects=False):
                gid = url.rsplit("gid=", 1)[-1]
                value = responses.get(gid)
                if value is None:
                    return _Resp(500, "")
                return _Resp(200, value)

        alerts: list[str] = []

        async def record_alert(detail: str) -> None:
            alerts.append(detail)

        with (
            patch.object(sheets, "PI_SHEET_GRADE_GIDS", ["1", "2"]),
            patch.object(sheets.httpx, "AsyncClient", return_value=_Client()),
            patch.object(sheets, "_alert_sheet_refresh_problem", new=record_alert),
            patch.object(sheets, "TAB_FETCH_ATTEMPTS", 1),
            patch("app.database.get_db", new=self._open_db()),
        ):
            ok = await sheets.fetch_all_pi_sheet_tabs()
        self.alerts = alerts
        return ok

    async def test_unreadable_tab_keeps_its_classes(self):
        ok = await self._refresh_with(
            {
                "1": self._csv("Nursery 2", "SNAISHA JAIN", "OLD NUR2"),
                # gid 2 (Prep 1) fails to load
            }
        )
        self.assertTrue(ok)

        db = await aiosqlite.connect(self.db_path)
        try:
            cur = await db.execute(
                "SELECT student_name, grade FROM snapshot_access_students "
                "ORDER BY grade, student_name"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()

        # Nursery 2 was re-read from the sheet, Prep 1 kept its saved row.
        self.assertEqual(
            rows,
            [
                ("OLD NUR2", "Nursery 2"),
                ("SNAISHA JAIN", "Nursery 2"),
                ("PREP KID", "Prep 1"),
            ],
        )
        self.assertTrue(self.alerts)

    async def test_mass_shrink_is_refused(self):
        ok = await self._refresh_with({"1": self._csv("Nursery 2", "ONLY KID")})
        # 1 parsed Nursery 2 student vs 2 saved is a >20% drop, so the refresh
        # keeps the saved data instead of deleting students.
        self.assertFalse(ok)
        db = await aiosqlite.connect(self.db_path)
        try:
            cur = await db.execute("SELECT COUNT(*) FROM snapshot_access_students")
            count = (await cur.fetchone())[0]
        finally:
            await db.close()
        self.assertEqual(count, 3)
        self.assertTrue(self.alerts)

    async def test_every_child_gets_a_birthday_even_outside_the_bot_list(self):
        tab = (
            "GRADE,STUDENT NAME,DOB,FATHER MOBILE NO.,MOTHER MOBILE NO.\n"
            "Nursery 2,SNAISHA JAIN,24.02.2018,9811111110,9822222220\n"
            "Nursery 2,OLD NUR2,10.03.2018,9811111111,9822222221\n"
            "Nursery 2,LEFT KID,11.03.2018,9811111112,\n"
            "Withdrawal,,,,\n"
            "Nursery 2,GONE KID,12.03.2018,9811111113,\n"
        )
        with patch.object(
            sheets, "PI_SHEET_BOT_ENABLED_STUDENTS", {"SNAISHA JAIN"}
        ):
            ok = await self._refresh_with({"1": tab, "2": tab})
        self.assertTrue(ok)

        db = await aiosqlite.connect(self.db_path)
        try:
            cur = await db.execute(
                "SELECT student_name FROM student_birthdays ORDER BY student_name"
            )
            names = [row[0] for row in await cur.fetchall()]
            cur = await db.execute("SELECT COUNT(*) FROM pi_sheet_students")
            bot_students = (await cur.fetchone())[0]
        finally:
            await db.close()

        self.assertEqual(names, ["LEFT KID", "OLD NUR2", "SNAISHA JAIN"])
        # The bot's own parent features stay limited to the allowlist.
        self.assertEqual(bot_students, 1)

    async def test_a_promoted_child_is_wished_once(self):
        header = "GRADE,STUDENT NAME,DOB,FATHER MOBILE NO.,MOTHER MOBILE NO.\n"
        row = "{grade},MOVED KID,24.02.2015,9811111110,9822222220\n"
        others = (
            "{grade},STAYS A,01.02.2015,9811111111,\n"
            "{grade},STAYS B,02.02.2015,9811111112,\n"
        )
        ok = await self._refresh_with(
            {
                "1": header
                + row.format(grade="Grade 5")
                + others.format(grade="Grade 5"),
                "2": header
                + row.format(grade="Grade 6")
                + others.format(grade="Grade 6"),
            }
        )
        self.assertTrue(ok)

        db = await aiosqlite.connect(self.db_path)
        try:
            cur = await db.execute(
                "SELECT grade FROM student_birthdays "
                "WHERE student_name = 'MOVED KID'"
            )
            grades = [row[0] for row in await cur.fetchall()]
        finally:
            await db.close()

        # Listed in both class tabs, wished once — in the higher grade.
        self.assertEqual(grades, ["Grade 6"])

    async def test_a_child_promoted_out_of_prep_is_named_by_grade(self):
        header = "GRADE,STUDENT NAME,DOB,FATHER MOBILE NO.,MOTHER MOBILE NO.\n"
        row = "{grade},PREP KID,24.02.2019,9811111110,9822222220\n"
        others = (
            "{grade},STAYS A,01.02.2019,9811111111,\n"
            "{grade},STAYS B,02.02.2019,9811111112,\n"
        )
        ok = await self._refresh_with(
            {
                "1": header
                + row.format(grade="Prep 2")
                + others.format(grade="Prep 2"),
                "2": header
                + row.format(grade="Grade 1")
                + others.format(grade="Grade 1"),
            }
        )
        self.assertTrue(ok)

        db = await aiosqlite.connect(self.db_path)
        try:
            cur = await db.execute(
                "SELECT grade FROM student_birthdays "
                "WHERE student_name = 'PREP KID'"
            )
            grades = [row[0] for row in await cur.fetchall()]
        finally:
            await db.close()

        # Prep 2 carries the bigger number but Grade 1 is the newer class.
        self.assertEqual(grades, ["Grade 1"])

    async def test_a_birthday_survives_a_new_class_row_without_a_dob(self):
        header = "GRADE,STUDENT NAME,DOB,FATHER MOBILE NO.,MOTHER MOBILE NO.\n"
        others = (
            "{grade},STAYS A,01.02.2015,9811111111,\n"
            "{grade},STAYS B,02.02.2015,9811111112,\n"
        )
        ok = await self._refresh_with(
            {
                "1": header
                + "Grade 5,NO DOB KID,24.02.2015,9811111110,9822222220\n"
                + others.format(grade="Grade 5"),
                "2": header
                + "Grade 6,NO DOB KID,,9811111110,9822222220\n"
                + others.format(grade="Grade 6"),
            }
        )
        self.assertTrue(ok)

        db = await aiosqlite.connect(self.db_path)
        try:
            cur = await db.execute(
                "SELECT grade, dob FROM student_birthdays "
                "WHERE student_name = 'NO DOB KID'"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()

        # The new class row has no date, so the old one keeps the wish alive.
        self.assertEqual(rows, [("Grade 5", "2015-02-24")])


if __name__ == "__main__":
    unittest.main()
