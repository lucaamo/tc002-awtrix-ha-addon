from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .engine import Engine
from .errors import BridgeError, unavailable, validation

LOGGER = logging.getLogger(__name__)

EntityFetcher = Callable[[str], Awaitable[dict[str, Any]]]
ServiceCaller = Callable[[str, str, dict[str, Any]], Awaitable[Any]]
EntityLister = Callable[[str], Awaitable[list[dict[str, Any]]]]
VOLUME_OSD_MS = 2000
VOLUME_SETTLE_MS = 3000

DEFAULT_SONOS_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "playerEntityId": "",
    "playlistMediaContentId": "",
    "playlistMediaContentType": "playlist",
    "volumeStep": 5,
    "longPressMs": 1200,
}


def validate_sonos_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise validation("Sonos settings must be an object")
    unknown = set(value) - set(DEFAULT_SONOS_SETTINGS)
    if unknown:
        key = min(unknown)
        raise validation(f"Unknown Sonos setting: {key}", key)
    result = copy.deepcopy(value)
    if "enabled" in result and not isinstance(result["enabled"], bool):
        raise validation("Must be boolean", "enabled")
    if "playerEntityId" in result:
        player = result["playerEntityId"]
        if not isinstance(player, str) or (
            player and not player.startswith("media_player.")
        ):
            raise validation("Choose a media_player entity", "playerEntityId")
        result["playerEntityId"] = player.strip()
    for key, maximum in (
        ("playlistMediaContentId", 1024),
        ("playlistMediaContentType", 128),
    ):
        if key in result:
            item = result[key]
            if not isinstance(item, str) or len(item) > maximum:
                raise validation(f"Must be text up to {maximum} characters", key)
            result[key] = item.strip()
    for key, minimum, maximum in (
        ("volumeStep", 1, 25),
        ("longPressMs", 600, 3000),
    ):
        if key in result:
            item = result[key]
            if (
                not isinstance(item, int)
                or isinstance(item, bool)
                or not minimum <= item <= maximum
            ):
                raise validation(
                    f"Must be an integer between {minimum} and {maximum}", key
                )
    return result


