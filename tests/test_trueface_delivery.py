import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks

from app.routes import trueface


class FakeCursor:
    async def fetchone(self):
        return None


class FakeDb:
    def __init__(self):
        self.statements = []
        self.commits = 0
        self.closed = False

    async def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return FakeCursor()

    async def commit(self):
        self.commits += 1

    async def close(self):
        self.closed = True


class FakeRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class TrueFaceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_returns_before_send_and_background_persists_success(self):
        event_date = datetime.now(trueface.IST).strftime("%Y-%m-%d")
        event = {
            "pin": "7",
            "name": "TEACHER ONE",
            "timestamp": f"{event_date} 07:00:00",
        }
        request_db = FakeDb()
        worker_db = FakeDb()
        background_tasks = BackgroundTasks()
        send = AsyncMock(return_value=True)

        def discard_task(coro):
            coro.close()

        with (
            patch.object(
                trueface,
                "_get_db",
                new=AsyncMock(side_effect=[request_db, worker_db]),
            ),
            patch.object(
                trueface,
                "_get_teacher",
                new=AsyncMock(
                    return_value={"name": "TEACHER ONE", "phone": "919999999999"}
                ),
            ),
            patch.object(
                trueface,
                "_get_attendance_record",
                new=AsyncMock(return_value=None),
            ),
            patch.object(trueface, "_log_mood_from_trueface", new=AsyncMock()),
            patch.object(
                trueface,
                "_log_trueface_to_gate_and_sighting",
                new=AsyncMock(),
            ),
            patch.object(trueface, "_send_arrival_whatsapp", send),
            patch.object(
                trueface.asyncio,
                "ensure_future",
                side_effect=discard_task,
            ),
        ):
            result = await trueface.receive_trueface_event(
                FakeRequest(event),
                background_tasks,
            )

            self.assertEqual(result["results"][0]["whatsapp"], "queued")
            send.assert_not_awaited()
            self.assertEqual(len(background_tasks.tasks), 1)

            task = background_tasks.tasks[0]
            await task.func(*task.args, **task.kwargs)

        send.assert_awaited_once()
        self.assertTrue(
            any(
                "UPDATE trueface_attendance SET arrival_whatsapp = 1" in sql
                for sql, _ in worker_db.statements
            )
        )
        self.assertEqual(worker_db.commits, 1)
        self.assertTrue(worker_db.closed)


if __name__ == "__main__":
    unittest.main()
