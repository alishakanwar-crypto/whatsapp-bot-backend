import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import aiosqlite

from app.routes import gate


class CPPlusVerifiedCorrectionTests(unittest.IsolatedAsyncioTestCase):
    def test_headcount_delivery_window_is_6am_through_5pm_ist(self):
        self.assertFalse(gate._headcount_delivery_window_open(
            datetime(2026, 7, 16, 5, 59, tzinfo=gate.IST)
        ))
        self.assertTrue(gate._headcount_delivery_window_open(
            datetime(2026, 7, 16, 6, 0, tzinfo=gate.IST)
        ))
        self.assertTrue(gate._headcount_delivery_window_open(
            datetime(2026, 7, 16, 17, 59, tzinfo=gate.IST)
        ))
        self.assertFalse(gate._headcount_delivery_window_open(
            datetime(2026, 7, 16, 18, 0, tzinfo=gate.IST)
        ))
        self.assertFalse(gate._headcount_delivery_window_open(
            datetime(2026, 7, 16, 22, 0, tzinfo=gate.IST)
        ))

    async def test_verified_report_is_not_sent_at_10pm(self):
        recount = {
            "date": "2026-07-16",
            "hour_start": "2026-07-16 15:00:00",
            "hour_end": "2026-07-16 16:00:00",
            "in_count": 34,
            "processed_frames": 0,
            "source": "camera_native_counter",
            "verified_at": "2026-07-16 22:00:00",
        }
        open_db = AsyncMock()
        upload = AsyncMock()
        send = AsyncMock()

        with (
            patch.object(
                gate, "_headcount_delivery_window_open", return_value=False,
            ),
            patch.object(gate, "_get_db", open_db),
            patch.object(gate, "upload_media_bytes_cloud", upload),
            patch.object(gate, "send_cloud_template_message", send),
        ):
            await gate._send_verified_cpplus_correction(recount)

        open_db.assert_not_awaited()
        upload.assert_not_awaited()
        send.assert_not_awaited()

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
        self.assertFalse(gate._is_fully_camera_verified(report))

    def test_non_cpplus_observations_keep_overlapping_sources_separate(self):
        start = datetime(2026, 7, 15, 9, tzinfo=gate.IST)
        end = datetime(2026, 7, 15, 10, tzinfo=gate.IST)
        observations = gate._build_non_cpplus_camera_observations(
            [
                {
                    "camera": "ENTRY GATE-1",
                    "direction": "IN",
                    "timestamp": "2026-07-15 09:05:00",
                },
                {
                    "camera": "Reception C1",
                    "direction": "IN",
                    "timestamp": "2026-07-15 09:07:00",
                },
                {
                    "camera": "DISPERSAL EXIT",
                    "direction": "OUT",
                    "timestamp": "2026-07-15 09:09:00",
                },
                {
                    "camera": "DISPERSAL EXIT",
                    "direction": "IN",
                    "timestamp": "2026-07-15 09:09:30",
                },
                {
                    "camera": "ENTRY GATE-OUTSIDE (CP Plus)",
                    "direction": "IN",
                    "timestamp": "2026-07-15 09:10:00",
                },
            ],
            start,
            end,
        )
        by_camera = {item["camera"]: item for item in observations}

        self.assertEqual(by_camera["ENTRY GATE-1"]["in_count"], 1)
        self.assertEqual(by_camera["Reception C1"]["in_count"], 1)
        self.assertEqual(by_camera["DISPERSAL EXIT"]["in_count"], 0)
        self.assertEqual(by_camera["DISPERSAL EXIT"]["out_count"], 1)
        self.assertNotIn("ENTRY GATE-OUTSIDE (CP Plus)", by_camera)
        self.assertIn("GALLERY MID", by_camera)
        self.assertEqual(
            by_camera["ENTRY GATE-1"]["role"], "C2 candidate / validator"
        )
        self.assertEqual(
            by_camera["Basement Main Gate"]["role"], "C4 candidate boundary"
        )

    def test_all_source_attendance_deduplicates_identity_and_preserves_sources(self):
        cutoff = datetime(2026, 7, 17, 9, 0, tzinfo=gate.IST)
        result = gate._build_all_source_attendance(
            attendance_records=[
                {
                    "person_id": "STUDENT_A_GRADE4B",
                    "name": "Student A",
                    "grade": "GRADE4B",
                    "camera_label": "Grade 4B C1",
                    "confidence": 0.94,
                    "status": "present",
                    "logged_at": "2026-07-17T07:20:00+05:30",
                },
                {
                    "person_id": "TEACHER_ALISHA",
                    "name": "Alisha Ahuja",
                    "grade": "",
                    "camera_label": "ENTRY GATE-1",
                    "confidence": 0.96,
                    "status": "present",
                    "logged_at": "2026-07-17T07:10:00+05:30",
                },
            ],
            trueface_records=[
                {
                    "pin": "42",
                    "name": "Alisha Kanwar",
                    "arrival_time": "07:05:00",
                    "departure_time": None,
                }
            ],
            dvr_sightings=[
                {
                    "person_id": "TEACHER_ALISHA",
                    "name": "Alisha Kanwar",
                    "camera": "ENTRY GATE-2",
                    "timestamp": "2026-07-17 07:07:00",
                    "direction": "IN",
                },
                {
                    "person_id": "TEACHER_ALISHA",
                    "name": "Alisha Kanwar",
                    "camera": "Reception C1",
                    "timestamp": "2026-07-17 07:15:00",
                    "direction": "IN",
                },
                {
                    "person_id": "TEACHER_ALISHA",
                    "name": "Alisha Kanwar",
                    "camera": "TrueFace 3000",
                    "timestamp": "2026-07-17 07:05:00",
                    "direction": "IN",
                },
            ],
            gate_entries=[],
            registered_people=[],
            contact_categories={},
            report_date="2026-07-17",
            cutoff=cutoff,
            pending_review_count=2,
        )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["students"], 1)
        self.assertEqual(result["staff"], 1)
        self.assertEqual(result["multi_source"], 1)
        self.assertEqual(result["pending_review"], 2)
        alisha = next(row for row in result["people"] if row["name"] == "Alisha Kanwar")
        self.assertEqual(alisha["verification"], "MULTI-SOURCE")
        self.assertEqual(alisha["first_seen"], "07:05 AM")
        self.assertEqual(
            alisha["sources"],
            ["DVR: ENTRY GATE-2", "DVR: Reception C1", "Face: ENTRY GATE-1", "TrueFace 3000"],
        )

    def test_attendance_and_boundary_roles_render_in_verified_pdf(self):
        report = {
            "total_entries": 12,
            "interval_entries": 12,
            "interval_display": "06:00 AM - 07:00 AM IST",
            "hourly_counts": [
                {"hour": "06:00 AM - 07:00 AM", "count": 12}
            ],
            "peak_hour": "06:00 AM - 07:00 AM",
            "peak_count": 12,
            "recording_verified_hours": 1,
            "completed_hours": 1,
            "latest_hour_verified": True,
            "is_final": False,
            "camera_observations": [
                {
                    "camera": "ENTRY GATE-1",
                    "role": "C2 candidate / validator",
                    "in_count": 8,
                    "out_count": 0,
                }
            ],
            "all_source_attendance": {
                "total": 1,
                "students": 1,
                "staff": 0,
                "multi_source": 1,
                "pending_review": 0,
                "cutoff": "07:00 AM",
                "people": [
                    {
                        "name": "Student A",
                        "category": "Students",
                        "grade": "GRADE4B",
                        "first_seen": "06:45 AM",
                        "last_seen": "06:50 AM",
                        "occupancy_status": "PRESENT",
                        "verification": "MULTI-SOURCE",
                        "sources": ["Face: Grade 4B C1", "DVR: ENTRY GATE-1"],
                    }
                ],
            },
        }

        pdf = gate._generate_cpplus_head_count_pdf(
            report, "17-07-2026", "07:02 AM"
        )

        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_all_source_attendance_uses_matched_gate_identity_and_cutoff(self):
        result = gate._build_all_source_attendance(
            attendance_records=[],
            trueface_records=[],
            dvr_sightings=[],
            gate_entries=[
                {
                    "matched_pin": "9",
                    "camera": "ENTRY GATE-OUTSIDE (CP Plus)",
                    "timestamp": "2026-07-17 08:45:00",
                    "direction": "IN",
                },
                {
                    "matched_pin": "9",
                    "camera": "DISPERSAL EXIT",
                    "timestamp": "2026-07-17 09:00:00",
                    "direction": "OUT",
                },
            ],
            registered_people=[{"pin": "9", "name": "Teacher B"}],
            contact_categories={},
            report_date="2026-07-17",
            cutoff=datetime(2026, 7, 17, 9, 0, tzinfo=gate.IST),
            pending_review_count=0,
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["people"][0]["verification"], "GATE ID ONLY")
        self.assertEqual(result["people"][0]["occupancy_status"], "PRESENT")
        self.assertEqual(result["people"][0]["last_seen"], "08:45 AM")

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
                CREATE TABLE attendance_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL,
                    student_name TEXT NOT NULL,
                    grade TEXT NOT NULL DEFAULT '',
                    camera_label TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'present',
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE trueface_attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pin TEXT NOT NULL,
                    name TEXT NOT NULL,
                    date TEXT NOT NULL,
                    arrival_time TEXT,
                    departure_time TEXT
                );
                CREATE TABLE teacher_dvr_sightings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    person_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    camera TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    outfit_color TEXT DEFAULT '',
                    outfit_description TEXT DEFAULT '',
                    outfit_colors_json TEXT DEFAULT '[]',
                    direction TEXT DEFAULT 'IN'
                );
                CREATE TABLE trueface_teachers (
                    pin TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT DEFAULT ''
                );
                CREATE TABLE trueface_contacts (
                    pin TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    category TEXT DEFAULT 'staff'
                );
                CREATE TABLE manual_review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL,
                    matched_name TEXT NOT NULL DEFAULT '',
                    grade TEXT NOT NULL DEFAULT '',
                    camera_label TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    snapshot_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            await db.executemany(
                "INSERT INTO cpplus_hourly_recounts "
                "(date, hour_start, hour_end, in_count, processed_frames, "
                "source, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "2026-07-15",
                        f"2026-07-15 {hour:02d}:00:00",
                        f"2026-07-15 {hour + 1:02d}:00:00",
                        count,
                        7200,
                        "school_pc_recording",
                        "2026-07-15 10:25:00",
                    )
                    for hour, count in ((6, 5), (7, 7), (8, 9), (9, 31))
                ],
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
            patch.object(
                gate, "_headcount_delivery_window_open", return_value=True,
            ),
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
        self.assertTrue(body_params[0].startswith("15-07-2026 "))
        self.assertEqual(body_params[1], "31")
        self.assertEqual(body_params[2], "52")
        self.assertEqual(len(body_params), 3)
        self.assertEqual(
            send.await_args_list[0].args[1], gate.GATE_REPORT_WHATSAPP_TEMPLATE
        )

        db = await aiosqlite.connect(self.db_path)
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM cpplus_recount_corrections "
                "WHERE sent_at != ''"
            )
            self.assertEqual((await cursor.fetchone())[0], 2)
        finally:
            await db.close()

    async def test_trusted_native_count_sends_one_correction(self):
        async def open_db():
            return await aiosqlite.connect(self.db_path)

        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute(
                "INSERT INTO cpplus_hourly_recounts "
                "(date, hour_start, hour_end, in_count, processed_frames, "
                "source, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-07-15",
                    "2026-07-15 10:00:00",
                    "2026-07-15 11:00:00",
                    7,
                    0,
                    "camera_native_counter",
                    "2026-07-15 11:02:00",
                ),
            )
            await db.commit()
        finally:
            await db.close()

        recount = {
            "date": "2026-07-15",
            "hour_start": "2026-07-15 10:00:00",
            "hour_end": "2026-07-15 11:00:00",
            "in_count": 7,
            "processed_frames": 0,
            "source": "camera_native_counter",
            "verified_at": "2026-07-15 11:02:00",
        }
        send = AsyncMock(return_value=True)

        with (
            patch.object(
                gate, "_headcount_delivery_window_open", return_value=True,
            ),
            patch.object(gate, "_get_db", new=open_db),
            patch.object(gate, "GATE_REPORT_WHATSAPP_PHONES", ["phone-a"]),
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

        self.assertEqual(send.await_count, 1)
        self.assertEqual(send.await_args.kwargs["body_params"][1], "7")

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
            patch.object(
                gate, "_headcount_delivery_window_open", return_value=True,
            ),
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
            patch.dict(
                os.environ,
                {"GATE_VERIFIED_ONLY_START": "2026-07-15 10:30:00"},
            ),
        ):
            await gate._send_verified_cpplus_correction(recount)
            await gate._send_verified_cpplus_correction(recount)

        self.assertEqual(
            [call.args[0] for call in send.await_args_list],
            ["phone-a", "phone-b", "phone-b"],
        )
        self.assertTrue(
            all(
                call.args[1] == gate.GATE_VERIFIED_CORRECTION_WHATSAPP_TEMPLATE
                for call in send.await_args_list
            )
        )
        self.assertTrue(
            all(len(call.kwargs["body_params"]) == 4 for call in send.await_args_list)
        )


if __name__ == "__main__":
    unittest.main()
