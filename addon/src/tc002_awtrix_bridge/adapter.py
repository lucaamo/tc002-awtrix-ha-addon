from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import secrets
import socket
import time
from collections.abc import Callable
from typing import Any

import aiohttp
from PIL import Image

from .config import AdapterConfig
from .errors import BridgeError
from .protocol import (
    decode_json_message,
    encode_frame,
    encode_json_message,
    sequence_is_newer,
)

LOGGER = logging.getLogger(__name__)
FRAME_WIDTH = 52
FRAME_HEIGHT = 16
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 3


class UdpAdapter(asyncio.DatagramProtocol):
    def __init__(
        self,
        config: AdapterConfig,
        *,
        on_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.transport: asyncio.DatagramTransport | None = None
        self.sequence = 0
        self.session_id = secrets.token_hex(16)
        self.last_sender: tuple[str, int] | None = None
        self.last_event_sequence: int | None = None
        self.last_event_uptime_ms: int | None = None
        self._hello_sequence: int | None = None
        self._hello_confirmed = False
        self._next_hello_at = 0.0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(
            lambda: self,
            local_addr=(self.config.listen_host, self.config.event_port),
            family=socket.AF_INET,
        )

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self._send_hello()

    def _send_hello(self) -> None:
        self._hello_sequence = self.send_control(
            "hello", {"heartbeatIntervalMs": 2000, "session": self.session_id}
        )
        self._next_hello_at = time.monotonic() + 2.0

    def connection_lost(self, error: Exception | None) -> None:
        self.transport = None
        if error:
            LOGGER.warning("TC002 adapter UDP endpoint lost: %s", error)

    def error_received(self, error: Exception) -> None:
        LOGGER.warning("TC002 adapter UDP error: %s", error)

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        if address[0] != self.config.device_host:
            LOGGER.warning("Ignored TC002 event from non-allow-listed address %s", address[0])
            return
        try:
            message = decode_json_message(data, self.config.token)
        except BridgeError as error:
            LOGGER.warning("Ignored invalid TC002 event: %s", error)
            return
        sequence = message["seq"]
        data_payload = message.get("data", {})
        uptime = data_payload.get("uptimeMs") if message["type"] == "heartbeat" else None
        companion_restarted = (
            type(uptime) is int
            and self.last_event_uptime_ms is not None
            and uptime < self.last_event_uptime_ms
        )
        if (
            self.last_event_sequence is not None
            and not companion_restarted
            and not sequence_is_newer(sequence, self.last_event_sequence)
        ):
            LOGGER.debug("Ignored duplicate/out-of-order TC002 event seq=%s", sequence)
            return
        self.last_event_sequence = sequence
        if type(uptime) is int:
            self.last_event_uptime_ms = uptime
        self.last_sender = address
        if message["type"] == "ack" and data_payload.get("replySeq") == self._hello_sequence:
            self._hello_confirmed = True
        if companion_restarted:
            self._hello_confirmed = False
        if message["type"] == "heartbeat":
            data_payload = {**data_payload, "transport": "udp"}
            if isinstance(data_payload.get("adapterVersion"), str):
                data_payload["appVersion"] = data_payload["adapterVersion"]
        self.on_event(message["type"], data_payload)

    def send_frame(self, frame: bytes, brightness: int) -> None:
        if self.transport is None:
            return
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        if not self._hello_confirmed and time.monotonic() >= self._next_hello_at:
            self._send_hello()
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        packet = encode_frame(frame, self.sequence, brightness, self.config.token)
        self.transport.sendto(packet, (self.config.device_host, self.config.frame_port))

    def send_control(self, command: str, data: dict[str, Any] | None = None) -> int:
        if self.transport is None:
            return self.sequence
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        packet = encode_json_message(command, self.sequence, data or {}, self.config.token)
        self.transport.sendto(packet, (self.config.device_host, self.config.frame_port))
        return self.sequence

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    async def stop(self) -> None:
        self.close()


def stock_http_payload(frame: bytes, brightness: int, duration: int) -> dict[str, Any]:
    """Encode one bridge RGB888 frame for the stock TC002 Custom-App endpoint."""
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"frame must contain exactly {FRAME_BYTES} bytes")
    if not 0 <= brightness <= 255:
        raise ValueError("brightness must be between 0 and 255")
    if brightness < 255:
        frame = bytes((channel * brightness + 127) // 255 for channel in frame)
    image = Image.frombytes("RGB", (FRAME_WIDTH, FRAME_HEIGHT), frame)
    encoded = io.BytesIO()
    image.save(encoded, format="PNG", compress_level=1)
    data_uri = "data:image/png;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")
    return {
        "duration": duration,
        "text": [],
        "image": [{"data": data_uri, "position": [0, 0]}],
        "draw": [],
    }


