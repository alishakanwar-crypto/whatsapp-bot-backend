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

            self.assertEqual(len(delivered), 1)
            self.assertIs(delivered[0], agent_ws._pending_images[request_id][0])
            self.assertTrue(agent_ws._pending_images[request_id][0]["_delivered"])
            self.assertEqual(
                agent_ws._pending_images[request_id][0]["image_total"], 2
            )
        finally:
            agent_ws._pending_images.pop(request_id, None)
            agent_ws._pending_image_callbacks.pop(request_id, None)


if __name__ == "__main__":
    unittest.main()
