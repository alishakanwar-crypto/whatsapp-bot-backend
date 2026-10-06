import base64
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app.routes import gate


class FakeCascade:
    """Fires on anything, like the Haar cascade does on paving texture."""

    def detectMultiScale(self, image, scaleFactor, minNeighbors, minSize):
        return [(0, 0, 60, 60)]


def _noise_jpeg_b64(height: int, width: int) -> str:
    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", img)
    return base64.b64encode(buf).decode("ascii")


class CpplusSnapshotShapeTests(unittest.TestCase):
    def test_square_ground_crop_is_rejected(self):
        with patch.object(gate, "_get_frontal_cascade", return_value=FakeCascade()):
            self.assertFalse(gate._has_clear_frontal_face(_noise_jpeg_b64(600, 600)))

    def test_person_shaped_crop_still_passes(self):
        with patch.object(gate, "_get_frontal_cascade", return_value=FakeCascade()):
            self.assertTrue(gate._has_clear_frontal_face(_noise_jpeg_b64(600, 250)))

    def test_empty_crop_is_rejected(self):
        self.assertFalse(gate._has_clear_frontal_face(""))


if __name__ == "__main__":
    unittest.main()
