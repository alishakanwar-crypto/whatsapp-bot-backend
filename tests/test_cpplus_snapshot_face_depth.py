import base64
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app.routes import gate


class CascadeAt:
    """Fires once, at a fixed box — like the Haar cascade on paving texture."""

    def __init__(self, box):
        self.box = box

    def detectMultiScale(self, image, scaleFactor, minNeighbors, minSize):
        return [self.box]


def _noise_jpeg_b64(height: int, width: int) -> str:
    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", img)
    return base64.b64encode(buf).decode("ascii")


class CpplusSnapshotFaceDepthTests(unittest.TestCase):
    def test_face_low_in_the_crop_is_rejected(self):
        # 874-tall crop with a "face" at y=698: paving below the visitor.
        with patch.object(
            gate, "_get_frontal_cascade", return_value=CascadeAt((414, 698, 62, 62))
        ):
            self.assertFalse(
                gate._has_clear_frontal_face(_noise_jpeg_b64(874, 563))
            )

    def test_face_in_the_upper_crop_still_passes(self):
        with patch.object(
            gate, "_get_frontal_cascade", return_value=CascadeAt((124, 182, 80, 80))
        ):
            self.assertTrue(
                gate._has_clear_frontal_face(_noise_jpeg_b64(740, 439))
            )


if __name__ == "__main__":
    unittest.main()
