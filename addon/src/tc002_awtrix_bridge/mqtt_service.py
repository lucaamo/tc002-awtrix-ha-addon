from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .discovery import home_assistant_discovery
from .engine import Engine
from .errors import BridgeError, invalid_json, not_found
from .http_service import ControlAdapter

LOGGER = logging.getLogger(__name__)


class MqttService:
    def __init__(
        self, engine: Engine, config: MqttConfig, adapter: ControlAdapter | None = None
    ) -> None:
        self.engine = engine
        self.config = config
        self.adapter = adapter
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self._result_echoes: dict[str, int] = {}
        self._result_echo_lock = threading.Lock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id)
        self.client.enable_logger(LOGGER)
        if config.username:
            self.client.username_pw_set(config.username, config.password)
        if config.tls:
            self.client.tls_set()
        self.client.will_set(f"{config.prefix}/availability", "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        engine.add_listener(self._state_changed)

    async def start(self) -> None:
        if not self.config.enabled:
            return
        self.loop = asyncio.get_running_loop()
        self.client.connect_async(self.config.host, self.config.port, self.config.keepalive)
        self.client.loop_start()

    async def stop(self) -> None:
        if not self.config.enabled:
            return
        if self.connected:
            self.client.publish(
                f"{self.config.prefix}/availability", "offline", qos=1, retain=True
            ).wait_for_publish(timeout=2)
        self.client.disconnect()
        self.client.loop_stop()
        self.connected = False

    def _on_connect(
        self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if reason_code != 0:
            LOGGER.warning("MQTT connection rejected: %s", reason_code)
            return
        client.subscribe(f"{self.config.prefix}/cmd/#", qos=1)
        if self.engine.config.compatibility.extensions:
            client.subscribe(f"{self.config.prefix}/extensions/audio/volume/set", qos=1)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._connected)

    def _connected(self) -> None:
        self.connected = True
        self.engine.mqtt_connected = True
        with self._result_echo_lock:
            self._result_echoes.clear()
        self.publish_all()

    def _on_disconnect(
        self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._disconnected)
        if reason_code != 0:
            LOGGER.warning("Unexpected MQTT disconnect: %s", reason_code)

    def _disconnected(self) -> None:
        self.connected = False
        self.engine.mqtt_connected = False

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        if self._consume_result_echo(message.topic):
            LOGGER.debug("Ignored locally published MQTT result echo on %s", message.topic)
            return
        if self.loop is None:
            return
        topic, payload = message.topic, bytes(message.payload)
        if topic == f"{self.config.prefix}/extensions/audio/volume/set":
            self.loop.call_soon_threadsafe(self._set_extension_volume, payload)
            return
        self.loop.call_soon_threadsafe(self._dispatch_and_reply, topic, payload)

    def _set_extension_volume(self, payload: bytes) -> None:
        if self.engine.config.adapter.mode != "udp":
            LOGGER.warning("Ignored volume command: native companion is unavailable")
            return
        try:
            value = int(payload.decode("ascii"))
            if not 0 <= value <= 100:
                raise ValueError
        except (UnicodeDecodeError, ValueError):
            LOGGER.warning("Ignored invalid extension volume payload")
            return
        self.engine.patch_settings({"mp3Volume": value})
        if self.adapter:
            self.adapter.send_control("audio.volume", {"volume": value})

    def _dispatch_and_reply(self, topic: str, payload: bytes) -> None:
        result_topic = f"{topic}/result"
        self.engine.message_count += 1
        try:
            value = self.dispatch(topic, payload)
            result: dict[str, Any] = {"ok": True}
            if value is not None and not self.engine.config.compatibility.strict:
                result["result"] = value
        except BridgeError as error:
            if error.code == "notFound" and error.message.startswith("Unknown command topic:"):
                LOGGER.warning("Ignored unknown MQTT command topic %s", topic)
                return
            result = error.as_result()
        except Exception:
            LOGGER.exception("Unhandled MQTT command error")
            result = {
                "ok": False,
                "error": {"code": "internalError", "message": "Internal error"},
            }
        self._mark_result_echo(result_topic)
        self.publish_json(result_topic, result, retain=False)

    def _mark_result_echo(self, topic: str) -> None:
        with self._result_echo_lock:
            self._result_echoes[topic] = self._result_echoes.get(topic, 0) + 1

    def _consume_result_echo(self, topic: str) -> bool:
        with self._result_echo_lock:
            count = self._result_echoes.get(topic, 0)
            if count == 0:
                return False
            if count == 1:
                self._result_echoes.pop(topic, None)
            else:
                self._result_echoes[topic] = count - 1
            return True

    @staticmethod
    def _json(payload: bytes, *, empty: Any = None) -> Any:
        if not payload:
            return empty
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise invalid_json() from error

    def dispatch(self, topic: str, payload: bytes) -> Any:
        prefix = f"{self.config.prefix}/cmd/"
        if not topic.startswith(prefix):
            raise not_found("Unknown command topic")
        operation = topic[len(prefix) :]
        if operation == "notify":
            return self.engine.notify(self._json(payload))
        if operation == "notify/dismiss":
            return {"removed": self.engine.dismiss_notification()}
        if operation.startswith("notify/dismiss/"):
            removed = self.engine.dismiss_notification(operation.split("/", 2)[2])
            if not removed:
                raise not_found("Notification not found", "name")
            return {"removed": removed}
        if operation.startswith("apps/pushed/"):
            name = operation.removeprefix("apps/pushed/")
            if not payload or payload.strip() == b"{}":
                try:
                    removed = self.engine.delete_app(name)
                except BridgeError as error:
                    if error.code != "notFound":
                        raise
                    removed = 0
                return {"removed": removed}
            return {"apps": self.engine.push_app(name, self._json(payload))}
        if operation == "apps/switch":
            try:
                decoded = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise invalid_json("App name must be UTF-8") from error
            candidate = decoded
            fast = False
            if decoded.lstrip().startswith("{"):
                body = self._json(payload)
                candidate = body.get("name", body.get("app", ""))
                fast = bool(body.get("fast", False))
            return self.engine.switch_app(candidate, fast=fast)
        if operation == "apps/next":
            return {"name": self.engine.next_app()}
        if operation == "apps/previous":
            return {"name": self.engine.previous_app()}
        if operation == "apps/order":
            self.engine.set_app_order(self._json(payload))
            return None
        if operation == "settings":
            patch = self._json(payload)
            settings = self.engine.patch_settings(patch)
            if self.adapter and "mp3Volume" in patch:
                self.adapter.send_control("audio.volume", {"volume": settings["mp3Volume"]})
            return settings
        if operation == "settings/reset":
            settings = self.engine.reset_settings()
            if self.adapter:
                self.adapter.send_control("audio.volume", {"volume": settings["mp3Volume"]})
            return settings
        if operation == "display":
            return self.engine.patch_display(self._json(payload))
        if operation == "display/moodlight":
            self.engine.set_moodlight(None if not payload else self._json(payload))
            return None
        if operation.startswith("indicators/"):
            try:
                number = int(operation.split("/", 1)[1])
            except ValueError as error:
                raise not_found("Unknown indicator") from error
            return self.engine.set_indicator(number, None if not payload else self._json(payload))
        if operation in {"audio/play", "sounds/play"}:
            if self.engine.config.adapter.mode != "udp":
                self.engine.unsupported_operation("Audio without the native companion")
            body = self._json(payload)
            if operation == "sounds/play" and isinstance(body, dict):
                if "name" in body:
                    body = {"sound": body["name"]}
                elif "builtin" in body:
                    body = {"sound": str(body["builtin"])}
                elif "rtttl" in body:
                    body = {"rtttl": body["rtttl"]}
            value = self.engine.play_audio(body)
            if self.adapter:
                self.adapter.send_control("audio.play", value)
            return value
        if operation == "audio/stop":
            if self.engine.config.adapter.mode != "udp":
                self.engine.unsupported_operation("Audio without the native companion")
            value = self.engine.stop_audio()
            if self.adapter:
                self.adapter.send_control("audio.stop", {})
            return value
        if operation == "audio/stations":
            self.engine.unsupported_operation("Internet radio stations")
        if operation == "device/reboot":
            if self.engine.config.adapter.mode != "udp":
                self.engine.unsupported_operation("Device reboot without the native companion")
            if not self.engine.config.adapter.allow_reboot:
                self.engine.unsupported_operation("Device reboot")
            if self.adapter is None:
                raise BridgeError("unavailable", "TC002 adapter is unavailable", status=503)
            return {"sequence": self.adapter.send_control("device.reboot", {})}
        if operation == "device/sleep":
            self.engine.unsupported_operation("Timed device sleep")
        if operation == "screen/get":
            self.publish_json(
                f"{self.config.prefix}/state/screen", self.engine.screen_state(), retain=False
            )
            return self.engine.screen_state()
        raise not_found(f"Unknown command topic: {operation}")

    def _state_changed(self, area: str) -> None:
        if not self.connected:
            return
        if area.startswith("button:"):
            name = area.split(":", 1)[1]
            self.publish(f"state/buttons/{name}", "1" if self.engine.buttons[name] else "0")
        elif area == "apps":
            self.publish("state/apps/active", self.engine.pages.current_name)
        elif area == "settings":
            self.publish_json(f"{self.config.prefix}/state/settings", self.engine.settings)
        elif area == "audio":
            self.publish_json(f"{self.config.prefix}/state/audio", self.engine.audio)
        elif area == "device":
            self.publish_json(f"{self.config.prefix}/state/device", self.engine.device_state())

    def publish_all(self) -> None:
        prefix = self.config.prefix
        self.client.publish(f"{prefix}/availability", "online", qos=1, retain=True)
        self.publish_json(f"{prefix}/state/capabilities", self.engine.capabilities())
        self.client.publish(f"{prefix}/state/prefix", prefix, qos=1, retain=True)
        self.client.publish(
            f"{prefix}/state/apps/active", self.engine.pages.current_name, qos=1, retain=True
        )
        self.publish_json(f"{prefix}/state/settings", self.engine.settings)
        self.publish_json(f"{prefix}/state/audio", self.engine.audio)
        self.publish_json(f"{prefix}/state/device", self.engine.device_state())
        for name, value in self.engine.buttons.items():
            self.client.publish(
                f"{prefix}/state/buttons/{name}", "1" if value else "0", qos=1, retain=True
            )
        discovery_topic = (
            f"{self.config.home_assistant_prefix}/device/{self.engine.config.uid}/config"
        )
        self.publish_json(discovery_topic, home_assistant_discovery(self.engine))

    def publish(self, suffix: str, value: str, *, retain: bool = True) -> None:
        self.client.publish(f"{self.config.prefix}/{suffix}", value, qos=1, retain=retain)

    def publish_json(self, topic: str, value: Any, *, retain: bool = True) -> None:
        self.client.publish(
            topic,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            qos=1,
            retain=retain,
        )

    def clear_retained_pushed_app(self, name: str) -> None:
        """Remove a retained app command so it cannot recreate a deleted page."""
        if not self.config.enabled:
            return
        self.client.publish(
            f"{self.config.prefix}/cmd/apps/pushed/{name}", b"", qos=1, retain=True
        )
