import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.routes import agent_ws


class QueuedSnapshotDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        agent_ws._agent_ws = object()
        agent_ws._queued_snapshots.clear()
        agent_ws._queue_tasks.clear()
        self._gap = agent_ws._QUEUE_RETRY_GAP
        agent_ws._QUEUE_RETRY_GAP = 0.0

    async def asyncTearDown(self):
        agent_ws._agent_ws = None
        agent_ws._queued_snapshots.clear()
        agent_ws._queue_tasks.clear()
        agent_ws._QUEUE_RETRY_GAP = self._gap

    async def _drain(self):
        await asyncio.gather(*list(agent_ws._queue_tasks))

    async def test_a_queued_photo_is_sent_as_an_image_message(self):
        result = {
            "success": True,
            "images": [{"image_base64": "aaa", "description": "G3C C1"}],
        }
        with patch.object(agent_ws, "request_snapshot",
                          AsyncMock(return_value=result)), \
            patch("app.services.whatsapp_service.upload_base64_image_cloud",
                  AsyncMock(return_value="media-1")) as upload, \
            patch("app.services.whatsapp_service.send_cloud_media",
                  AsyncMock(return_value=True)) as send, \
            patch("app.services.snapshot_audit_service.log_snapshot_request",
                  AsyncMock()) as logged, \
            patch("app.services.snapshot_audit_service.mark_resolved",
                  AsyncMock()) as resolved:
            self.assertTrue(
                agent_ws.queue_snapshot_request("GRADE 3C", "91999", "91999")
            )
            await self._drain()

        upload.assert_awaited_once()
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[1], "image")
        self.assertEqual(send.await_args.kwargs["media_id"], "media-1")
        resolved.assert_awaited_once()
        self.assertEqual(
            logged.await_args.args[2],
            "delivered",
        )
        self.assertEqual(agent_ws._queued_snapshots, [])

    async def test_a_failed_capture_is_tried_again_before_giving_up(self):
        results = [
            {"success": False, "error": "camera busy"},
            {
                "success": True,
                "images": [{"image_base64": "aaa", "description": "G3C C1"}],
            },
        ]
        with patch.object(agent_ws, "request_snapshot",
                          AsyncMock(side_effect=results)) as asked, \
            patch("app.services.whatsapp_service.upload_base64_image_cloud",
                  AsyncMock(return_value="media-1")), \
            patch("app.services.whatsapp_service.send_cloud_media",
                  AsyncMock(return_value=True)), \
            patch("app.services.whatsapp_service.send_whatsapp_message",
                  AsyncMock()) as told, \
            patch("app.services.snapshot_audit_service.log_snapshot_request",
                  AsyncMock()), \
            patch("app.services.snapshot_audit_service.mark_resolved",
                  AsyncMock()):
            agent_ws.queue_snapshot_request("GRADE 3C", "91999", "91999")
            await self._drain()

        self.assertEqual(asked.await_count, 2)
        told.assert_not_awaited()

    async def test_a_parent_promised_a_photo_is_told_when_it_never_comes(self):
        with patch.object(agent_ws, "request_snapshot",
                          AsyncMock(return_value={"success": False,
                                                  "error": "no camera"})), \
            patch("app.services.whatsapp_service.send_whatsapp_message",
                  AsyncMock(return_value=True)) as told, \
            patch("app.services.snapshot_audit_service.log_snapshot_request",
                  AsyncMock()) as logged, \
            patch("app.services.snapshot_audit_service.mark_resolved",
                  AsyncMock()) as resolved:
            agent_ws.queue_snapshot_request("GRADE 3C", "91999", "91999")
            await self._drain()

        told.assert_awaited_once()
        self.assertIn("send your request again", told.await_args.args[1])
        resolved.assert_not_awaited()
        self.assertEqual(logged.await_args.args[2], "capture_failed")
        self.assertEqual(agent_ws._queued_snapshots, [])

    async def test_an_agent_that_never_returns_ends_in_a_message(self):
        agent_ws._agent_ws = None
        with patch.object(agent_ws, "wait_for_agent",
                          AsyncMock(return_value=False)), \
            patch.object(agent_ws, "request_snapshot", AsyncMock()) as asked, \
            patch("app.services.whatsapp_service.send_whatsapp_message",
                  AsyncMock(return_value=True)) as told, \
            patch("app.services.snapshot_audit_service.log_snapshot_request",
                  AsyncMock()), \
            patch("app.services.snapshot_audit_service.mark_resolved",
                  AsyncMock()):
            agent_ws.queue_snapshot_request("GRADE 3C", "91999", "91999")
            await self._drain()

        asked.assert_not_awaited()
        told.assert_awaited_once()

    async def test_the_same_request_is_not_queued_twice(self):
        with patch.object(agent_ws, "wait_for_agent",
                          AsyncMock(return_value=False)), \
            patch("app.services.whatsapp_service.send_whatsapp_message",
                  AsyncMock(return_value=True)), \
            patch("app.services.snapshot_audit_service.log_snapshot_request",
                  AsyncMock()), \
            patch("app.services.snapshot_audit_service.mark_resolved",
                  AsyncMock()):
            agent_ws.queue_snapshot_request("GRADE 3C", "91999", "91999")
            agent_ws.queue_snapshot_request("GRADE 3C", "91999", "91999")
            self.assertEqual(len(agent_ws._queued_snapshots), 1)
            await self._drain()


if __name__ == "__main__":
    unittest.main()