class StockHttpAdapter:
    """Render frames through the HTTP Custom-App API included in stock TC002 firmware."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        on_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.sequence = 0
        self._running = False
        self._session: aiohttp.ClientSession | None = None
        self._frame_event = asyncio.Event()
        self._latest_frame: tuple[bytes, int] | None = None
        self._last_queued: tuple[bytes, int] | None = None
        self._last_queued_at = 0.0
        self._last_heartbeat_at = 0.0
        self._frame_task: asyncio.Task[None] | None = None
        self._probe_task: asyncio.Task[None] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.config.device_host}:{self.config.http_port}"

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.config.http_timeout)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._running = True
        self._frame_task = asyncio.create_task(self._frame_worker(), name="stock-http-frames")
        self._probe_task = asyncio.create_task(self._probe_worker(), name="stock-http-probe")

    def send_frame(self, frame: bytes, brightness: int) -> None:
        if not self._running:
            return
        if len(frame) != FRAME_BYTES:
            LOGGER.warning("Ignored invalid stock HTTP frame length: %d", len(frame))
            return
        value = (bytes(frame), brightness)
        now = asyncio.get_running_loop().time()
        refresh_interval = max(1.0, self.config.blackout_timeout / 2)
        if value == self._last_queued and now - self._last_queued_at < refresh_interval:
            return
        self._last_queued = value
        self._last_queued_at = now
        self._latest_frame = value
        self._frame_event.set()

    async def _frame_worker(self) -> None:
        minimum_interval = 1.0 / self.config.http_max_fps
        next_send = 0.0
        while self._running:
            await self._frame_event.wait()
            self._frame_event.clear()
            if not self._running:
                break
            delay = next_send - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            value = self._latest_frame
            if value is None:
                continue
            try:
                await self._post_frame(*value)
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                LOGGER.warning("TC002 stock HTTP frame failed: %s", error)
                self.on_event("error", {"message": str(error)})
            next_send = asyncio.get_running_loop().time() + minimum_interval
            if self._latest_frame != value:
                self._frame_event.set()

    async def _post_frame(self, frame: bytes, brightness: int) -> None:
        assert self._session is not None
        duration = max(1, math.ceil(self.config.blackout_timeout))
        payload = stock_http_payload(frame, brightness, duration)
        url = f"{self.base_url}/api/custom"
        params = {"name": self.config.http_app}
        async with self._session.post(url, params=params, json=payload) as response:
            result = await response.json(content_type=None)
            if response.status != 200 or not isinstance(result, dict) or result.get("code") != 200:
                raise ValueError(f"HTTP {response.status}: {result!r}")
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        self._report_heartbeat({"transport": "stock_http", "error": None})

    async def _probe_worker(self) -> None:
        interval = max(1.0, min(5.0, self.config.heartbeat_timeout / 2))
        while self._running:
            try:
                await self._probe()
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                LOGGER.warning("TC002 stock HTTP probe failed: %s", error)
                self.on_event("error", {"message": str(error)})
            await asyncio.sleep(interval)

    async def _probe(self) -> None:
        assert self._session is not None
        async with self._session.get(f"{self.base_url}/getBase") as response:
            result = await response.json(content_type=None)
            if response.status != 200 or not isinstance(result, dict):
                raise ValueError(f"HTTP {response.status}: invalid /getBase response")
        data: dict[str, Any] = {"transport": "stock_http", "error": None}
        if isinstance(result.get("mcuVer"), str):
            data["mcuVersion"] = result["mcuVer"]
        if isinstance(result.get("appVer"), str):
            data["appVersion"] = result["appVer"]
        if isinstance(result.get("ip"), str):
            data["ipAddress"] = result["ip"]
        self._report_heartbeat(data, force=True)

    def _report_heartbeat(self, data: dict[str, Any], *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and now - self._last_heartbeat_at < 1.0:
            return
        self._last_heartbeat_at = now
        self.on_event("heartbeat", data)

    def send_control(self, command: str, data: dict[str, Any] | None = None) -> int:
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        LOGGER.info("Stock HTTP adapter does not implement control command %s", command)
        return self.sequence

    async def stop(self) -> None:
        self._running = False
        self._frame_event.set()
        tasks = [task for task in (self._frame_task, self._probe_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._frame_task = None
        self._probe_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None


def awtrix_ng_payload(frame: bytes, brightness: int, lifetime_ms: int) -> dict[str, Any]:
    """Keep the bridge's 52x16 RGB frame intact in an NG pushed app."""
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"frame must contain exactly {FRAME_BYTES} bytes")
    if not 0 <= brightness <= 255:
        raise ValueError("brightness must be between 0 and 255")
    if brightness < 255:
        frame = bytes((channel * brightness + 127) // 255 for channel in frame)
    return {
        "draw": [
            ["bitmap", 0, 0, FRAME_WIDTH, FRAME_HEIGHT, base64.b64encode(frame).decode("ascii")]
        ],
        "lifetimeMs": lifetime_ms,
        "lifetimeExpiry": "remove",
    }


class AwtrixNgHttpAdapter(StockHttpAdapter):
    """Publish bridge frames to AWTRIX NG without changing its native apps."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        on_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        super().__init__(config, on_event=on_event)
        self._aggregate_removed = False

    async def _post_frame(self, frame: bytes, brightness: int) -> None:
        assert self._session is not None
        if not self.config.ng_aggregate_enabled:
            if not self._aggregate_removed:
                url = f"{self.base_url}/api/v1/apps/pushed/{self.config.http_app}"
                async with self._session.delete(url) as response:
                    if response.status not in {200, 404}:
                        result = await response.text()
                        raise ValueError(f"HTTP {response.status}: {result!r}")
                self._aggregate_removed = True
            self._report_heartbeat({"transport": "awtrix_ng_http", "error": None})
            return
        lifetime_ms = max(1000, math.ceil(self.config.blackout_timeout * 1000))
        payload = awtrix_ng_payload(frame, brightness, lifetime_ms)
        url = f"{self.base_url}/api/v1/apps/pushed/{self.config.http_app}"
        async with self._session.put(url, json=payload) as response:
            result = await response.json(content_type=None)
            if (
                response.status != 200
                or not isinstance(result, dict)
                or result.get("ok") is not True
            ):
                raise ValueError(f"HTTP {response.status}: {result!r}")
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        self._report_heartbeat({"transport": "awtrix_ng_http", "error": None})

    async def _probe(self) -> None:
        assert self._session is not None
        async with self._session.get(f"{self.base_url}/api/v1/display/screen") as response:
            screen = await response.json(content_type=None)
            if response.status != 200 or not isinstance(screen, dict):
                raise ValueError(f"HTTP {response.status}: invalid display response")
            if screen.get("width") != FRAME_WIDTH or screen.get("height") != FRAME_HEIGHT:
                raise ValueError("AWTRIX NG display is not 52x16")
        async with self._session.get(f"{self.base_url}/api/v1/device") as response:
            device = await response.json(content_type=None)
            if response.status != 200 or not isinstance(device, dict):
                raise ValueError(f"HTTP {response.status}: invalid device response")
        data: dict[str, Any] = {"transport": "awtrix_ng_http", "error": None}
        if isinstance(device.get("version"), str):
            data["appVersion"] = device["version"]
        if isinstance(device.get("ipAddress"), str):
            data["ipAddress"] = device["ipAddress"]
        self._report_heartbeat(data, force=True)


class AwtrixNgMqttAdapter:
    """Send coalesced bridge frames through the existing broker connection."""

    def __init__(
        self, config: AdapterConfig, *, on_event: Callable[[str, dict[str, Any]], None]
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.client: Any = None
        self.prefix = config.ng_mqtt_prefix
        self.online = False
        self.last_frame: bytes | None = None
        self.last_brightness = -1
        self.last_sent = 0.0
        self.last_heartbeat = 0.0
        self.switched = False
        self.aggregate_removed = False
        self.sequence = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        if self.client is not None and self.online:
            self.client.publish(f"{self.prefix}/cmd/apps/pushed/{self.config.http_app}", b"", qos=1)
        self.online = False
        self.client = None

    def attach(self, client: Any) -> None:
        self.client = client

    def set_online(self, online: bool) -> None:
        self.online = online
        self.switched = False
        self.aggregate_removed = False
        self.last_frame = None
        if online:
            self.on_event("heartbeat", {"transport": "awtrix_ng_mqtt", "error": None})

    def send_frame(self, frame: bytes, brightness: int) -> None:
        if not self.online or self.client is None:
            return
        now = time.monotonic()
        if now - self.last_heartbeat >= 2.0:
            self.on_event("heartbeat", {"transport": "awtrix_ng_mqtt", "error": None})
            self.last_heartbeat = now
        if not self.config.ng_aggregate_enabled:
            if not self.aggregate_removed:
                result = self.client.publish(
                    f"{self.prefix}/cmd/apps/pushed/{self.config.http_app}",
                    b"",
                    qos=1,
                    retain=False,
                )
                if result.rc != 0:
                    LOGGER.warning("AWTRIX NG MQTT aggregate cleanup failed: %s", result.rc)
                    return
                self.aggregate_removed = True
            return
        interval = 1 / self.config.http_max_fps
        refresh = max(1.0, self.config.blackout_timeout / 2)
        if now - self.last_sent < interval:
            return
        if (
            frame == self.last_frame
            and brightness == self.last_brightness
            and now - self.last_sent < refresh
        ):
            return
        payload = awtrix_ng_payload(
            frame, brightness, max(1000, math.ceil(self.config.blackout_timeout * 1000))
        )
        # Let NG rotate to its other apps, including Berry controls such as Sonos.
        payload["durationMs"] = 10000
        topic = f"{self.prefix}/cmd/apps/pushed/{self.config.http_app}"
        result = self.client.publish(
            topic, json.dumps(payload, separators=(",", ":")), qos=0, retain=False
        )
        if result.rc != 0:
            LOGGER.warning("AWTRIX NG MQTT frame publish failed: %s", result.rc)
            return
        if not self.switched:
            self.client.publish(
                f"{self.prefix}/cmd/apps/switch",
                json.dumps({"name": self.config.http_app, "fast": True}),
                qos=1,
                retain=False,
            )
            self.switched = True
        self.last_frame = frame
        self.last_brightness = brightness
        self.last_sent = now
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF

    def send_control(self, command: str, data: dict[str, Any] | None = None) -> int:
        return self.sequence


class MemoryAdapter:
    """Deterministic adapter used by unit, golden and service tests."""

    def __init__(self) -> None:
        self.frames: list[tuple[bytes, int]] = []
        self.controls: list[tuple[str, dict[str, Any]]] = []

    async def start(self) -> None:
        return None

    def send_frame(self, frame: bytes, brightness: int) -> None:
        self.frames.append((frame, brightness))

    def send_control(self, command: str, data: dict[str, Any] | None = None) -> int:
        self.controls.append((command, data or {}))
        return len(self.controls)

    def close(self) -> None:
        return None

    async def stop(self) -> None:
        self.close()
