from __future__ import annotations

from io import BytesIO
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

COVER_SIZE = 10


def cover_payload(data: bytes, *, size: int = COVER_SIZE) -> dict[str, Any]:
    """Decode and centre-crop an album image into an RGB888 pixel matrix."""
    if not data:
        raise ValueError("Album artwork is empty")
    try:
        with Image.open(BytesIO(data)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS)
            pixels = []
            for y in range(size):
                for x in range(size):
                    red, green, blue = image.getpixel((x, y))
                    pixels.append((red << 16) | (green << 8) | blue)
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("Album artwork is not a supported image") from error
    return {"width": size, "height": size, "pixels": pixels}
