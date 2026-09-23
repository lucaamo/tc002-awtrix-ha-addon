from __future__ import annotations

import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .errors import validation

UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(slots=True)
class HttpConfig:
    host: str = "0.0.0.0"
    port: int = 7000
    username: str = ""
    password: str = ""


@dataclass(slots=True)
class MqttConfig:
    enabled: bool = True
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    prefix: str = "tc002"
    client_id: str = "tc002-awtrix-bridge"
    keepalive: int = 30
    tls: bool = False
    home_assistant_prefix: str = "homeassistant"


@dataclass(slots=True)
class AdapterConfig:
    enabled: bool = True
    mode: str = "udp"
    device_host: str = "192.168.1.50"
    frame_port: int = 9876
    listen_host: str = "0.0.0.0"
    event_port: int = 9877
    token: str = ""
    http_port: int = 80
    http_app: str = "awtrix_bridge"
    http_timeout: float = 3.0
    http_max_fps: int = 10
    heartbeat_timeout: float = 10.0
    blackout_timeout: float = 5.0
    allow_reboot: bool = False
    allow_poweroff: bool = False


@dataclass(slots=True)
class RendererConfig:
    mode: str = "awtrix_compatible"
    fps: int = 20
    gamma: float = 1.9


@dataclass(slots=True)
class CompatibilityConfig:
    mode: str = "awtrix-ng"
    strict: bool = True
    extensions: bool = False


@dataclass(slots=True)
class PersistenceConfig:
    directory: Path = Path("/data")
    persist_pushed_apps: bool = False
    persist_current_app: bool = False


