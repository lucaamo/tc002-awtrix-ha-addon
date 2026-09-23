from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any

from .errors import validation

MAGIC = b"ATX1"
VERSION = 1
FRAME_TYPE = 1
WIDTH = 52
HEIGHT = 16
RGB_SIZE = WIDTH * HEIGHT * 3
HEADER = struct.Struct("!4sBBHIHHB3x")
TAG_SIZE = 16


def frame_tag(content: bytes, token: str) -> bytes:
    key = token.encode("utf-8")
    if len(key) > 32:
        raise validation("Adapter token must be at most 32 UTF-8 bytes", "token")
    return hashlib.blake2s(content, key=key, digest_size=TAG_SIZE).digest()


def encode_frame(frame: bytes, sequence: int, brightness: int, token: str = "") -> bytes:
    if len(frame) != RGB_SIZE:
        raise validation(f"Frame must contain exactly {RGB_SIZE} RGB bytes", "frame")
    if not 0 <= brightness <= 255:
        raise validation("Brightness must be between 0 and 255", "brightness")
    header = HEADER.pack(
        MAGIC, VERSION, FRAME_TYPE, 0, sequence & 0xFFFFFFFF, WIDTH, HEIGHT, brightness
    )
    content = header + frame
    return content + (frame_tag(content, token) if token else b"")


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    sequence: int
    brightness: int
    pixels: bytes


def decode_frame(datagram: bytes, token: str = "") -> DecodedFrame:
    expected = HEADER.size + RGB_SIZE + (TAG_SIZE if token else 0)
    if len(datagram) != expected:
        raise validation(f"Frame datagram must be {expected} bytes", "datagram")
    content = datagram[: HEADER.size + RGB_SIZE]
    if token and datagram[-TAG_SIZE:] != frame_tag(content, token):
        raise validation("Frame authentication failed", "tag")
    magic, version, message_type, flags, sequence, width, height, brightness = HEADER.unpack(
        content[: HEADER.size]
    )
    if magic != MAGIC or version != VERSION or message_type != FRAME_TYPE or flags != 0:
        raise validation("Unsupported frame header", "header")
    if width != WIDTH or height != HEIGHT:
        raise validation("Unsupported frame dimensions", "dimensions")
    return DecodedFrame(sequence, brightness, content[HEADER.size :])


def sequence_is_newer(candidate: int, previous: int) -> bool:
    difference = (candidate - previous) & 0xFFFFFFFF
    return 0 < difference < 0x80000000


def encode_json_message(
    message_type: str, sequence: int, data: dict[str, Any], token: str = ""
) -> bytes:
    envelope: dict[str, Any] = {
        "version": VERSION,
        "type": message_type,
        "seq": sequence & 0xFFFFFFFF,
        "data": data,
    }
    if token:
        envelope["token"] = token
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 8192:
        raise validation("Control datagram exceeds 8192 bytes", "data")
    return encoded


def decode_json_message(datagram: bytes, token: str = "") -> dict[str, Any]:
    if len(datagram) > 8192:
        raise validation("Event datagram exceeds 8192 bytes", "datagram")
    try:
        message = json.loads(datagram.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise validation("Malformed event JSON", "datagram") from error
    if not isinstance(message, dict):
        raise validation("Event must be an object", "datagram")
    if type(message.get("version")) is not int or message["version"] != VERSION:
        raise validation("Unsupported event protocol version", "version")
    if not isinstance(message.get("type"), str):
        raise validation("Event type must be a string", "type")
    if type(message.get("seq")) is not int or not 0 <= message["seq"] <= 0xFFFFFFFF:
        raise validation("Event seq must be a 32-bit unsigned integer", "seq")
    if not isinstance(message.get("data", {}), dict):
        raise validation("Event data must be an object", "data")
    if token and message.get("token") != token:
        raise validation("Event authentication failed", "token")
    return message
