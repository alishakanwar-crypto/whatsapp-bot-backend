import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import aiosqlite

from app.routes import gate


class CPPlusVerifiedCorrectionTests(unittest.IsolatedAsyncioTestCase):
    def test_report_stays_provisional_without_recount(self):
        entry_times = [
            datetime(2026, 7, 15, 9, 10, tzinfo=gate.IST),
            datetime(2026, 7, 15, 9, 20, tzinfo=gate.IST),
        ]
        report = gate._build_cpplus_head_count_report(
            entry_times,
            datetime(2026, 7, 15, 10, tzinfo=gate.IST),
        )

        self.assertEqual(report["interval_entries"], 2)
        self.assertFalse(report["latest_hour_verified"])

    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.executescript(
                """
                CREATE TABLE gate_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    camera TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    attire_color TEXT DEFAULT '',
                    reconciled INTEGER DEFAULT 0,
                    matched_pin TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    person_crop TEXT DEFAULT ''
                );
                CREATE TABLE cpplus_hourly_recounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    hour_start TEXT NOT NULL,
                    hour_end TEXT NOT NULL,
                    in_count INTEGER NOT NULL,
                    processed_frames INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    UNIQUE(date, hour_start)
                );
                CREATE TABLE cpplus_native_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    hour_start TEXT NOT NULL,
                    hour_end TEXT NOT NULL,
                    in_count INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    UNIQUE(date, hour_start)
                );
                CREATE TABLE cpplus_recount_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    hour_start TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(date, hour_start, phone)
                );
                """
            )
            await db.executemany(
                "INSERT INTO gate_entries "
                "(date, timestamp, camera, direction) VALUES (?, ?, ?, 'IN')",
                [
                    ("2026-07-15", "2026-07-15 09:10:00", "Gate CP Plus"),
                    ("2026-07-15", "2026-07-15 09:20:00", "Gate CP Plus"),
                    ("2026-07-15", "2026-07-15 09:30:00", "Other camera"),
                ],
            )
            await db.execute(
                "INSERT INTO cpplus_hourly_recounts "
                "(date, hour_start, hour_end, in_count, processed_frames, "
                "source, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-07-15",
                    "2026-07-15 09:00:00",
                    "2026-07-15 10:00:00",
                    31,
                    7200,
                    "school_pc_recording",
                    "2026-07-15 10:25:00",
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def asyncTearDown(self):
        os.unlink(self.db_path)

    async def test_untrusted_native_count_is_captured_but_not_accepted(self):
        async def open_db():
            return await aiosqlite.connect(self.db_path)

        request = AsyncMock()
        request.json.return_value = {
            "date": "2026-07-15",
            "hour_start": "2026-07-15 10:00:00",
            "hour_end": "2026-07-15 11:00:00",
            "in_count": 7,
            "processed_frames": 0,
            "source": "camera_native_counter",
        }

        with (
            patch.object(gate, "_get_db", new=open_db),
            patch.dict(os.environ, {"CPPLUS_NATIVE_COUNTER_TRUSTED": "0"}),
            self.assertRaises(gate.HTTPException) as raised,
        ):
            await gate.receive_cpplus_hourly_recount(request)

        self.assertEqual(raised.exception.status_code, 409)
        db = await aiosqlite.connect(self.db_path)
        try:
            observation = await db.execute_fetchall(
                "SELECT in_count FROM cpplus_native_observations "
                "WHERE date = ? AND hour_start = ?",
                ("2026-07-15", "2026-07-15 10:00:00"),
            )
            recount = await db.execute_fetchall(
                "SELECT in_count FROM cpplus_hourly_recounts "
                "WHERE date = ? AND hour_start = ?",
                ("2026-07-15", "2026-07-15 10:00:00"),
            )
        finally:
            await db.close()

        self.assertEqual(observation, [(7,)])
        self.assertEqual(recount, [])

    async def test_verified_correction_targets_recount_hour_and_sends_once(self):
        async def open_db():
            return await aiosqlite.connect(self.db_path)

        recount = {
            "date": "2026-07-15",
            "hour_start": "2026-07-15 09:00:00",
            "hour_end": "2026-07-15 10:00:00",
            "in_count": 31,
            "processed_frames": 7200,
            "source": "school_pc_recording",
            "verified_at": "2026-07-15 10:25:00",
        }
        send = AsyncMock(return_value=True)
        upload = AsyncMock(return_value="document-id")

        with (
            patch.object(gate, "_get_db", new=open_db),
            patch.object(
                gate,
                "GATE_REPORT_WHATSAPP_PHONES",
                ["phone-a", "phone-b"],
            ),
            patch.object(gate, "_generate_cpplus_head_count_pdf", return_value=b"PDF"),
            patch.object(gate, "upload_media_bytes_cloud", upload),
            patch.object(gate, "send_cloud_template_message", send),
        ):
            await gate._send_verified_cpplus_correction(recount)
            await gate._send_verified_cpplus_correction(recount)

        self.assertEqual(send.await_count, 2)
        body_params = send.await_args_list[0].kwargs["body_params"]
        self.assertIn("09:00 AM - 10:00 AM IST", body_params[0])
        self.assertEqual(body_params[1], "31")
        self.assertEqual(body_params[2], "31")
        self.assertEqual(body_params[3], "2")

        db = await aiosqlite.connect(self.db_path)
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM cpplus_recount_corrections "
                "WHERE sent_at != ''"
            )
            self.assertEqual((await cursor.fetchone())[0], 2)
        finally:
            await db.close()

    async def test_retry_does_not_resend_successful_recipient(self):
        async def open_db():
            return await aiosqlite.connect(self.db_path)

        recount = {
            "date": "2026-07-15",
            "hour_start": "2026-07-15 09:00:00",
            "hour_end": "2026-07-15 10:00:00",
            "in_count": 31,
            "processed_frames": 7200,
            "source": "school_pc_recording",
            "verified_at": "2026-07-15 10:25:00",
        }
        send = AsyncMock(side_effect=[True, False, True])

        with (
            patch.object(gate, "_get_db", new=open_db),
            patch.object(
                gate,
                "GATE_REPORT_WHATSAPP_PHONES",
                ["phone-a", "phone-b"],
            ),
            patch.object(gate, "_generate_cpplus_head_count_pdf", return_value=b"PDF"),
            patch.object(
                gate,
                "upload_media_bytes_cloud",
                AsyncMock(return_value="document-id"),
            ),
            patch.object(gate, "send_cloud_template_message", send),
        ):
            await gate._send_verified_cpplus_correction(recount)
            await gate._send_verified_cpplus_correction(recount)

        self.assertEqual(
            [call.args[0] for call in send.await_args_list],
            ["phone-a", "phone-b", "phone-b"],
        )


if __name__ == "__main__":
    unittest.main()
