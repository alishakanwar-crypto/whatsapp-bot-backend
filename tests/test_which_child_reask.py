"""A re-ask of "which child?" must restart the pending row's one-hour window."""
import unittest
from unittest.mock import AsyncMock, patch

from app.routes import webhook


class WhichChildReAskTests(unittest.IsolatedAsyncioTestCase):
    def _pending(self) -> dict:
        return {
            "reply_to": "wamid.1",
            "original_query": f"{webhook._CT_PENDING_PREFIX}who is the class teacher",
        }

    async def test_re_ask_refreshes_that_row_only(self):
        pending = self._pending()

        with patch.object(webhook, "get_pending_query", AsyncMock(return_value=pending)), \
             patch.object(webhook, "refresh_pending_query", AsyncMock()) as refresh, \
             patch.object(webhook, "save_pending_query", AsyncMock()) as save, \
             patch.object(webhook, "delete_pending_query", AsyncMock()) as delete, \
             patch.object(webhook, "_answer_class_teacher_question",
                          AsyncMock(return_value=webhook._CT_ASK_WHICH_CHILD)):
            reply = await webhook.try_answer_pending_class_teacher("919000000000", "my son")

        self.assertIn("could not match", reply)
        refresh.assert_awaited_once_with(
            "919000000000", pending["reply_to"], pending["original_query"]
        )
        save.assert_not_awaited()
        delete.assert_not_awaited()

    async def test_an_answered_question_clears_the_pending_row(self):
        pending = self._pending()

        with patch.object(webhook, "get_pending_query", AsyncMock(return_value=pending)), \
             patch.object(webhook, "refresh_pending_query", AsyncMock()) as refresh, \
             patch.object(webhook, "delete_pending_query", AsyncMock()) as delete, \
             patch.object(webhook, "_answer_class_teacher_question",
                          AsyncMock(return_value="Ms Neha Sharma")):
            reply = await webhook.try_answer_pending_class_teacher("919000000000", "Riya 5A")

        self.assertEqual(reply, "Ms Neha Sharma")
        refresh.assert_not_awaited()
        delete.assert_awaited_once_with("919000000000")

    async def test_a_reply_that_arrived_meanwhile_is_not_undone(self):
        """The stale re-ask must not resurrect a question already dealt with."""
        pending = self._pending()
        await webhook.save_pending_query(
            "919000000000", pending["reply_to"], pending["original_query"]
        )
        await webhook.delete_pending_query("919000000000")

        await webhook.refresh_pending_query(
            "919000000000", pending["reply_to"], pending["original_query"]
        )

        self.assertIsNone(await webhook.get_pending_query("919000000000"))


if __name__ == "__main__":
    unittest.main()
