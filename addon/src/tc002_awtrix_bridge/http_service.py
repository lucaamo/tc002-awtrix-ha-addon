from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from aiohttp import web

from . import __version__
from .config import HttpConfig
from .engine import Engine
from .errors import BridgeError, invalid_json, not_found, unsupported, validation
from .lametric import (
    MODE_FAITHFUL_2X,
    SUPPORTED_MODES,
    LametricIconInvalid,
    LametricIconNotFound,
    LametricIconUnavailable,
    fetch_lametric_icon,
)
from .radio_proxy import RadioProxy
from .sonos import SonosController
from .studio import StudioManager

LOGGER = logging.getLogger(__name__)
ASSET_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}\.(?:gif|jpe?g)$", re.IGNORECASE)
LEGACY_LAMETRIC_NAME = re.compile(r"^(?P<icon_id>[0-9]+)_tc002\.gif$", re.IGNORECASE)
HTTP_SERVICE_KEY = web.AppKey("http_service", object)
DASHBOARD_PATH = Path(__file__).with_name("web") / "index.html"


class ControlAdapter(Protocol):
    def send_control(self, command: str, data: dict[str, Any] | None = None) -> int: ...


class StudioTarget(Protocol):
    def document(self) -> dict[str, Any]: ...

    async def sync_once(self) -> None: ...

    async def show(self, name: str) -> None: ...


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.Response:
    try:
        return await handler(request)
    except BridgeError as error:
        body = error.as_result()
        body.pop("ok", None)
        return web.json_response(body, status=error.status)
    except web.HTTPException:
        raise
    except Exception:
        LOGGER.exception("Unhandled HTTP API error")
        return web.json_response(
            {"error": {"code": "internalError", "message": "Internal error"}},
            status=500,
        )


def _auth_middleware(config: HttpConfig) -> Any:
    @web.middleware
    async def authenticate(request: web.Request, handler: Any) -> web.Response:
        if not config.username:
            return await handler(request)
        expected = "Basic " + base64.b64encode(
            f"{config.username}:{config.password}".encode()
        ).decode("ascii")
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="AWTRIX"'})
        return await handler(request)

    return authenticate


@web.middleware
async def method_override_middleware(request: web.Request, handler: Any) -> web.Response:
    requested = request.headers.get("X-HTTP-Method-Override")
    if requested is None:
        return await handler(request)
    if request.method != "POST":
        raise BridgeError(
            "methodNotAllowed", "X-HTTP-Method-Override is only accepted on POST", status=405
        )
    override = requested.strip().upper()
    if override not in {"PUT", "PATCH", "DELETE"}:
        raise BridgeError(
            "methodNotAllowed",
            "X-HTTP-Method-Override must be PUT, PATCH or DELETE",
            status=405,
        )
    service = cast("HttpService", request.app[HTTP_SERVICE_KEY])
    return await service.dispatch_method_override(request, override)