@dataclass(slots=True)
class BridgeConfig:
    uid: str = "tc002"
    name: str = "TC002 AWTRIX Bridge"
    timezone: str = "Europe/Rome"
    http: HttpConfig = field(default_factory=HttpConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    renderer: RendererConfig = field(default_factory=RendererConfig)
    compatibility: CompatibilityConfig = field(default_factory=CompatibilityConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)


def _merge_dataclass(instance: Any, values: dict[str, Any], section: str) -> None:
    allowed = set(instance.__dataclass_fields__)
    unknown = set(values) - allowed
    if unknown:
        name = min(unknown)
        raise validation(f"Unknown configuration key: {section}.{name}", f"{section}.{name}")
    for key, value in values.items():
        if key == "directory":
            value = Path(value)
        setattr(instance, key, value)


def load_config(path: str | Path) -> BridgeConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise validation("Configuration root must be an object")
    raw = deepcopy(raw)
    if "render" in raw:
        if "renderer" in raw:
            raise validation("Use render or renderer, not both", "render")
        render = raw.pop("render")
        if not isinstance(render, dict):
            raise validation("Configuration section render must be an object", "render")
        unknown_render = set(render) - {"layout_mode", "fps", "gamma"}
        if unknown_render:
            name = min(unknown_render)
            raise validation(f"Unknown configuration key: render.{name}", f"render.{name}")
        raw["renderer"] = {
            ("mode" if key == "layout_mode" else key): value for key, value in render.items()
        }

    config = BridgeConfig()
    top_allowed = set(config.__dataclass_fields__)
    unknown = set(raw) - top_allowed
    if unknown:
        name = min(unknown)
        raise validation(f"Unknown configuration section: {name}", name)

    for key, value in raw.items():
        current = getattr(config, key)
        if hasattr(current, "__dataclass_fields__"):
            if not isinstance(value, dict):
                raise validation(f"Configuration section {key} must be an object", key)
            _merge_dataclass(current, value, key)
        else:
            setattr(config, key, value)

    config.mqtt.username = os.getenv("TC002_MQTT_USERNAME", config.mqtt.username)
    config.mqtt.password = os.getenv("TC002_MQTT_PASSWORD", config.mqtt.password)
    config.adapter.token = os.getenv("TC002_ADAPTER_TOKEN", config.adapter.token)
    config.http.username = os.getenv("TC002_HTTP_USERNAME", config.http.username)
    config.http.password = os.getenv("TC002_HTTP_PASSWORD", config.http.password)

    mqtt_values = raw.get("mqtt", {})
    if isinstance(mqtt_values, dict) and "prefix" not in mqtt_values:
        config.mqtt.prefix = config.uid

    text_fields = {
        "uid": config.uid,
        "name": config.name,
        "timezone": config.timezone,
        "http.host": config.http.host,
        "http.username": config.http.username,
        "http.password": config.http.password,
        "mqtt.host": config.mqtt.host,
        "mqtt.username": config.mqtt.username,
        "mqtt.password": config.mqtt.password,
        "mqtt.prefix": config.mqtt.prefix,
        "mqtt.client_id": config.mqtt.client_id,
        "mqtt.home_assistant_prefix": config.mqtt.home_assistant_prefix,
        "adapter.device_host": config.adapter.device_host,
        "adapter.mode": config.adapter.mode,
        "adapter.listen_host": config.adapter.listen_host,
        "adapter.token": config.adapter.token,
        "adapter.http_app": config.adapter.http_app,
    }
    for field_name, value in text_fields.items():
        if not isinstance(value, str):
            raise validation("Must be a string", field_name)
    boolean_fields = {
        "mqtt.enabled": config.mqtt.enabled,
        "mqtt.tls": config.mqtt.tls,
        "adapter.enabled": config.adapter.enabled,
        "adapter.allow_reboot": config.adapter.allow_reboot,
        "adapter.allow_poweroff": config.adapter.allow_poweroff,
        "compatibility.strict": config.compatibility.strict,
        "compatibility.extensions": config.compatibility.extensions,
        "persistence.persist_pushed_apps": config.persistence.persist_pushed_apps,
        "persistence.persist_current_app": config.persistence.persist_current_app,
    }
    for field_name, value in boolean_fields.items():
        if not isinstance(value, bool):
            raise validation("Must be boolean", field_name)
    integer_fields = {
        "http.port": (config.http.port, 1, 65535),
        "mqtt.port": (config.mqtt.port, 1, 65535),
        "mqtt.keepalive": (config.mqtt.keepalive, 1, 65535),
        "adapter.frame_port": (config.adapter.frame_port, 1, 65535),
        "adapter.event_port": (config.adapter.event_port, 1, 65535),
        "adapter.http_port": (config.adapter.http_port, 1, 65535),
        "adapter.http_max_fps": (config.adapter.http_max_fps, 1, 20),
        "renderer.fps": (config.renderer.fps, 1, 60),
    }
    for field_name, (value, minimum, maximum) in integer_fields.items():
        if type(value) is not int or not minimum <= value <= maximum:
            raise validation(f"Must be an integer between {minimum} and {maximum}", field_name)
    positive_numbers = {
        "renderer.gamma": config.renderer.gamma,
        "adapter.heartbeat_timeout": config.adapter.heartbeat_timeout,
        "adapter.blackout_timeout": config.adapter.blackout_timeout,
        "adapter.http_timeout": config.adapter.http_timeout,
    }
    for field_name, value in positive_numbers.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise validation("Must be a positive number", field_name)
    if not UID_RE.fullmatch(config.uid):
        raise validation("uid must match [A-Za-z0-9_-]{1,64}", "uid")
    if not config.mqtt.prefix or any(char in config.mqtt.prefix for char in "#+\0"):
        raise validation("mqtt.prefix must be non-empty and contain no MQTT wildcards", "mqtt.prefix")
    if not config.mqtt.home_assistant_prefix:
        raise validation("Must not be empty", "mqtt.home_assistant_prefix")
    if not config.adapter.device_host:
        raise validation("Must not be empty", "adapter.device_host")
    if config.adapter.mode not in {"udp", "stock_http"}:
        raise validation("Must be udp or stock_http", "adapter.mode")
    if not UID_RE.fullmatch(config.adapter.http_app):
        raise validation(
            "adapter.http_app must match [A-Za-z0-9_-]{1,64}", "adapter.http_app"
        )
    try:
        ZoneInfo(config.timezone)
    except ZoneInfoNotFoundError as error:
        raise validation("Unknown IANA timezone", "timezone") from error

    if config.compatibility.mode != "awtrix-ng":
        raise validation("compatibility.mode must be awtrix-ng", "compatibility.mode")
    if config.renderer.mode not in {"awtrix_compatible", "tc002_native"}:
        raise validation("Unknown renderer mode", "renderer.mode")
    if len(config.adapter.token.encode("utf-8")) > 32:
        raise validation("adapter.token must be at most 32 UTF-8 bytes", "adapter.token")
    if config.compatibility.strict and config.compatibility.extensions:
        raise validation(
            "Namespaced extensions require compatibility.strict=false",
            "compatibility.extensions",
        )
    if config.compatibility.strict and (
        config.persistence.persist_pushed_apps or config.persistence.persist_current_app
    ):
        raise validation(
            "pushed/current app persistence changes AWTRIX restart semantics and requires strict=false",
            "persistence",
        )
    return config
