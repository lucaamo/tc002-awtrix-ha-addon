from __future__ import annotations

import asyncio
import logging
from typing import Any

from .adapter import (
    AwtrixNgHttpAdapter,
    AwtrixNgMqttAdapter,
    MemoryAdapter,
    StockHttpAdapter,
    UdpAdapter,
)
from .config import BridgeConfig
from .engine import Engine
from .errors import BridgeError
from .http_service import HttpService
from .mqtt_service import MqttService
from .sonos import SonosController
from .studio import StudioManager

LOGGER = logging.getLogger(__name__)
BUTTON_LONG_PRESS_MS = 800
VOLUME_LEVELS = (0, 17, 33, 50, 67, 83, 100)
BRIGHTNESS_STEP = 32


def feedback_volume_step(volume: int) -> int:
    return (max(0, min(100, volume)) * 6 + 50) // 100


def feedback_sound_asset(volume: int) -> str | None:
    step = feedback_volume_step(volume)
    return f"volume_{step}" if step else None


class BridgeService:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.engine = Engine(config)
        self.adapter: (
            UdpAdapter
            | StockHttpAdapter
            | AwtrixNgHttpAdapter
            | AwtrixNgMqttAdapter
            | MemoryAdapter
        )
        if config.adapter.enabled:
            if config.adapter.mode == "stock_http":
                self.adapter = StockHttpAdapter(config.adapter, on_event=self._adapter_event)
            elif config.adapter.mode == "awtrix_ng_http":
                self.adapter = AwtrixNgHttpAdapter(config.adapter, on_event=self._adapter_event)
            elif config.adapter.mode == "awtrix_ng_mqtt":
                self.adapter = AwtrixNgMqttAdapter(config.adapter, on_event=self._adapter_event)
            else:
                self.adapter = UdpAdapter(config.adapter, on_event=self._adapter_event)
        else:
            self.adapter = MemoryAdapter()
        self.mqtt = MqttService(self.engine, config.mqtt, self.adapter)
        self.studio = StudioManager(self.engine)
        self.sonos = SonosController(
            self.engine,
            fetch_entity=self.studio.fetch_entity,
            call_service=self.studio.call_service,
            list_entities=self.studio.list_entities,
        )
        self.http = HttpService(
            self.engine,
            config.http,
            self.adapter,
            self.studio,
            self.mqtt.clear_retained_pushed_app,
            self.sonos,
        )
        self._running = False
        self._render_task: asyncio.Task[None] | None = None
        self._last_notification_generation: int | None = None
        self._notification_audio_active = False
        self._button_pressed_ms: dict[str, int] = {}
        self._sonos_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await self.adapter.start()
        await self.mqtt.start()
        await self.studio.start()
        await self.sonos.start()
        await self.http.start()
        self._running = True
        self._render_task = asyncio.create_task(self._render_loop(), name="render-loop")
        LOGGER.info(
            "TC002 AWTRIX bridge listening on http://%s:%d",
            self.config.http.host,
            self.config.http.port,
        )

    async def stop(self) -> None:
        self._running = False
        if self._render_task is not None:
            self._render_task.cancel()
            await asyncio.gather(self._render_task, return_exceptions=True)
            self._render_task = None
        for task in self._sonos_tasks:
            task.cancel()
        if self._sonos_tasks:
            await asyncio.gather(*self._sonos_tasks, return_exceptions=True)
            self._sonos_tasks.clear()
        await self.http.stop()
        await self.sonos.stop()
        await self.studio.stop()
        await self.adapter.stop()
        await self.mqtt.stop()

    async def run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    async def _render_loop(self) -> None:
        interval = 1.0 / self.config.renderer.fps
        next_device_publish = 0
        while self._running:
            started = asyncio.get_running_loop().time()
            now_ms = self.engine.monotonic_ms()
            self.engine.check_adapter_timeout(now_ms)
            result = self.engine.render(now_ms)
            self.adapter.send_frame(result.frame, int(self.engine.display["brightness"]))
            self._sync_notification_audio()
            if now_ms >= next_device_publish:
                self.engine._changed("device")
                next_device_publish = now_ms + 1000
            remaining = interval - (asyncio.get_running_loop().time() - started)
            await asyncio.sleep(max(0, remaining))

    def _sync_notification_audio(self) -> None:
        if self.config.adapter.mode != "udp":
            return
        active = self.engine.notifications.active
        generation = active.generation if active is not None else None
        if generation == self._last_notification_generation:
            return
        self._last_notification_generation = generation
        if self._notification_audio_active:
            self.adapter.send_control("audio.stop", {})
            self._notification_audio_active = False
        if active is None:
            return
        sound = active.spec.get("sound")
        if sound is not None and self.engine.settings.get("soundEnabled", True):
            self.adapter.send_control(
                "audio.play",
                {
                    "asset": str(sound),
                    "loop": bool(active.spec.get("soundLoop", False)),
                    "volume": int(self.engine.settings.get("mp3Volume", 70)),
                },
            )
            self._notification_audio_active = True

    def _adapter_event(self, event_type: str, data: dict[str, Any]) -> None:
        now_ms = self.engine.monotonic_ms()
        if event_type == "heartbeat":
            self.engine.update_adapter(data, now_ms)
            return
        if event_type == "button":
            name = data.get("button")
            pressed = data.get("pressed")
            if isinstance(name, str) and isinstance(pressed, bool):
                self.engine.update_adapter({}, now_ms)
                self.engine.update_button(name, pressed)
                if name in {"left", "right", "knob"}:
                    if pressed:
                        self._button_pressed_ms.setdefault(name, now_ms)
                    else:
                        started_ms = self._button_pressed_ms.pop(name, None)
                        if started_ms is not None:
                            held_ms = now_ms - started_ms
                            if name == "knob":
                                if held_ms >= int(self.sonos.settings["longPressMs"]):
                                    self._schedule_sonos(self.sonos.toggle_mode())
                                elif self.sonos.active:
                                    self._schedule_sonos(self.sonos.press())
                            else:
                                self._handle_adjustment_button(
                                    name,
                                    held_ms >= BUTTON_LONG_PRESS_MS,
                                    now_ms,
                                )
                if name == "knob" and self.config.compatibility.extensions and self.mqtt.connected:
                    self.mqtt.publish(
                        "extensions/events/knob", "press" if pressed else "release", retain=False
                    )
            return
        if event_type == "knob":
            direction = data.get("direction")
            if direction not in {"clockwise", "anticlockwise"}:
                return
            self.engine.update_adapter({}, now_ms)
            if self.sonos.active:
                action = (
                    self.sonos.next_track()
                    if direction == "clockwise"
                    else self.sonos.previous_track()
                )
                self._schedule_sonos(action)
            elif direction == "clockwise":
                self.engine.next_app(now_ms)
            else:
                self.engine.previous_app(now_ms)
            if (
                self.config.adapter.mode == "udp"
                and self.engine.settings.get("soundEnabled", True)
                and self.engine.settings.get("knobSoundEnabled", True)
            ):
                volume = int(self.engine.settings.get("mp3Volume", 70))
                asset = feedback_sound_asset(volume)
                if asset is not None:
                    self.adapter.send_control(
                        "audio.play",
                        {"asset": asset, "loop": False, "volume": volume},
                    )
            if self.config.compatibility.extensions and self.mqtt.connected:
                self.mqtt.publish("extensions/events/knob", direction, retain=False)
            return
        if event_type == "audio":
            if isinstance(data.get("playing"), bool):
                self.engine.audio["playing"] = data["playing"]
                self.engine._changed("audio")
            return
        if event_type == "error":
            self.engine.update_adapter({"error": data.get("message", "adapter error")}, now_ms)

    def _handle_adjustment_button(self, name: str, long_press: bool, now_ms: int) -> None:
        direction = -1 if name == "left" else 1
        if self.sonos.active:
            self._schedule_sonos(self.sonos.adjust_volume(direction))
            return
        if long_press:
            current = int(self.engine.settings["brightness"])
            value = max(0, min(255, current + direction * BRIGHTNESS_STEP))
            self.engine.patch_settings({"brightness": value})
            return

        current = int(self.engine.settings["mp3Volume"])
        if direction < 0:
            value = next((level for level in reversed(VOLUME_LEVELS) if level < current), 0)
        else:
            value = next((level for level in VOLUME_LEVELS if level > current), 100)
        self.engine.patch_settings({"mp3Volume": value})
        if self.config.adapter.mode == "udp":
            self.adapter.send_control("audio.volume", {"volume": value})
        notification = {
            "name": "tc002_volume_osd",
            "text": f"{value}%",
            "textCase": "asTyped",
            "textCenter": True,
            "textOffsetX": 6,
            "textColor": "#38BDF8",
            "backgroundColor": "#000000",
            "durationMs": 1200,
            "stack": False,
            "scroll": {"mode": "static", "whenFits": "static", "holdMs": 0},
            "progress": value,
            "progressColor": "#38BDF8",
            "progressTrackColor": "#132938",
            "draw": [
                ["rectFill", 2, 6, 3, 5, "#38BDF8"],
                ["line", 5, 6, 8, 3, "#38BDF8"],
                ["line", 8, 3, 8, 12, "#38BDF8"],
                ["line", 8, 12, 5, 10, "#38BDF8"],
                ["line", 11, 5, 11, 10, "#38BDF8"],
                ["line", 14, 3, 14, 12, "#38BDF8"],
            ],
        }
        feedback_asset = feedback_sound_asset(value)
        if feedback_asset is not None:
            notification["sound"] = feedback_asset
        self.engine.notify(notification, now_ms=now_ms)

    def _schedule_sonos(self, action: Any) -> None:
        async def run() -> None:
            try:
                await action
            except BridgeError as error:
                LOGGER.warning("Sonos command failed: %s", error)
                self.engine.notify(
                    {
                        "name": "sonos_error",
                        "text": f"SONOS: {error.message}",
                        "textCase": "asTyped",
                        "textColor": "#FF453A",
                        "backgroundColor": "#000000",
                        "durationMs": 2500,
                        "stack": False,
                    }
                )
            except Exception:
                LOGGER.exception("Unexpected Sonos command failure")

        task = asyncio.create_task(run(), name="sonos-input")
        self._sonos_tasks.add(task)
        task.add_done_callback(self._sonos_tasks.discard)
