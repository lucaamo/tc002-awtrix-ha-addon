from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping

from aiohttp import web

LOGGER = logging.getLogger(__name__)

# These three national stations currently publish their live audio as HLS/AAC.
# The TC002 audio backend decodes MP3, so the Home Assistant add-on converts
# only these known public feeds to a small mono MP3 stream on demand.
RADIO_PROXY_SOURCES: Mapping[str, str] = {
    "radio-italia": (
        "https://radioitaliasmi.akamaized.net/hls/live/2093120/RISMI/stream01/streamPlaylist.m3u8"
    ),
    "radio-deejay": (
        "https://4c4b867c89244861ac216426883d1ad0.msvdn.net/radiodeejay/radiodeejay/master_ma.m3u8"
    ),
    "m2o": ("https://4c4b867c89244861ac216426883d1ad0.msvdn.net/radiom2o/radiom2o/master_ma.m3u8"),
}


class RadioProxy:
    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        sources: Mapping[str, str] = RADIO_PROXY_SOURCES,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path or os.environ.get("TC002_FFMPEG", "ffmpeg")
        self.sources = dict(sources)
        self._processes: set[asyncio.subprocess.Process] = set()

    def command(self, source: str) -> tuple[str, ...]:
        return (
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "2",
            "-i",
            source,
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "96k",
            "-write_xing",
            "0",
            "-id3v2_version",
            "0",
            "-f",
            "mp3",
            "pipe:1",
        )

    async def stream(self, request: web.Request) -> web.StreamResponse:
        station = request.match_info["station"]
        source = self.sources.get(station)
        if source is None:
            raise web.HTTPNotFound(
                text='{"error":{"code":"notFound","message":"unknown radio station"}}',
                content_type="application/json",
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(source),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            LOGGER.error("Cannot start radio transcoder: %s", error)
            raise web.HTTPServiceUnavailable(
                text='{"error":{"code":"unavailable","message":"radio transcoder unavailable"}}',
                content_type="application/json",
            ) from error
        self._processes.add(process)
        response: web.StreamResponse | None = None
        try:
            assert process.stdout is not None
            try:
                first = await asyncio.wait_for(process.stdout.read(8192), timeout=15)
            except TimeoutError as error:
                raise web.HTTPBadGateway(
                    text='{"error":{"code":"upstreamTimeout","message":"radio source timed out"}}',
                    content_type="application/json",
                ) from error
            if not first:
                detail = await self._stderr(process)
                LOGGER.warning("Radio source %s produced no MP3 data: %s", station, detail)
                raise web.HTTPBadGateway(
                    text='{"error":{"code":"upstreamError","message":"radio source unavailable"}}',
                    content_type="application/json",
                )
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "audio/mpeg",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
            await response.prepare(request)
            await response.write(first)
            while chunk := await process.stdout.read(8192):
                await response.write(chunk)
            return response
        except (ConnectionResetError, BrokenPipeError):
            if response is not None:
                return response
            raise
        finally:
            await self._terminate(process)

    async def close(self) -> None:
        await asyncio.gather(*(self._terminate(process) for process in tuple(self._processes)))

    async def _stderr(self, process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(process.stderr.read(2048), timeout=1)
        except TimeoutError:
            return ""
        return data.decode("utf-8", "replace").strip()

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        self._processes.discard(process)
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()