class SonosController:
    def __init__(
        self,
        engine: Engine,
        *,
        fetch_entity: EntityFetcher,
        call_service: ServiceCaller,
        list_entities: EntityLister,
    ) -> None:
        self.engine = engine
        self._fetch_entity = fetch_entity
        self._call_service = call_service
        self._list_entities = list_entities
        self.settings = copy.deepcopy(DEFAULT_SONOS_SETTINGS)
        stored = self.engine.store.load("sonos_settings", {})
        if isinstance(stored, dict):
            try:
                self.settings.update(validate_sonos_settings(stored))
            except BridgeError as error:
                LOGGER.warning("Ignoring invalid Sonos settings: %s", error)
        self.active = False
        self.session_started = False
        self.player_state = "idle"
        self.friendly_name = "Sonos"
        self.artist = ""
        self.title = ""
        self.volume = 0
        self._volume_pending_until_ms = 0
        self._volume_overlay_value = 0
        self._volume_overlay_until_ms = 0
        self.last_error: str | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._refresh_loop(), name="sonos-control")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.active:
            self.active = False
            self.engine.clear_exclusive_page()

    async def _refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1)
            if self.active:
                await self.refresh()

    async def update_settings(self, patch: Any) -> dict[str, Any]:
        async with self._lock:
            return self.patch_settings(patch)

    def patch_settings(self, patch: Any) -> dict[str, Any]:
        candidate = copy.deepcopy(self.settings)
        candidate.update(validate_sonos_settings(patch))
        self.engine.store.save("sonos_settings", candidate)
        player_changed = candidate["playerEntityId"] != self.settings["playerEntityId"]
        self.settings = candidate
        if player_changed:
            self.session_started = False
            self._volume_pending_until_ms = 0
            self._volume_overlay_until_ms = 0
            self.artist = self.title = ""
            self.volume = 0
        if not self.settings["enabled"] and self.active:
            self.active = False
            self.engine.clear_exclusive_page()
        elif self.active:
            self._render()
        return copy.deepcopy(self.settings)

    async def players(self) -> list[dict[str, str]]:
        try:
            values = await self._list_entities("media_player")
        except BridgeError:
            return []
        result = []
        for value in values:
            entity_id = value.get("entity_id")
            attributes = value.get("attributes")
            if (
                not isinstance(entity_id, str)
                or not entity_id.startswith("media_player.")
                or not isinstance(attributes, dict)
            ):
                continue
            result.append(
                {
                    "entityId": entity_id,
                    "name": str(attributes.get("friendly_name") or entity_id),
                }
            )
        return sorted(result, key=lambda item: (item["name"].lower(), item["entityId"]))

    async def document(self, *, include_players: bool = True) -> dict[str, Any]:
        result = {
            "settings": copy.deepcopy(self.settings),
            "state": self.state(),
        }
        if include_players:
            result["players"] = await self.players()
        return result

    def state(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "sessionStarted": self.session_started,
            "playerState": self.player_state,
            "friendlyName": self.friendly_name,
            "artist": self.artist,
            "title": self.title,
            "volume": self.volume,
            "lastError": self.last_error,
        }

    async def toggle_mode(self) -> bool:
        async with self._lock:
            if self.active:
                self.active = False
                self.session_started = False
                self.last_error = None
                self.engine.clear_exclusive_page()
                return False
            if not self.settings["enabled"]:
                raise unavailable("Sonos mode is disabled")
            if not self.settings["playerEntityId"]:
                raise validation("Choose a Sonos player first", "playerEntityId")
            self._volume_pending_until_ms = 0
            self._volume_overlay_until_ms = 0
            self.active = True
            self.session_started = False
            self.last_error = None
            self.player_state = "loading"
            self.artist = ""
            self.title = "Premi per avviare"
            self._render()
            await self._refresh_unlocked()
            return True

    async def press(self) -> None:
        async with self._lock:
            self._require_active()
            player = self.settings["playerEntityId"]
            playlist = self.settings["playlistMediaContentId"]
            if not self.session_started and playlist:
                await self._call_service(
                    "media_player",
                    "play_media",
                    {
                        "entity_id": player,
                        "media_content_id": playlist,
                        "media_content_type": self.settings["playlistMediaContentType"],
                    },
                )
                self.session_started = True
            else:
                await self._call_service(
                    "media_player", "media_play_pause", {"entity_id": player}
                )
                self.session_started = True
            await self._refresh_unlocked()

    async def next_track(self) -> None:
        await self._command("media_next_track")

    async def previous_track(self) -> None:
        await self._command("media_previous_track")

    async def _command(self, service: str) -> None:
        async with self._lock:
            self._require_active()
            await self._call_service(
                "media_player", service, {"entity_id": self.settings["playerEntityId"]}
            )
            self.session_started = True
            await self._refresh_unlocked()

    async def adjust_volume(self, direction: int) -> int:
        async with self._lock:
            self._require_active()
            await self._refresh_unlocked()
            value = max(
                0,
                min(100, self.volume + (1 if direction > 0 else -1) * self.settings["volumeStep"]),
            )
            await self._call_service(
                "media_player",
                "volume_set",
                {
                    "entity_id": self.settings["playerEntityId"],
                    "volume_level": value / 100,
                },
            )
            self.volume = value
            self._volume_pending_until_ms = self.engine.monotonic_ms() + VOLUME_SETTLE_MS
            self.last_error = None
            self._volume_overlay_value = value
            self._volume_overlay_until_ms = self.engine.monotonic_ms() + VOLUME_OSD_MS
            self._render(volume_overlay=True)
            return value

    async def refresh(self) -> None:
        async with self._lock:
            await self._refresh_unlocked()

    async def remote_command(self, body: Any) -> dict[str, Any]:
        """Execute one validated command received from the AWTRIX NG remote."""
        if not isinstance(body, dict):
            raise validation("Sonos remote command must be an object")
        action = body.get("action")
        if action not in {
            "refresh",
            "play_pause",
            "next",
            "previous",
            "volume",
            "delta",
            "play_media",
        }:
            raise validation("Unknown Sonos remote action", "action")
        player = body.get("player_entity_id") or self.settings["playerEntityId"]
        if not isinstance(player, str) or not player.startswith("media_player."):
            raise validation("Choose a media_player entity", "player_entity_id")
        player = player.strip()

        async with self._lock:
            current = await self._fetch_entity(player)
            if not isinstance(current, dict):
                raise validation("Home Assistant did not return the selected player")
            if player != self.settings["playerEntityId"]:
                self.patch_settings({"playerEntityId": player})

            if action == "play_pause":
                await self._call_service(
                    "media_player", "media_play_pause", {"entity_id": player}
                )
                self.session_started = True
            elif action == "next":
                await self._call_service(
                    "media_player", "media_next_track", {"entity_id": player}
                )
                self.session_started = True
            elif action == "previous":
                await self._call_service(
                    "media_player", "media_previous_track", {"entity_id": player}
                )
                self.session_started = True
            elif action in {"volume", "delta"}:
                attributes = current.get("attributes")
                attributes = attributes if isinstance(attributes, dict) else {}
                level = attributes.get("volume_level")
                observed = (
                    round(float(level) * 100)
                    if isinstance(level, (int, float)) and not isinstance(level, bool)
                    else self.volume
                )
                raw = body.get("value")
                if not isinstance(raw, int) or isinstance(raw, bool):
                    raise validation("Volume must be an integer", "value")
                if action == "delta" and not -25 <= raw <= 25:
                    raise validation("Volume delta must be between -25 and 25", "value")
                if action == "volume" and not 0 <= raw <= 100:
                    raise validation("Volume must be between 0 and 100", "value")
                target = max(0, min(100, observed + raw if action == "delta" else raw))
                await self._call_service(
                    "media_player",
                    "volume_set",
                    {"entity_id": player, "volume_level": target / 100},
                )
                self.volume = target
                self._volume_pending_until_ms = self.engine.monotonic_ms() + VOLUME_SETTLE_MS
            elif action == "play_media":
                content_id = body.get("media_content_id")
                content_type = body.get("media_content_type")
                if not isinstance(content_id, str) or not content_id.strip():
                    raise validation("Media content is required", "media_content_id")
                if not isinstance(content_type, str) or not content_type.strip():
                    raise validation("Media content type is required", "media_content_type")
                await self._call_service(
                    "media_player",
                    "play_media",
                    {
                        "entity_id": player,
                        "media_content_id": content_id.strip(),
                        "media_content_type": content_type.strip(),
                    },
                )
                self.session_started = True

            updated = await self._fetch_entity(player)
            self._apply_entity_state(updated)
            self.last_error = None
            return self.state()

    async def _refresh_unlocked(self) -> None:
        if not self.active:
            return
        try:
            player = self.settings["playerEntityId"]
            value = await self._fetch_entity(player)
            if not self.active or player != self.settings["playerEntityId"]:
                return
            self._apply_entity_state(value)
            self.last_error = None
        except BridgeError as error:
            self.last_error = error.message
        except Exception as error:  # keep hardware input tasks isolated
            LOGGER.exception("Cannot refresh Sonos player")
            self.last_error = str(error)
        if (
            self.last_error is None
            and self.engine.monotonic_ms() < self._volume_overlay_until_ms
        ):
            return
        self._render()

    def _apply_entity_state(self, value: dict[str, Any]) -> None:
        attributes = value.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        self.player_state = str(value.get("state") or "unknown")
        self.friendly_name = str(attributes.get("friendly_name") or "Sonos")
        self.artist = str(attributes.get("media_artist") or "")
        self.title = str(
            attributes.get("media_title")
            or attributes.get("media_channel")
            or attributes.get("source")
            or ""
        )
        level = attributes.get("volume_level")
        if isinstance(level, (int, float)) and not isinstance(level, bool):
            observed = max(0, min(100, round(float(level) * 100)))
            if observed == self.volume:
                self._volume_pending_until_ms = 0
            if self.engine.monotonic_ms() >= self._volume_pending_until_ms:
                self.volume = observed

    def _require_active(self) -> None:
        if not self.active:
            raise unavailable("Sonos mode is not active")

    def _render(self, *, volume_overlay: bool = False) -> None:
        if not self.active:
            return
        volume_overlay = volume_overlay or (
            self.engine.monotonic_ms() < self._volume_overlay_until_ms
        )
        if self.last_error:
            header = "SONOS ERRORE"
            body = self.last_error
            color = "#FF453A"
        elif volume_overlay:
            header = "SONOS"
            body = f"{self._volume_overlay_value}%"
            color = "#FFFFFF"
        else:
            header = self.artist or self.friendly_name or "SONOS"
            if self.title:
                body = self.title
            elif self.player_state == "playing":
                body = "In riproduzione"
            elif self.player_state == "paused":
                body = "In pausa"
            else:
                body = "Premi per avviare"
            color = "#FFFFFF"
        if self.player_state == "playing":
            symbol = ">"
        elif self.player_state == "paused":
            symbol = "II"
        else:
            symbol = "*"
        text = " · ".join(part for part in (symbol, self.artist, self.title or body) if part)
        show_music_icon = not volume_overlay and self.last_error is None
        draw = [
            ["line", 2, 4, 2, 11, color],
            ["line", 2, 4, 7, 2, color],
            ["line", 7, 2, 7, 9, color],
            ["circleFill", 1, 12, 2, color],
            ["circleFill", 6, 10, 2, color],
        ]
        spec = {
            "text": text,
            "textCase": "asTyped",
            "font": "small",
            "textColor": color,
            "backgroundColor": "#000000",
            "textCenter": True,
            "repeat": 99,
            "scroll": {
                "mode": "wrap",
                "direction": "left",
                "entry": "inline",
                "whenFits": "static",
                "speed": int(self.engine.settings.get("scroll", {}).get("speed", 50)),
                "gap": 8,
                "holdMs": 700,
            },
            "draw": [] if volume_overlay or show_music_icon else draw,
            "_contentLeft": 15 if volume_overlay else 12 if show_music_icon else 10,
            "_twoLineLayout": True,
            "_headerText": header,
            "_twoLineBody": body,
            "_scrollHeader": not volume_overlay,
            "_headerRestText": header if self.last_error is None else "SONOS",
        }
        if volume_overlay:
            spec["_sonosVolumeIcon"] = self._volume_overlay_value
        elif show_music_icon:
            spec["_sonosMusicIcon"] = self.player_state == "playing"
        self.engine.set_exclusive_page("SonosControl", spec)
