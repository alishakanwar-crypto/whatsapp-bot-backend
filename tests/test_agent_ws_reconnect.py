import asyncio
import unittest

from starlette.websockets import WebSocketDisconnect

from app.routes import agent_ws


class FakeRequestSocket:
    def __init__(self, result=None, error=None, replacement=None):
        self.result = result
        self.error = error
        self.replacement = replacement
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)
        if self.error:
            raise self.error
        if self.replacement is not None:
            agent_ws._agent_ws = self.replacement
        future = agent_ws._pending_requests[payload["request_id"]]
        future.set_result(self.result)


class DisconnectingWebSocket:
    headers = {}

    async def accept(self):
        return None

    async def receive_text(self):
        raise WebSocketDisconnect()


class AgentWebSocketReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        agent_ws._agent_ws = None
        agent_ws._agent_websockets.clear()
        agent_ws._pending_requests.clear()
        agent_ws._pending_request_websockets.clear()
        agent_ws._pending_images.clear()
        agent_ws._queued_snapshots.clear()

    async def asyncTearDown(self):
        agent_ws._agent_ws = None
        agent_ws._agent_websockets.clear()
        agent_ws._pending_requests.clear()
        agent_ws._pending_request_websockets.clear()
        agent_ws._pending_images.clear()
        agent_ws._queued_snapshots.clear()

    async def test_snapshot_retries_after_mid_request_disconnect(self):
        recovered = FakeRequestSocket(
            result={
                "success": True,
                "classroom": "GRADE 5B",
                "image_count": 2,
                "images": [{"description": "G5B C1"}, {"description": "G5B C2"}],
            }
        )
        disconnected = FakeRequestSocket(
            result={"success": False, "error": "Agent disconnected"},
            replacement=recovered,
        )
        agent_ws._agent_websockets.extend([disconnected, recovered])
        agent_ws._agent_ws = disconnected

        result = await agent_ws.request_snapshot("GRADE 5B", timeout=1.0)

        self.assertTrue(result["success"])
        self.assertEqual(result["image_count"], 2)
        self.assertEqual(len(disconnected.sent), 1)
        self.assertEqual(len(recovered.sent), 1)

    async def test_stale_socket_send_uses_another_connected_agent(self):
        recovered = FakeRequestSocket(
            result={
                "success": True,
                "classroom": "GRADE 3C",
                "image_count": 1,
                "images": [{"description": "G3C C1"}],
            }
        )
        stale = FakeRequestSocket(error=RuntimeError("closed"))
        agent_ws._agent_websockets.extend([recovered, stale])
        agent_ws._agent_ws = stale

        result = await agent_ws.request_snapshot("GRADE 3C", timeout=1.0)

        self.assertTrue(result["success"])
        self.assertEqual(len(stale.sent), 1)
        self.assertEqual(len(recovered.sent), 1)

    async def test_disconnect_preserves_other_agent_and_its_request(self):
        surviving = object()
        disconnecting = DisconnectingWebSocket()
        agent_ws._agent_websockets.append(surviving)

        loop = asyncio.get_running_loop()
        disconnected_future = loop.create_future()
        surviving_future = loop.create_future()
        agent_ws._pending_requests.update({
            "disconnected": disconnected_future,
            "surviving": surviving_future,
        })
        agent_ws._pending_request_websockets.update({
            "disconnected": disconnecting,
            "surviving": surviving,
        })
        agent_ws._pending_images.update({
            "disconnected": [],
            "surviving": [],
        })

        await agent_ws.agent_websocket(disconnecting)

        self.assertIs(agent_ws._agent_ws, surviving)
        self.assertEqual(
            disconnected_future.result(),
            {"success": False, "error": "Agent disconnected"},
        )
        self.assertFalse(surviving_future.done())
        self.assertIn("surviving", agent_ws._pending_requests)


if __name__ == "__main__":
    unittest.main()
