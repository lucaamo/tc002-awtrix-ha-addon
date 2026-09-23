from __future__ import annotations

import base64
import binascii
import io
import math
import random
import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image, ImageSequence

from .errors import BridgeError
from .validation import parse_color

RGB = tuple[int, int, int]

# Physical-pixel refinements for the TC002's 52x16 panel. The source AWTRIX
# fonts are only 6 pixels high; doubling these glyphs produces square 2x2
# blocks instead of readable shapes. The native bitmaps keep the original
# advances while using the TC002's intermediate pixels.
TC002_NATIVE_GLYPHS = {
    "m": (
        "##.###.###.",
        "###.###.###",
        "##..##...##",
        "##..##...##",
        "##..##...##",
        "##..##...##",
        "##..##...##",
        "##..##...##",
    ),
    "³": (
        "####.",
        "...#.",
        "..##.",
        "...#.",
        "...#.",
        "####.",
    ),
}


def rgb(value: Any, fallback: int = 0xFFFFFF) -> RGB:
    try:
        packed = parse_color(value)
    except BridgeError:
        packed = fallback
    assert packed is not None
    return ((packed >> 16) & 255, (packed >> 8) & 255, packed & 255)


def _symmetric_nearest_indices(source_size: int, target_size: int) -> list[int]:
    """Map even pixel-art dimensions without favouring either edge."""

    if source_size % 2 or target_size % 2:
        return [
            min(source_size - 1, ((index * 2 + 1) * source_size) // (target_size * 2))
            for index in range(target_size)
        ]
    source_half = source_size // 2
    target_half = target_size // 2
    left = [
        min(source_half - 1, ((index * 2 + 1) * source_half) // (target_half * 2))
        for index in range(target_half)
    ]
    return left + [source_size - 1 - index for index in reversed(left)]


def _resize_pixel_art_symmetric(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Nearest-neighbour resize with mirrored sampling around the centre."""

    width, height = size
    x_indices = _symmetric_nearest_indices(image.width, width)
    y_indices = _symmetric_nearest_indices(image.height, height)
    source = image.load()
    output = Image.new(image.mode, size)
    target = output.load()
    for target_y, source_y in enumerate(y_indices):
        for target_x, source_x in enumerate(x_indices):
            target[target_x, target_y] = source[source_x, source_y]
    return output


class Canvas:
    def __init__(self, width: int, height: int, color: RGB = (0, 0, 0)) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(color * (width * height))

    def set(self, x: int, y: int, color: RGB) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            index = (y * self.width + x) * 3
            self.pixels[index : index + 3] = bytes(color)

    def get(self, x: int, y: int) -> RGB:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return (0, 0, 0)
        index = (y * self.width + x) * 3
        return tuple(self.pixels[index : index + 3])  # type: ignore[return-value]

    def fill(self, color: RGB) -> None:
        self.pixels[:] = bytes(color) * (self.width * self.height)

    def line(self, x0: int, y0: int, x1: int, y1: int, color: RGB) -> None:
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            self.set(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy

    def rect(self, x: int, y: int, width: int, height: int, color: RGB, fill: bool = False) -> None:
        if width <= 0 or height <= 0:
            return
        if fill:
            for yy in range(y, y + height):
                self.line(x, yy, x + width - 1, yy, color)
            return
        self.line(x, y, x + width - 1, y, color)
        self.line(x, y + height - 1, x + width - 1, y + height - 1, color)
        self.line(x, y, x, y + height - 1, color)
        self.line(x + width - 1, y, x + width - 1, y + height - 1, color)

    def circle(self, cx: int, cy: int, radius: int, color: RGB, fill: bool = False) -> None:
        if radius < 0:
            return
        x, y, decision = radius, 0, 1 - radius
        while x >= y:
            if fill:
                self.line(cx - x, cy + y, cx + x, cy + y, color)
                self.line(cx - x, cy - y, cx + x, cy - y, color)
                self.line(cx - y, cy + x, cx + y, cy + x, color)
                self.line(cx - y, cy - x, cx + y, cy - x, color)
            else:
                for px, py in (
                    (cx + x, cy + y),
                    (cx + y, cy + x),
                    (cx - y, cy + x),
                    (cx - x, cy + y),
                    (cx - x, cy - y),
                    (cx - y, cy - x),
                    (cx + y, cy - x),
                    (cx + x, cy - y),
                ):
                    self.set(px, py, color)
            y += 1
            if decision <= 0:
                decision += 2 * y + 1
            else:
                x -= 1
                decision += 2 * (y - x) + 1

    def blit(self, source: Canvas, x: int, y: int) -> None:
        for sy in range(source.height):
            for sx in range(source.width):
                self.set(x + sx, y + sy, source.get(sx, sy))


@dataclass(slots=True)
class Glyph:
    width: int
    height: int
    x_offset: int
    y_offset: int
    advance: int
    rows: list[int]


class BdfFont:
    def __init__(self, path: Path, *, fallback: BdfFont | None = None) -> None:
        self.ascent = 6
        self.height = 6
        self.glyphs: dict[int, Glyph] = {}
        self.fallback = fallback
        self._load(path)

    def _load(self, path: Path) -> None:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.startswith("FONT_ASCENT "):
                self.ascent = int(line.split()[1])
            elif line.startswith("FONTBOUNDINGBOX "):
                self.height = int(line.split()[2])
            elif line.startswith("STARTCHAR"):
                encoding, advance = -1, 0
                width = height = x_offset = y_offset = 0
                rows: list[int] = []
                index += 1
                while index < len(lines) and lines[index] != "ENDCHAR":
                    current = lines[index]
                    if current.startswith("ENCODING "):
                        encoding = int(current.split()[1])
                    elif current.startswith("DWIDTH "):
                        advance = int(current.split()[1])
                    elif current.startswith("BBX "):
                        _, w, h, xo, yo = current.split()
                        width, height, x_offset, y_offset = map(int, (w, h, xo, yo))
                    elif current == "BITMAP":
                        index += 1
                        while index < len(lines) and lines[index] != "ENDCHAR":
                            rows.append(int(lines[index], 16))
                            index += 1
                        break
                    index += 1
                if encoding >= 0:
                    self.glyphs[encoding] = Glyph(
                        width, height, x_offset, y_offset, max(1, advance), rows
                    )
            index += 1

    def glyph(self, character: str) -> Glyph:
        encoding = 0x00B5 if ord(character) == 0x03BC else ord(character)
        glyph = self.glyphs.get(encoding)
        if glyph is not None:
            return glyph
        if self.fallback is not None:
            return self.fallback.glyph(character)
        return self.glyphs.get(ord("?"), Glyph(1, 1, 0, 0, 2, [0]))

    def text_width(self, text: str) -> int:
        return sum(self.glyph(character).advance for character in text)

    def draw(
        self,
        canvas: Canvas,
        x: int,
        y: int,
        text: str,
        color: RGB,
        *,
        scale: int = 1,
        letter_spacing: int = 0,
        clip_left: int | None = None,
        clip_right: int | None = None,
    ) -> int:
        scale = max(1, scale)
        cursor = x
        for character_index, character in enumerate(text):
            glyph = self.glyph(character)
            top = y + self.ascent - glyph.y_offset - glyph.height
            native_rows = TC002_NATIVE_GLYPHS.get(character) if scale == 2 else None
            if native_rows is not None:
                pixel_y = y + (top - y) * scale
                for row_index, row in enumerate(native_rows):
                    for column, value in enumerate(row):
                        if value != "#":
                            continue
                        target_x = cursor + glyph.x_offset * scale + column
                        if clip_left is not None and target_x < clip_left:
                            continue
                        if clip_right is not None and target_x >= clip_right:
                            continue
                        canvas.set(target_x, pixel_y + row_index, color)
                cursor += glyph.advance * scale
                if character_index < len(text) - 1:
                    cursor += letter_spacing
                continue
            byte_width = max(1, (glyph.width + 7) // 8)
            for row_index, row_bits in enumerate(glyph.rows):
                total_bits = byte_width * 8
                for column in range(glyph.width):
                    if row_bits & (1 << (total_bits - 1 - column)):
                        pixel_x = cursor + (glyph.x_offset + column) * scale
                        pixel_y = y + (top - y + row_index) * scale
                        for block_y in range(scale):
                            for block_x in range(scale):
                                target_x = pixel_x + block_x
                                if clip_left is not None and target_x < clip_left:
                                    continue
                                if clip_right is not None and target_x >= clip_right:
                                    continue
                                canvas.set(target_x, pixel_y + block_y, color)
            cursor += glyph.advance * scale
            if character_index < len(text) - 1:
                cursor += letter_spacing
        return cursor - x


class IconCache:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory
        self._images: dict[str, tuple[list[Image.Image] | None, float]] = {}

    @staticmethod
    def _safe_identifier(value: str) -> bool:
        return (
            bool(value)
            and len(value) <= 64
            and all(char.isalnum() or char in "_-" for char in value)
        )

    @staticmethod
    def _decode(opened: Image.Image) -> list[Image.Image]:
        images = []
        pixels = 0
        for frame in ImageSequence.Iterator(opened):
            pixels += frame.width * frame.height
            if frame.width > 128 or frame.height > 128 or pixels > 262144 or len(images) >= 256:
                raise ValueError("Icon exceeds decoded image budget")
            images.append(frame.convert("RGBA").copy())
        return images

    def get(self, value: Any) -> list[Image.Image] | None:
        if value is None:
            return None
        key = str(value)
        if key in self._images:
            images, attempted = self._images[key]
            if images is not None:
                return [image.copy() for image in images]
            if time.monotonic() - attempted < 5:
                return None
        images: list[Image.Image] | None = None
        try:
            if len(key) > 64:
                data = base64.b64decode(key, validate=True)
                if len(data) <= 1_000_000:
                    with Image.open(io.BytesIO(data)) as opened:
                        images = self._decode(opened)
            elif self.directory is not None and self._safe_identifier(key):
                for suffix in (".gif", ".jpg", ".jpeg"):
                    candidate = self.directory / f"{key}{suffix}"
                    if candidate.is_file() and candidate.stat().st_size <= 1_000_000:
                        with Image.open(candidate) as opened:
                            images = self._decode(opened)
                        break
        except (OSError, ValueError, binascii.Error, Image.DecompressionBombError):
            images = None
        if key not in self._images and len(self._images) >= 32:
            self._images.pop(next(iter(self._images)))
        self._images[key] = (images, time.monotonic())
        return [image.copy() for image in images] if images else None

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._images.clear()
        else:
            self._images.pop(key, None)


@dataclass(slots=True)
class RenderResult:
    frame: bytes
    scroll_complete: bool
    logical_width: int
    logical_height: int


class Renderer:
    WIDTH = 52
    HEIGHT = 16

    def __init__(self, mode: str = "awtrix_compatible", icon_directory: Path | None = None) -> None:
        if mode not in {"awtrix_compatible", "tc002_native"}:
            raise ValueError("invalid renderer mode")
        self.mode = mode
        asset_dir = Path(__file__).with_name("assets")
        self.small = BdfFont(asset_dir / "awtrix.bdf")
        self.large = BdfFont(asset_dir / "MatrixChunky6.bdf")
        self.native_small = BdfFont(asset_dir / "PixbarCompact9.bdf")
        self.native_large = BdfFont(asset_dir / "PixbarSans12.bdf")
        self.native_digits = BdfFont(asset_dir / "PixbarDigits14.bdf")
        self.font_families = {
            "awtrix_1x": (self.small,) * 3,
            "silkscreen": (
                BdfFont(asset_dir / "SilkscreenCompact8.bdf", fallback=self.native_small),
                BdfFont(asset_dir / "SilkscreenLarge14.bdf", fallback=self.native_large),
                BdfFont(asset_dir / "SilkscreenDigits14.bdf", fallback=self.native_digits),
            ),
            "scientifica": (
                BdfFont(asset_dir / "ScientificaRegular11.bdf", fallback=self.native_small),
            ) * 3,
            "scientifica_italic": (
                BdfFont(asset_dir / "ScientificaItalic11.bdf", fallback=self.native_small),
            ) * 3,
        }
        self.icons = IconCache(icon_directory)

    def render(
        self,
        spec: dict[str, Any],
        *,
        render_key: str,
        elapsed_ms: int,
        settings: dict[str, Any],
        indicators: list[dict[str, Any]] | None = None,
    ) -> RenderResult:
        # Per-app templates never mutate the global typography or stored payload.
        template = spec.get("displayTemplate", "inherit")
        if self.mode == "tc002_native" and template in {"focus", "labelled"}:
            settings = {**settings, "fontFamily": "silkscreen", "displayLayout": "singleLine"}
            spec = {**spec, "font": "large"}
            if template == "labelled":
                settings["displayLayout"] = "twoLine"
                spec.update({"_twoLineLayout": True, "_headerText": spec.get("displayTitle") or "VALORE"})
        logical_width, logical_height = (32, 8) if self.mode == "awtrix_compatible" else (52, 16)
        canvas = Canvas(logical_width, logical_height, rgb(spec.get("backgroundColor"), 0))
        self._effect(canvas, spec, elapsed_ms)
        text_color = rgb(spec.get("textColor", settings.get("textColor", "#FFFFFF")))

        if not spec.get("textInFront", False):
            self._draw_commands(canvas, spec.get("draw", []), text_color)
            self._charts(canvas, spec, text_color)

        content_left = 0
        sonos_volume = spec.get("_sonosVolumeIcon")
        if isinstance(sonos_volume, (int, float)) and not isinstance(sonos_volume, bool):
            content_left = self._draw_sonos_volume_icon(
                canvas, elapsed_ms, max(0, min(100, round(float(sonos_volume))))
            )
        sonos_music = spec.get("_sonosMusicIcon")
        if isinstance(sonos_music, bool):
            content_left = self._draw_sonos_music_icon(canvas, elapsed_ms, sonos_music)
        icon_frames = self.icons.get(spec.get("icon"))
        if icon_frames:
            content_left = self._draw_icon(canvas, icon_frames, spec, elapsed_ms)
        internal_content_left = spec.get("_contentLeft", 0)
        if isinstance(internal_content_left, int) and not isinstance(internal_content_left, bool):
            content_left = max(content_left, min(canvas.width - 1, internal_content_left))

        complete = self._draw_text(
            canvas,
            spec,
            text_color,
            content_left=content_left,
            elapsed_ms=elapsed_ms,
            settings=settings,
        )

        if spec.get("textInFront", False):
            self._draw_commands(canvas, spec.get("draw", []), text_color)
            self._charts(canvas, spec, text_color)

        self._progress(canvas, spec)
        self._overlay(canvas, str(spec.get("overlay", "")), elapsed_ms)

        output = Canvas(self.WIDTH, self.HEIGHT)
        offset_x = 10 if logical_width == 32 else 0
        offset_y = 4 if logical_height == 8 else 0
        output.blit(canvas, offset_x, offset_y)
        self._indicators(output, indicators or [], elapsed_ms)
        return RenderResult(bytes(output.pixels), complete, logical_width, logical_height)

    def _draw_icon(
        self, canvas: Canvas, images: list[Image.Image], spec: dict[str, Any], elapsed_ms: int
    ) -> int:
        image = images[(elapsed_ms // 100) % len(images)]
        width, height = image.size
        if width >= canvas.width:
            target_width = canvas.width
            content_left = 0
        else:
            if canvas.height == 8:
                target_width = min(8, width)
            elif spec.get("_twoLineLayout"):
                target_width = min(
                    12,
                    width if height >= canvas.height else max(width, round(width * 1.5)),
                )
            elif height >= canvas.height:
                # Full-height TC002 assets carry an intentional width. In
                # particular, optimized LaMetric icons are 12×16 so the text
                # gains four columns without shifting the icon off-centre.
                target_width = min(16, width)
            else:
                target_width = min(16, max(width, round(width * 1.5)))
            content_left = target_width + 1
        if height > canvas.height or width != target_width:
            target_height = min(canvas.height, max(1, round(height * target_width / max(width, 1))))
            image = _resize_pixel_art_symmetric(image, (target_width, target_height))
        x = int(spec.get("iconOffsetX", 0))
        y = max(0, (canvas.height - image.height) // 2)
        for iy in range(image.height):
            for ix in range(image.width):
                red, green, blue, alpha = image.getpixel((ix, iy))
                if alpha == 0:
                    continue
                if alpha < 255:
                    background = canvas.get(x + ix, y + iy)
                    inverse = 255 - alpha
                    red = (red * alpha + background[0] * inverse + 127) // 255
                    green = (green * alpha + background[1] * inverse + 127) // 255
                    blue = (blue * alpha + background[2] * inverse + 127) // 255
                canvas.set(x + ix, y + iy, (red, green, blue))
        return content_left

    @staticmethod
    def _draw_sonos_volume_icon(canvas: Canvas, elapsed_ms: int, volume: int) -> int:
        """Draw a compact animated speaker/equalizer for the Sonos volume OSD."""

        cyan = (56, 189, 248)
        blue = (37, 99, 235)
        canvas.rect(1, 6, 3, 5, cyan, True)
        canvas.line(4, 6, 7, 3, blue)
        canvas.line(7, 3, 7, 12, blue)
        canvas.line(7, 12, 4, 10, blue)

        phase = (elapsed_ms // 140) % 3
        heights = (3, 7, 5)
        colors = ((53, 230, 180), (247, 201, 72), (255, 69, 164))
        active_bars = 0 if volume == 0 else min(3, 1 + (volume - 1) // 34)
        for index, x in enumerate((9, 11, 13)):
            height = heights[(index + phase) % len(heights)]
            color = colors[index] if index < active_bars else tuple(
                channel // 5 for channel in colors[index]
            )
            top = 8 - height // 2
            canvas.line(x, top, x, top + height - 1, color)
        return 15

    @staticmethod
    def _draw_sonos_music_icon(canvas: Canvas, elapsed_ms: int, playing: bool) -> int:
        """Draw animated multicolor double notes for the normal Sonos page."""

        phase = (elapsed_ms // 180) % 3 if playing else 0
        colors = ((53, 230, 180), (56, 189, 248), (255, 69, 164))
        left = colors[phase]
        right = colors[(phase + 1) % 3]
        beam = colors[(phase + 2) % 3]

        canvas.line(3, 3, 8, 2, beam)
        canvas.line(3, 4, 8, 3, beam)
        canvas.line(3, 3, 3, 11, left)
        canvas.line(8, 2, 8, 10, right)
        canvas.circle(2, 12, 2, left, True)
        canvas.circle(7, 11, 2, right, True)

        sparkles = ((0, 3), (10, 6), (10, 13))
        for index, (x, y) in enumerate(sparkles):
            color = colors[(index + phase) % 3]
            canvas.set(x, y, color)
            if index == phase:
                canvas.set(x, y + 1 if y < 15 else y - 1, color)
        return 12

    @staticmethod
    def _plain_text(value: Any) -> str:
        if isinstance(value, list):
            return "".join(
                str(fragment.get("text", "")) for fragment in value if isinstance(fragment, dict)
            )
        return "" if value is None else str(value)

    def _draw_text(
        self,
        canvas: Canvas,
        spec: dict[str, Any],
        color: RGB,
        *,
        content_left: int,
        elapsed_ms: int,
        settings: dict[str, Any],
    ) -> bool:
        text = self._plain_text(spec.get("text", ""))
        text_case = spec.get("textCase", "inherit")
        if text_case == "upper" or (text_case == "inherit" and settings.get("uppercase", True)):
            text = text.upper()
        family_name = str(settings.get("fontFamily", "scientifica"))
        use_native_font = self.mode == "tc002_native" and family_name != "awtrix"
        time_date_font_size = spec.get("_timeDateFontSize")
        use_faithful_digits = (
            self.mode == "tc002_native"
            and spec.get("_fontRole") == "digits"
            and (
                time_date_font_size == "large"
                or (time_date_font_size is None and family_name == "silkscreen")
            )
        )
        if use_faithful_digits:
            # Large Time and Date use the clean AWTRIX 3x5 glyphs rendered as
            # exact 2x2 pixel blocks, matching the former Silkscreen layout.
            font = self.small
            font_scale = 2
        elif self.mode == "tc002_native" and time_date_font_size == "small":
            # The built-in selector is intentionally independent of the font
            # used by other apps: Small is the current Scientifica face.
            font = self.font_families["scientifica"][0]
            font_scale = 1
        elif use_native_font:
            small_font, large_font, digits_font = self.font_families.get(
                family_name, self.font_families["silkscreen"]
            )
            if spec.get("_fontRole") == "digits":
                font = digits_font
            else:
                font = large_font if spec.get("font") == "large" else small_font
            font_scale = 1
        else:
            font = self.large if spec.get("font") == "large" else self.small
            font_scale = 2 if self.mode == "tc002_native" else 1
        default_letter_spacing = (
            -1
            if self.mode == "tc002_native" and family_name in {"awtrix", "silkscreen"}
            else 0
        )
        letter_spacing = int(spec.get("_letterSpacing", default_letter_spacing))
        if (
            use_native_font
            and family_name in {"awtrix_1x", "silkscreen", "scientifica"}
            and settings.get("displayLayout") == "twoLine"
            and spec.get("_twoLineLayout")
            and spec.get("_headerText")
        ):
            faithful_1x = family_name == "awtrix_1x"
            return self._draw_two_line_text(
                canvas,
                spec,
                header_font=(
                    self.font_families["awtrix_1x"][0]
                    if faithful_1x
                    else self.font_families["silkscreen"][0]
                ),
                body_font=self.font_families[family_name][0],
                body_color=color,
                content_left=content_left,
                elapsed_ms=elapsed_ms,
                settings=settings,
                body_letter_spacing=letter_spacing,
                header_letter_spacing=0 if faithful_1x else -1,
                header_y=-1 if faithful_1x else -4,
                body_y=7 if faithful_1x else 4,
            )
        if spec.get("displayTemplate") == "focus" and use_native_font:
            viewport = canvas.width - content_left
            compact = self.font_families["silkscreen"][0]
            if (self._tracked_width(font, text, letter_spacing) > viewport
                    and self._tracked_width(compact, text, letter_spacing) <= viewport):
                font = compact
        font_height = font.height * font_scale
        width = font.text_width(text) * font_scale
        if text:
            width += (len(text) - 1) * letter_spacing
        viewport = max(1, canvas.width - content_left)
        scroll = settings.get("scroll", {}).copy()
        scroll.update(spec.get("scroll", {}))
        when_fits = scroll.get("whenFits", "static")
        mode = scroll.get("mode", "wrap")
        speed = max(1, int(scroll.get("speed", 100)))
        hold = max(0, int(scroll.get("holdMs", 1000)))
        repeat = max(1, int(spec.get("repeat", 1)))
        should_scroll = width > viewport or when_fits == "scroll"
        if mode == "static" or not should_scroll:
            x = content_left + int(spec.get("textOffsetX", 0))
            if spec.get("textCenter", False):
                x = content_left + max(0, (viewport - width) // 2) + int(spec.get("textOffsetX", 0))
            self._draw_colored_text(
                canvas,
                font,
                x,
                max(0, (canvas.height - font_height) // 2),
                spec,
                text,
                color,
                scale=font_scale,
                letter_spacing=letter_spacing,
                clip_left=content_left,
                clip_right=canvas.width,
            )
            return elapsed_ms >= hold

        gap = max(0, int(scroll.get("gap", 8)))
        direction = scroll.get("direction", "left")
        active_ms = max(0, elapsed_ms - hold)
        step = active_ms // speed
        if mode in {"wrap", "loop"}:
            distance = width + gap
            cycle = step // max(1, distance)
            position = step % max(1, distance)
            base = (
                content_left - position if direction == "left" else content_left + position - width
            )
            y = max(0, (canvas.height - font_height) // 2)
            self._draw_colored_text(
                canvas,
                font,
                base,
                y,
                spec,
                text,
                color,
                scale=font_scale,
                letter_spacing=letter_spacing,
                clip_left=content_left,
                clip_right=canvas.width,
            )
            if mode == "wrap":
                secondary = base + distance if direction == "left" else base - distance
                self._draw_colored_text(
                    canvas,
                    font,
                    secondary,
                    y,
                    spec,
                    text,
                    color,
                    scale=font_scale,
                    letter_spacing=letter_spacing,
                    clip_left=content_left,
                    clip_right=canvas.width,
                )
            complete = cycle >= repeat
        else:  # bounce
            distance = max(1, width - viewport)
            cycle_length = distance * 2
            cycle = step // cycle_length
            position = step % cycle_length
            offset = position if position <= distance else cycle_length - position
            base = (
                content_left - offset if direction == "left" else content_left - distance + offset
            )
            self._draw_colored_text(
                canvas,
                font,
                base,
                max(0, (canvas.height - font_height) // 2),
                spec,
                text,
                color,
                scale=font_scale,
                letter_spacing=letter_spacing,
                clip_left=content_left,
                clip_right=canvas.width,
            )
            complete = cycle >= repeat
        return complete

    @staticmethod
    def _tracked_width(font: BdfFont, text: str, letter_spacing: int, scale: int = 1) -> int:
        if not text:
            return 0
        return max(0, font.text_width(text) * scale + (len(text) - 1) * letter_spacing)

    @classmethod
    def _fit_line(
        cls, font: BdfFont, text: str, width: int, letter_spacing: int = 0
    ) -> str:
        if cls._tracked_width(font, text, letter_spacing) <= width:
            return text
        candidate = text.rstrip()
        while candidate and cls._tracked_width(font, candidate, letter_spacing) > width:
            candidate = candidate[:-1].rstrip()
        return candidate

    def _draw_two_line_text(
        self,
        canvas: Canvas,
        spec: dict[str, Any],
        *,
        header_font: BdfFont,
        body_font: BdfFont,
        body_color: RGB,
        content_left: int,
        elapsed_ms: int,
        settings: dict[str, Any],
        body_letter_spacing: int,
        header_letter_spacing: int,
        header_y: int,
        body_y: int,
    ) -> bool:
        header = str(spec.get("_headerText", "")).upper()
        body = str(spec.get("_twoLineBody", self._plain_text(spec.get("text", ""))))
        text_case = spec.get("textCase", "inherit")
        if text_case == "upper" or (text_case == "inherit" and settings.get("uppercase", True)):
            body = body.upper()

        viewport = max(1, canvas.width - content_left)
        scroll = settings.get("scroll", {}).copy()
        scroll.update(spec.get("scroll", {}))
        when_fits = scroll.get("whenFits", "static")
        mode = scroll.get("mode", "wrap")
        speed = max(1, int(scroll.get("speed", 100)))
        hold = max(0, int(scroll.get("holdMs", 1000)))
        gap = max(0, int(scroll.get("gap", 8)))
        direction = scroll.get("direction", "left")

        header_width = self._tracked_width(header_font, header, header_letter_spacing)
        header_color = rgb(spec.get("_headerColor"), 0x8FA3B8)
        # Silkscreen Compact uses y=-4/4 to reserve rows 0..4 and 5..15.
        # Faithful AWTRIX 1x uses its natural five-pixel glyphs at y=-1/7,
        # leaving the two lines distinct without scaling either bitmap.
        if bool(spec.get("_scrollHeader")) and header_width > viewport:
            active_ms = max(0, elapsed_ms - hold)
            step = active_ms // speed
            if step < header_width:
                header_x = (
                    content_left - step
                    if direction == "left"
                    else content_left + step - header_width
                )
                header_font.draw(
                    canvas,
                    header_x,
                    header_y,
                    header,
                    header_color,
                    letter_spacing=header_letter_spacing,
                    clip_left=content_left,
                    clip_right=canvas.width,
                )
            else:
                resting_header = str(spec.get("_headerRestText", header)).upper()
                resting_header = self._fit_line(
                    header_font, resting_header, viewport, header_letter_spacing
                )
                resting_width = self._tracked_width(
                    header_font, resting_header, header_letter_spacing
                )
                header_x = content_left + max(0, (viewport - resting_width) // 2)
                header_font.draw(
                    canvas,
                    header_x,
                    header_y,
                    resting_header,
                    header_color,
                    letter_spacing=header_letter_spacing,
                    clip_left=content_left,
                    clip_right=canvas.width,
                )
        else:
            header = self._fit_line(header_font, header, viewport, header_letter_spacing)
            header_width = self._tracked_width(header_font, header, header_letter_spacing)
            header_x = content_left + max(0, (viewport - header_width) // 2)
            header_font.draw(
                canvas,
                header_x,
                header_y,
                header,
                header_color,
                letter_spacing=header_letter_spacing,
                clip_left=content_left,
                clip_right=canvas.width,
            )

        body_width = self._tracked_width(body_font, body, body_letter_spacing)
        repeat = max(1, int(spec.get("repeat", 1)))
        should_scroll = body_width > viewport or when_fits == "scroll"
        if mode == "static" or not should_scroll:
            body_x = content_left + max(0, (viewport - body_width) // 2)
            body_font.draw(
                canvas,
                body_x,
                body_y,
                body,
                body_color,
                letter_spacing=body_letter_spacing,
                clip_left=content_left,
                clip_right=canvas.width,
            )
            return elapsed_ms >= hold

        active_ms = max(0, elapsed_ms - hold)
        step = active_ms // speed
        if mode in {"wrap", "loop"}:
            distance = body_width + gap
            cycle = step // max(1, distance)
            position = step % max(1, distance)
            if bool(spec.get("_stopBodyAfterRepeat")) and cycle >= repeat:
                base = content_left + max(0, (viewport - body_width) // 2)
                body_font.draw(
                    canvas,
                    base,
                    body_y,
                    body,
                    body_color,
                    letter_spacing=body_letter_spacing,
                    clip_left=content_left,
                    clip_right=canvas.width,
                )
                return True
            base = (
                content_left - position
                if direction == "left"
                else content_left + position - body_width
            )
            body_font.draw(
                canvas,
                base,
                body_y,
                body,
                body_color,
                letter_spacing=body_letter_spacing,
                clip_left=content_left,
                clip_right=canvas.width,
            )
            if mode == "wrap":
                secondary = base + distance if direction == "left" else base - distance
                body_font.draw(
                    canvas,
                    secondary,
                    4,
                    body,
                    body_color,
                    letter_spacing=body_letter_spacing,
                    clip_left=content_left,
                    clip_right=canvas.width,
                )
            return cycle >= repeat

        distance = max(1, body_width - viewport)
        cycle_length = distance * 2
        cycle = step // cycle_length
        position = step % cycle_length
        if bool(spec.get("_stopBodyAfterRepeat")) and cycle >= repeat:
            base = content_left + max(0, (viewport - body_width) // 2)
            body_font.draw(
                canvas,
                base,
                4,
                body,
                body_color,
                letter_spacing=body_letter_spacing,
                clip_left=content_left,
                clip_right=canvas.width,
            )
            return True
        offset = position if position <= distance else cycle_length - position
        base = content_left - offset if direction == "left" else content_left - distance + offset
        body_font.draw(
            canvas,
            base,
            4,
            body,
            body_color,
            letter_spacing=body_letter_spacing,
            clip_left=content_left,
            clip_right=canvas.width,
        )
        return cycle >= repeat

    def _draw_colored_text(
        self,
        canvas: Canvas,
        font: BdfFont,
        x: int,
        y: int,
        spec: dict[str, Any],
        text: str,
        default_color: RGB,
        *,
        scale: int = 1,
        letter_spacing: int = 0,
        clip_left: int | None = None,
        clip_right: int | None = None,
    ) -> None:
        fragments = spec.get("text")
        if not isinstance(fragments, list):
            font.draw(
                canvas,
                x,
                y,
                text,
                default_color,
                scale=scale,
                letter_spacing=letter_spacing,
                clip_left=clip_left,
                clip_right=clip_right,
            )
            return
        cursor = x
        uppercase = text != self._plain_text(fragments)
        visible_fragments = [fragment for fragment in fragments if str(fragment.get("text", ""))]
        for fragment_index, fragment in enumerate(visible_fragments):
            raw = str(fragment.get("text", ""))
            value = raw.upper() if uppercase else raw
            fragment_color = rgb(fragment.get("color"), int.from_bytes(bytes(default_color), "big"))
            cursor += font.draw(
                canvas,
                cursor,
                y,
                value,
                fragment_color,
                scale=scale,
                clip_left=clip_left,
                clip_right=clip_right,
            )
            if fragment_index < len(visible_fragments) - 1:
                cursor += letter_spacing

    def _draw_commands(self, canvas: Canvas, commands: Any, default_color: RGB) -> None:
        if not isinstance(commands, list):
            return
        for command in commands:
            if not isinstance(command, list) or not command or not isinstance(command[0], str):
                continue
            try:
                name = command[0]
                if name == "pixels":
                    color = default_color if command[1] is None else rgb(command[1])
                    for index in range(2, len(command) - 1, 2):
                        canvas.set(int(command[index]), int(command[index + 1]), color)
                    continue
                color = default_color
                if name != "bitmap" and len(command) > {
                    "pixel": 3,
                    "line": 5,
                    "rect": 5,
                    "rectFill": 5,
                    "circle": 4,
                    "circleFill": 4,
                    "text": 4,
                }.get(name, 999):
                    color = rgb(command[-1])
                if name == "pixel":
                    canvas.set(int(command[1]), int(command[2]), color)
                elif name == "line":
                    canvas.line(*(int(v) for v in command[1:5]), color)
                elif name in {"rect", "rectFill"}:
                    canvas.rect(*(int(v) for v in command[1:5]), color, name == "rectFill")
                elif name in {"circle", "circleFill"}:
                    canvas.circle(*(int(v) for v in command[1:4]), color, name == "circleFill")
                elif name == "text":
                    self.small.draw(
                        canvas, int(command[1]), int(command[2]) - 5, str(command[3]), color
                    )
                elif name == "bitmap":
                    self._bitmap(canvas, command)
            except (TypeError, ValueError, IndexError):
                continue

    @staticmethod
    def _bitmap(canvas: Canvas, command: list[Any]) -> None:
        x, y, width, height = (int(value) for value in command[1:5])
        data = command[5]
        colors: list[RGB] = []
        if isinstance(data, str):
            decoded = base64.b64decode(data, validate=True)
            colors = [tuple(decoded[i : i + 3]) for i in range(0, len(decoded) - 2, 3)]  # type: ignore[list-item]
        elif isinstance(data, list):
            colors = [rgb(item) for item in data]
        for index, color in enumerate(colors[: width * height]):
            canvas.set(x + index % width, y + index // width, color)

    def _charts(self, canvas: Canvas, spec: dict[str, Any], default_color: RGB) -> None:
        values = spec.get("barChart")
        if isinstance(values, list) and values:
            maximum = max(values) if spec.get("chartAutoscale", True) else 100
            minimum = min(values) if spec.get("chartAutoscale", True) else 0
            span = max(1, maximum - minimum)
            color = rgb(spec.get("chartColor"), int.from_bytes(bytes(default_color), "big"))
            start = max(0, canvas.width - len(values))
            for offset, value in enumerate(values[-canvas.width :]):
                height = round((float(value) - minimum) * (canvas.height - 1) / span) + 1
                canvas.line(
                    start + offset, canvas.height - 1, start + offset, canvas.height - height, color
                )
        values = spec.get("lineChart")
        if isinstance(values, list) and len(values) >= 2:
            maximum = max(values) if spec.get("chartAutoscale", True) else 100
            minimum = min(values) if spec.get("chartAutoscale", True) else 0
            span = max(1, maximum - minimum)
            color = rgb(spec.get("chartColor"), int.from_bytes(bytes(default_color), "big"))
            points = []
            for x, value in enumerate(values[-canvas.width :]):
                y = canvas.height - 1 - round((float(value) - minimum) * (canvas.height - 1) / span)
                points.append((x + max(0, canvas.width - len(values)), y))
            for left, right in pairwise(points):
                canvas.line(*left, *right, color)

    @staticmethod
    def _progress(canvas: Canvas, spec: dict[str, Any]) -> None:
        if "progress" not in spec:
            return
        progress = float(spec["progress"])
        if progress < 0:
            return
        progress = max(0.0, min(100.0, progress))
        track = rgb(spec.get("progressTrackColor"), 0x202020)
        active = rgb(spec.get("progressColor"), 0x00FF00)
        canvas.line(0, canvas.height - 1, canvas.width - 1, canvas.height - 1, track)
        end = round((canvas.width - 1) * progress / 100)
        if progress > 0:
            canvas.line(0, canvas.height - 1, end, canvas.height - 1, active)

    @staticmethod
    def _effect(canvas: Canvas, spec: dict[str, Any], elapsed_ms: int) -> None:
        effect = str(spec.get("effect", "")).lower()
        if not effect:
            return
        phase = elapsed_ms // max(20, int(100 / max(0.1, float(spec.get("effectSpeed", 1)))))
        rng = random.Random((phase << 8) ^ sum(ord(char) for char in effect))
        for y in range(canvas.height):
            for x in range(canvas.width):
                color: RGB | None = None
                if effect in {"checkerboard", "theaterchase"}:
                    if (x + y + phase) % 2 == 0:
                        color = (32, 32, 32)
                elif effect in {"movingline", "pingpong"}:
                    if x == phase % canvas.width:
                        color = (0, 64, 255)
                elif effect in {"matrix", "twinklingstars", "fireworks"} and rng.random() < 0.08:
                    color = (
                        (0, rng.randrange(80, 256), 0) if effect == "matrix" else (255, 255, 255)
                    )
                elif effect in {
                    "colorwaves",
                    "plasma",
                    "plasmacloud",
                    "pacifica",
                    "swirlin",
                    "swirlout",
                }:
                    angle = (x * 17 + y * 29 + phase * 7) * math.pi / 128
                    color = (
                        round((math.sin(angle) + 1) * 24),
                        round((math.sin(angle + 2.1) + 1) * 24),
                        round((math.sin(angle + 4.2) + 1) * 24),
                    )
                if color is not None:
                    canvas.set(x, y, color)

    @staticmethod
    def _overlay(canvas: Canvas, overlay: str, elapsed_ms: int) -> None:
        if overlay not in {"rain", "snow", "drizzle", "storm", "thunder", "frost"}:
            return
        phase = elapsed_ms // 100
        rng = random.Random(phase)
        count = max(1, canvas.width // (3 if overlay in {"rain", "storm"} else 5))
        for _ in range(count):
            x = rng.randrange(canvas.width)
            y = rng.randrange(canvas.height)
            color = (100, 150, 255) if overlay != "snow" else (220, 220, 255)
            canvas.set(x, y, color)
            if overlay in {"rain", "storm"}:
                canvas.set(x, y - 1, color)
        if overlay == "thunder" and phase % 23 == 0:
            for x in range(canvas.width):
                for y in range(canvas.height):
                    current = canvas.get(x, y)
                    canvas.set(x, y, tuple(min(255, value + 96) for value in current))  # type: ignore[arg-type]
        if overlay == "frost":
            for x in range(canvas.width):
                canvas.set(x, 0, (150, 200, 255))

    @staticmethod
    def _indicators(output: Canvas, indicators: list[dict[str, Any]], elapsed_ms: int) -> None:
        positions = [(0, 0), (output.width - 1, 0), (output.width - 1, output.height - 1)]
        for index, indicator in enumerate(indicators[:3]):
            if not indicator.get("on", False):
                continue
            blink = int(indicator.get("blinkMs", 0) or 0)
            if blink and (elapsed_ms // blink) % 2:
                continue
            output.set(*positions[index], rgb(indicator.get("color"), 0xFFFFFF))
