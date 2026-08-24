import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

from app.routes import webhook


class SiblingSnapshotTests(unittest.IsolatedAsyncioTestCase):
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
                    UNIQUE(student_name, grade, phone)
                );
                """
            )
            await db.executemany(
                "INSERT INTO snapshot_access_students "
                "(student_name, grade, father_mobile, mother_mobile) "
                "VALUES (?, ?, ?, ?)",
                [
                    # Younger sibling's row carries both parents' numbers.
                    ("AARAV JAIN", "Grade 1B", "919811111111", "919822222222"),
                    # Elder sibling's row only carries the father's number.
                    ("AVYAN JAIN", "Grade 5A", "919811111111", ""),
                    ("UNRELATED CHILD", "Grade 2A", "919833333333", ""),
                ],
            )
            await db.commit()
        finally:
            await db.close()

    async def asyncTearDown(self):
        os.unlink(self.db_path)

    async def _children(self, phone):
        async def open_db():
            return await aiosqlite.connect(self.db_path)

        async def no_general_access(_sender):
            return []

        with (
            patch.object(webhook, "get_db", new=open_db),
            patch.object(
                webhook, "_lookup_parent_child_class", new=no_general_access
            ),
        ):
            return await webhook._lookup_snapshot_parent_child_class(phone)

    async def test_mother_sees_both_siblings_despite_missing_number(self):
        children = await self._children("919822222222")
        self.assertEqual(
            {child["grade"] for child in children},
            {"Grade 1B", "Grade 5A"},
        )

    async def test_other_families_are_not_linked(self):
        children = await self._children("919833333333")
        self.assertEqual(
            [child["student_name"] for child in children],
            ["UNRELATED CHILD"],
        )

    async def test_both_classrooms_are_captured_without_asking(self):
        handled_locations = []

        async def fake_handler(sender, message_text, reply_to,
                               forced_location=None):
            handled_locations.append(forced_location)
            return True

        with (
            patch.object(webhook, "send_whatsapp_message", new=AsyncMock()),
            patch.object(
                webhook,
                "detect_and_handle_snapshot_request",
                new=fake_handler,
            ),
        ):
            handled = await webhook._send_all_ward_classrooms(
                "919822222222",
                "show my child",
                "919822222222",
                [
                    {"student_name": "AVYAN JAIN", "grade": "Grade 5A"},
                    {"student_name": "AARAV JAIN", "grade": "Grade 1B"},
                ],
            )

        self.assertTrue(handled)
        self.assertEqual(handled_locations, ["GRADE 5A", "GRADE 1B"])

    async def test_capped_at_three_classrooms(self):
        handled_locations = []

        async def fake_handler(sender, message_text, reply_to,
                               forced_location=None):
            handled_locations.append(forced_location)
            return True

        with (
            patch.object(webhook, "send_whatsapp_message", new=AsyncMock()),
            patch.object(
                webhook,
                "detect_and_handle_snapshot_request",
                new=fake_handler,
            ),
        ):
            await webhook._send_all_ward_classrooms(
                "919822222222",
                "show my child",
                "919822222222",
                [
                    {"student_name": "A", "grade": "Grade 5A"},
                    {"student_name": "B", "grade": "Grade 1B"},
                    {"student_name": "C", "grade": "Nursery 2"},
                    {"student_name": "D", "grade": "Prep 1"},
                ],
            )

        self.assertEqual(len(handled_locations), 3)


if __name__ == "__main__":
    unittest.main()
