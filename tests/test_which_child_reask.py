"""A re-ask of "which child?" must restart the pending row's one-hour window."""
import unittest
from unittest.mock import AsyncMock, patch

from app.routes import webhook


class WhichChildReAskTests(unittest.IsolatedAsyncioTestCase):
    async def test_re_ask_saves_the_pending_row_again(self):
        pending = {
            "reply_to": "wamid.1",
            "original_query": f"{webhook._CT_PENDING_PREFIX}who is the class teacher",
        }

        with patch.object(webhook, "get_pending_query", AsyncMock(return_value=pending)), \
             patch.object(webhook, "save_pending_query", AsyncMock()) as save, \
             patch.object(webhook, "delete_pending_query", AsyncMock()) as delete, \
             patch.object(webhook, "_answer_class_teacher_question",
                          AsyncMock(return_value=webhook._CT_ASK_WHICH_CHILD)):
            reply = await webhook.try_answer_pending_class_teacher("919000000000", "my son")

        self.assertIn("could not match", reply)
        save.assert_awaited_once_with(
            "919000000000", pending["reply_to"], pending["original_query"]
        )
        delete.assert_not_awaited()

    async def test_an_answered_question_clears_the_pending_row(self):
        pending = {
            "reply_to": "wamid.1",
            "original_query": f"{webhook._CT_PENDING_PREFIX}who is the class teacher",
        }

        with patch.object(webhook, "get_pending_query", AsyncMock(return_value=pending)), \
             patch.object(webhook, "save_pending_query", AsyncMock()) as save, \
             patch.object(webhook, "delete_pending_query", AsyncMock()) as delete, \
             patch.object(webhook, "_answer_class_teacher_question",
                          AsyncMock(return_value="Ms Neha Sharma")):
            reply = await webhook.try_answer_pending_class_teacher("919000000000", "Riya 5A")

        self.assertEqual(reply, "Ms Neha Sharma")
        save.assert_not_awaited()
        delete.assert_awaited_once_with("919000000000")


if __name__ == "__main__":
    unittest.main()
