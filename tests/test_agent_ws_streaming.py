import asyncio
import unittest

from app.routes import agent_ws


class AgentWebSocketStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_image_callback_runs_before_completion(self):
        request_id = "request-1"
        delivered = []

        async def callback(image):
            delivered.append(image)
            image["_delivered"] = True

        agent_ws._pending_images[request_id] = []
        agent_ws._pending_image_callbacks[request_id] = callback
        try:
            await agent_ws._store_snapshot_image({
                "type": "snapshot_image",
                "request_id": request_id,
                "image_index": 0,
                "image_total": 2,
                "filename": "classroom.jpg",
                "image_base64": "aW1hZ2U=",
                "size_bytes": 5,
                "description": "GRADE 4A C1",
            })
            await agent_ws._await_image_deliveries(request_id)

            self.assertEqual(len(delivered), 1)
            self.assertIs(delivered[0], agent_ws._pending_images[request_id][0])
            self.assertTrue(agent_ws._pending_images[request_id][0]["_delivered"])
            self.assertEqual(
                agent_ws._pending_images[request_id][0]["image_total"], 2
            )
        finally:
            agent_ws._pending_images.pop(request_id, None)
            agent_ws._pending_image_callbacks.pop(request_id, None)
            agent_ws._pending_image_deliveries.pop(request_id, None)

    async def test_whatsapp_delivery_does_not_hold_up_the_campus_link(self):
        """A slow WhatsApp send must not stall images arriving behind it."""
        request_id = "request-2"
        release = asyncio.Event()
        started = []

        async def slow_callback(image):
            started.append(image["description"])
            await release.wait()

        agent_ws._pending_images[request_id] = []
        agent_ws._pending_image_callbacks[request_id] = slow_callback
        try:
            for index, desc in enumerate(("GRADE 4A C1", "GRADE 4A C2")):
                await asyncio.wait_for(
                    agent_ws._store_snapshot_image({
                        "request_id": request_id,
                        "image_index": index,
                        "image_total": 2,
                        "filename": f"classroom{index}.jpg",
                        "image_base64": "aW1hZ2U=",
                        "size_bytes": 5,
                        "description": desc,
                    }),
                    timeout=1,
                )

            # Both images are on the server even though nothing has been
            # delivered yet, and the second delivery waits for the first.
            self.assertEqual(len(agent_ws._pending_images[request_id]), 2)
            await asyncio.sleep(0)
            self.assertEqual(started, ["GRADE 4A C1"])

            release.set()
            await agent_ws._await_image_deliveries(request_id)
            self.assertEqual(started, ["GRADE 4A C1", "GRADE 4A C2"])
        finally:
            release.set()
            agent_ws._pending_images.pop(request_id, None)
            agent_ws._pending_image_callbacks.pop(request_id, None)
            agent_ws._pending_image_deliveries.pop(request_id, None)

    async def test_request_finishes_only_once_photos_are_sent(self):
        request_id = "request-3"
        release = asyncio.Event()

        async def slow_callback(image):
            await release.wait()
            image["_delivered"] = True

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        agent_ws._pending_images[request_id] = []
        agent_ws._pending_image_callbacks[request_id] = slow_callback
        agent_ws._pending_requests[request_id] = future
        try:
            await agent_ws._store_snapshot_image({
                "request_id": request_id,
                "image_index": 0,
                "image_total": 1,
                "filename": "classroom.jpg",
                "image_base64": "aW1hZ2U=",
                "size_bytes": 5,
                "description": "GRADE 4A C1",
            })
            completion = asyncio.create_task(
                agent_ws._complete_snapshot_request(request_id, "GRADE 4A")
            )
            await asyncio.sleep(0)
            self.assertFalse(future.done())

            release.set()
            await completion
            result = await future
            self.assertTrue(result["success"])
            self.assertEqual(result["image_count"], 1)
            self.assertTrue(result["images"][0]["_delivered"])
        finally:
            release.set()
            agent_ws._pending_images.pop(request_id, None)
            agent_ws._pending_image_callbacks.pop(request_id, None)
            agent_ws._pending_requests.pop(request_id, None)
            agent_ws._pending_image_deliveries.pop(request_id, None)


if __name__ == "__main__":
    unittest.main()
