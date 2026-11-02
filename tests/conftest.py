from __future__ import annotations

import io

import pytest
from PIL import Image


@pytest.fixture
def jpeg_bytes():
    def make(color=(40, 90, 140)) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (320, 180), color).save(output, "JPEG", quality=75)
        return output.getvalue()

    return make
