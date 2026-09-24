"""Volatile App Studio output for an AWTRIX NG 52x16 device."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from .adapter import awtrix_ng_payload
from .engine import Engine
from .errors import not_found, unavailable

if TYPE_CHECKING:
    from .studio import StudioManager

APP_PREFIX = "Studio_"
LEGACY_APP_PREFIX = "tc002studio_"
APP_LIFETIME_MS = 15000
REFRESH_SECONDS = 7.0


def ng_studio_name(name: str) -> str:
    """Build a readable, deterministic AWTRIX id from the Studio title."""
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_name).strip("_-")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        slug = "App_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:6]
    return (APP_PREFIX + slug)[:32].rstrip("_-")


def _reserved_app_id(name: str) -> bool:
    return name.startswith((APP_PREFIX, LEGACY_APP_PREFIX))


class NgStudioPublisher:
    def __init__(
        self,
        engine: Engine,
        base_url: str,
        *,
        studio: StudioManager | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.engine = engine
        self.studio = studio
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._sent: dict[str, tuple[bytes, float]] = {}
        self._signatures: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.connected = False
        self.last_sync_at: str | None = None
        self.last_error: str | None = None
        self.synced_apps: set[str] = set()
        self.active_app: str | None = None
        self._remote_to_local: dict[str, str] = {}

    async def start(self) -> None:
        if self._session is not None:
            return
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        self.connected = False

    def document(self) -> dict[str, Any]:
        desired = self._desired()
        return {
            "enabled": True,
            "kind": "awtrix_ng",
            "connected": self.connected,
            "lastSyncAt": self.last_sync_at,
            "lastError": self.last_error,
            "activeApp": self.active_app,
            "syncedApps": sorted(self.synced_apps & set(desired.values())),
            "pendingApps": sorted(set(desired.values()) - self.synced_apps),
        }

    def record_error(self, error: BaseException) -> None:
        self.connected = False
        self.last_error = str(error)[:240]

    def _desired(self) -> dict[str, str]:
        desired: dict[str, str] = {}
        for name in self.engine.pages.visible_names():
            if self.engine.pages.pages[name].origin != "studio":
                continue
            app_id = ng_studio_name(name)
            if app_id in desired and desired[app_id] != name:
                suffix = "_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:6]
                app_id = app_id[: 32 - len(suffix)].rstrip("_-") + suffix
            desired[app_id] = name
        return desired

    def _switch_on_change(self, name: str) -> bool:
        if self.studio is None:
            return False
        definition = self.studio.definitions.get(name)
        return bool(definition and definition.get("switchOnChange"))

    def _signature(self, name: str) -> str:
        page = self.engine.pages.pages[name]
        return json.dumps(page.spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    async def _publish(self, app_id: str, name: str, now_ms: int, *, force: bool) -> bool:
        assert self._session is not None
        page = self.engine.pages.pages.get(name)
        if page is None or page.origin != "studio" or name in self.engine.pages.disabled:
            raise not_found(f"Enabled Studio app not found: {name}", "name")
        result = self.engine.render_studio_app(
            name, elapsed_ms=max(0, now_ms - page.received_ms)
        )
        frame = result.frame
        last = self._sent.get(app_id)
        changed = last is not None and last[0] != frame
        due = force or last is None or last[0] != frame or time.monotonic() - last[1] >= REFRESH_SECONDS
        if not due:
            return False
        payload: dict[str, Any] = awtrix_ng_payload(
            frame, int(self.engine.display["brightness"]), APP_LIFETIME_MS
        )
        payload["durationMs"] = int(page.spec.get("durationMs", 7000))
        async with self._session.put(
            f"{self.base_url}/api/v1/apps/pushed/{app_id}", json=payload
        ) as response:
            response.raise_for_status()
            body = await response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise ValueError(f"AWTRIX NG rejected Studio app {app_id}")
        self._sent[app_id] = (frame, time.monotonic())
        self._remote_to_local[app_id] = name
        self.synced_apps.add(name)
        return changed

    async def _select(self, app_id: str) -> None:
        assert self._session is not None
        async with self._session.put(
            f"{self.base_url}/api/v1/apps/active",
            json={"name": app_id, "fast": True},
        ) as response:
            response.raise_for_status()
            body = await response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise ValueError(f"AWTRIX NG rejected app selection {app_id}")

    def _mark_success(self) -> None:
        self.connected = True
        self.last_error = None
        self.last_sync_at = datetime.now(UTC).isoformat()

    async def _sync_order(
        self, apps: list[Any], desired: dict[str, str]
    ) -> None:
        """Keep Studio apps mutually ordered without disturbing native app positions."""
        assert self._session is not None
        has_obsolete_reserved = any(
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and _reserved_app_id(item["name"])
            and item["name"] not in desired
            for item in apps
        )
        remote_items = [
            item
            for item in apps
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item.get("present") is True
            and not (
                _reserved_app_id(item["name"]) and item["name"] not in desired
            )
        ]
        names = [item["name"] for item in remote_items]
        current = [name for name in names if name in desired]
        wanted = [name for name in desired if name in current]
        missing = [name for name in desired if name not in names]
        if (
            not has_obsolete_reserved
            and not missing
            and (len(wanted) < 2 or current == wanted)
        ):
            return
        replacements = iter(wanted)
        order = [next(replacements) if name in desired else name for name in names]
        order.extend(missing)
        disabled = [item["name"] for item in remote_items if item.get("enabled") is False]
        async with self._session.put(
            f"{self.base_url}/api/v1/apps/order",
            json={"order": order, "disabled": disabled},
        ) as response:
            response.raise_for_status()
            body = await response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise ValueError("AWTRIX NG rejected Studio app order")

    async def sync_once(self, *, now_ms: int | None = None) -> None:
        """Reconcile only names in the reserved Studio namespace."""
        if self._session is None:
            raise RuntimeError("publisher is not started")
        async with self._lock:
            try:
                now_ms = self.engine.monotonic_ms() if now_ms is None else now_ms
                async with self._session.get(f"{self.base_url}/api/v1/apps") as response:
                    response.raise_for_status()
                    apps = await response.json()
                if not isinstance(apps, list):
                    raise TypeError("AWTRIX NG returned an invalid app inventory")
                remote = {
                    item["name"]
                    for item in apps
                    if isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and _reserved_app_id(item["name"])
                    and item.get("present") is True
                    and item.get("origin") == "pushed"
                }
                desired = self._desired()
                self._remote_to_local = desired.copy()
                for app_id in sorted(remote - desired.keys()):
                    async with self._session.delete(
                        f"{self.base_url}/api/v1/apps/{app_id}"
                    ) as response:
                        response.raise_for_status()
                    self._sent.pop(app_id, None)
                    self._signatures.pop(app_id, None)
                self.synced_apps = {
                    name for app_id, name in desired.items() if app_id in remote
                }
                for app_id, name in desired.items():
                    signature = self._signature(name)
                    previous_signature = self._signatures.get(app_id)
                    force = app_id not in remote
                    await self._publish(app_id, name, now_ms, force=force)
                    self._signatures[app_id] = signature
                    if (
                        previous_signature is not None
                        and previous_signature != signature
                        and self._switch_on_change(name)
                    ):
                        await self._select(app_id)
                await self._sync_order(apps, desired)
                self._mark_success()
            except Exception as error:
                self.record_error(error)
                raise

    async def refresh_active_once(self) -> bool:
        """Refresh an active bitmap often enough for smooth Studio animation."""
        if self._session is None:
            raise RuntimeError("publisher is not started")
        async with self._lock:
            try:
                async with self._session.get(f"{self.base_url}/api/v1/device") as response:
                    response.raise_for_status()
                    device = await response.json()
                if not isinstance(device, dict):
                    raise TypeError("AWTRIX NG returned invalid device state")
                remote_name = device.get("currentApp")
                name = self._remote_to_local.get(remote_name) if isinstance(remote_name, str) else None
                self.active_app = name
                if name is not None:
                    await self._publish(
                        remote_name, name, self.engine.monotonic_ms(), force=True
                    )
                self._mark_success()
                return name is not None
            except Exception as error:
                self.record_error(error)
                raise

    async def show(self, name: str) -> None:
        """Publish a Studio app immediately, then select it on AWTRIX NG."""
        if self._session is None:
            raise unavailable("AWTRIX NG Studio publisher is not started")
        app_id = next(
            (app_id for app_id, local_name in self._desired().items() if local_name == name),
            ng_studio_name(name),
        )
        async with self._lock:
            try:
                await self._publish(app_id, name, self.engine.monotonic_ms(), force=True)
                await self._select(app_id)
                self.active_app = name
                self._mark_success()
            except Exception as error:
                self.record_error(error)
                if isinstance(error, aiohttp.ClientError | TimeoutError):
                    raise unavailable("Cannot reach AWTRIX NG") from error
                raise
