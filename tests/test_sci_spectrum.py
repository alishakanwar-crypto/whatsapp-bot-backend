import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app import database
from app.services import sci_spectrum_service as sci_spectrum


class SciSpectrumTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sci-spectrum.db"
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                CREATE TABLE sci_spectrum_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phase TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    wa_message_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    status_updated_at TEXT NOT NULL DEFAULT ''
                )
                ;
                CREATE TABLE sci_spectrum_welcomed (
                    phone TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    welcomed_at TEXT NOT NULL
                )
                """
            )
        self.teachers_path = Path(self.temp_dir.name) / "teachers.json"
        self.teachers_path.write_text(
            json.dumps([{"name": "Dr Example", "phone": "919000000001"}]),
            encoding="utf-8",
        )
        self.event_now = datetime(
            2026, 8, 1, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    async def asyncSetUp(self):
        self.db_patch = patch.object(database, "DB_PATH", str(self.db_path))
        self.db_patch.start()
        self.env_patch = patch.dict(
            "os.environ",
            {
                "SCI_SPECTRUM_TEACHERS_FILE": str(self.teachers_path),
                "SCI_SPECTRUM_SHEET_CSV_URL": "",
                "SCI_SPECTRUM_THANKYOU_QR_URL": "",
            },
            clear=False,
        )
        self.env_patch.start()

    async def asyncTearDown(self):
        self.env_patch.stop()
        self.db_patch.stop()

    async def test_thankyou_includes_teachers_and_three_evidence_recipients(self):
        sender = AsyncMock(return_value=True)
        with (
            patch.object(sci_spectrum, "SCI_SPECTRUM_ENABLED", True),
            patch.dict(
                "os.environ",
                {
                    "SCI_SPECTRUM_EVIDENCE_PHONES": (
                        "919599488106,918076455224,919111111111"
                    ),
                    "SCI_SPECTRUM_THANKYOU_QR_URL": (
                        "https://example.test/scispectrum-qr.png"
                    ),
                },
            ),
            patch.object(
                sci_spectrum.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
        ):
            accepted = await sci_spectrum.send_thankyou_messages()

        self.assertEqual(accepted, 4)
        self.assertEqual(
            [call.kwargs["to"] for call in sender.await_args_list],
            [
                "919000000001",
                "919599488106",
                "918076455224",
                "919111111111",
            ],
        )
        self.assertTrue(
            all(
                call.kwargs["header_image_url"]
                == "https://example.test/scispectrum-qr.png"
                for call in sender.await_args_list
            )
        )

    async def test_thankyou_sends_evidence_when_teacher_source_is_empty(self):
        sender = AsyncMock(return_value=True)
        evidence = ["919599488106", "918076455224", "919111111111"]
        with (
            patch.object(sci_spectrum, "SCI_SPECTRUM_ENABLED", True),
            patch.object(sci_spectrum, "_load_teachers", AsyncMock(return_value=[])),
            patch.object(sci_spectrum, "_evidence_recipients", return_value=evidence),
            patch.object(
                sci_spectrum.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
        ):
            accepted = await sci_spectrum.send_thankyou_messages()

        self.assertEqual(accepted, len(evidence))
        self.assertEqual(
            [call.kwargs["to"] for call in sender.await_args_list], evidence
        )

    async def test_welcome_poll_deduplicates_but_thankyou_does_not(self):
        sender = AsyncMock(return_value=True)
        with (
            patch.object(sci_spectrum, "SCI_SPECTRUM_ENABLED", True),
            patch.dict(
                "os.environ",
                {"SCI_SPECTRUM_EVIDENCE_PHONES": "919599488106"},
            ),
            patch.object(
                sci_spectrum.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
        ):
            self.assertEqual(
                await sci_spectrum.send_welcome_messages(self.event_now), 1
            )
            self.assertEqual(
                await sci_spectrum.send_welcome_messages(self.event_now), 0
            )
            self.assertEqual(await sci_spectrum.send_thankyou_messages(), 2)
            self.assertEqual(await sci_spectrum.send_thankyou_messages(), 2)

        self.assertEqual(sender.await_count, 5)
        self.assertTrue(
            all(
                "header_image_url" not in call.kwargs
                for call in sender.await_args_list[2:]
            )
        )
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM sci_spectrum_deliveries").fetchone()[0],
                5,
            )

    async def test_fetch_sheet_teachers_parses_and_normalizes_rows(self):
        class Response:
            text = (
                "S.No,Teacher Name,WhatsApp Number\n"
                "1,Dr Sheet,9000000003\n"
                "2,No Phone,\n"
                "3,,919000000004\n"
                "4,Invalid,12345\n"
            )

            def raise_for_status(self):
                return None

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, timeout):
                return Response()

        with (
            patch.dict(
                "os.environ",
                {"SCI_SPECTRUM_SHEET_CSV_URL": "https://example.test/teachers.csv"},
            ),
            patch.object(sci_spectrum.httpx, "AsyncClient", return_value=Client()),
        ):
            teachers = await sci_spectrum._fetch_sheet_teachers()

        self.assertEqual(
            teachers, [{"name": "Dr Sheet", "phone": "919000000003"}]
        )

    async def test_event_date_configures_welcome_poll_guard(self):
        configured_date = date(2026, 8, 3)
        configured_now = datetime(
            2026, 8, 3, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")
        )
        other_date = datetime(
            2026, 8, 4, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")
        )
        sender = AsyncMock(return_value=True)
        with (
            patch.object(sci_spectrum, "EVENT_DATE", configured_date),
            patch.object(sci_spectrum, "SCI_SPECTRUM_ENABLED", True),
            patch.object(
                sci_spectrum,
                "_load_teachers",
                AsyncMock(
                    return_value=[
                        {"name": "Configured Teacher", "phone": "919000000006"}
                    ]
                ),
            ),
            patch.object(
                sci_spectrum.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
        ):
            self.assertEqual(
                await sci_spectrum.poll_and_send_welcomes(configured_now), 1
            )
            self.assertEqual(
                await sci_spectrum.poll_and_send_welcomes(other_date), 0
            )

        self.assertEqual(sender.await_count, 1)

    async def test_invalid_event_date_uses_default(self):
        with patch.dict(
            "os.environ", {"SCI_SPECTRUM_EVENT_DATE": "not-a-date"}
        ):
            self.assertEqual(
                sci_spectrum._configured_event_date(), date(2026, 8, 1)
            )

    async def test_live_welcome_poll_sends_new_teachers_once(self):
        first = [
            {"name": "Teacher One", "phone": "919000000001"},
            {"name": "Teacher Two", "phone": "919000000002"},
        ]
        second = first + [{"name": "Teacher Three", "phone": "919000000003"}]
        fetch = AsyncMock(side_effect=[first, first, second])
        sender = AsyncMock(return_value=True)
        with (
            patch.object(sci_spectrum, "SCI_SPECTRUM_ENABLED", True),
            patch.dict(
                "os.environ",
                {"SCI_SPECTRUM_SHEET_CSV_URL": "https://example.test/live.csv"},
            ),
            patch.object(sci_spectrum, "_fetch_sheet_teachers", fetch),
            patch.object(
                sci_spectrum.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
        ):
            self.assertEqual(
                await sci_spectrum.poll_and_send_welcomes(self.event_now), 2
            )
            self.assertEqual(
                await sci_spectrum.poll_and_send_welcomes(self.event_now), 0
            )
            self.assertEqual(
                await sci_spectrum.poll_and_send_welcomes(self.event_now), 1
            )

        self.assertEqual(sender.await_count, 3)
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT phone FROM sci_spectrum_welcomed ORDER BY phone"
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("919000000001",),
                ("919000000002",),
                ("919000000003",),
            ],
        )

    async def test_failed_welcome_releases_claim_for_retry(self):
        teachers = [{"name": "Retry Teacher", "phone": "919000000005"}]
        sender = AsyncMock(side_effect=[False, True])
        with (
            patch.object(sci_spectrum, "SCI_SPECTRUM_ENABLED", True),
            patch.dict(
                "os.environ",
                {"SCI_SPECTRUM_SHEET_CSV_URL": "https://example.test/live.csv"},
            ),
            patch.object(
                sci_spectrum, "_fetch_sheet_teachers",
                AsyncMock(return_value=teachers),
            ),
            patch.object(
                sci_spectrum.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
        ):
            self.assertEqual(
                await sci_spectrum.poll_and_send_welcomes(self.event_now), 0
            )
            self.assertEqual(
                await sci_spectrum.poll_and_send_welcomes(self.event_now), 1
            )

        self.assertEqual(sender.await_count, 2)

    async def test_empty_teacher_file_skips_without_sending(self):
        self.teachers_path.write_text("[]", encoding="utf-8")
        sender = AsyncMock(return_value=True)
        with (
            patch.object(sci_spectrum, "SCI_SPECTRUM_ENABLED", True),
            patch.object(
                sci_spectrum.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
        ):
            self.assertEqual(await sci_spectrum.send_welcome_messages(), 0)
            self.assertEqual(await sci_spectrum.send_thankyou_messages(), 2)
        self.assertEqual(sender.await_count, 2)

    async def test_feature_gate_default_off_skips_all_sends(self):
        sender = AsyncMock(return_value=True)
        with (
            patch.object(sci_spectrum, "SCI_SPECTRUM_ENABLED", False),
            patch.object(
                sci_spectrum.whatsapp_service,
                "send_cloud_template_message",
                sender,
            ),
        ):
            self.assertEqual(await sci_spectrum.send_welcome_messages(), 0)
            self.assertEqual(await sci_spectrum.send_thankyou_messages(), 0)
        sender.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
