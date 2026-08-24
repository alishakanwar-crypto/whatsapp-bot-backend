import os
import tempfile
import unittest

import aiosqlite

from app.services.sheet_refresh_service import apply_manual_students


class ManualStudentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.executescript(
            """
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
            CREATE TABLE manual_students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                father_name TEXT DEFAULT '',
                mother_name TEXT DEFAULT '',
                father_mobile TEXT DEFAULT '',
                mother_mobile TEXT DEFAULT '',
                note TEXT DEFAULT '',
                UNIQUE(student_name, grade)
            );
            INSERT INTO manual_students
            (student_name, grade, father_name, mother_name,
             father_mobile, mother_mobile)
            VALUES ('Lakshika', 'Grade 11D', 'Varun', 'Meenakshi',
                    '918930110759', '918221013927');
            """
        )
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        os.unlink(self.db_path)

    async def test_manual_student_is_applied_to_the_student_tables(self):
        applied = await apply_manual_students(self.db)
        self.assertEqual(applied, 1)

        cursor = await self.db.execute(
            "SELECT grade, father_mobile, mother_mobile FROM pi_sheet_students"
        )
        self.assertEqual(
            await cursor.fetchall(),
            [("Grade 11D", "918930110759", "918221013927")],
        )

        cursor = await self.db.execute(
            "SELECT student_name, grade FROM snapshot_access_students"
        )
        self.assertEqual(await cursor.fetchall(), [("Lakshika", "Grade 11D")])

        cursor = await self.db.execute(
            "SELECT phone FROM snapshot_access_grants ORDER BY phone"
        )
        self.assertEqual(
            [row[0] for row in await cursor.fetchall()],
            ["918221013927", "918930110759"],
        )

    async def test_reapplying_after_a_refresh_wipe_restores_access(self):
        await apply_manual_students(self.db)
        # A PI Sheet refresh rewrites both student tables from the sheet.
        await self.db.execute("DELETE FROM pi_sheet_students")
        await self.db.execute("DELETE FROM snapshot_access_students")

        await apply_manual_students(self.db)

        cursor = await self.db.execute("SELECT COUNT(*) FROM pi_sheet_students")
        self.assertEqual((await cursor.fetchone())[0], 1)
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM snapshot_access_students"
        )
        self.assertEqual((await cursor.fetchone())[0], 1)
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM snapshot_access_grants"
        )
        self.assertEqual((await cursor.fetchone())[0], 2)

    async def test_a_student_already_in_the_sheet_is_not_duplicated(self):
        await self.db.execute(
            "INSERT INTO pi_sheet_students "
            "(student_name, grade, father_mobile, mother_mobile) "
            "VALUES ('LAKSHIKA', 'Grade 11D', '918930110759', '918221013927')"
        )
        await apply_manual_students(self.db)

        cursor = await self.db.execute("SELECT COUNT(*) FROM pi_sheet_students")
        self.assertEqual((await cursor.fetchone())[0], 1)


if __name__ == "__main__":
    unittest.main()
