from __future__ import annotations

import copy
import logging
import math
import os
import re
import socket
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .config import BridgeConfig
from .errors import BridgeError, not_found, unsupported, validation
from .notifications import NotificationManager
from .pages import PageManager
from .persistence import AtomicJsonStore
from .renderer import Renderer, RenderResult
from .validation import (
    DEFAULT_SETTINGS,
    DISPLAY_LAYOUTS,
    FONT_FAMILIES,
    TRANSITIONS,
    parse_color,
    validate_app_name,
    validate_notification_payload,
    validate_pushed_payload,
    validate_settings_patch,
)

StateListener = Callable[[str], None]
LOGGER = logging.getLogger(__name__)
AUDIO_ASSET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LEGACY_FONT_FAMILIES = {"pixbar", "tiny5", "dotgothic", "jersey"}
MUSIC_PAGE_NAMES = {"mediaplayer", "MusicaSonos", "ha11_musica"}

# Short, display-oriented labels for the two-line Silkscreen layout. The
# aliases are removed from the beginning of the existing MQTT text so Home
# Assistant automations can keep publishing their AWTRIX-compatible payloads.
TWO_LINE_PAGE_LABELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "sensor_awtrix_co2": ("CO2", ("CO2",)),
    "sensor_awtrix_pm2_5": ("PM2.5", ("PM2.5",)),
    "sensor_awtrix_umidita": ("UMIDITA'", ("UMIDITA'", "UMIDITA")),
    "sensor_energia_attiva_prelevata_": ("ENERGIA", ("ENERGIA OGGI", "OGGI", "ENERGIA")),
    "sensor_homey_alfa_measure_power": ("POTENZA", ("POTENZA",)),
    "sensor_smart_thermostat_x_temper": ("CASA", ("CASA",)),
    "weather_meteo_home": ("METEO", ("METEO",)),
    "PesoVittoria": ("PESO", ("PESO VITTORIA", "PESO")),
    "birthday": ("AUGURI", ("COMPLEANNO", "AUGURI")),
    "mediaplayer": ("MUSICA", ("MUSICA",)),
}