class HttpService:
    def __init__(
        self,
        engine: Engine,
        config: HttpConfig,
        adapter: ControlAdapter | None = None,
        studio: StudioManager | None = None,
        app_delete_hook: Callable[[str], None] | None = None,
        sonos: SonosController | None = None,
        studio_target: StudioTarget | None = None,
        radio_proxy: RadioProxy | None = None,
    ) -> None:
        self.engine = engine
        self.config = config
        self.adapter = adapter
        self.studio = studio
        self.app_delete_hook = app_delete_hook
        self.sonos = sonos
        self.studio_target = studio_target
        self.radio_proxy = radio_proxy or RadioProxy()
        self.app = web.Application(
            client_max_size=1_100_000,
            middlewares=[error_middleware, _auth_middleware(config), method_override_middleware],
        )
        self.app[HTTP_SERVICE_KEY] = self
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._routes()

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.config.host, self.config.port)
        await self.site.start()

    async def stop(self) -> None:
        await self.radio_proxy.close()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    def _routes(self) -> None:
        router = self.app.router
        router.add_get("/", self.dashboard)
        router.add_get("/healthz", self.health)
        router.add_post("/api/v1/notifications", self.notify)
        router.add_delete("/api/v1/notifications/active", self.dismiss_active)
        router.add_delete("/api/v1/notifications/{name}", self.dismiss_named)
        router.add_get("/api/v1/apps", self.apps)
        router.add_put("/api/v1/apps/active", self.switch_app)
        router.add_post("/api/v1/apps/next", self.next_app)
        router.add_post("/api/v1/apps/previous", self.previous_app)
        router.add_put("/api/v1/apps/order", self.app_order)
        router.add_put("/api/v1/apps/pushed/{name}", self.push_app)
        router.add_delete("/api/v1/apps/{name}", self.delete_app)
        router.add_get("/api/v1/scripts/shared", self.unsupported_scripts)
        router.add_get("/api/v1/apps/script/{name}", self.unsupported_scripts)
        router.add_put("/api/v1/apps/script/{name}", self.unsupported_scripts)
        router.add_get("/api/v1/apps/{name}/config", self.unsupported_scripts)
        router.add_patch("/api/v1/apps/{name}/config", self.unsupported_scripts)
        router.add_get("/api/v1/settings", self.get_settings)
        router.add_patch("/api/v1/settings", self.patch_settings)
        router.add_post("/api/v1/settings/reset", self.reset_settings)
        router.add_get("/api/v1/display", self.get_display)
        router.add_patch("/api/v1/display", self.patch_display)
        router.add_put("/api/v1/display/moodlight", self.moodlight)
        router.add_delete("/api/v1/display/moodlight", self.clear_moodlight)
        router.add_put("/api/v1/indicators/{number}", self.indicator)
        router.add_delete("/api/v1/indicators/{number}", self.clear_indicator)
        router.add_get("/api/v1/audio", self.get_audio)
        router.add_get("/api/v1/radio-proxy/{station}", self.radio_proxy.stream)
        router.add_post("/api/v1/audio/play", self.audio_play)
        router.add_post("/api/v1/audio/stop", self.audio_stop)
        router.add_get("/api/v1/audio/stations", self.unsupported_radio)
        router.add_put("/api/v1/audio/stations", self.unsupported_radio)
        router.add_post("/api/v1/device/reboot", self.reboot)
        router.add_post("/api/v1/device/sleep", self.sleep)
        router.add_post("/api/v1/device/factory-reset", self.factory_reset)
        router.add_get("/api/v1/device", self.device)
        router.add_get("/api/v1/display/screen", self.screen)
        router.add_get("/api/v1/capabilities", self.capabilities)
        router.add_get("/api/v1/version", self.version)
        router.add_get("/version", self.version_plain)
        router.add_get("/api/v1/system/wifi-scan", self.wifi_scan)
        router.add_get("/api/v1/logs", self.logs)
        router.add_post("/update", self.unsupported_update)
        router.add_post("/api/v1/restore", self.unsupported_update)
        router.add_get("/api/v1/files", self.list_files)
        router.add_post("/api/v1/files", self.upload_file)
        router.add_delete("/api/v1/files", self.delete_file)
        router.add_get("/api/v1/studio/apps", self.studio_apps)
        router.add_post("/api/v1/studio/sync", self.studio_sync)
        router.add_get("/api/v1/studio/entities", self.studio_entities)
        router.add_post("/api/v1/studio/apps/{name}/show", self.studio_show)
        router.add_get("/api/v1/studio/apps/{name}/preview", self.studio_live_preview)
        router.add_post("/api/v1/studio/apps/{name}/rename", self.studio_rename)
        router.add_post("/api/v1/studio/import", self.studio_import)
        router.add_post("/api/v1/studio/apps", self.studio_create)
        router.add_put("/api/v1/studio/apps/{name}", self.studio_update)
        router.add_delete("/api/v1/studio/apps/{name}", self.studio_delete)
        router.add_post("/api/v1/studio/preview", self.studio_preview)
        router.add_post("/api/v1/studio/icons/lametric", self.studio_import_lametric_icon)
        router.add_get("/api/v1/sonos", self.sonos_document)
        router.add_patch("/api/v1/sonos/settings", self.sonos_settings)
        router.add_post("/api/v1/sonos/toggle", self.sonos_toggle)
        router.add_post("/api/v1/sonos/actions/{action}", self.sonos_action)
        router.add_post("/{tail:.*}", self.method_override_without_header)

    async def dashboard(self, request: web.Request) -> web.Response:
        return web.Response(
            text=DASHBOARD_PATH.read_text(encoding="utf-8"),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def _json(self, request: web.Request, *, allow_empty: bool = False) -> Any:
        if request.method in {"PUT", "PATCH"} and request.content_type != "application/json":
            raise BridgeError(
                "unsupportedMediaType", "Content-Type must be application/json", status=415
            )
        body = await request.read()
        if len(body) > 8192:
            raise BridgeError("payloadTooLarge", "JSON body exceeds 8192 bytes", status=413)
        if not body and allow_empty:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise invalid_json() from error

    def _ok(self, result: Any = None, *, status: int = 200) -> web.Response:
        body: dict[str, Any] = {"ok": True}
        if result is not None and not self.engine.config.compatibility.strict:
            body["result"] = result
        return web.json_response(body, status=status)

    async def health(self, request: web.Request) -> web.Response:
        body = {
            "ok": True,
            "version": __version__,
            "adapterConnected": self.engine.check_adapter_timeout(),
        }
        if self.studio_target is not None:
            body["studioTarget"] = self.studio_target.document()
        return web.json_response(body)

    async def notify(self, request: web.Request) -> web.Response:
        return self._ok(self.engine.notify(await self._json(request)))

    async def dismiss_active(self, request: web.Request) -> web.Response:
        return self._ok({"removed": self.engine.dismiss_notification()})

    async def dismiss_named(self, request: web.Request) -> web.Response:
        removed = self.engine.dismiss_notification(request.match_info["name"])
        if not removed:
            raise not_found("Notification not found", "name")
        return self._ok({"removed": removed})

    async def apps(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.pages.inventory())

    async def switch_app(self, request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            raise BridgeError(
                "unsupportedMediaType", "Content-Type must be application/json", status=415
            )
        body = await request.read()
        if not body:
            raise invalid_json("App name is required")
        if len(body) > 8192:
            raise BridgeError("payloadTooLarge", "JSON body exceeds 8192 bytes", status=413)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise invalid_json("App name must be UTF-8") from error
        if text.lstrip().startswith("{"):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as error:
                raise invalid_json() from error
            name = value.get("name", value.get("app", ""))
            fast = bool(value.get("fast", False))
        else:
            name, fast = text, False
        return self._ok(self.engine.switch_app(name, fast=fast))

    async def next_app(self, request: web.Request) -> web.Response:
        return self._ok({"name": self.engine.next_app()})

    async def previous_app(self, request: web.Request) -> web.Response:
        return self._ok({"name": self.engine.previous_app()})

    async def app_order(self, request: web.Request) -> web.Response:
        self.engine.set_app_order(await self._json(request))
        return self._ok()

    async def push_app(self, request: web.Request) -> web.Response:
        payload = await self._json(request)
        if payload == {}:
            raise BridgeError(
                "validationFailed", "A body is required; use DELETE to remove the app", status=422
            )
        return self._ok({"apps": self.engine.push_app(request.match_info["name"], payload)})

    async def delete_app(self, request: web.Request) -> web.Response:
        return self._ok({"removed": self._delete_pushed_app(request.match_info["name"])})

    def _delete_pushed_app(self, name: str) -> int:
        if self.studio is not None and name in self.studio.definitions:
            raise BridgeError(
                "conflict",
                "Delete this app from App Studio",
                "name",
                409,
            )
        removed = self.engine.delete_app(name)
        if self.app_delete_hook is not None:
            self.app_delete_hook(name)
        return removed

    async def unsupported_scripts(self, request: web.Request) -> web.Response:
        raise unsupported("Berry scripts are not available on the TC002 bridge")

    async def get_settings(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.settings)

    async def patch_settings(self, request: web.Request) -> web.Response:
        payload = await self._json(request)
        settings = self.engine.patch_settings(payload)
        if self.adapter is not None and "mp3Volume" in payload:
            self.adapter.send_control("audio.volume", {"volume": settings["mp3Volume"]})
        return web.json_response(settings)

    async def reset_settings(self, request: web.Request) -> web.Response:
        settings = self.engine.reset_settings()
        if self.adapter is not None:
            self.adapter.send_control("audio.volume", {"volume": settings["mp3Volume"]})
        return self._ok(settings)

    async def get_display(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.display)

    async def patch_display(self, request: web.Request) -> web.Response:
        return self._ok(self.engine.patch_display(await self._json(request)))

    async def moodlight(self, request: web.Request) -> web.Response:
        payload = await self._json(request)
        if payload == {}:
            raise BridgeError(
                "validationFailed",
                "A body is required; use DELETE to turn moodlight off",
                status=422,
            )
        self.engine.set_moodlight(payload)
        return self._ok()

    async def clear_moodlight(self, request: web.Request) -> web.Response:
        self.engine.set_moodlight(None)
        return self._ok()

    async def indicator(self, request: web.Request) -> web.Response:
        try:
            number = int(request.match_info["number"])
        except ValueError as error:
            raise not_found("Indicator must be 1, 2 or 3") from error
        payload = await self._json(request)
        if payload == {}:
            raise BridgeError(
                "validationFailed",
                "A body is required; use DELETE to turn indicator off",
                status=422,
            )
        return self._ok(self.engine.set_indicator(number, payload))

    async def clear_indicator(self, request: web.Request) -> web.Response:
        try:
            number = int(request.match_info["number"])
        except ValueError as error:
            raise not_found("Indicator must be 1, 2 or 3") from error
        return self._ok(self.engine.set_indicator(number, None))

    async def get_audio(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.audio)

    async def audio_play(self, request: web.Request) -> web.Response:
        if self.engine.config.adapter.mode != "udp":
            raise unsupported("Audio requires adapter.mode=udp and the native companion")
        value = self.engine.play_audio(await self._json(request))
        if self.adapter:
            self.adapter.send_control("audio.play", value)
        return self._ok(value)

    async def audio_stop(self, request: web.Request) -> web.Response:
        if self.engine.config.adapter.mode != "udp":
            raise unsupported("Audio requires adapter.mode=udp and the native companion")
        value = self.engine.stop_audio()
        if self.adapter:
            self.adapter.send_control("audio.stop", {})
        return self._ok(value)

    async def unsupported_radio(self, request: web.Request) -> web.Response:
        raise unsupported("Internet radio is not a verified TC002 capability")

    async def reboot(self, request: web.Request) -> web.Response:
        if self.engine.config.adapter.mode != "udp":
            raise unsupported("Device reboot requires adapter.mode=udp and the native companion")
        if not self.engine.config.adapter.allow_reboot:
            raise unsupported("Device reboot is disabled by adapter.allow_reboot")
        if self.adapter is None:
            raise BridgeError("unavailable", "TC002 adapter is unavailable", status=503)
        sequence = self.adapter.send_control("device.reboot", {})
        return self._ok({"sequence": sequence})

    async def sleep(self, request: web.Request) -> web.Response:
        raise unsupported("Timed sleep and remote wake are not verified on TC002")

    async def factory_reset(self, request: web.Request) -> web.Response:
        raise unsupported("AWTRIX factory reset cannot be mapped safely to TC002")

    async def device(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.device_state())

    async def screen(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.screen_state())

    async def capabilities(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.capabilities())

    async def version(self, request: web.Request) -> web.Response:
        return web.json_response({"version": __version__})

    async def version_plain(self, request: web.Request) -> web.Response:
        return web.Response(text=__version__, content_type="text/plain")

    async def wifi_scan(self, request: web.Request) -> web.Response:
        raise unsupported("TC002 Wi-Fi scan is not implemented by protocol version 1")

    async def logs(self, request: web.Request) -> web.Response:
        return web.json_response({"lines": [], "note": "Use container logs"})

    async def unsupported_update(self, request: web.Request) -> web.Response:
        raise unsupported("AWTRIX firmware/restore images are incompatible with TC002")

    @property
    def _icon_directory(self) -> Path:
        path = Path(self.engine.config.persistence.directory) / "icons"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def list_files(self, request: web.Request) -> web.Response:
        requested = request.query.get("dir", "/ICONS")
        if requested != "/ICONS":
            raise unsupported("Only the /ICONS asset directory is implemented")
        self._canonicalize_legacy_lametric_icons()
        files = [
            {"name": item.name, "size": item.stat().st_size}
            for item in sorted(self._icon_directory.iterdir())
            if item.is_file() and ASSET_NAME.fullmatch(item.name)
        ]
        return web.json_response({"files": files})

    async def upload_file(self, request: web.Request) -> web.Response:
        if not request.content_type.startswith("multipart/"):
            raise BridgeError("unsupportedMediaType", "Expected multipart file upload", status=415)
        reader = await request.multipart()
        part = await reader.next()
        while part is not None and not part.filename:
            part = await reader.next()
        if part is None or not part.filename:
            raise BridgeError("validationFailed", "Multipart upload requires a file", "file", 422)
        filename = Path(part.filename).name
        if not ASSET_NAME.fullmatch(filename):
            raise BridgeError(
                "invalidName",
                "Icon name must be 1-32 safe characters plus .gif/.jpg/.jpeg",
                "file",
                400,
            )
        content = bytearray()
        while chunk := await part.read_chunk():
            content.extend(chunk)
            if len(content) > 1_000_000:
                raise BridgeError("payloadTooLarge", "Icon exceeds 1 MB", "file", 413)
        if not content or len(content) > 1_000_000:
            raise BridgeError(
                "payloadTooLarge", "Icon must be between 1 byte and 1 MB", "file", 413
            )
        if filename.lower().endswith(".gif") and not content.startswith((b"GIF87a", b"GIF89a")):
            raise BridgeError("validationFailed", "File content is not GIF", "file", 422)
        if filename.lower().endswith((".jpg", ".jpeg")) and not content.startswith(b"\xff\xd8\xff"):
            raise BridgeError("validationFailed", "File content is not JPEG", "file", 422)
        self._store_icon(filename, content)
        return self._ok({"name": filename, "size": len(content)})

    def _store_icon(self, filename: str, content: bytes) -> Path:
        target = self._icon_directory / filename
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        self.engine.renderer.icons.invalidate(target.stem)
        return target

    def _replace_icon_references(self, old: str, new: str) -> None:
        if self.studio is not None:
            self.studio.replace_icon_reference(old, new)
        else:
            for page in self.engine.pages.pages.values():
                if page.spec.get("icon") == old:
                    page.spec["icon"] = new

    def _canonicalize_legacy_lametric_icons(self) -> None:
        """Collapse old ``ID``/``ID_tc002`` pairs into one last-selected asset."""

        for legacy in self._icon_directory.iterdir():
            match = LEGACY_LAMETRIC_NAME.fullmatch(legacy.name)
            if not legacy.is_file() or match is None:
                continue
            icon_id = match.group("icon_id")
            canonical = legacy.with_name(f"{icon_id}.gif")
            if not canonical.exists() or legacy.stat().st_mtime_ns > canonical.stat().st_mtime_ns:
                os.replace(legacy, canonical)
            else:
                legacy.unlink()
            self.engine.renderer.icons.invalidate(icon_id)
            self.engine.renderer.icons.invalidate(f"{icon_id}_tc002")
            self._replace_icon_references(f"{icon_id}_tc002", icon_id)

    async def delete_file(self, request: web.Request) -> web.Response:
        requested = request.query.get("path", "")
        requested = requested.removeprefix("/ICONS/")
        filename = Path(requested).name
        if filename != requested and not request.query.get("path", "").startswith("/ICONS/"):
            raise BridgeError("invalidPath", "Path must be under /ICONS", "path", 400)
        if not ASSET_NAME.fullmatch(filename):
            raise BridgeError(
                "invalidPath", "Path must name a GIF or JPEG under /ICONS", "path", 400
            )
        target = self._icon_directory / filename
        try:
            target.unlink()
        except FileNotFoundError as error:
            raise not_found("File not found", "path") from error
        self.engine.renderer.icons.invalidate(target.stem)
        return self._ok()

    def _authenticated_studio(self, request: web.Request) -> StudioManager:
        ingress_remote = request.remote in {"172.30.32.2", "127.0.0.1", "::1"}
        if not ingress_remote or not request.headers.get("X-Remote-User-Id"):
            raise BridgeError(
                "forbidden",
                "App Studio is only available through authenticated Home Assistant Ingress",
                status=403,
            )
        if self.studio is None:
            raise BridgeError("unavailable", "App Studio is unavailable", status=503)
        return self.studio

    def _authenticated_sonos(self, request: web.Request) -> SonosController:
        self._authenticated_studio(request)
        if self.sonos is None:
            raise BridgeError("unavailable", "Sonos control is unavailable", status=503)
        return self.sonos

    async def sonos_document(self, request: web.Request) -> web.Response:
        include_players = request.query.get("players", "1") != "0"
        return web.json_response(
            await self._authenticated_sonos(request).document(include_players=include_players)
        )

    async def sonos_settings(self, request: web.Request) -> web.Response:
        sonos = self._authenticated_sonos(request)
        return web.json_response(await sonos.update_settings(await self._json(request)))

    async def sonos_toggle(self, request: web.Request) -> web.Response:
        sonos = self._authenticated_sonos(request)
        return self._ok({"active": await sonos.toggle_mode()})

    async def sonos_action(self, request: web.Request) -> web.Response:
        sonos = self._authenticated_sonos(request)
        action = request.match_info["action"]
        if action == "playPause":
            await sonos.press()
            result: Any = None
        elif action == "next":
            await sonos.next_track()
            result = None
        elif action == "previous":
            await sonos.previous_track()
            result = None
        elif action == "volumeUp":
            result = {"volume": await sonos.adjust_volume(1)}
        elif action == "volumeDown":
            result = {"volume": await sonos.adjust_volume(-1)}
        else:
            raise not_found("Unknown Sonos action", "action")
        return self._ok(result)

    async def studio_apps(self, request: web.Request) -> web.Response:
        document = self._authenticated_studio(request).document()
        document["target"] = (
            self.studio_target.document()
            if self.studio_target is not None
            else {"enabled": False, "kind": "bridge", "connected": True}
        )
        return web.json_response(document)

    async def studio_sync(self, request: web.Request) -> web.Response:
        self._authenticated_studio(request)
        if self.studio_target is None:
            return web.json_response({"enabled": False, "kind": "bridge", "connected": True})
        await self.studio_target.sync_once()
        return web.json_response(self.studio_target.document())

    async def studio_show(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        name = request.match_info["name"]
        if name not in studio.definitions:
            raise not_found(f"Studio app not found: {name}", "name")
        item = studio.item(name)
        if not item["enabled"] or not item["present"]:
            raise not_found(f"Studio app not found or disabled: {name}", "name")
        if self.studio_target is not None:
            await self.studio_target.show(name)
        else:
            self.engine.switch_app(name, fast=False)
        return self._ok()

    async def studio_entities(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        domain = request.query.get("domain", "")
        if domain not in {"sensor", "binary_sensor", "input_number", "weather", "calendar", "todo"}:
            raise validation("Unsupported entity domain", "domain")
        entries = await studio.list_entities(domain)
        entities = []
        for item in entries:
            entity_id = item.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            attributes = item.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            entities.append(
                {
                    "entityId": entity_id,
                    "name": str(attributes.get("friendly_name") or entity_id)[:100],
                    "state": str(item.get("state") or "")[:64],
                    "unit": str(attributes.get("unit_of_measurement") or "")[:24],
                }
            )
        return web.json_response({"entities": entities})

    async def studio_live_preview(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        return web.json_response(await studio.preview_live(request.match_info["name"]))

    async def studio_import(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        return web.json_response(await studio.import_definitions(await self._json(request)))

    async def studio_rename(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        body = await self._json(request)
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            raise validation("New app name is required", "name")
        return web.json_response(await studio.rename(request.match_info["name"], body["name"]))

    async def studio_create(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        return web.json_response(await studio.create(await self._json(request)), status=201)

    async def studio_update(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        name = request.match_info["name"]
        return web.json_response(await studio.update(name, await self._json(request)))

    async def studio_delete(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        studio.delete(request.match_info["name"])
        return self._ok()

    async def studio_preview(self, request: web.Request) -> web.Response:
        studio = self._authenticated_studio(request)
        body = await self._json(request)
        if not isinstance(body, dict):
            raise invalid_json("Studio preview must be an object")
        sample_value = body.pop("sampleValue", None)
        return web.json_response(studio.preview(body, sample_value=sample_value))

    async def studio_import_lametric_icon(self, request: web.Request) -> web.Response:
        self._authenticated_studio(request)
        body = await self._json(request)
        if not isinstance(body, dict):
            raise invalid_json("LaMetric import must be an object")
        raw_icon_id = body.get("iconId")
        if isinstance(raw_icon_id, bool) or not (
            isinstance(raw_icon_id, int)
            or (isinstance(raw_icon_id, str) and raw_icon_id.isascii() and raw_icon_id.isdigit())
        ):
            raise BridgeError("validationFailed", "LaMetric icon ID must be numeric", "iconId", 422)
        icon_id = int(raw_icon_id)
        if not 1 <= icon_id <= 999_999_999:
            raise BridgeError(
                "validationFailed",
                "LaMetric icon ID must be between 1 and 999999999",
                "iconId",
                422,
            )
        mode = body.get("mode", MODE_FAITHFUL_2X)
        if not isinstance(mode, str) or mode not in SUPPORTED_MODES:
            raise BridgeError(
                "validationFailed",
                "Mode must be faithful2x or tc002Optimized",
                "mode",
                422,
            )

        try:
            icon = await fetch_lametric_icon(icon_id, mode=mode)
        except LametricIconNotFound as error:
            raise BridgeError("notFound", str(error), "iconId", 404) from error
        except LametricIconInvalid as error:
            raise BridgeError("invalidRemoteAsset", str(error), "iconId", 502) from error
        except LametricIconUnavailable as error:
            raise BridgeError("lametricUnavailable", str(error), "iconId", 502) from error

        filename = f"{icon_id}.gif"
        target = self._store_icon(filename, icon.content)
        legacy_stem = f"{icon_id}_tc002"
        legacy = self._icon_directory / f"{legacy_stem}.gif"
        if legacy.exists():
            legacy.unlink()
            self.engine.renderer.icons.invalidate(legacy_stem)
        self._replace_icon_references(legacy_stem, str(icon_id))
        return web.json_response(
            {
                "iconId": icon_id,
                "iconName": target.stem,
                "name": filename,
                "width": icon.width,
                "height": icon.height,
                "frames": icon.frame_count,
                "durationsMs": list(icon.durations_ms),
                "sourceFormat": icon.source_format,
                "mode": icon.mode,
            },
            status=201,
        )

    async def method_override_without_header(self, request: web.Request) -> web.Response:
        raise BridgeError("methodNotAllowed", "No POST route for this path", status=405)

    async def dispatch_method_override(self, request: web.Request, override: str) -> web.Response:
        path = request.path
        if path.startswith("/api/v1/apps/script/"):
            raise BridgeError(
                "methodNotAllowed",
                "X-HTTP-Method-Override cannot upload a script source",
                status=405,
            )
        if path == "/api/v1/notifications/active" and override == "DELETE":
            return await self.dismiss_active(request)
        if path.startswith("/api/v1/notifications/") and override == "DELETE":
            name = path.removeprefix("/api/v1/notifications/")
            removed = self.engine.dismiss_notification(name)
            if not removed:
                raise not_found("Notification not found", "name")
            return self._ok({"removed": removed})
        if path == "/api/v1/settings" and override == "PATCH":
            return await self.patch_settings(request)
        if path == "/api/v1/sonos/settings" and override == "PATCH":
            return await self.sonos_settings(request)
        if path == "/api/v1/display" and override == "PATCH":
            return await self.patch_display(request)
        if path == "/api/v1/apps/active" and override == "PUT":
            return await self.switch_app(request)
        if path == "/api/v1/apps/order" and override == "PUT":
            return await self.app_order(request)
        if path.startswith("/api/v1/apps/pushed/") and override == "PUT":
            name = path.removeprefix("/api/v1/apps/pushed/")
            payload = await self._json(request)
            if payload == {}:
                raise BridgeError(
                    "validationFailed",
                    "A body is required; use DELETE to remove the app",
                    status=422,
                )
            return self._ok({"apps": self.engine.push_app(name, payload)})
        if path.startswith("/api/v1/apps/") and override == "DELETE":
            name = path.removeprefix("/api/v1/apps/")
            return self._ok({"removed": self._delete_pushed_app(name)})
        if path.startswith("/api/v1/studio/apps/"):
            name = path.removeprefix("/api/v1/studio/apps/")
            studio = self._authenticated_studio(request)
            if override == "PUT":
                return web.json_response(await studio.update(name, await self._json(request)))
            if override == "DELETE":
                studio.delete(name)
                return self._ok()
        if path == "/api/v1/display/moodlight":
            if override == "PUT":
                return await self.moodlight(request)
            if override == "DELETE":
                return await self.clear_moodlight(request)
        if path.startswith("/api/v1/indicators/"):
            try:
                number = int(path.removeprefix("/api/v1/indicators/"))
            except ValueError as error:
                raise not_found("Indicator must be 1, 2 or 3") from error
            if override == "PUT":
                payload = await self._json(request)
                if payload == {}:
                    raise BridgeError(
                        "validationFailed",
                        "A body is required; use DELETE to turn indicator off",
                        status=422,
                    )
                return self._ok(self.engine.set_indicator(number, payload))
            if override == "DELETE":
                return self._ok(self.engine.set_indicator(number, None))
        raise BridgeError(
            "methodNotAllowed", "Unsupported method override for this path", status=405
        )
