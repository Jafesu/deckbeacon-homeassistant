"""DeckBeacon hub: integration key, device endpoints, pairing windows, device runtimes.

One hub per HA instance (ADR-0001/0002). It owns:

* the integration's static X25519 key (HA storage), the responder key for every device;
* ``/api/deckbeacon/v1/pair`` (404 unless a pairing window is open) and
  ``/api/deckbeacon/v1/ws`` (Noise IK sessions from paired devices);
* a :class:`DeviceRuntime` per config entry holding live state for entities.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.ban import process_wrong_login
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from jsonschema import Draft202012Validator

from . import noise
from .artwork import ArtworkStore, ArtworkView
from .const import (
    COMMAND_ACK_TIMEOUT,
    CONF_DEVICE_ID,
    CONF_DEVICE_STATIC,
    CONF_EXTRA_URL,
    CONF_GM_LOCAL,
    CONF_GM_REMOTE,
    CONF_METRIC_KEYS,
    CONF_UPD_LOCAL,
    CONF_UPD_REMOTE,
    CONF_UPDATE_CHANNEL,
    DOMAIN,
    EVENT_DEVICE,
    EXPECTED_DROP_GRACE,
    HANDSHAKE_TIMEOUT,
    HEARTBEAT_SECONDS,
    HELLO_TIMEOUT,
    INTEGRATION_VERSION,
    MAX_FRAME,
    MAX_PREAUTH_PER_IP,
    MAX_PREAUTH_TOTAL,
    PAIRING_WINDOW_SECONDS,
    PATH_PAIR,
    PATH_SESSION,
    SIGNAL_NEW_METRIC,
    SIGNAL_UPDATE,
    TERMINAL_STATES,
)
from .protocol import Channel, InvalidMessage, ProtocolError, load_validator, now_iso, responder_handshake, ulid

_LOGGER = logging.getLogger(__name__)
_HUB_LOCK = "deckbeacon_hub_lock"
CLOSE_REVOKED = 4003
CLOSE_SUPERSEDED = 4009
CLOSE_TRY_AGAIN = 1013


async def async_get_hub(hass: HomeAssistant) -> Hub:
    """Return the singleton hub, creating it (and registering views) on first use."""
    lock = hass.data.setdefault(_HUB_LOCK, asyncio.Lock())
    async with lock:
        if DOMAIN not in hass.data:
            hub = Hub(hass)
            await hub.async_load()
            hass.data[DOMAIN] = hub
        return hass.data[DOMAIN]


@dataclass
class PairingWindow:
    """One open pairing window (owned by one config flow)."""

    flow_id: str
    expires: float
    claimed: bool = False
    device_arrived: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    agent_decision: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    ha_decision: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    finished: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    request: dict[str, Any] | None = None
    device_static: str | None = None
    sas: str | None = None

    def cancel(self, reason: str) -> None:
        for fut in (self.device_arrived, self.agent_decision, self.ha_decision, self.finished):
            if not fut.done():
                fut.set_exception(PairingAborted(reason))
                # Waiters may already be gone; mark the exception as retrieved.
                fut.add_done_callback(lambda f: f.exception())


class PairingAborted(Exception):
    """Pairing ended without success."""


class DeviceRuntime:
    """Live state of one paired device (feeds the entities)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.device_id: str = entry.data[CONF_DEVICE_ID]
        self.metrics: dict[str, dict[str, Any]] = {}
        self.capabilities: list[dict[str, Any]] = []
        self.hello: dict[str, Any] = {}
        self.channel: Channel | None = None
        self.transport: str | None = None
        self.via: str | None = None  # URL kind the agent used (protocol 1.6)
        self.last_seen: datetime | None = None
        self.connected_since: datetime | None = None
        self.last_event_seq = 0
        # Physical power state, distinct from connection state (spec 01 invariant).
        self.power_state = "unknown"
        self.expect_drop_at: float | None = None  # monotonic deadline after an announced sleep/shutdown
        self.library: dict[int, dict[str, Any]] = {}
        self.game_picker: Any = None  # select.GamePicker, used by the Launch button
        self.library_revision = 0
        self._pages: dict[int, dict[int, list[dict[str, Any]]]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self.options_snapshot: dict[str, Any] = {}
        self._store: Store = Store(hass, 1, f"{DOMAIN}.events.{entry.entry_id}")
        self._supersedes: list[float] = []
        self.artwork = ArtworkStore(self)

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.last_event_seq = int(data.get("last_event_seq", 0))
        # Restore an announced state (e.g. "suspended") across reloads and HA
        # restarts; found on the G15 when a reload erased a live "suspended".
        restored = data.get("power_state")
        if restored in _STICKY_POWER_STATES:
            self.power_state = restored

    @property
    def connected(self) -> bool:
        return self.channel is not None

    def _data(self) -> dict[str, Any]:
        return {"last_event_seq": self.last_event_seq, "power_state": self.power_state}

    def _set_power(self, state: str) -> None:
        if state != self.power_state:
            self.power_state = state
            self._store.async_delay_save(self._data, 1.0)

    @callback
    def notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    async def attach(self, channel: Channel, hello: dict[str, Any]) -> None:
        old = self.channel
        if old is not None:
            now = time.monotonic()
            self._supersedes = [t for t in self._supersedes if now - t < 300] + [now]
            if len(self._supersedes) >= 3:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"possible_clone_{self.device_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="possible_clone",
                    translation_placeholders={"name": self.entry.title},
                )
            await old.ws.close(code=CLOSE_SUPERSEDED, message=b"superseded by a newer session")
        self.channel = channel
        self.hello = hello
        self._set_power("on")  # it is talking to us
        self.expect_drop_at = None
        self.transport = hello["session"]["transport"]
        self.via = hello["session"].get("via")
        self.connected_since = self.last_seen = datetime.now(UTC)
        self.notify()

    def detach(self, channel: Channel) -> None:
        if self.channel is channel:
            self.channel = None
            self.transport = None
            # Never show the previous session's values as current after reconnect
            # (spec 04); seen on the G15 as a ~130 ms flash of the old "Last boot".
            self.metrics = {}
            # An announced power-off that is followed by the drop means "off". An
            # announced suspend/reboot stays as announced. An unannounced drop proves
            # nothing about power (Wi-Fi loss, crash, HA restart...): unknown.
            self.expect_drop_at = None
            if self.power_state == "shutting_down":
                self._set_power("off")
            elif self.power_state not in _STICKY_POWER_STATES:
                self._set_power("unknown")
            self.notify()

    def touch(self) -> None:
        self.last_seen = datetime.now(UTC)

    def apply_telemetry(self, metrics: list[dict[str, Any]]) -> None:
        new = []
        for m in metrics:
            if m["key"] not in self.metrics and m["key"] not in self.entry.data.get(CONF_METRIC_KEYS, []):
                new.append(m["key"])
            self.metrics[m["key"]] = m
        if new:
            keys = sorted(set(self.entry.data.get(CONF_METRIC_KEYS, [])) | set(new))
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_METRIC_KEYS: keys})
            async_dispatcher_send(self.hass, SIGNAL_NEW_METRIC.format(self.entry.entry_id), new)
        self.notify()

    def granted(self, transport: str) -> list[str]:
        """Permission tiers for this transport (spec 10: separate local/remote policy)."""
        opts = self.entry.options
        tiers = ["monitoring"]
        if opts.get(CONF_GM_REMOTE if transport == "remote" else CONF_GM_LOCAL, transport != "remote"):
            tiers.append("game_management")
        if opts.get(CONF_UPD_REMOTE if transport == "remote" else CONF_UPD_LOCAL, True):
            tiers.append("updates")
        return tiers

    def apply_library(self, body: dict[str, Any]) -> None:
        """Assemble paged snapshots; switch atomically when a revision is complete."""
        rev, page, pages = body["revision"], body["page"], body["pages"]
        if rev < self.library_revision:
            return
        got = self._pages.setdefault(rev, {})
        got[page] = body["games"]
        if len(got) == pages:
            self.library = {g["app_id"]: g for p in sorted(got) for g in got[p]}
            self.library_revision = rev
            self._pages = {r: v for r, v in self._pages.items() if r > rev}
            self.notify()

    def resolve_command(self, body: dict[str, Any]) -> None:
        fut = self._pending.get(body["command_id"])
        if fut is not None and not fut.done() and body["state"] in TERMINAL_STATES:
            fut.set_result(body)

    async def async_send_command(self, name: str, args: dict[str, Any], ttl_s: int = 60, *, confirmed: bool = False) -> dict[str, Any]:
        """Send a command and wait for its first decisive state (dispatched/confirmed/rejected/...)."""
        ch = self.channel
        if ch is None:
            raise ConnectionError("device is not connected")
        command_id = ulid()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[command_id] = fut
        try:
            body = {"command_id": command_id, "name": name, "args": args, "ttl_s": ttl_s, "issued_at": now_iso()}
            if confirmed:  # the user explicitly confirmed a sensitive action in HA
                body["confirmed_by_user"] = True
            await ch.send("command", body)
            async with asyncio.timeout(COMMAND_ACK_TIMEOUT):
                return await fut
        finally:
            self._pending.pop(command_id, None)

    def accept_event(self, event: dict[str, Any]) -> bool:
        """Fire the HA event unless already delivered. Returns True if fired."""
        seq = event["event_seq"]
        if seq <= self.last_event_seq:
            return False
        self.last_event_seq = seq
        if event["live"]:  # backfilled lifecycle events are history, not current state
            new_state = _POWER_EVENTS.get(event["event_type"])
            if event["event_type"] == "device_shutting_down" and event["data"].get("kind") in _REBOOT_KINDS:
                new_state = "rebooting"
            if new_state in ("suspended", "shutting_down", "rebooting"):
                self.expect_drop_at = time.monotonic() + EXPECTED_DROP_GRACE
            if new_state is not None and new_state != self.power_state:
                self._set_power(new_state)
                self.notify()
        self._store.async_delay_save(self._data, 1.0)
        self.hass.bus.async_fire(
            EVENT_DEVICE,
            {
                "device_id": self.device_id,
                "entry_id": self.entry.entry_id,
                "event_type": event["event_type"],
                "event_id": event["event_id"],
                "event_seq": seq,
                "live": event["live"],
                "occurred_at": event["occurred_at"],
                "data": event["data"],
            },
        )
        return True

    async def async_flush(self) -> None:
        await self._store.async_save(self._data())