class Engine:
    def __init__(self, config: BridgeConfig, *, now_ms: int | None = None) -> None:
        self.config = config
        self.started_monotonic = time.monotonic()
        self.store = AtomicJsonStore(config.persistence.directory)
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        stored_settings = self.store.load("settings", {})
        if isinstance(stored_settings, dict):
            migrated_font = stored_settings.get("fontFamily") in LEGACY_FONT_FAMILIES
            if migrated_font:
                stored_settings = {**stored_settings, "fontFamily": "scientifica"}
            try:
                self.settings.update(validate_settings_patch(stored_settings))
                if migrated_font:
                    self.store.save("settings", self.settings)
            except BridgeError as error:
                LOGGER.warning("Ignoring invalid persisted settings: %s", error)
        order_state = self.store.load("app_order", {"order": ["Time", "Date"], "disabled": []})
        self.pages = PageManager()
        if isinstance(order_state, dict):
            try:
                self.pages.set_order(
                    order_state.get("order", ["Time", "Date"]), order_state.get("disabled", [])
                )
            except (BridgeError, AttributeError) as error:
                LOGGER.warning("Ignoring invalid persisted app order: %s", error)
        current_ms = self.monotonic_ms() if now_ms is None else now_ms
        self.pages.page_started_ms = current_ms
        self.notifications = NotificationManager()
        self.renderer = Renderer(
            config.renderer.mode,
            icon_directory=Path(config.persistence.directory) / "icons",
        )
        self.display: dict[str, Any] = {
            "power": True,
            "brightness": int(self.settings["brightness"]),
            "overlay": "",
            "overlaySettings": {},
            "moodlight": None,
        }
        self.indicators = [
            {"on": False, "color": "#000000", "blinkMs": 0, "fadeMs": 0} for _ in range(3)
        ]
        self.audio: dict[str, Any] = {
            "playing": False,
            "source": None,
            "volume": int(self.settings["mp3Volume"]),
            "backend": "tc002",
        }
        self.adapter: dict[str, Any] = {
            "connected": False,
            "lastSeenMs": None,
            "transport": None,
            "mcuVersion": None,
            "appVersion": None,
            "batteryPercent": None,
            "ipAddress": None,
            "wifiRssi": None,
            "error": None,
        }
        self.buttons = {"left": False, "select": False, "right": False}
        self.last_frame = bytes(52 * 16 * 3)
        self.frame_sequence = 0
        self.message_count = 0
        self.mqtt_connected = False
        self._listeners: list[StateListener] = []
        self._notification_started_ms: int | None = None
        self._transition_frame: bytes | None = None
        self._transition_started_ms: int | None = None
        self._transition_effect: str | None = None
        self._page_render_token: tuple[str, int, int] | None = None
        self._page_wait_for_scroll = False
        self._page_scroll_complete = False
        self._exclusive_page_name: str | None = None
        self._exclusive_page_spec: dict[str, Any] | None = None
        self._exclusive_page_started_ms = current_ms
        stored_controls = self.store.load("device_controls", {})
        if isinstance(stored_controls, dict):
            try:
                if stored_controls.get("moodlight") is not None:
                    self.set_moodlight(stored_controls["moodlight"])
                stored_indicators = stored_controls.get("indicators", [])
                if isinstance(stored_indicators, list):
                    for number, indicator in enumerate(stored_indicators[:3], start=1):
                        if not isinstance(indicator, dict):
                            continue
                        candidate = {
                            key: value
                            for key, value in indicator.items()
                            if key in {"color", "blinkMs", "fadeMs"}
                        }
                        if not indicator.get("on", False):
                            candidate["color"] = 0
                        self.set_indicator(number, candidate)
            except BridgeError as error:
                LOGGER.warning("Ignoring invalid persisted device controls: %s", error)

        if config.persistence.persist_pushed_apps:
            stored_apps = self.store.load("pushed_apps", {})
            if isinstance(stored_apps, dict):
                for name, spec in stored_apps.items():
                    try:
                        validate_app_name(name)
                        validated = validate_pushed_payload(
                            spec, strict=config.compatibility.strict
                        )[0]
                        self.pages.upsert(name, validated, current_ms)
                    except BridgeError as error:
                        LOGGER.warning("Ignoring invalid persisted pushed app %s: %s", name, error)
                        continue
        if config.persistence.persist_current_app:
            stored_current = self.store.load("current_app", {})
            if isinstance(stored_current, dict):
                name = stored_current.get("name")
                if (
                    isinstance(name, str)
                    and name in self.pages.pages
                    and name not in self.pages.disabled
                ):
                    self.pages.current_name = name

    @staticmethod
    def monotonic_ms() -> int:
        return int(time.monotonic() * 1000)

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def _changed(self, *areas: str) -> None:
        for area in areas:
            for listener in tuple(self._listeners):
                listener(area)

    def push_app(
        self,
        name: str,
        payload: Any,
        now_ms: int | None = None,
        *,
        origin: str = "pushed",
    ) -> list[str]:
        validate_app_name(name)
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        specs = validate_pushed_payload(payload, strict=self.config.compatibility.strict)
        names = (
            [name]
            if len(specs) == 1 and not isinstance(payload, list)
            else [f"{name}{index}" for index in range(len(specs))]
        )
        if any(len(child) > 32 for child in names):
            raise validation("Expanded app name exceeds 32 characters", "name")
        previous_current = self.pages.current_name
        previous_order = self.pages.order.copy()
        previous_disabled = self.pages.disabled.copy()
        previous_targets = [
            candidate
            for candidate in previous_order
            if candidate == name or self.pages._numbered_child(candidate, name)
        ]
        insertion_index = None
        if previous_targets:
            first_target = previous_order.index(previous_targets[0])
            insertion_index = sum(
                1
                for candidate in previous_order[:first_target]
                if candidate not in previous_targets
            )
        self.pages.delete_base(name)
        for child, spec in zip(names, specs, strict=True):
            self.pages.upsert(child, spec, now_ms, origin=origin)
        if insertion_index is not None:
            remaining = [candidate for candidate in self.pages.order if candidate not in names]
            self.pages.order = (
                remaining[:insertion_index] + names + remaining[insertion_index:]
            )
        disabled_targets = {
            candidate
            for candidate in previous_disabled
            if candidate == name or self.pages._numbered_child(candidate, name)
        }
        for child in names:
            if child in disabled_targets or (name in disabled_targets and len(names) > 1):
                self.pages.disabled.add(child)
        if previous_current in names:
            self.pages.current_name = previous_current
        self._persist_apps_if_enabled()
        self._changed("apps", "device")
        return names

    def delete_app(self, name: str) -> int:
        title_names = [
            candidate
            for candidate in set(self.pages.pages) | set(self.pages.order)
            if candidate == name or self.pages._numbered_child(candidate, name)
        ]
        removed = self.pages.delete_base(name)
        if not removed:
            raise not_found(f"Pushed app not found: {name}", "name")
        titles = self.settings.get("appTitles")
        if isinstance(titles, dict) and any(candidate in titles for candidate in title_names):
            self.settings["appTitles"] = {
                candidate: title
                for candidate, title in titles.items()
                if candidate not in title_names
            }
            self.store.save("settings", self.settings)
            self._changed("settings")
        self.store.save(
            "app_order", {"order": self.pages.order, "disabled": sorted(self.pages.disabled)}
        )
        self._persist_apps_if_enabled()
        self._persist_current_app_if_enabled()
        self._changed("apps", "device")
        return removed

    def _persist_apps_if_enabled(self) -> None:
        if not self.config.persistence.persist_pushed_apps:
            return
        apps = {
            name: page.spec for name, page in self.pages.pages.items() if page.origin == "pushed"
        }
        self.store.save("pushed_apps", apps)

    def _persist_current_app_if_enabled(self) -> None:
        if self.config.persistence.persist_current_app:
            self.store.save("current_app", {"name": self.pages.current_name})

    def notify(self, payload: Any, now_ms: int | None = None) -> dict[str, Any]:
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        spec = validate_notification_payload(payload, strict=self.config.compatibility.strict)
        if "soundRtttl" in spec:
            raise unsupported("RTTTL is not available on TC002", "soundRtttl")
        was_empty = self.notifications.active is None
        item = self.notifications.push(spec, now_ms)
        if spec.get("wakeup", False):
            self._changed("display")
        if was_empty:
            self._notification_started_ms = now_ms
        self._changed("device")
        return {"name": item.name, "generation": item.generation, "queued": len(self.notifications)}

    def dismiss_notification(self, name: str | None = None, now_ms: int | None = None) -> int:
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        if name is None:
            removed = 1 if self.notifications.dismiss_active(now_ms) else 0
        else:
            removed = self.notifications.dismiss_named(name, now_ms)
        self._finish_notification_pause_if_empty(now_ms)
        if removed:
            self._changed("device")
        return removed

    def _finish_notification_pause_if_empty(self, now_ms: int) -> None:
        if self.notifications.active is None and self._notification_started_ms is not None:
            self.pages.page_started_ms += now_ms - self._notification_started_ms
            self._notification_started_ms = None

    def switch_app(
        self, name: str, *, fast: bool = False, now_ms: int | None = None
    ) -> dict[str, Any]:
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        if not fast:
            self._begin_transition(now_ms)
        page = self.pages.switch(name, now_ms)
        self._persist_current_app_if_enabled()
        self._changed("apps", "device")
        return {"name": page.name, "fast": bool(fast)}

    def next_app(self, now_ms: int | None = None) -> str:
        if self.settings.get("blockNavigation", False):
            raise validation("Navigation is blocked by settings", "blockNavigation")
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        self._begin_transition(now_ms)
        name = self.pages.step(1, now_ms).name
        self._persist_current_app_if_enabled()
        self._changed("apps", "device")
        return name

    def previous_app(self, now_ms: int | None = None) -> str:
        if self.settings.get("blockNavigation", False):
            raise validation("Navigation is blocked by settings", "blockNavigation")
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        self._begin_transition(now_ms)
        name = self.pages.step(-1, now_ms).name
        self._persist_current_app_if_enabled()
        self._changed("apps", "device")
        return name

    def set_exclusive_page(
        self, name: str, spec: dict[str, Any], now_ms: int | None = None
    ) -> None:
        validate_app_name(name)
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        if name != self._exclusive_page_name or spec != self._exclusive_page_spec:
            self._exclusive_page_started_ms = now_ms
        self._exclusive_page_name = name
        self._exclusive_page_spec = copy.deepcopy(spec)
        self._transition_frame = None
        self._transition_started_ms = None
        self._changed("apps", "device")

    def clear_exclusive_page(self, now_ms: int | None = None) -> None:
        if self._exclusive_page_name is None:
            return
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        self._exclusive_page_name = None
        self._exclusive_page_spec = None
        self.pages.page_started_ms = now_ms
        self._page_render_token = None
        self._page_wait_for_scroll = False
        self._page_scroll_complete = False
        self._changed("apps", "device")

    def set_app_order(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise validation("App order must be an object")
        if self.config.compatibility.strict:
            if "disabled" not in payload:
                raise validation("App order requires disabled", "disabled")
            order = payload.get("order", self.pages.order)
            disabled = payload["disabled"]
        else:
            order = payload.get("order", payload.get("apps", self.pages.order))
            disabled = payload.get("disabled", [])
        self.pages.set_order(order, disabled)
        self.store.save(
            "app_order", {"order": self.pages.order, "disabled": sorted(self.pages.disabled)}
        )
        self._changed("apps")

    def patch_settings(self, payload: Any) -> dict[str, Any]:
        patch = validate_settings_patch(payload)
        candidate = copy.deepcopy(self.settings)
        candidate.update(patch)
        if "weekdayBar" in patch:
            candidate["weekdayBar"] = copy.deepcopy(self.settings["weekdayBar"])
            candidate["weekdayBar"].update(patch["weekdayBar"])
        self.store.save("settings", candidate)
        self.settings = candidate
        self.display["brightness"] = int(candidate["brightness"])
        self.audio["volume"] = int(candidate["mp3Volume"])
        self._changed("settings", "display", "device")
        return copy.deepcopy(self.settings)

    def reset_settings(self) -> dict[str, Any]:
        candidate = copy.deepcopy(DEFAULT_SETTINGS)
        self.store.save("settings", candidate)
        self.settings = candidate
        self.display["brightness"] = int(self.settings["brightness"])
        self.audio["volume"] = int(self.settings["mp3Volume"])
        self._changed("settings", "display", "device")
        return copy.deepcopy(self.settings)

    def patch_display(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise validation("Display patch must be an object")
        allowed = {"power", "overlay", "overlaySettings"}
        unknown = set(payload) - allowed
        if unknown:
            key = min(unknown)
            raise validation(f"Unknown display field: {key}", key)
        candidate = copy.deepcopy(self.display)
        if "power" in payload:
            if not isinstance(payload["power"], bool):
                raise validation("power must be boolean", "power")
            candidate["power"] = payload["power"]
        if "overlay" in payload:
            overlay = payload["overlay"]
            if overlay is not None and not isinstance(overlay, str):
                raise validation("overlay must be a string or null", "overlay")
            overlay = "" if overlay is None else overlay.lower()
            if overlay not in {"", "rain", "snow", "drizzle", "storm", "thunder", "frost"}:
                raise validation("Unknown overlay", "overlay")
            candidate["overlay"] = overlay
        if "overlaySettings" in payload:
            if not isinstance(payload["overlaySettings"], dict):
                raise validation("overlaySettings must be an object", "overlaySettings")
            candidate["overlaySettings"] = copy.deepcopy(payload["overlaySettings"])
        self.display = candidate
        self._changed("display", "settings", "device")
        return copy.deepcopy(self.display)

    def set_moodlight(self, payload: Any | None) -> None:
        if payload is not None and not isinstance(payload, dict):
            raise validation("Moodlight must be an object")
        if payload == {}:
            payload = None
        if payload is not None:
            unknown = set(payload) - {"brightness", "color", "kelvin", "effect", "effectSpeed"}
            if unknown:
                key = min(unknown)
                raise validation(f"Unknown moodlight field: {key}", key)
            payload = copy.deepcopy(payload)
            brightness = payload.get("brightness", 100)
            if not isinstance(brightness, int) or isinstance(brightness, bool) or not 0 <= brightness <= 255:
                raise validation("Moodlight brightness must be from 0 to 255", "brightness")
            payload["brightness"] = brightness
            if "color" in payload:
                packed = parse_color(payload["color"], "color")
                assert packed is not None
                payload["color"] = f"#{packed:06X}"
            if "kelvin" in payload:
                kelvin = payload["kelvin"]
                if (
                    not isinstance(kelvin, int)
                    or isinstance(kelvin, bool)
                    or (kelvin != 0 and not 1_000 <= kelvin <= 40_000)
                ):
                    raise validation("Moodlight kelvin must be 0 or from 1000 to 40000", "kelvin")
        self.display["moodlight"] = copy.deepcopy(payload)
        self._persist_device_controls()
        self._changed("display")

    def _persist_device_controls(self) -> None:
        self.store.save(
            "device_controls",
            {"moodlight": self.display.get("moodlight"), "indicators": self.indicators},
        )

    @staticmethod
    def _kelvin_color(kelvin: int) -> str:
        temperature = max(1_000, min(40_000, kelvin)) / 100
        if temperature <= 66:
            red = 255
            green = 99.4708025861 * math.log(temperature) - 161.1195681661
            blue = 0 if temperature <= 19 else 138.5177312231 * math.log(temperature - 10) - 305.0447927307
        else:
            red = 329.698727446 * ((temperature - 60) ** -0.1332047592)
            green = 288.1221695283 * ((temperature - 60) ** -0.0755148492)
            blue = 255
        channels = [max(0, min(255, round(value))) for value in (red, green, blue)]
        return f"#{channels[0]:02X}{channels[1]:02X}{channels[2]:02X}"

    def set_indicator(self, number: int, payload: Any | None) -> dict[str, Any]:
        if number not in {1, 2, 3}:
            raise not_found("Indicator must be 1, 2 or 3", "indicator")
        if payload is None or payload == {}:
            value = {"on": False, "color": "#FFFFFF", "blinkMs": 0, "fadeMs": 0}
        else:
            if not isinstance(payload, dict):
                raise validation("Indicator must be an object")
            unknown = set(payload) - {"color", "blinkMs", "fadeMs"}
            if unknown:
                key = min(unknown)
                raise validation(f"Unknown indicator field: {key}", key)
            value = copy.deepcopy(self.indicators[number - 1])
            if "color" in payload:
                if payload["color"] is None:
                    value["on"] = False
                else:
                    packed = parse_color(payload["color"], "color")
                    assert packed is not None
                    value["on"] = packed != 0
                    if packed != 0:
                        value["color"] = f"#{packed:06X}"
            for key in ("blinkMs", "fadeMs"):
                if key in payload:
                    item = payload[key]
                    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                        raise validation(f"{key} must be a non-negative integer", key)
                    value[key] = item
        self.indicators[number - 1] = value
        self._persist_device_controls()
        self._changed("display", "device")
        return copy.deepcopy(value)

    def play_audio(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise validation("Audio play command must be an object")
        source_keys = [
            key
            for key in ("sound", "mp3", "melody", "track", "rtttl", "station", "index", "url")
            if key in payload
        ]
        if len(source_keys) != 1:
            raise validation(
                "exactly one of sound, mp3, melody, track, rtttl, station, index or url is required"
            )
        source_key = source_keys[0]
        source = payload[source_key]
        if source_key in {"track", "rtttl", "station", "index", "url"}:
            raise unsupported(f"Audio source {source_key} is not available on TC002", source_key)
        if not isinstance(source, str):
            raise validation(f"{source_key} must be a string", source_key)
        if not AUDIO_ASSET_NAME.fullmatch(source):
            raise validation(
                "TC002 audio asset must contain 1-64 letters, digits, underscore or hyphen",
                source_key,
            )
        volume = int(self.settings["mp3Volume"])
        self.audio.update(
            {
                "playing": True,
                "source": source,
                "sourceType": source_key,
                "loop": False,
                "volume": volume,
            }
        )
        self._changed("audio")
        return copy.deepcopy(self.audio)

    def stop_audio(self) -> dict[str, Any]:
        self.audio.update({"playing": False, "source": None, "loop": False})
        self._changed("audio")
        return copy.deepcopy(self.audio)

    def update_button(self, name: str, pressed: bool) -> None:
        if name not in self.buttons:
            if not self.config.compatibility.extensions:
                return
        else:
            self.buttons[name] = pressed
            self._changed(f"button:{name}")

    def update_adapter(self, data: dict[str, Any], now_ms: int | None = None) -> None:
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        self.adapter["connected"] = True
        self.adapter["lastSeenMs"] = now_ms
        mapping = {
            "transport": "transport",
            "mcuVersion": "mcuVersion",
            "appVersion": "appVersion",
            "batteryPercent": "batteryPercent",
            "ipAddress": "ipAddress",
            "wifiRssi": "wifiRssi",
            "error": "error",
        }
        for source, target in mapping.items():
            if source in data:
                self.adapter[target] = data[source]
        battery = self.adapter.get("batteryPercent")
        if not isinstance(battery, int) or not 0 <= battery <= 100:
            self.adapter["batteryPercent"] = None
        self._changed("device")

    def check_adapter_timeout(self, now_ms: int | None = None) -> bool:
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        last_seen = self.adapter.get("lastSeenMs")
        connected = bool(
            last_seen is not None
            and now_ms - int(last_seen) <= int(self.config.adapter.heartbeat_timeout * 1000)
        )
        changed = connected != self.adapter["connected"]
        self.adapter["connected"] = connected
        if changed:
            self._changed("device")
        return connected

    def render(self, now_ms: int | None = None, wall_time: datetime | None = None) -> RenderResult:
        now_ms = self.monotonic_ms() if now_ms is None else now_ms
        if self.pages.expire_lifetimes(now_ms):
            self._persist_current_app_if_enabled()
        active_notification = self.notifications.active

        exclusive_page = self._exclusive_page_spec
        if active_notification is not None:
            spec = active_notification.spec
            render_key = f"notification:{active_notification.generation}"
            elapsed = now_ms - active_notification.started_ms
        elif exclusive_page is not None and self._exclusive_page_name is not None:
            spec = exclusive_page
            render_key = f"exclusive:{self._exclusive_page_name}"
            elapsed = now_ms - self._exclusive_page_started_ms
        else:
            current = self.pages.current
            page_token = (
                current.name,
                current.received_ms,
                self.pages.page_started_ms,
            )
            if page_token != self._page_render_token:
                self._page_render_token = page_token
                self._page_wait_for_scroll = False
                self._page_scroll_complete = False
            self._rotate_if_due(now_ms)
            page = self.pages.current
            spec = self._page_spec(page.name, page.spec, wall_time)
            render_key = f"app:{page.name}:{page.received_ms}"
            elapsed = now_ms - self.pages.page_started_ms

        if self.display.get("moodlight") is not None:
            mood = self.display["moodlight"]
            color = mood.get("color", mood.get("backgroundColor", "#FFFFFF"))
            if int(mood.get("kelvin", 0) or 0) > 0:
                color = self._kelvin_color(int(mood["kelvin"]))
            packed = parse_color(color, "color") or 0
            factor = int(mood.get("brightness", 100)) / 255
            red = round(((packed >> 16) & 255) * factor)
            green = round(((packed >> 8) & 255) * factor)
            blue = round((packed & 255) * factor)
            spec = {
                "backgroundColor": f"#{red:02X}{green:02X}{blue:02X}",
                "effect": mood.get("effect", ""),
                "effectSpeed": mood.get("effectSpeed", 1),
            }
            render_key = "moodlight"

        if self.display.get("overlay") and "overlay" not in spec:
            spec = copy.deepcopy(spec)
            spec["overlay"] = self.display["overlay"]

        result = self.renderer.render(
            spec,
            render_key=render_key,
            elapsed_ms=max(0, elapsed),
            settings=self.settings,
            indicators=self.indicators,
        )
        notification_wakeup = bool(
            active_notification and active_notification.spec.get("wakeup", False)
        )
        if not self.display.get("power", True) and not notification_wakeup:
            result = RenderResult(bytes(52 * 16 * 3), result.scroll_complete, 52, 16)
        result = self._apply_transition(result, now_ms)
        self.last_frame = result.frame
        self.frame_sequence = (self.frame_sequence + 1) & 0xFFFFFFFF

        if active_notification is None and exclusive_page is None:
            self._page_render_token = (
                page.name,
                page.received_ms,
                self.pages.page_started_ms,
            )
            self._page_wait_for_scroll = bool(spec.get("_stopBodyAfterRepeat"))
            self._page_scroll_complete = result.scroll_complete

        if active_notification is not None and self.notifications.should_expire(
            now_ms,
            scroll_complete=result.scroll_complete,
            default_duration_ms=int(self.settings.get("appDurationMs", 7000)),
        ):
            self.notifications.dismiss_active(now_ms)
            self._finish_notification_pause_if_empty(now_ms)
            self._changed("device")
        return result

    def render_studio_app(
        self, name: str, *, elapsed_ms: int = 0, wall_time: datetime | None = None
    ) -> RenderResult:
        """Render one Studio page without navigating or consuming notifications."""
        page = self.pages.pages.get(name)
        if page is None or page.origin != "studio" or name in self.pages.disabled:
            raise not_found(f"Enabled Studio app not found: {name}", "name")
        spec = self._page_spec(name, page.spec, wall_time)
        if self.display.get("overlay") and "overlay" not in spec:
            spec = {**spec, "overlay": self.display["overlay"]}
        return self.renderer.render(
            spec,
            render_key=f"studio:{name}:{page.received_ms}",
            elapsed_ms=max(0, elapsed_ms),
            settings=self.settings,
            indicators=self.indicators,
        )

    def _rotate_if_due(self, now_ms: int) -> None:
        page = self.pages.current
        duration = int(page.spec.get("durationMs", self.settings.get("appDurationMs", 7000)))
        elapsed = now_ms - self.pages.page_started_ms
        if page.spec.get("timingMode") == "scrolls":
            due = elapsed >= 600_000 or (elapsed >= 1_000 and self._page_scroll_complete)
        else:
            due = duration > 0 and elapsed >= duration
        if due:
            if self._page_wait_for_scroll and not self._page_scroll_complete:
                return
            self._begin_transition(now_ms)
            self.pages.step(1, now_ms)
            self._page_render_token = None
            self._page_wait_for_scroll = False
            self._page_scroll_complete = False
            self._persist_current_app_if_enabled()
            self._changed("apps", "device")

    def _begin_transition(self, now_ms: int) -> None:
        if not self.settings.get("autoTransition", True):
            self._transition_frame = None
            self._transition_started_ms = None
            self._transition_effect = None
            return
        self._transition_frame = self.last_frame
        self._transition_started_ms = now_ms
        effect = str(self.settings.get("transitionEffect", "Fade")).lower()
        if effect == "random":
            choices = ("slide", "fade", "curtain", "blocks", "diamond", "wave", "rain")
            effect = choices[(self.frame_sequence + now_ms // 1000) % len(choices)]
        self._transition_effect = effect

    def _apply_transition(self, result: RenderResult, now_ms: int) -> RenderResult:
        if self._transition_frame is None or self._transition_started_ms is None:
            return result
        duration = int(self.settings.get("transitionDurationMs", 1000))
        elapsed = now_ms - self._transition_started_ms
        if duration <= 0 or elapsed >= duration:
            self._transition_frame = None
            self._transition_started_ms = None
            self._transition_effect = None
            return result
        amount = max(0.0, min(1.0, elapsed / duration))
        previous = self._transition_frame
        current = result.frame
        effect = self._transition_effect or "fade"
        width, height = result.logical_width, result.logical_height
        if width != 52 or height != 16:
            width, height = 52, 16

        if effect in {"fade", "dim"}:
            if amount < 0.5:
                level = 1.0 - amount * 2.0
                mixed = bytes(round(channel * level) for channel in previous)
            else:
                level = amount * 2.0 - 1.0
                mixed = bytes(round(channel * level) for channel in current)
        elif effect == "flash":
            if amount < 0.5:
                level = amount * 2.0
                mixed = bytes(round(channel * (1.0 - level) + 255 * level) for channel in previous)
            else:
                level = amount * 2.0 - 1.0
                mixed = bytes(round(255 * (1.0 - level) + channel * level) for channel in current)
        elif effect == "blink":
            phase = min(7, int(amount * 8))
            mixed = previous if phase in {0, 3} else current if phase in {1, 4, 6, 7} else bytes(len(current))
        elif effect in {"slide", "rotate"}:
            shift = min(width, round(amount * width))
            output = bytearray(len(current))
            for y in range(height):
                reverse = effect == "rotate" and y % 2 == 1
                for x in range(width):
                    if reverse:
                        source = previous if x >= shift else current
                        source_x = x - shift if source is previous else width - shift + x
                    else:
                        source = previous if x < width - shift else current
                        source_x = x + shift if source is previous else x - (width - shift)
                    target_index = (y * width + x) * 3
                    source_index = (y * width + source_x) * 3
                    output[target_index : target_index + 3] = source[source_index : source_index + 3]
            mixed = bytes(output)
        else:
            seed = (self._transition_started_ms or 0) // 100

            def reveal(x: int, y: int) -> bool:
                if effect == "cover":
                    return x < amount * width
                if effect == "uncover":
                    return x >= width * (1.0 - amount)
                if effect == "curtain":
                    return abs(x - (width - 1) / 2) <= amount * width / 2
                if effect == "split":
                    return abs(x - (width - 1) / 2) >= (1.0 - amount) * width / 2
                if effect == "blinds":
                    return y % 4 < amount * 4
                if effect in {"blocks", "pixelate"}:
                    block = (x // 4) + (y // 4) * 13
                    rank = ((block * 37 + seed * 17) % 101) / 100
                    return rank <= amount
                if effect in {"zoom", "ripple"}:
                    dx = x - (width - 1) / 2
                    dy = (y - (height - 1) / 2) * (width / height)
                    radius = width * 0.72 * amount
                    return dx * dx + dy * dy <= radius * radius
                if effect == "diamond":
                    distance = abs(x - (width - 1) / 2) + abs(y - (height - 1) / 2) * 2
                    return distance <= amount * (width / 2 + height)
                if effect == "wave":
                    wave = (0, 2, 4, 6, 7, 6, 4, 2, 0, -2, -4, -6, -7, -6, -4, -2)
                    return x <= amount * (width + 14) - 7 + wave[y % len(wave)]
                if effect in {"rain", "melt"}:
                    column = x if effect == "rain" else x // 4
                    delay = ((column * 11 + seed * 7) % height) / height
                    cutoff = (amount * 2.0 - delay) * height
                    return y <= cutoff
                if effect == "interlace":
                    row_phase = amount * 2.0 - (0.0 if y % 2 == 0 else 1.0)
                    return x <= max(0.0, min(1.0, row_phase)) * width
                if effect == "reload":
                    return y < amount * height
                return x < amount * width

            output = bytearray(len(current))
            for y in range(height):
                for x in range(width):
                    index = (y * width + x) * 3
                    source = current if reveal(x, y) else previous
                    output[index : index + 3] = source[index : index + 3]
            mixed = bytes(output)
        return RenderResult(
            mixed, result.scroll_complete, result.logical_width, result.logical_height
        )

    def _page_spec(
        self, name: str, spec: dict[str, Any], wall_time: datetime | None
    ) -> dict[str, Any]:
        if name not in {"Time", "Date"}:
            return self._two_line_page_spec(name, spec)
        if wall_time is None:
            wall_time = datetime.now(ZoneInfo(self.config.timezone))
        if name == "Time":
            use_24 = self.settings.get("time24h", True)
            show_seconds = self.settings.get("timeShowSeconds", False)
            pattern = "%H:%M:%S" if use_24 and show_seconds else "%H:%M"
            if not use_24:
                # The glyphs retain enough natural side bearing to separate
                # the meridiem marker without a full blank cell.  Removing
                # that cell keeps HH:MM AM/PM static on the 52-pixel panel.
                pattern = "%I:%M:%S%p" if show_seconds else "%I:%M%p"
            text = wall_time.strftime(pattern)
            if not self.settings.get("timeLeadingZero", True):
                text = text.lstrip("0")
            result = {
                "text": text,
                "textCenter": True,
                # Both native sizes use one physical-pixel optical correction.
                # Large copies the former Silkscreen 2× digit layout; Small
                # keeps the current Scientifica rendering.
                "textOffsetX": 1,
                "textCase": "asTyped",
                "textColor": self.settings.get("timeColor") or self.settings["textColor"],
                "_fontRole": "digits",
                "_timeDateFontSize": self.settings.get("timeDateFontSize", "small"),
                "_letterSpacing": (
                    -1 if self.settings.get("timeDateFontSize", "small") == "large" else 0
                ),
            }
            if not use_24 and self.settings.get("fontFamily") in {"pixbar", "tiny5"}:
                result["_letterSpacing"] = -1
            return result
        separator = {"dot": ".", "slash": "/", "dash": "-"}[self.settings["dateSeparator"]]
        order = self.settings["dateOrder"]
        year = str(wall_time.year)
        if self.settings["dateYearMode"] == "twoDigit":
            year = year[-2:]
        parts = {
            "dayMonthYear": [f"{wall_time.day:02d}", f"{wall_time.month:02d}", year],
            "monthDayYear": [f"{wall_time.month:02d}", f"{wall_time.day:02d}", year],
            "yearMonthDay": [year, f"{wall_time.month:02d}", f"{wall_time.day:02d}"],
        }[order]
        if self.settings["dateYearMode"] == "none":
            parts = [part for part in parts if part != year]
        time_date_font_size = self.settings.get("timeDateFontSize", "small")
        return {
            "text": separator.join(parts),
            "textCenter": True,
            "textOffsetX": 1,
            "textCase": "asTyped",
            "textColor": self.settings.get("dateColor") or self.settings["textColor"],
            "_fontRole": "digits",
            "_timeDateFontSize": time_date_font_size,
            # Large faithfully copies the former Silkscreen 2× tracking.
            # Small keeps Scientifica's natural one-pixel separation.
            "_letterSpacing": -1 if time_date_font_size == "large" else 0,
        }

    def _two_line_page_spec(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        if (
            self.config.renderer.mode != "tc002_native"
            or self.settings.get("fontFamily")
            not in {"awtrix_1x", "silkscreen", "scientifica"}
            or self.settings.get("displayLayout") != "twoLine"
            or spec.get("displayTemplate", "inherit") != "inherit"
            or name == "CasaViva"
        ):
            return spec
        raw_text = spec.get("text")
        if isinstance(raw_text, list):
            text = "".join(
                str(fragment.get("text", ""))
                for fragment in raw_text
                if isinstance(fragment, dict)
            )
        elif raw_text is None:
            text = ""
        else:
            text = str(raw_text)
        text = text.strip()
        if not text:
            return spec

        configured = TWO_LINE_PAGE_LABELS.get(name)
        if configured is None:
            readable_name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
            readable_name = readable_name.replace("_", " ").replace("-", " ")
            words = [word for word in readable_name.split() if word.lower() not in {"sensor"}]
            title = " ".join(words).upper() or "APP"
            aliases = (title,)
        else:
            title, aliases = configured

        body = text
        dynamic_header: str | None = None
        if name in MUSIC_PAGE_NAMES:
            separators = list(re.finditer(r"\s+[-–—]\s+", body))
            if separators:
                separator = separators[-1]
                track = body[: separator.start()].strip()
                artist = body[separator.end() :].strip()
                if track and artist:
                    dynamic_header = artist
                    body = track
        for alias in sorted(aliases, key=len, reverse=True):
            match = re.match(rf"^\s*{re.escape(alias)}(?:\s*[:\-]\s*|\s+)", body, re.IGNORECASE)
            if match:
                body = body[match.end() :].strip()
                break
        if not body:
            body = text

        result = copy.deepcopy(spec)
        result["_twoLineLayout"] = True
        result["_headerText"] = (
            dynamic_header or self.settings.get("appTitles", {}).get(name) or title
        )
        result["_twoLineBody"] = body
        result["_scrollHeader"] = dynamic_header is not None
        if dynamic_header is not None:
            result["_headerRestText"] = dynamic_header.split(maxsplit=1)[0]
            result["repeat"] = int(self.settings.get("musicTitleScrollRepeats", 3))
            result["_stopBodyAfterRepeat"] = True
        return result

    def device_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "version": __version__,
            "uid": self.config.uid,
            "boardType": "Ulanzi TC002",
            "soc": "TC002 Linux/FlyThings",
            "hostname": socket.gethostname(),
            "uptimeSeconds": int(time.monotonic() - self.started_monotonic),
            "brightness": self.display["brightness"],
            "matrixPower": self.display["power"],
            "currentApp": self._exclusive_page_name or self.pages.current_name,
            "exclusiveMode": self._exclusive_page_name is not None,
            "exclusiveApp": self._exclusive_page_name,
            "indicators": copy.deepcopy(self.indicators),
            "messageCount": self.message_count,
            "mqtt": {
                "enabled": self.config.mqtt.enabled,
                "connected": self.mqtt_connected,
                "prefix": self.config.mqtt.prefix,
            },
            "adapter": copy.deepcopy(self.adapter),
            "scripting": False,
            "fps": self.config.renderer.fps,
            "bridge": {
                "deviceHost": self.config.adapter.device_host,
                "adapterMode": self.config.adapter.mode,
                "httpApp": self.config.adapter.http_app,
                "frameLifetimeSeconds": self.config.adapter.blackout_timeout,
                "rendererMode": self.config.renderer.mode,
            },
        }
        try:
            state["freeHeapBytes"] = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, OSError, ValueError):
            pass
        for key in ("batteryPercent", "ipAddress", "wifiRssi"):
            if self.adapter.get(key) is not None:
                state[key] = self.adapter[key]
        return state

    def capabilities(self) -> dict[str, Any]:
        capabilities: dict[str, Any] = {
            "width": 52,
            "height": 16,
            "logicalWidth": 32 if self.config.renderer.mode == "awtrix_compatible" else 52,
            "logicalHeight": 8 if self.config.renderer.mode == "awtrix_compatible" else 16,
            "audio": self.config.adapter.mode == "udp",
            "scripting": False,
            "light": False,
            "temperature": False,
            "humidity": False,
            "pressure": False,
            "battery": self.adapter.get("batteryPercent") is not None,
            "transitions": TRANSITIONS,
            "fontFamilies": list(FONT_FAMILIES),
            "displayLayouts": list(DISPLAY_LAYOUTS),
            "rendererMode": self.config.renderer.mode,
        }
        return capabilities

    def screen_state(self) -> dict[str, Any]:
        pixels = [
            (self.last_frame[index] << 16)
            | (self.last_frame[index + 1] << 8)
            | self.last_frame[index + 2]
            for index in range(0, len(self.last_frame), 3)
        ]
        return {"width": 52, "height": 16, "pixels": pixels}

    def unsupported_operation(self, operation: str) -> None:
        raise unsupported(f"{operation} cannot be mapped safely from AWTRIX NG to TC002")
