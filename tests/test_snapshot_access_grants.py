import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite

from app.routes import webhook


class SnapshotAccessGrantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.executescript(
                """
                CREATE TABLE snapshot_access_students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_name TEXT NOT NULL,
                    grade TEXT NOT NULL,
                    father_mobile TEXT DEFAULT '',
                    mother_mobile TEXT DEFAULT '',
                    UNIQUE(student_name, grade)
                );
                CREATE TABLE snapshot_access_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_name TEXT NOT NULL,
                    grade TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_name, grade, phone)
                );
                """
            )
            await db.executemany(
                "INSERT INTO snapshot_access_grants "
                "(student_name, grade, phone) VALUES (?, ?, ?)",
                [
                    ("Younger Sibling", "Nursery 1", "919876543210"),
                    ("Older Sibling", "Grade 4B", "919876543210"),
                ],
            )
            await db.execute("DELETE FROM snapshot_access_students")
            await db.commit()
        finally:
            await db.close()

    async def asyncTearDown(self):
        os.unlink(self.db_path)

    async def test_grants_survive_snapshot_student_refresh(self):
        async def open_db():
            return await aiosqlite.connect(self.db_path)

        async def no_general_access(_sender):
            return []

        with (
            patch.object(webhook, "get_db", new=open_db),
            patch.object(
                webhook,
                "_lookup_parent_child_class",
                new=no_general_access,
            ),
        ):
            children = await webhook._lookup_snapshot_parent_child_class(
                "919876543210"
            )

        self.assertEqual(
            {child["grade"] for child in children},
            {"Nursery 1", "Grade 4B"},
        )
        self.assertEqual(
            webhook._restrict_parent_snapshot_location("NUR-1", children),
            "NUR-1",
        )
        self.assertEqual(
            webhook._restrict_parent_snapshot_location("GRADE 4B", children),
            "GRADE 4B",
        )

    async def test_offline_request_queues_resolved_classroom(self):
        from app.routes import agent_ws

        queue_request = Mock(return_value=True)
        with (
            patch.object(webhook, "_is_snapshot_request", return_value=True),
            patch.object(webhook, "_is_admin_panel", return_value=True),
            patch.object(
                webhook,
                "_extract_classroom_for_queue",
                new=AsyncMock(return_value="NUR-1"),
            ),
            patch.object(webhook, "send_whatsapp_message", new=AsyncMock()),
            patch.object(agent_ws, "wait_for_agent", new=AsyncMock(return_value=False)),
            patch.object(agent_ws, "record_snapshot_failure"),
            patch.object(
                agent_ws,
                "get_health_state",
                return_value={"consecutive_failures": 1},
            ),
            patch.object(agent_ws, "queue_snapshot_request", new=queue_request),
        ):
            handled = await webhook.detect_and_handle_snapshot_request(
                "919876543210", "Show Nursery 1", "919876543210"
            )

        self.assertTrue(handled)
        queue_request.assert_called_once_with(
            "NUR-1", "919876543210", "919876543210"
        )


if __name__ == "__main__":
    unittest.main()
