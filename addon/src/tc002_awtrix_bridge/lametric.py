from __future__ import annotations

import io
import ssl
from dataclasses import dataclass

import aiohttp
import certifi
from PIL import Image, ImageSequence, UnidentifiedImageError

MAX_DOWNLOAD_BYTES = 1_000_000
MAX_FRAMES = 256
SOURCE_SIZE = (8, 8)
FAITHFUL_SIZE = (16, 16)
OPTIMIZED_SIZE = (12, 16)
LAMETRIC_BASE_URL = "https://developer.lametric.com/content/apps/icon_thumbs"
MODE_FAITHFUL_2X = "faithful2x"
MODE_TC002_OPTIMIZED = "tc002Optimized"
SUPPORTED_MODES = {MODE_FAITHFUL_2X, MODE_TC002_OPTIMIZED}


class LametricIconError(Exception):
    """Base error for a rejected or unavailable LaMetric icon."""


class LametricIconNotFound(LametricIconError):
    """The requested icon ID does not exist in the public gallery."""


class LametricIconUnavailable(LametricIconError):
    """The official LaMetric endpoint could not be reached."""


class LametricIconInvalid(LametricIconError):
    """The downloaded asset is not a supported original LaMetric icon."""


@dataclass(frozen=True)
class LametricIcon:
    icon_id: int
    content: bytes
    frame_count: int
    durations_ms: tuple[int, ...]
    source_format: str
    source_url: str
    mode: str = MODE_FAITHFUL_2X
    width: int = FAITHFUL_SIZE[0]
    height: int = FAITHFUL_SIZE[1]


def _scale2x(image: Image.Image) -> Image.Image:
    """Scale RGBA pixel art 2x while refining corners and diagonal edges."""

    source = image.load()
    output = Image.new("RGBA", FAITHFUL_SIZE, (0, 0, 0, 0))
    target = output.load()
    for y in range(image.height):
        for x in range(image.width):
            center = source[x, y]
            top = source[x, y - 1] if y > 0 else center
            left = source[x - 1, y] if x > 0 else center
            right = source[x + 1, y] if x + 1 < image.width else center
            bottom = source[x, y + 1] if y + 1 < image.height else center
            top_left = top_right = bottom_left = bottom_right = center
            if top != bottom and left != right:
                if left == top:
                    top_left = left
                if top == right:
                    top_right = right
                if left == bottom:
                    bottom_left = left
                if bottom == right:
                    bottom_right = right
            target[x * 2, y * 2] = top_left
            target[x * 2 + 1, y * 2] = top_right
            target[x * 2, y * 2 + 1] = bottom_left
            target[x * 2 + 1, y * 2 + 1] = bottom_right
    return output


def _compress_width_symmetric(image: Image.Image, width: int) -> Image.Image:
    """Narrow pixel art with mirrored samples so neither side is favoured."""

    half_source = image.width // 2
    half_target = width // 2
    left = [
        min(half_source - 1, ((index * 2 + 1) * half_source) // (half_target * 2))
        for index in range(half_target)
    ]
    x_indices = left + [image.width - 1 - index for index in reversed(left)]
    source = image.load()
    output = Image.new(image.mode, (width, image.height))
    target = output.load()
    for y in range(image.height):
        for x, source_x in enumerate(x_indices):
            target[x, y] = source[source_x, y]
    return output


def scale_lametric_icon(
    content: bytes, *, mode: str = MODE_FAITHFUL_2X
) -> tuple[bytes, tuple[int, ...], str]:
    """Convert an original 8x8 LaMetric PNG/GIF for the TC002 panel."""

    if not content or len(content) > MAX_DOWNLOAD_BYTES:
        raise LametricIconInvalid("LaMetric icon must be between 1 byte and 1 MB")
    if mode not in SUPPORTED_MODES:
        raise LametricIconInvalid(f"Unsupported LaMetric scaling mode: {mode}")

    try:
        with Image.open(io.BytesIO(content)) as source:
            source_format = (source.format or "").upper()
            if source_format not in {"GIF", "PNG"}:
                raise LametricIconInvalid("LaMetric icon must be a PNG or GIF")
            if source.size != SOURCE_SIZE:
                raise LametricIconInvalid(
                    f"Expected an original 8x8 LaMetric icon, got {source.width}x{source.height}"
                )
            if int(getattr(source, "n_frames", 1)) > MAX_FRAMES:
                raise LametricIconInvalid(f"LaMetric icon exceeds {MAX_FRAMES} frames")

            loop = int(source.info.get("loop", 0))
            frames: list[Image.Image] = []
            durations: list[int] = []
            for frame in ImageSequence.Iterator(source):
                duration = max(20, int(frame.info.get("duration") or 100))
                durations.append(duration)
                source_frame = frame.convert("RGBA")
                if mode == MODE_FAITHFUL_2X:
                    frames.append(source_frame.resize(FAITHFUL_SIZE, Image.Resampling.NEAREST))
                else:
                    frames.append(
                        _compress_width_symmetric(
                            _scale2x(source_frame), OPTIMIZED_SIZE[0]
                        )
                    )
    except LametricIconInvalid:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as error:
        raise LametricIconInvalid("Downloaded LaMetric asset is not a valid image") from error

    if not frames:
        raise LametricIconInvalid("LaMetric icon has no frames")

    output = io.BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=loop,
        disposal=2,
        optimize=False,
    )
    return output.getvalue(), tuple(durations), source_format


async def fetch_lametric_icon(
    icon_id: int, *, mode: str = MODE_FAITHFUL_2X
) -> LametricIcon:
    """Fetch an icon by numeric ID only from LaMetric's official asset host."""

    timeout = aiohttp.ClientTimeout(total=20)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    headers = {"User-Agent": "TC002-AWTRIX-Bridge/1"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for suffix in ("gif", "png"):
                url = f"{LAMETRIC_BASE_URL}/{icon_id}.{suffix}"
                async with session.get(url, ssl=ssl_context) as response:
                    if response.status == 404:
                        continue
                    if response.status != 200:
                        raise LametricIconUnavailable(
                            f"LaMetric returned HTTP {response.status}"
                        )
                    source = await response.content.read(MAX_DOWNLOAD_BYTES + 1)
                converted, durations, source_format = scale_lametric_icon(source, mode=mode)
                return LametricIcon(
                    icon_id=icon_id,
                    content=converted,
                    frame_count=len(durations),
                    durations_ms=durations,
                    source_format=source_format,
                    source_url=url,
                    mode=mode,
                    width=OPTIMIZED_SIZE[0] if mode == MODE_TC002_OPTIMIZED else FAITHFUL_SIZE[0],
                    height=FAITHFUL_SIZE[1],
                )
    except LametricIconError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError) as error:
        raise LametricIconUnavailable("Could not reach the LaMetric icon service") from error

    raise LametricIconNotFound(f"LaMetric icon {icon_id} was not found")
