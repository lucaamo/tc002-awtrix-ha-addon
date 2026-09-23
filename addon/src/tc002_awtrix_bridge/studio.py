from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp

from .engine import Engine
from .errors import BridgeError, not_found, unavailable, validation
from .rain_display import rain_expected_in_12_hours, rain_timeline
from .validation import DISPLAY_TEMPLATES, parse_color, validate_app_name, validate_pushed_payload

LOGGER = logging.getLogger(__name__)
ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
ICON_RE = re.compile(r"^[A-Za-z0-9_-]{0,64}$")
EFFECTS = {
    "",
    "BrickBreaker",
    "Checkerboard",
    "ColorWaves",
    "Fade",
    "Fireworks",
    "LookingEyes",
    "Matrix",
    "MovingLine",
    "Pacifica",
    "PingPong",
    "Plasma",
    "PlasmaCloud",
    "Radar",
    "Ripple",
    "Snake",
    "SwirlIn",
    "SwirlOut",
    "TheaterChase",
    "TwinklingStars",
}
OVERLAYS = {"", "rain", "snow", "drizzle", "storm", "thunder", "frost"}
WEATHER_ICONS = {
    "clear-night": "clear-night",
    "cloudy": "cloudy",
    "fog": "fog",
    "hail": "hail",
    "lightning": "lightning",
    "lightning-rainy": "lightning-rainy",
    "partlycloudy": "partlycloudy",
    "pouring": "pouring",
    "rainy": "rainy",
    "snowy": "snowy",
    "snowy-rainy": "snowy-rainy",
    "sunny": "sunny",
    "windy": "windy",
    "windy-variant": "windy",
}
STUDIO_KEYS = {
    "displayTemplate",
    "displayTitle",
    "name",
    "sourceType",
    "staticText",
    "entityId",
    "attribute",
    "prefix",
    "suffix",
    "useEntityUnit",
    "decimals",
    "visibleWhen",
    "hideUnavailable",
    "icon",
    "textColor",
    "backgroundColor",
    "font",
    "textCenter",
    "durationMs",
    "scrollSpeed",
    "scrollHoldMs",
    "effect",
    "overlay",
    "enabled",
    "valueTemplate",
    "unitOverride",
    "textCase",
    "iconMode",
    "colorMode",
    "gradientColor",
    "useThreshold",
    "thresholdValue",
    "lowValueTextColor",
    "highValueTextColor",
    "repeat",
    "lifetimeMs",
    "lifetimeExpiry",
    "switchOnChange",
    "scrollEnabled",
    "targetDateTime",
    "countdownTitle",
    "showTitle",
    "showSeconds",
    "reachedText",
    "weatherLanguage",
    "showWeatherText",
    "showTemperature",
    "showWindSpeed",
    "showHumidity",
    "dynamicWeatherIcon",
    "hoursAhead",
    "maxItems",
    "emptyText",
    "showEmpty",
    "chartType",
    "chartAutoscale",
    "chartColor",
    "showRainWhenDry",
    "compactLayout",
    "birthdays",
}

DEFAULT_DEFINITION: dict[str, Any] = {
    "displayTemplate": "inherit",
    "displayTitle": "",
    "name": "",
    "sourceType": "static",
    "staticText": "Ciao!",
    "entityId": "",
    "attribute": "",
    "prefix": "",
    "suffix": "",
    "useEntityUnit": True,
    "decimals": -1,
    "visibleWhen": "",
    "hideUnavailable": True,
    "icon": "",
    "textColor": "#FFFFFF",
    "backgroundColor": "#000000",
    "font": "large",
    "textCenter": True,
    "durationMs": 10_000,
    "scrollSpeed": 85,
    "scrollHoldMs": 1_000,
    "effect": "",
    "overlay": "",
    "enabled": True,
    "valueTemplate": "",
    "unitOverride": "",
    "textCase": "asTyped",
    "iconMode": "push",
    "colorMode": "solid",
    "gradientColor": "#00FFFF",
    "useThreshold": False,
    "thresholdValue": 0.0,
    "lowValueTextColor": "#00FF7F",
    "highValueTextColor": "#FF453A",
    "repeat": 1,
    "lifetimeMs": 0,
    "lifetimeExpiry": "remove",
    "switchOnChange": False,
    "scrollEnabled": True,
    "targetDateTime": "2026-12-31T23:59:59",
    "countdownTitle": "Countdown",
    "showTitle": True,
    "showSeconds": True,
    "reachedText": "Ora!",
    "weatherLanguage": "it",
    "showWeatherText": True,
    "showTemperature": True,
    "showWindSpeed": False,
    "showHumidity": True,
    "dynamicWeatherIcon": True,
    "hoursAhead": 24,
    "maxItems": 3,
    "emptyText": "Nessun elemento",
    "showEmpty": True,
    "chartType": "barChart",
    "chartAutoscale": True,
    "showRainWhenDry": True,
    "compactLayout": False,
    "chartColor": "#4DB8FF",
    "birthdays": [],
}

