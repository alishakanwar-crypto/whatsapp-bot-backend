import time
import unittest

from app.routes import agent_ws


class FakeSocket:
    def __init__(self, send_error: Exception | None = None):
        self.sent: list[dict] = []
        self.closed_with: tuple[int, str] | None = None
        self.send_error = send_error

    async def send_json(self, payload):
        if self.send_error:
            raise self.send_error
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed_with = (code, reason)


class AgentLinkKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        agent_ws._agent_websockets.clear()
        agent_ws._agent_last_message_at.clear()
        self._interval = agent_ws._AGENT_PING_INTERVAL_SECONDS
        agent_ws._AGENT_PING_INTERVAL_SECONDS = 0.01

    async def asyncTearDown(self):
        agent_ws._AGENT_PING_INTERVAL_SECONDS = self._interval
        agent_ws._agent_websockets.clear()
        agent_ws._agent_last_message_at.clear()

    async def test_pings_a_live_link(self):
        socket = FakeSocket()
        agent_ws._agent_websockets.append(socket)
        agent_ws._note_agent_message(socket)

        async def one_ping():
            await agent_ws._keep_agent_link_honest(socket)

        # The socket leaving the registry ends the loop after the first ping.
        import asyncio

        task = asyncio.create_task(one_ping())
        await asyncio.sleep(0.05)
        agent_ws._agent_websockets.remove(socket)
        await task

        self.assertEqual(socket.sent[0], {"type": "ping"})
        self.assertIsNone(socket.closed_with)

    async def test_closes_a_silent_link(self):
        socket = FakeSocket()
        agent_ws._agent_websockets.append(socket)
        agent_ws._agent_last_message_at[id(socket)] = (
            time.time() - agent_ws._AGENT_SILENCE_LIMIT_SECONDS - 5
        )

        await agent_ws._keep_agent_link_honest(socket)

        self.assertIsNotNone(socket.closed_with)
        self.assertEqual(socket.sent, [])

    async def test_health_reports_how_long_the_agent_has_been_offline(self):
        agent_ws._agent_ws = None
        agent_ws._health_state["last_connected_at"] = time.time() - 600
        agent_ws._health_state["last_disconnected_at"] = time.time() - 30

        health = agent_ws.get_health_state()

        self.assertFalse(health["connected"])
        self.assertLess(health["disconnected_seconds"], 60)
        self.assertGreater(health["disconnected_seconds"], 20)


if __name__ == "__main__":
    unittest.main()