_STICKY_POWER_STATES = ("suspended", "shutting_down", "rebooting", "off")
_REBOOT_KINDS = ("reboot", "soft-reboot", "kexec")
_POWER_EVENTS = {
    "device_suspending": "suspended",
    "device_shutting_down": "shutting_down",
    "device_resumed": "on",
    "device_booted": "on",
}


class Hub:
    """Integration-wide state."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.static: noise.KeyPair | None = None
        self.validator: Draft202012Validator | None = None
        self.runtimes: dict[str, DeviceRuntime] = {}  # entry_id -> runtime
        self.windows: list[PairingWindow] = []
        self.preauth_by_ip: dict[str, int] = defaultdict(int)
        self.preauth_total = 0
        self._key_store: Store = Store(hass, 1, f"{DOMAIN}.keys", private=True)

    async def async_load(self) -> None:
        data = await self._key_store.async_load()
        if data and "static_private" in data:
            self.static = noise.KeyPair(bytes.fromhex(data["static_private"]))
        else:
            self.static = noise.KeyPair.generate()
            await self._key_store.async_save({"static_private": self.static.private.hex()})
        self.validator = await self.hass.async_add_executor_job(load_validator)
        self.hass.http.register_view(PairView(self))
        self.hass.http.register_view(SessionView(self))
        self.hass.http.register_view(ArtworkView(self))

    # ---- pairing windows -------------------------------------------------
    def open_window(self, flow_id: str) -> PairingWindow:
        self._expire_windows()
        win = PairingWindow(flow_id=flow_id, expires=time.monotonic() + PAIRING_WINDOW_SECONDS)
        self.windows.append(win)
        return win

    def close_window(self, win: PairingWindow, reason: str) -> None:
        win.cancel(reason)
        if win in self.windows:
            self.windows.remove(win)

    def _expire_windows(self) -> None:
        now = time.monotonic()
        for win in list(self.windows):
            if win.expires < now:
                self.close_window(win, "timeout")

    def claim_window(self) -> PairingWindow | None:
        self._expire_windows()
        for win in self.windows:
            if not win.claimed:
                win.claimed = True  # exactly one device per window
                return win
        return None

    # ---- devices -----------------------------------------------------------
    def entry_for_static(self, static_hex: str) -> ConfigEntry | None:
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_DEVICE_STATIC) == static_hex:
                return entry
        return None

    async def revoke(self, entry: ConfigEntry) -> None:
        runtime = self.runtimes.pop(entry.entry_id, None)
        if runtime and runtime.channel:
            ch = runtime.channel
            try:
                await ch.send("error", {"error": {"code": "permission_denied", "message": "device removed from Home Assistant"}, "fatal": True})
            finally:
                await ch.ws.close(code=CLOSE_REVOKED, message=b"revoked")

    def connection_profile(self, request: web.Request, extra: str | None = None, via: str | None = None) -> list[dict[str, str]]:
        """HA's addresses for devices: internal, external, HA Cloud, plus an
        owner-set extra URL (VPN / reverse proxy) as manual_remote (ADR-0011).

        The address the device reached us on is always included: HA's
        configured URLs can be wrong, and a refresh must never drop a path
        that demonstrably works."""
        urls: list[dict[str, str]] = []
        attempts = (
            ("internal", {"allow_external": False, "allow_cloud": False}),
            ("external", {"allow_internal": False, "allow_cloud": False, "prefer_external": True}),
            ("cloud", {"allow_internal": False, "allow_external": False, "require_cloud": True}),
        )
        for kind, kwargs in attempts:
            try:
                url = get_url(self.hass, **kwargs)
            except NoURLAvailableError:
                continue
            if url.startswith(("http://", "https://")) and all(u["url"] != url for u in urls):
                urls.append({"url": url.rstrip("/"), "kind": kind})
        if extra and extra.startswith(("http://", "https://")) and all(u["url"] != extra.rstrip("/") for u in urls):
            urls.append({"url": extra.rstrip("/"), "kind": "manual_remote"})
        here = f"{request.scheme}://{request.host}"
        if all(u["url"] != here for u in urls):
            remote = via in ("external", "cloud", "manual_remote")
            urls.insert(0, {"url": here, "kind": "manual_remote" if remote else "manual"})
        return urls[:8]

    # ---- pre-auth accounting -----------------------------------------------
    def preauth_enter(self, ip: str) -> bool:
        if self.preauth_total >= MAX_PREAUTH_TOTAL or self.preauth_by_ip[ip] >= MAX_PREAUTH_PER_IP:
            return False
        self.preauth_total += 1
        self.preauth_by_ip[ip] += 1
        return True

    def preauth_leave(self, ip: str) -> None:
        self.preauth_total -= 1
        self.preauth_by_ip[ip] -= 1
        if self.preauth_by_ip[ip] <= 0:
            del self.preauth_by_ip[ip]


class _DeviceView(HomeAssistantView):
    requires_auth = False  # authentication is the Noise handshake (ADR-0002)
    cors_allowed = False

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    async def _open(self, request: web.Request) -> web.WebSocketResponse | None:
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=None, autoping=True)
        if not ws.can_prepare(request).ok:
            return None
        await ws.prepare(request)
        return ws


class PairView(_DeviceView):
    url = PATH_PAIR
    name = "api:deckbeacon:pair"

    async def get(self, request: web.Request) -> web.StreamResponse:
        hub = self.hub
        win = hub.claim_window()
        if win is None:
            return web.Response(status=404)
        ip = request.remote or "?"
        if not hub.preauth_enter(ip):
            win.claimed = False
            return web.Response(status=429)
        try:
            return await self._pair(request, win)
        finally:
            hub.preauth_leave(ip)

    async def _pair(self, request: web.Request, win: PairingWindow) -> web.StreamResponse:
        hub = self.hub
        ws = await self._open(request)
        if ws is None:
            win.claimed = False
            return web.Response(status=400)
        try:
            session = await responder_handshake(ws, "XX", noise.PROLOGUE_PAIR, hub.static, HANDSHAKE_TIMEOUT)
            ch = Channel(ws, session, hub.validator)
            req = await ch.recv(HELLO_TIMEOUT)
            if req["type"] != "pair_request":
                raise ProtocolError("expected pair_request")
            static_hex = session.remote_static.hex()
            if hub.entry_for_static(static_hex) is not None:
                await ch.send("pair_abort", {"reason": "already_paired"})
                hub.close_window(win, "already_paired")
                return ws
            win.request, win.device_static, win.sas = req["body"], static_hex, noise.sas(session.handshake_hash)
            win.device_arrived.set_result(True)

            async def agent_side() -> None:
                msg = await ch.recv(PAIRING_WINDOW_SECONDS)
                if msg["type"] == "pair_confirm":
                    if not win.agent_decision.done():
                        win.agent_decision.set_result(bool(msg["body"]["accepted"]))
                elif msg["type"] == "pair_abort" and not win.agent_decision.done():
                    win.agent_decision.set_result(False)

            agent_task = asyncio.create_task(agent_side())
            try:
                remaining = max(1.0, win.expires - time.monotonic())
                async with asyncio.timeout(remaining):
                    ha_ok = await win.ha_decision
                    if not ha_ok:
                        await ch.send("pair_abort", {"reason": "sas_rejected"})
                        return ws
                    if not await win.agent_decision:
                        return ws
                    # Wait for the flow to create the entry so the agent's first
                    # session is recognised (never mistaken for a revoked device).
                    await win.finished
                    for _ in range(100):
                        if hub.entry_for_static(static_hex) is not None:
                            break
                        await asyncio.sleep(0.1)
                    await ch.send(
                        "pair_complete",
                        {"instance_name": hub.hass.config.location_name or "Home Assistant", "connection_profile": {"urls": hub.connection_profile(request)}},
                    )
            except TimeoutError:
                await ch.send("pair_abort", {"reason": "timeout"})
            except PairingAborted as err:
                reason = str(err) if str(err) in {"already_paired", "timeout", "window_closed", "user_cancelled"} else "window_closed"
                if not ws.closed:
                    await ch.send("pair_abort", {"reason": reason})
            finally:
                agent_task.cancel()
        except (ProtocolError, ConnectionResetError) as err:
            _LOGGER.debug("pairing connection failed: %s", err)
            await process_wrong_login(request)
            if not win.device_arrived.done():
                win.claimed = False  # garbage connection must not burn the window
        finally:
            await ws.close()
        return ws


class SessionView(_DeviceView):
    url = PATH_SESSION
    name = "api:deckbeacon:ws"

    async def get(self, request: web.Request) -> web.StreamResponse:
        hub = self.hub
        ip = request.remote or "?"
        if not hub.preauth_enter(ip):
            return web.Response(status=429)
        authed = False
        try:
            ws = await self._open(request)
            if ws is None:
                return web.Response(status=400)
            try:
                session = await responder_handshake(ws, "IK", noise.PROLOGUE_SESSION, hub.static, HANDSHAKE_TIMEOUT)
                ch = Channel(ws, session, hub.validator)
                hello = await ch.recv(HELLO_TIMEOUT)
                if hello["type"] != "hello":
                    raise ProtocolError("expected hello")
                entry = hub.entry_for_static(session.remote_static.hex())
                if entry is None or hello["body"]["device"]["device_id"] != entry.data[CONF_DEVICE_ID]:
                    await ch.send("error", {"error": {"code": "permission_denied", "message": "device not paired"}, "fatal": True})
                    await process_wrong_login(request)
                    await ws.close(code=CLOSE_REVOKED, message=b"not paired")
                    return ws
                runtime = hub.runtimes.get(entry.entry_id)
                if runtime is None or entry.state is not ConfigEntryState.LOADED:
                    await ch.send("error", {"error": {"code": "unavailable", "message": "integration starting", "retryable": True}, "fatal": True})
                    await ws.close(code=CLOSE_TRY_AGAIN, message=b"try again")
                    return ws
                hub.preauth_leave(ip)
                authed = True
                await self._serve(ch, runtime, hello["body"], request)
            except (ProtocolError, ConnectionResetError) as err:
                _LOGGER.debug("device session ended: %s", err)
                if not authed:
                    await process_wrong_login(request)
            finally:
                await ws.close()
            return ws
        finally:
            if not authed:
                hub.preauth_leave(ip)

    async def _serve(self, ch: Channel, runtime: DeviceRuntime, hello: dict[str, Any], request: web.Request) -> None:
        await runtime.attach(ch, hello)
        via = hello["session"].get("via")
        try:
            await ch.send(
                "welcome",
                {
                    "protocol": 1,
                    "integration_version": INTEGRATION_VERSION,
                    "heartbeat_s": HEARTBEAT_SECONDS,
                    "last_received_event_seq": runtime.last_event_seq,
                    "policy": {
                        "transport": hello["session"]["transport"],
                        "granted": runtime.granted(hello["session"]["transport"]),
                        "update_channel": runtime.entry.options.get(CONF_UPDATE_CHANNEL, "stable"),
                    },
                    # Refreshes the device's stored addresses (ADR-0011).
                    "connection_profile": {"urls": self.hub.connection_profile(request, runtime.entry.options.get(CONF_EXTRA_URL), via)},
                },
            )
            while True:
                timeout = 3 * HEARTBEAT_SECONDS
                if runtime.expect_drop_at is not None:
                    timeout = max(0.0, min(timeout, runtime.expect_drop_at - time.monotonic()))
                try:
                    msg = await ch.recv(timeout)
                except InvalidMessage as err:
                    # Lenient receiver (COMPATIBILITY.md).
                    seq = _unknown_event_seq(self.hub.validator, err.env)
                    if seq is not None:
                        # Event type from a newer agent: acknowledge so it is not resent, skip it.
                        _LOGGER.info("ignoring unknown event type %r from %s", err.env["body"].get("event_type"), runtime.entry.title)
                        runtime.touch()
                        await ch.send("ack", {"ref_id": err.env["id"], "event_seq": seq})
                        continue
                    if err.env.get("type") not in _KNOWN_TYPES and isinstance(err.env.get("id"), str):
                        await ch.send("error", {"error": {"code": "unsupported_type", "message": "unknown message type"}, "fatal": False})
                        continue
                    raise
                runtime.touch()
                await self._handle(ch, runtime, msg)
        finally:
            runtime.detach(ch)

    async def _handle(self, ch: Channel, runtime: DeviceRuntime, msg: dict[str, Any]) -> None:
        body = msg["body"]
        match msg["type"]:
            case "telemetry":
                runtime.apply_telemetry(body["metrics"])
            case "capabilities":
                runtime.capabilities = body["capabilities"]
                runtime.notify()
            case "event":
                runtime.accept_event(body)
                await ch.send("ack", {"ref_id": msg["id"], "event_seq": body["event_seq"]})
            case "ping":
                await ch.send("pong", {"ref_id": msg["id"]})
            case "command_result":
                runtime.resolve_command(body)
                runtime.hass.bus.async_fire(f"{DOMAIN}_command_result", {"device_id": runtime.device_id, **body})
            case "library":
                runtime.apply_library(body)
            case "artwork":
                runtime.artwork.add_chunk(body)
            case "pong" | "ack" | "error":
                pass
            case _:
                await ch.send("error", {"ref_id": msg["id"], "error": {"code": "unsupported_type", "message": f"{msg['type']} not expected"}, "fatal": False})


_KNOWN_TYPES = {
    "hello",
    "welcome",
    "capabilities",
    "telemetry",
    "event",
    "command",
    "command_result",
    "notification",
    "notification_status",
    "ack",
    "ping",
    "pong",
    "error",
    "pair_request",
    "pair_confirm",
    "pair_complete",
    "pair_abort",
    "library",
    "artwork",
}


def _unknown_event_seq(validator: Draft202012Validator, env: dict[str, Any]) -> int | None:
    """Return event_seq if env is a valid event except for an unknown event_type."""
    body = env.get("body")
    if env.get("type") != "event" or not isinstance(body, dict) or not isinstance(body.get("event_type"), str):
        return None
    probe = {**env, "body": {**body, "event_type": "capability_changed"}}
    if any(True for _ in validator.iter_errors(probe)):
        return None
    return body["event_seq"]
