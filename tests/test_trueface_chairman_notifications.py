import unittest
from unittest.mock import AsyncMock, patch

from app.routes import trueface


class ChairmanArrivalNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_text_template_when_live_photo_is_missing(self):
        send = AsyncMock(return_value=True)
        upload = AsyncMock()

        with (
            patch.object(trueface, "WHATSAPP_DISABLED", False),
            patch.object(trueface, "CHAIRMAN_PHONE", "919999996562"),
            patch(
                "app.services.whatsapp_service.send_cloud_template_message",
                send,
            ),
            patch(
                "app.services.whatsapp_service.upload_base64_image_cloud",
                upload,
            ),
        ):
            result = await trueface._notify_chairman_arrival(
                "ALISHA KANWAR", "7:00 AM",
            )

        self.assertTrue(result)
        upload.assert_not_awaited()
        send.assert_awaited_once_with(
            to="919999996562",
            template_name=trueface.CHAIRMAN_TEXT_TEMPLATE,
            language_code="en",
            body_params=["Alisha Kanwar", "7:00 AM"],
            header_image_id=None,
        )

    async def test_uses_text_template_when_live_photo_upload_fails(self):
        send = AsyncMock(return_value=True)
        upload = AsyncMock(return_value=None)

        with (
            patch.object(trueface, "WHATSAPP_DISABLED", False),
            patch.object(trueface, "CHAIRMAN_PHONE", "919999996562"),
            patch(
                "app.services.whatsapp_service.send_cloud_template_message",
                send,
            ),
            patch(
                "app.services.whatsapp_service.upload_base64_image_cloud",
                upload,
            ),
        ):
            result = await trueface._notify_chairman_arrival(
                "ALISHA KANWAR", "7:00 AM", "photo-data",
            )

        self.assertTrue(result)
        send.assert_awaited_once_with(
            to="919999996562",
            template_name=trueface.CHAIRMAN_TEXT_TEMPLATE,
            language_code="en",
            body_params=["Alisha Kanwar", "7:00 AM"],
            header_image_id=None,
        )

    async def test_prefers_photo_template_when_upload_succeeds(self):
        send = AsyncMock(return_value=True)
        upload = AsyncMock(return_value="media-id")

        with (
            patch.object(trueface, "WHATSAPP_DISABLED", False),
            patch.object(trueface, "CHAIRMAN_PHONE", "919999996562"),
            patch(
                "app.services.whatsapp_service.send_cloud_template_message",
                send,
            ),
            patch(
                "app.services.whatsapp_service.upload_base64_image_cloud",
                upload,
            ),
        ):
            result = await trueface._notify_chairman_arrival(
                "ALISHA KANWAR", "7:00 AM", "photo-data",
            )

        self.assertTrue(result)
        send.assert_awaited_once_with(
            to="919999996562",
            template_name=trueface.CHAIRMAN_TEMPLATE,
            language_code="en",
            body_params=["Alisha Kanwar", "7:00 AM"],
            header_image_id="media-id",
        )


if __name__ == "__main__":
    unittest.main()
