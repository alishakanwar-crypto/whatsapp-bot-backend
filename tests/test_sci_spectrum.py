import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import database
from app.services import sci_spectrum_service as sci_spectrum


class SciSpectrumTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sci-spectrum.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
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
                """
            )
        self.teachers_path = Path(self.temp_dir.name) / "teachers.json"
        self.teachers_path.write_text(
            json.dumps([{"name": "Dr Example", "phone": "919000000001"}]),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    async def asyncSetUp(self):
        self.db_patch = patch.object(database, "DB_PATH", str(self.db_path))
        self.db_patch.start()
        self.env_patch = patch.dict(
            "os.environ",
            {"SCI_SPECTRUM_TEACHERS_FILE": str(self.teachers_path)},
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
                    )
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

    async def test_welcome_and_thankyou_never_deduplicate(self):
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
            self.assertEqual(await sci_spectrum.send_welcome_messages(), 1)
            self.assertEqual(await sci_spectrum.send_welcome_messages(), 1)
            self.assertEqual(await sci_spectrum.send_thankyou_messages(), 2)
            self.assertEqual(await sci_spectrum.send_thankyou_messages(), 2)

        self.assertEqual(sender.await_count, 6)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM sci_spectrum_deliveries").fetchone()[0],
                6,
            )

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
            self.assertEqual(await sci_spectrum.send_thankyou_messages(), 0)
        sender.assert_not_awaited()

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
