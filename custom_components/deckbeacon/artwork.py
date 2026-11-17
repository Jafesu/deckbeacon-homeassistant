"""Game artwork: fetched from the device on demand, cached on disk, served through
an authenticated view (M8, ADR-0009). Never exposes agent file URLs: the agent
re-encodes images and sends them in chunks over the Noise session."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .hub import DeviceRuntime, Hub

_LOGGER = logging.getLogger(__name__)

KINDS = ("capsule", "header", "hero", "logo")
_EXT = {"image/jpeg": "jpg", "image/png": "png"}
MAX_CACHE_BYTES = 100 << 20
REFRESH_AFTER = 7 * 86400  # Steam artwork rarely changes; refresh weekly when online
MISSING_TTL = 86400  # "no artwork" answers are remembered for a day (no repeated asks)
FETCH_TIMEOUT = 20
SIGN_TTL = timedelta(hours=24)
ARTWORK_URL = "/api/deckbeacon/artwork/{entry_id}/{app_id}/{kind}"


class ArtworkStore:
    """Per-device artwork cache: <config>/.cache/deckbeacon/<device_id>/."""

    def __init__(self, runtime: DeviceRuntime) -> None:
        self.runtime = runtime
        self.dir = Path(runtime.hass.config.path(".cache", "deckbeacon", runtime.device_id))
        self._index: dict[str, dict[str, Any]] | None = None
        self._chunks: dict[str, dict[str, Any]] = {}  # command_id -> partial image
        self._inflight: dict[str, asyncio.Future] = {}
        self._missing: dict[str, float] = {}

    # ---- chunks from the agent ------------------------------------------------
    def add_chunk(self, body: dict[str, Any]) -> None:
        now = time.monotonic()
        for cid in [c for c, p in self._chunks.items() if now - p["at"] > 60]:
            del self._chunks[cid]  # abandoned transfers
        part = self._chunks.setdefault(body["command_id"], {"at": now, "parts": {}, "meta": body})
        if len(self._chunks) > 8 or body["total"] != part["meta"]["total"] or body["sha256"] != part["meta"]["sha256"]:
            self._chunks.pop(body["command_id"], None)
            return
        part["parts"][body["seq"]] = base64.b64decode(body["data"], validate=True)

    def _assemble(self, command_id: str) -> tuple[bytes, dict[str, Any]] | None:
        part = self._chunks.pop(command_id, None)
        if part is None:
            return None
        meta = part["meta"]
        if sorted(part["parts"]) != list(range(meta["total"])):
            return None
        data = b"".join(part["parts"][i] for i in range(meta["total"]))
        if hashlib.sha256(data).hexdigest() != meta["sha256"] or not _magic_ok(data, meta["mime"]):
            _LOGGER.warning("Discarding artwork with a bad checksum or type from %s", self.runtime.entry.title)
            return None
        return data, meta

    # ---- disk cache -----------------------------------------------------------
    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self._index is None:
            try:
                self._index = json.loads((self.dir / "index.json").read_text())
            except (OSError, ValueError):
                self._index = {}
        return self._index

    def _read(self, key: str) -> tuple[bytes, str, float] | None:
        meta = self._load_index().get(key)
        if not meta:
            return None
        try:
            return (self.dir / meta["file"]).read_bytes(), meta["mime"], meta["fetched"]
        except OSError:
            return None

    def _write(self, key: str, data: bytes, mime: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        index = self._load_index()
        name = f"{key}.{_EXT[mime]}"
        tmp = self.dir / (name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.dir / name)
        index[key] = {"file": name, "mime": mime, "fetched": time.time(), "bytes": len(data)}
        total = sum(m["bytes"] for m in index.values())
        for old_key, meta in sorted(index.items(), key=lambda kv: kv[1]["fetched"]):
            if total <= MAX_CACHE_BYTES or old_key == key:
                continue
            (self.dir / meta["file"]).unlink(missing_ok=True)
            total -= meta["bytes"]
            del index[old_key]
        (self.dir / "index.json.tmp").write_text(json.dumps(index))
        os.replace(self.dir / "index.json.tmp", self.dir / "index.json")

    # ---- public ---------------------------------------------------------------
    async def async_get(self, app_id: int, kind: str) -> tuple[bytes, str] | None:
        """Cached artwork; fetched from the device when missing or stale and online."""
        key = f"{app_id}-{kind}"
        hass = self.runtime.hass
        cached = await hass.async_add_executor_job(self._read, key)
        stale = cached is None or time.time() - cached[2] > REFRESH_AFTER
        if stale and self.runtime.connected and time.monotonic() >= self._missing.get(key, 0):
            fresh = await self._fetch(app_id, kind)
            if fresh is not None:
                return fresh
        return (cached[0], cached[1]) if cached else None

    async def _fetch(self, app_id: int, kind: str) -> tuple[bytes, str] | None:
        key = f"{app_id}-{kind}"
        if key in self._inflight:  # one request per (app, kind)
            return await asyncio.shield(self._inflight[key])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        result: tuple[bytes, str] | None = None
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                res = await self.runtime.async_send_command("get_artwork", {"app_id": app_id, "kind": kind}, ttl_s=30)
            if res["state"] == "confirmed":
                got = self._assemble(res["command_id"])
                if got is not None:
                    data, meta = got
                    await self.runtime.hass.async_add_executor_job(self._write, key, data, meta["mime"])
                    result = (data, meta["mime"])
            elif res["state"] == "rejected":  # no artwork of this kind, or an older agent
                self._missing[key] = time.monotonic() + MISSING_TTL
        except (TimeoutError, ConnectionError) as err:
            _LOGGER.debug("Artwork fetch %s failed: %s", key, err)
        finally:
            self._inflight.pop(key, None)
            fut.set_result(result)
        return result


def _magic_ok(data: bytes, mime: str) -> bool:
    if mime == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if mime == "image/png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    return False


def signed_url(hass: HomeAssistant, entry_id: str, app_id: int, kind: str, refresh_token_id: str | None) -> str:
    """A signed path usable in <img> (which cannot send the bearer token)."""
    path = ARTWORK_URL.format(entry_id=entry_id, app_id=app_id, kind=kind)
    return async_sign_path(hass, path, SIGN_TTL, refresh_token_id=refresh_token_id)


class ArtworkView(HomeAssistantView):
    """Authenticated artwork proxy (spec 08: no exposed agent file URLs)."""

    url = "/api/deckbeacon/artwork/{entry_id}/{app_id}/{kind}"
    name = "api:deckbeacon:artwork"
    requires_auth = True

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    async def get(self, request: web.Request, entry_id: str, app_id: str, kind: str) -> web.Response:
        runtime = self.hub.runtimes.get(entry_id)
        if runtime is None or kind not in KINDS or not app_id.isdigit():
            return web.Response(status=HTTPStatus.NOT_FOUND)
        aid = int(app_id)
        if aid not in runtime.library:  # only apps the device reported
            return web.Response(status=HTTPStatus.NOT_FOUND)
        got = await runtime.artwork.async_get(aid, kind)
        if got is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        data, mime = got
        return web.Response(body=data, content_type=mime, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})