EntityFetcher = Callable[[str], Awaitable[dict[str, Any]]]
ServiceCaller = Callable[[str, str, dict[str, Any]], Awaitable[Any]]


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise validation("Must be a string", field)
    if len(value) > maximum:
        raise validation(f"Must not exceed {maximum} characters", field)
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise validation(f"Must be an integer from {minimum} to {maximum}", field)
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise validation("Must be true or false", field)
    return value


def validate_definition(payload: Any, *, expected_name: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise validation("Studio app must be an object")
    unknown = set(payload) - STUDIO_KEYS
    if unknown:
        key = min(unknown)
        raise validation(f"Unknown Studio field: {key}", key)
    candidate = copy.deepcopy(DEFAULT_DEFINITION)
    candidate.update(payload)
    name = validate_app_name(candidate["name"])
    if name in {"Time", "Date"}:
        raise validation("Time and Date are reserved app names", "name")
    if expected_name is not None and name != expected_name:
        raise validation("An existing Studio app cannot be renamed", "name")

    source_types = {"static", "entity", "countdown", "weather", "rain", "calendar", "todo", "birthday"}
    if candidate["sourceType"] not in source_types:
        raise validation("Unknown Studio app type", "sourceType")
    if candidate["displayTemplate"] not in DISPLAY_TEMPLATES:
        raise validation("Unknown display template", "displayTemplate")
    candidate["displayTitle"] = _text(candidate["displayTitle"], "displayTitle", 32)
    if any(ord(c) < 32 for c in candidate["displayTitle"]):
        raise validation("Use a single-line title", "displayTitle")
    if candidate["sourceType"] == "rain" and candidate["displayTemplate"] != "inherit":
        raise validation("Rain charts use the default template", "displayTemplate")
    if candidate["compactLayout"] and candidate["sourceType"] in {"weather", "calendar", "todo"} and candidate["displayTemplate"] != "inherit":
        raise validation("Compact layouts use the default template", "displayTemplate")
    candidate["staticText"] = _text(candidate["staticText"], "staticText", 256)
    candidate["entityId"] = _text(candidate["entityId"], "entityId", 128).lower()
    if candidate["sourceType"] in {"entity", "weather", "rain", "calendar", "todo"} and not ENTITY_ID_RE.fullmatch(
        candidate["entityId"]
    ):
        raise validation("Enter a valid Home Assistant entity ID", "entityId")
    required_domain = {"weather": "weather", "rain": "weather", "calendar": "calendar", "todo": "todo"}.get(candidate["sourceType"])
    if required_domain and not candidate["entityId"].startswith(f"{required_domain}."):
        raise validation(f"Choose a {required_domain} entity", "entityId")
    candidate["attribute"] = _text(candidate["attribute"], "attribute", 64)
    candidate["prefix"] = _text(candidate["prefix"], "prefix", 48)
    candidate["suffix"] = _text(candidate["suffix"], "suffix", 48)
    candidate["visibleWhen"] = _text(candidate["visibleWhen"], "visibleWhen", 64)
    candidate["valueTemplate"] = _text(candidate["valueTemplate"], "valueTemplate", 256)
    candidate["unitOverride"] = _text(candidate["unitOverride"], "unitOverride", 48)
    candidate["targetDateTime"] = _text(candidate["targetDateTime"], "targetDateTime", 40)
    candidate["countdownTitle"] = _text(candidate["countdownTitle"], "countdownTitle", 48)
    candidate["reachedText"] = _text(candidate["reachedText"], "reachedText", 128)
    candidate["emptyText"] = _text(candidate["emptyText"], "emptyText", 128)
    candidate["icon"] = _text(candidate["icon"], "icon", 64)
    if not ICON_RE.fullmatch(candidate["icon"]):
        raise validation("Icon must be a safe file name without extension", "icon")

    candidate["decimals"] = _integer(candidate["decimals"], "decimals", -1, 3)
    candidate["durationMs"] = _integer(candidate["durationMs"], "durationMs", 1_000, 3_600_000)
    candidate["scrollSpeed"] = _integer(
        candidate["scrollSpeed"], "scrollSpeed", 10, 1_000
    )
    candidate["scrollHoldMs"] = _integer(
        candidate["scrollHoldMs"], "scrollHoldMs", 0, 10_000
    )
    candidate["repeat"] = _integer(candidate["repeat"], "repeat", 0, 100)
    candidate["lifetimeMs"] = _integer(
        candidate["lifetimeMs"], "lifetimeMs", 0, 86_400_000
    )
    candidate["hoursAhead"] = _integer(candidate["hoursAhead"], "hoursAhead", 1, 720)
    candidate["maxItems"] = _integer(candidate["maxItems"], "maxItems", 1, 20)
    threshold = candidate["thresholdValue"]
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise validation("Threshold must be numeric", "thresholdValue")
    candidate["thresholdValue"] = float(threshold)
    for field in (
        "useEntityUnit",
        "hideUnavailable",
        "textCenter",
        "enabled",
        "useThreshold",
        "switchOnChange",
        "scrollEnabled",
        "showTitle",
        "showSeconds",
        "showWeatherText",
        "showTemperature",
        "showWindSpeed",
        "showHumidity",
        "dynamicWeatherIcon",
        "showEmpty",
        "chartAutoscale",
        "showRainWhenDry",
        "compactLayout",
    ):
        candidate[field] = _boolean(candidate[field], field)

    if candidate["font"] not in {"small", "large"}:
        raise validation("font must be small or large", "font")
    if candidate["effect"] not in EFFECTS:
        raise validation("Unknown effect", "effect")
    if candidate["overlay"] not in OVERLAYS:
        raise validation("Unknown overlay", "overlay")
    if candidate["textCase"] not in {"inherit", "upper", "asTyped"}:
        raise validation("Invalid text case", "textCase")
    if candidate["iconMode"] not in {"fixed", "pushOnce", "push"}:
        raise validation("Invalid icon mode", "iconMode")
    if candidate["colorMode"] not in {"solid", "gradient", "rainbow"}:
        raise validation("Invalid color mode", "colorMode")
    if candidate["lifetimeExpiry"] not in {"remove", "mark"}:
        raise validation("Invalid lifetime expiry", "lifetimeExpiry")
    if candidate["weatherLanguage"] not in {"it", "en"}:
        raise validation("Weather language must be it or en", "weatherLanguage")
    if candidate["chartType"] not in {"barChart", "lineChart"}:
        raise validation("Invalid chart type", "chartType")
    if candidate["sourceType"] == "countdown":
        try:
            datetime.fromisoformat(candidate["targetDateTime"])
        except ValueError as error:
            raise validation("Use an ISO date and time", "targetDateTime") from error
    birthdays = candidate["birthdays"]
    if not isinstance(birthdays, list) or len(birthdays) > 100:
        raise validation("Enter at most 100 birthdays", "birthdays")
    if candidate["sourceType"] == "birthday" and not birthdays:
        raise validation("Enter at least one birthday", "birthdays")
    validated_birthdays = []
    for index, entry in enumerate(birthdays):
        field = f"birthdays.{index}"
        if not isinstance(entry, dict) or set(entry) != {"name", "date", "message"}:
            raise validation("Birthday must contain name, date and message", field)
        person = _text(entry["name"], f"{field}.name", 48).strip()
        birthday_date = _text(entry["date"], f"{field}.date", 5)
        message = _text(entry["message"], f"{field}.message", 160).strip()
        if not person or any(ord(char) < 32 for char in person + message):
            raise validation("Enter a single-line name and message", field)
        try:
            if not re.fullmatch(r"\d{2}-\d{2}", birthday_date):
                raise ValueError("Invalid date format")
            date_value = birthday_date.split("-")
            date(2000, int(date_value[0]), int(date_value[1]))
        except ValueError as error:
            raise validation("Use MM-DD for the birthday", f"{field}.date") from error
        validated_birthdays.append({"name": person, "date": birthday_date, "message": message})
    candidate["birthdays"] = validated_birthdays if candidate["sourceType"] == "birthday" else []
    for field in (
        "textColor",
        "backgroundColor",
        "gradientColor",
        "lowValueTextColor",
        "highValueTextColor",
        "chartColor",
    ):
        candidate[field] = f"#{parse_color(candidate[field], field):06X}"
    return candidate


class StudioManager:
    def __init__(
        self,
        engine: Engine,
        *,
        fetch_entity: EntityFetcher | None = None,
        call_service: ServiceCaller | None = None,
    ) -> None:
        self.engine = engine
        self._fetch_entity_override = fetch_entity
        self._call_service_override = call_service
        self._token = os.getenv("SUPERVISOR_TOKEN", "")
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.definitions: dict[str, dict[str, Any]] = {}
        self.runtime: dict[str, dict[str, Any]] = {}
        stored = self.engine.store.load("studio_apps", {})
        if isinstance(stored, dict):
            for name, value in stored.items():
                try:
                    definition = validate_definition(value, expected_name=name)
                except BridgeError as error:
                    LOGGER.warning("Ignoring invalid Studio app %s: %s", name, error)
                    continue
                self.definitions[name] = definition
                self.runtime[name] = {
                    "lastValue": None,
                    "lastRawValue": None,
                    "lastError": None,
                    "visible": True,
                }
                if definition["sourceType"] != "birthday":
                    self._publish(definition, None)
                self._set_enabled(name, definition["enabled"])

    @property
    def home_assistant_available(self) -> bool:
        return self._fetch_entity_override is not None or bool(self._token)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._fetch_entity_override is None and self._token:
            timeout = aiohttp.ClientTimeout(total=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        await self.refresh_all(force=True)
        self._task = asyncio.create_task(self._loop(), name="studio-apps")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(2)
            await self.refresh_all()

    async def _fetch_home_assistant_entity(self, entity_id: str) -> dict[str, Any]:
        if self._session is None or not self._token:
            raise unavailable("Home Assistant API access is unavailable")
        url = f"http://supervisor/core/api/states/{quote(entity_id, safe='')}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            async with self._session.get(url, headers=headers) as response:
                if response.status == 404:
                    raise not_found(f"Home Assistant entity not found: {entity_id}", "entityId")
                if response.status != 200:
                    raise unavailable(f"Home Assistant API returned HTTP {response.status}")
                value = await response.json()
        except (aiohttp.ClientError, TimeoutError) as error:
            raise unavailable("Cannot reach the Home Assistant API") from error
        if not isinstance(value, dict):
            raise unavailable("Home Assistant returned an invalid entity state")
        return value

    async def _call_home_assistant_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        return_response: bool = True,
    ) -> Any:
        if self._call_service_override is not None:
            return await self._call_service_override(domain, service, data)
        if self._session is None or not self._token:
            raise unavailable("Home Assistant service API access is unavailable")
        url = f"http://supervisor/core/api/services/{quote(domain)}/{quote(service)}"
        if return_response:
            url += "?return_response"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            async with self._session.post(url, headers=headers, json=data) as response:
                if response.status != 200:
                    detail = (await response.text()).strip()
                    message = f"Home Assistant service returned HTTP {response.status}"
                    if detail:
                        message += f": {detail[:240]}"
                    raise unavailable(message)
                return await response.json()
        except (aiohttp.ClientError, TimeoutError) as error:
            raise unavailable("Cannot reach the Home Assistant service API") from error

    async def _list_home_assistant_entities(self, domain: str) -> list[dict[str, Any]]:
        if self._session is None or not self._token:
            raise unavailable("Home Assistant API access is unavailable")
        url = "http://supervisor/core/api/states"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            async with self._session.get(url, headers=headers) as response:
                if response.status != 200:
                    raise unavailable(f"Home Assistant API returned HTTP {response.status}")
                value = await response.json()
        except (aiohttp.ClientError, TimeoutError) as error:
            raise unavailable("Cannot reach the Home Assistant API") from error
        if not isinstance(value, list):
            raise unavailable("Home Assistant returned an invalid entity list")
        prefix = f"{domain}."
        return [
            item
            for item in value
            if isinstance(item, dict) and str(item.get("entity_id", "")).startswith(prefix)
        ]

    async def _fetch_entity(self, entity_id: str) -> dict[str, Any]:
        if self._fetch_entity_override is not None:
            return await self._fetch_entity_override(entity_id)
        return await self._fetch_home_assistant_entity(entity_id)

    async def fetch_entity(self, entity_id: str) -> dict[str, Any]:
        return await self._fetch_entity(entity_id)

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        return await self._call_home_assistant_service(
            domain, service, data, return_response=False
        )

    async def list_entities(self, domain: str) -> list[dict[str, Any]]:
        return await self._list_home_assistant_entities(domain)

    def _save(self) -> None:
        self.engine.store.save("studio_apps", self.definitions)

    def _set_enabled(self, name: str, enabled: bool) -> None:
        disabled = set(self.engine.pages.disabled)
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        order = self.engine.pages.order.copy()
        if name not in order:
            order.append(name)
        self.engine.set_app_order({"order": order, "disabled": sorted(disabled)})

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str):
            return None
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None

    @classmethod
    def _format_number(cls, definition: dict[str, Any], raw: Any) -> str:
        numeric = cls._number(raw)
        if numeric is None:
            return str(raw)
        decimals = int(definition["decimals"])
        if decimals >= 0:
            return f"{numeric:.{decimals}f}"
        if numeric.is_integer():
            return str(int(numeric))
        return str(round(numeric, 3))

    @classmethod
    def _format_value(cls, definition: dict[str, Any], raw: Any, unit: str = "") -> str:
        if raw is None:
            rendered = "--"
        else:
            rendered = cls._format_number(definition, raw)
        unit = definition.get("unitOverride") or unit
        suffix = definition["suffix"]
        if definition["useEntityUnit"] and not suffix and unit:
            suffix = f" {unit}"
        text = f"{definition['prefix']}{rendered}{suffix}"
        template = definition.get("valueTemplate", "")
        if template:
            text = (
                template.replace("{value}", rendered)
                .replace("{unit}", unit)
                .replace("{state}", "" if raw is None else str(raw))
            )
        return text

    def _spec(
        self,
        definition: dict[str, Any],
        raw: Any,
        unit: str = "",
        *,
        text_override: str | None = None,
        extra: dict[str, Any] | None = None,
        icon_override: str | None = None,
    ) -> dict[str, Any]:
        text = (
            definition["staticText"]
            if definition["sourceType"] == "static"
            else self._format_value(definition, raw, unit)
        )
        if text_override is not None:
            text = text_override
        text_color = definition["textColor"]
        numeric = self._number(raw)
        if definition.get("useThreshold") and numeric is not None:
            text_color = (
                definition["highValueTextColor"]
                if numeric >= definition["thresholdValue"]
                else definition["lowValueTextColor"]
            )
        spec: dict[str, Any] = {
            "text": text,
            "textCase": definition["textCase"],
            "textColor": text_color,
            "backgroundColor": definition["backgroundColor"],
            "font": definition["font"],
            "textCenter": definition["textCenter"],
            "durationMs": definition["durationMs"],
            "repeat": definition["repeat"],
            "lifetimeMs": definition["lifetimeMs"],
            "lifetimeExpiry": definition["lifetimeExpiry"],
            "scroll": {
                "mode": "wrap" if definition["scrollEnabled"] else "static",
                "direction": "left",
                "entry": "inline",
                "whenFits": "static",
                "speed": definition["scrollSpeed"],
                "gap": 8,
                "holdMs": definition["scrollHoldMs"],
            },
        }
        if definition["displayTemplate"] != "inherit":
            spec["displayTemplate"] = definition["displayTemplate"]
            spec["displayTitle"] = definition["displayTitle"] or definition["name"]
        if definition["colorMode"] == "rainbow":
            spec.update({"textColor": "palette", "palette": "Rainbow"})
        elif definition["colorMode"] == "gradient":
            spec.update(
                {
                    "textColor": "palette",
                    "palette": [text_color, definition["gradientColor"]],
                }
            )
        if extra:
            spec.update(extra)
        for key in ("effect", "overlay"):
            if definition[key]:
                spec[key] = definition[key]
        icon = definition["icon"] if icon_override is None else icon_override
        if icon:
            spec["icon"] = icon
            spec["iconMode"] = definition["iconMode"]
        return validate_pushed_payload(spec, strict=True)[0]

    def _publish(self, definition: dict[str, Any], raw: Any, unit: str = "") -> None:
        self.engine.push_app(
            definition["name"], self._spec(definition, raw, unit), origin="studio"
        )

    def _compact_spec(self, definition: dict[str, Any], heading: str, value: str) -> dict[str, Any]:
        color = definition["textColor"]
        return self._spec(
            definition, value, text_override="", icon_override="",
            extra={"draw": [
                ["text", 0, 6, heading[:9].upper(), color],
                ["line", 0, 8, 51, 8, "#274354"],
                ["text", 0, 15, value[:9], color],
            ]},
        )

    @staticmethod
    def _service_entity_payload(payload: Any, entity_id: str) -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("service_response"), dict):
            payload = payload["service_response"]
        if isinstance(payload, dict) and isinstance(payload.get(entity_id), dict):
            return payload[entity_id]
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _weather_text(cls, definition: dict[str, Any], state: str, attributes: dict[str, Any]) -> str:
        italian = {
            "clear-night": "Notte limpida",
            "cloudy": "Nuvoloso",
            "fog": "Nebbia",
            "hail": "Grandine",
            "lightning": "Temporale",
            "partlycloudy": "Parzialmente nuvoloso",
            "pouring": "Pioggia forte",
            "rainy": "Pioggia",
            "snowy-rainy": "Neve e pioggia",
            "snowy": "Neve",
            "sunny": "Soleggiato",
            "windy": "Ventoso",
        }
        parts: list[str] = []
        if definition["showWeatherText"]:
            parts.append(italian.get(state, state) if definition["weatherLanguage"] == "it" else state)
        if definition["showTemperature"] and attributes.get("temperature") is not None:
            temperature = cls._format_number(definition, attributes["temperature"])
            parts.append(f"{temperature}{attributes.get('temperature_unit', '°C')}")
        if definition["showWindSpeed"] and attributes.get("wind_speed") is not None:
            wind_speed = cls._format_number(definition, attributes["wind_speed"])
            parts.append(f"{wind_speed}{attributes.get('wind_speed_unit', '')}")
        if definition["showHumidity"] and attributes.get("humidity") is not None:
            humidity = cls._format_number(definition, attributes["humidity"])
            parts.append(f"{humidity}%")
        return " · ".join(parts)

    def _countdown_text(self, definition: dict[str, Any]) -> str:
        zone = ZoneInfo(self.engine.config.timezone)
        target = datetime.fromisoformat(definition["targetDateTime"])
        if target.tzinfo is None:
            target = target.replace(tzinfo=zone)
        remaining = max(0, int((target - datetime.now(zone)).total_seconds()))
        if remaining <= 0:
            return definition["reachedText"]
        days, remainder = divmod(remaining, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, seconds = divmod(remainder, 60)
        if days:
            value = f"{days}g {hours:02d}:{minutes:02d}"
        elif definition["showSeconds"]:
            value = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            value = f"{hours:02d}:{minutes:02d}"
        return f"{definition['countdownTitle']} {value}" if definition["showTitle"] else value

    async def _resolved_spec(self, definition: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        source_type = definition["sourceType"]
        if source_type == "countdown":
            text = self._countdown_text(definition)
            return self._spec(definition, text, text_override=text), text
        if source_type == "birthday":
            now = datetime.now(ZoneInfo(self.engine.config.timezone))
            today = now.strftime("%m-%d")
            matching = [entry for entry in definition["birthdays"] if entry["date"] == today]
            if not matching:
                raise unavailable("No birthday today")
            entry = matching[(now.hour * 60 + now.minute) % len(matching)]
            message = entry["message"] or "Buon compleanno {name}!"
            text = message.replace("{name}", entry["name"])
            return self._spec(definition, text, text_override=text), text

        entity = await self._fetch_entity(definition["entityId"])
        state = str(entity.get("state") or "")
        attributes = entity.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        if definition["visibleWhen"] and state != definition["visibleWhen"]:
            raise unavailable("The configured visibility condition is not met")

        if source_type == "entity":
            raw = attributes.get(definition["attribute"]) if definition["attribute"] else state
            if raw is None or str(raw).lower() in {"unknown", "unavailable", "none"}:
                raise unavailable("The configured entity value is unavailable")
            unit = str(attributes.get("unit_of_measurement") or "")
            return self._spec(definition, raw, unit), raw

        if source_type == "weather":
            text = self._weather_text(definition, state, attributes)
            if definition["compactLayout"]:
                condition = (text.split(" · ", 1)[0] if definition["showWeatherText"] else "METEO")[:9]
                temperature = attributes.get("temperature")
                value = f"{self._format_number(definition, temperature)}°" if temperature is not None else text
                return self._compact_spec(definition, condition, value), text
            icon = None
            if definition["dynamicWeatherIcon"]:
                icon = WEATHER_ICONS.get(state, definition["icon"])
            return self._spec(
                definition, state, text_override=text, icon_override=icon
            ), text

        if source_type == "rain":
            payload = await self._call_home_assistant_service(
                "weather", "get_forecasts", {"entity_id": definition["entityId"], "type": "hourly"}
            )
            response = self._service_entity_payload(payload, definition["entityId"])
            forecasts = response.get("forecast", [])
            if not isinstance(forecasts, list) or not forecasts:
                raise unavailable("Hourly rain forecast is unavailable")
            unit = str(attributes.get("precipitation_unit") or "mm")
            if not definition["showRainWhenDry"] and not rain_expected_in_12_hours(
                forecasts, unit=unit
            ):
                raise unavailable("No rain forecast in the next 12 hours")
            draw, values, _ = rain_timeline(
                forecasts,
                chart_type=definition["chartType"],
                autoscale=definition["chartAutoscale"],
                chart_color=definition["chartColor"],
                unit=unit,
            )
            if not values:
                raise unavailable("Hourly rain forecast has no usable entries")
            return self._spec(
                definition, sum(values), text_override="", extra={"draw": draw}, icon_override=""
            ), values

        if source_type == "calendar":
            now = datetime.now(ZoneInfo(self.engine.config.timezone))
            payload = await self._call_home_assistant_service(
                "calendar",
                "get_events",
                {
                    "entity_id": definition["entityId"],
                    "start_date_time": now.isoformat(),
                    "end_date_time": (now + timedelta(hours=definition["hoursAhead"])).isoformat(),
                },
            )
            response = self._service_entity_payload(payload, definition["entityId"])
            items = response.get("events", [])
        else:  # todo
            payload = await self._call_home_assistant_service(
                "todo", "get_items", {"entity_id": definition["entityId"], "status": "needs_action"}
            )
            response = self._service_entity_payload(payload, definition["entityId"])
            items = response.get("items", [])
        labels = [
            str(item.get("summary") or item.get("subject") or "").strip()
            for item in items[: definition["maxItems"]]
            if isinstance(item, dict)
        ]
        labels = [label for label in labels if label]
        if not labels and not definition["showEmpty"]:
            raise unavailable("No items to show")
        if definition["compactLayout"]:
            heading = "AGENDA" if source_type == "calendar" else f"TODO {len(labels)}"
            if source_type == "calendar" and items and isinstance(items[0], dict):
                start = items[0].get("start")
                if isinstance(start, dict):
                    start = start.get("dateTime") or start.get("date")
                if isinstance(start, str):
                    try:
                        instant = datetime.fromisoformat(start)
                        heading = instant.strftime("%H:%M") if "T" in start else instant.strftime("%d/%m")
                    except ValueError:
                        pass
            value = labels[0] if labels else definition["emptyText"]
            return self._compact_spec(definition, heading, value), labels
        text = f"{definition['prefix']}{' · '.join(labels)}" if labels else definition["emptyText"]
        return self._spec(definition, text, text_override=text), labels

    def _hide(self, name: str) -> None:
        page = self.engine.pages.pages.get(name)
        if page is None or page.origin not in {"pushed", "studio"}:
            return
        try:
            self.engine.delete_app(name)
        except BridgeError as error:
            if error.code != "notFound":
                raise

    async def refresh(self, name: str, *, force: bool = False) -> None:
        definition = self.definitions.get(name)
        if definition is None or definition["sourceType"] == "static":
            return
        runtime = self.runtime.setdefault(
            name,
            {"lastValue": None, "lastRawValue": None, "lastError": None, "visible": True},
        )
        runtime["lastRefreshMs"] = self.engine.monotonic_ms()
        try:
            spec, raw = await self._resolved_spec(definition)
            if self.definitions.get(name) is not definition:
                return
            signature = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            changed = signature != runtime.get("lastSignature")
            now_ms = self.engine.monotonic_ms()
            lifetime_ms = int(definition["lifetimeMs"])
            refresh_lifetime = bool(
                lifetime_ms
                and now_ms - int(runtime.get("lastPublishMs") or 0)
                >= max(1_000, lifetime_ms // 2)
            )
            if force or changed or refresh_lifetime or not runtime.get("visible", True):
                self.engine.push_app(definition["name"], spec, origin="studio")
                runtime["lastPublishMs"] = now_ms
                if (
                    definition["switchOnChange"]
                    and changed
                    and runtime.get("lastSignature") is not None
                ):
                    self.engine.switch_app(definition["name"], fast=False)
            text = (
                next((item[3] for item in spec.get("draw", []) if item[0] == "text"), "")
                if definition["sourceType"] == "rain" else spec.get("text", "")
            )
            runtime.update(
                {
                    "lastValue": text,
                    "lastRawValue": raw,
                    "lastSignature": signature,
                    "lastError": None,
                    "visible": True,
                    "visibilityReason": None,
                    "lastUpdatedAt": datetime.now(UTC).isoformat(),
                }
            )
        except BridgeError as error:
            if self.definitions.get(name) is not definition:
                return
            if error.message == "No rain forecast in the next 12 hours":
                runtime.update({"lastError": None, "visible": False, "visibilityReason": "dryForecast"})
                self._hide(name)
                return
            if error.message == "No birthday today":
                runtime.update({"lastError": None, "visible": False, "visibilityReason": "noBirthdayToday"})
                self._hide(name)
                return
            runtime.update({"lastError": error.message, "visible": not definition["hideUnavailable"], "visibilityReason": "unavailable"})
            if definition["hideUnavailable"]:
                self._hide(name)
            else:
                self._publish(definition, None)
        except Exception as error:  # pragma: no cover - final safety net for external APIs
            if self.definitions.get(name) is not definition:
                return
            LOGGER.exception("Unexpected Studio refresh failure for %s", name)
            runtime.update({"lastError": str(error), "visible": False})
            self._hide(name)

    async def refresh_all(self, *, force: bool = False) -> None:
        now_ms = self.engine.monotonic_ms()
        names = [
            name
            for name, definition in self.definitions.items()
            if definition["sourceType"] != "static"
            and (force or now_ms - self.runtime.get(name, {}).get("lastRefreshMs", -60_000)
                 >= (2_000 if definition["sourceType"] in {"entity", "countdown"} else 60_000))
        ]
        if names:
            await asyncio.gather(*(self.refresh(name) for name in names))

    async def create(self, payload: Any) -> dict[str, Any]:
        definition = validate_definition(payload)
        name = definition["name"]
        if name in self.definitions:
            raise validation("A Studio app with this name already exists", "name")
        if name in self.engine.pages.pages:
            raise validation("This name is already used by another bridge app", "name")
        self.definitions[name] = definition
        self.runtime[name] = {
            "lastValue": None,
            "lastRawValue": None,
            "lastError": None,
            "visible": True,
        }
        if definition["sourceType"] == "static":
            self._publish(definition, None)
            self.runtime[name]["lastValue"] = definition["staticText"]
        elif definition["sourceType"] != "birthday":
            self._publish(definition, None)
        self._set_enabled(name, definition["enabled"])
        self._save()
        if definition["sourceType"] != "static":
            await self.refresh(name, force=True)
        return self.item(name)

    async def update(self, name: str, payload: Any) -> dict[str, Any]:
        if name not in self.definitions:
            raise not_found(f"Studio app not found: {name}", "name")
        definition = validate_definition(payload, expected_name=name)
        self.definitions[name] = definition
        if definition["sourceType"] == "static":
            self._publish(definition, None)
            self.runtime[name] = {
                "lastValue": definition["staticText"],
                "lastRawValue": definition["staticText"],
                "lastError": None,
                "visible": True,
            }
        else:
            await self.refresh(name, force=True)
        self._set_enabled(name, definition["enabled"])
        self._save()
        return self.item(name)

    def delete(self, name: str) -> None:
        if name not in self.definitions:
            raise not_found(f"Studio app not found: {name}", "name")
        self.definitions.pop(name)
        self.runtime.pop(name, None)
        self._hide(name)
        order = [candidate for candidate in self.engine.pages.order if candidate != name]
        disabled = sorted(self.engine.pages.disabled - {name})
        self.engine.set_app_order({"order": order, "disabled": disabled})
        self._save()

    async def rename(self, old_name: str, new_name: str) -> dict[str, Any]:
        if old_name not in self.definitions:
            raise not_found(f"Studio app not found: {old_name}", "name")
        new_name = validate_app_name(new_name)
        if new_name == old_name:
            return self.item(old_name)
        if new_name in self.definitions or new_name in self.engine.pages.pages:
            raise validation("This app name is already in use", "name")
        definition = validate_definition({**self.definitions[old_name], "name": new_name})
        self._spec(definition, None)
        order = [new_name if name == old_name else name for name in self.engine.pages.order]
        disabled = {new_name if name == old_name else name for name in self.engine.pages.disabled}
        runtime = self.runtime.pop(old_name, {})
        self.definitions.pop(old_name)
        self._hide(old_name)
        self.definitions[new_name] = definition
        self.runtime[new_name] = runtime
        if definition["sourceType"] != "birthday":
            self._publish(definition, None)
        self.engine.set_app_order({"order": order, "disabled": sorted(disabled)})
        self._save()
        if definition["sourceType"] != "static":
            await self.refresh(new_name, force=True)
        return self.item(new_name)

    def replace_icon_reference(self, old: str, new: str) -> int:
        """Move saved and currently published apps to a canonical icon name."""

        changed = 0
        for definition in self.definitions.values():
            if definition.get("icon") == old:
                definition["icon"] = new
                changed += 1
        for page in self.engine.pages.pages.values():
            if page.spec.get("icon") == old:
                page.spec["icon"] = new
        if changed:
            self._save()
        return changed

    def item(self, name: str) -> dict[str, Any]:
        definition = self.definitions[name]
        runtime = self.runtime.get(name, {})
        return {
            **copy.deepcopy(definition),
            "lastValue": runtime.get("lastValue"),
            "lastRawValue": runtime.get("lastRawValue"),
            "lastError": runtime.get("lastError"),
            "visible": runtime.get("visible", True),
            "present": name in self.engine.pages.pages,
            "visibilityReason": runtime.get("visibilityReason"),
            "lastUpdatedAt": runtime.get("lastUpdatedAt"),
        }

    def document(self) -> dict[str, Any]:
        return {
            "apps": [self.item(name) for name in self.definitions],
            "homeAssistantAvailable": self.home_assistant_available,
            "timezone": self.engine.config.timezone,
        }

    async def import_definitions(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("apps"), list):
            raise validation("Import must contain an apps list", "apps")
        if not 1 <= len(payload["apps"]) <= 50:
            raise validation("Import must contain 1 to 50 apps", "apps")
        definitions = [validate_definition(item) for item in payload["apps"]]
        names = [item["name"] for item in definitions]
        if len(set(names)) != len(names):
            raise validation("Import contains duplicate app names", "apps")
        if any(name in self.definitions or name in self.engine.pages.pages for name in names):
            raise validation("Import name conflicts with an existing app", "apps")
        for definition in definitions:
            await self.create(definition)
        return {"imported": names}

    def _render_preview_spec(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        spec = self.engine._page_spec(name, spec, None)
        result = self.engine.renderer.render(
            spec,
            render_key=f"studio-preview:{name}",
            elapsed_ms=self.engine.monotonic_ms() % 60_000,
            settings=self.engine.settings,
        )
        pixels = [
            (result.frame[index] << 16)
            | (result.frame[index + 1] << 8)
            | result.frame[index + 2]
            for index in range(0, len(result.frame), 3)
        ]
        description = str(spec.get("text") or " ".join(
            str(command[3]) for command in spec.get("draw", [])
            if isinstance(command, list) and len(command) > 3 and command[0] == "text"
        ) or name)
        warning = None
        if len(str(spec.get("text") or "")) > 12 and spec.get("scroll", {}).get("mode") == "static":
            warning = "Long static text may be clipped on the 52-pixel panel"
        return {"width": 52, "height": 16, "pixels": pixels, "description": description[:120], "warning": warning}

    async def preview_live(self, name: str) -> dict[str, Any]:
        definition = self.definitions.get(name)
        if definition is None:
            raise not_found(f"Studio app not found: {name}", "name")
        if definition["sourceType"] == "static":
            spec = self._spec(definition, definition["staticText"])
        else:
            spec, _ = await self._resolved_spec(definition)
        return self._render_preview_spec(name, spec)

    def preview(self, payload: Any, *, sample_value: Any = None) -> dict[str, Any]:
        definition = validate_definition(payload)
        unit = ""
        if definition["sourceType"] != "static" and sample_value is None:
            sample_value = self.runtime.get(definition["name"], {}).get("lastRawValue") or "123"
        if definition["sourceType"] == "countdown":
            text = self._countdown_text(definition)
            spec = self._spec(definition, text, text_override=text)
        elif definition["sourceType"] == "weather":
            text = "Soleggiato · 21°C · 55%"
            icon = "sunny" if definition["dynamicWeatherIcon"] else None
            spec = (
                self._compact_spec(definition, "SOLE", "21°C")
                if definition["compactLayout"] else
                self._spec(definition, sample_value, text_override=text, icon_override=icon)
            )
        elif definition["sourceType"] == "rain":
            draw, _, _ = rain_timeline(
                [
                    {"precipitation": amount}
                    for amount in (0, 0, 0.4, 1.2, 2.6, 1.1, 0.2, 0, 0, 0)
                ],
                chart_type=definition["chartType"],
                autoscale=definition["chartAutoscale"],
                chart_color=definition["chartColor"],
            )
            spec = self._spec(
                definition,
                sample_value,
                text_override="",
                extra={"draw": draw},
                icon_override="",
            )
        elif definition["sourceType"] in {"calendar", "todo"}:
            if definition["compactLayout"]:
                heading = "AGENDA" if definition["sourceType"] == "calendar" else "TODO 2"
                spec = self._compact_spec(definition, heading, "Esempio")
            else:
                spec = self._spec(
                    definition,
                    sample_value,
                    text_override=f"{definition['prefix']}Esempio · Secondo elemento",
                )
        elif definition["sourceType"] == "birthday":
            entry = definition["birthdays"][0]
            message = entry["message"] or "Buon compleanno {name}!"
            spec = self._spec(definition, entry["name"], text_override=message.replace("{name}", entry["name"]))
        else:
            spec = self._spec(definition, sample_value, unit)
        return self._render_preview_spec(definition["name"], spec)
