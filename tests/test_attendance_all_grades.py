import os
import tempfile
import unittest
from unittest import mock

import aiosqlite

import app.main as main


class AttendanceEligibilityTests(unittest.IsolatedAsyncioTestCase):
    """Attendance messages must reach a parent in any class, not only 9-12."""

    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = await aiosqlite.connect(self.db_path)
        await db.executescript(
            """
            CREATE TABLE pi_sheet_students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                father_mobile TEXT DEFAULT '',
                mother_mobile TEXT DEFAULT ''
            );
            CREATE TABLE snapshot_access_students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                father_mobile TEXT DEFAULT '',
                mother_mobile TEXT DEFAULT ''
            );
            CREATE TABLE snapshot_access_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                phone TEXT NOT NULL
            );
            CREATE TABLE notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                message_type TEXT,
                student_name TEXT,
                status TEXT,
                wa_message_id TEXT
            );
            """
        )
        await db.executemany(
            "INSERT INTO pi_sheet_students "
            "(student_name, grade, father_mobile, mother_mobile) "
            "VALUES (?, ?, ?, ?)",
            [
                ("ANANT SHARMA", "Nursery 1", "919000000001", "919000000002"),
                ("HARDIK BHATIA", "Grade 6B", "919311446262", "919555255488"),
                ("RIYA VERMA", "Grade 11D", "919000000003", ""),
            ],
        )
        await db.execute(
            "INSERT INTO snapshot_access_grants (student_name, grade, phone) "
            "VALUES ('Lakshika', 'Grade 11D', '918221013927')"
        )
        await db.commit()
        await db.close()

    async def asyncTearDown(self):
        os.unlink(self.db_path)

    async def _send(self, phone: str):
        sent: list[str] = []

        async def _fake_template(to, name, **kwargs):
            sent.append(to)
            return True

        request = mock.Mock()
        request.headers = {}
        request.json = mock.AsyncMock(return_value={
            "phone": phone,
            "template_name": "ppis_attendance_alert",
            "template_params": ["Test Child", "08:12 AM"],
        })
        with mock.patch.dict(os.environ, {"AGENT_SECRET": ""}, clear=False), \
                mock.patch("app.database.DB_PATH", self.db_path), \
                mock.patch(
                    "app.services.whatsapp_service.send_cloud_template_message",
                    _fake_template):
            result = await main.api_send_whatsapp(request)
        return result, sent

    def _assert_not_filtered(self, result):
        # A closed-school day may still block; eligibility must not.
        self.assertNotEqual(result.get("reason"),
                            "Number is not a known parent contact")

    async def test_nursery_parent_is_notified(self):
        result, _ = await self._send("919000000001")
        self._assert_not_filtered(result)

    async def test_middle_school_parent_is_notified(self):
        result, _ = await self._send("9311446262")
        self._assert_not_filtered(result)

    async def test_senior_school_parent_still_notified(self):
        result, _ = await self._send("919000000003")
        self._assert_not_filtered(result)

    async def test_manually_granted_parent_is_notified(self):
        result, _ = await self._send("918221013927")
        self._assert_not_filtered(result)

    async def test_unknown_number_stays_blocked(self):
        result, sent = await self._send("919999999999")
        self.assertEqual(result.get("status"), "blocked")
        self.assertEqual(result.get("reason"),
                         "Number is not a known parent contact")
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
