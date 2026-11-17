"""WebSocket API for the optional dashboard cards (M8). Cards talk to the
integration only, never to the agent."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .artwork import KINDS, signed_url
from .const import DOMAIN


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_devices)
    websocket_api.async_register_command(hass, ws_library)
    websocket_api.async_register_command(hass, ws_artwork_url)


def _runtimes(hass: HomeAssistant) -> dict[str, Any]:
    hub = hass.data.get(DOMAIN)
    return hub.runtimes if hub is not None else {}


def _by_device(hass: HomeAssistant, device_id: str):
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        if (rt := _runtimes(hass).get(entry_id)) is not None:
            return rt
    return None


def _ha_device_id(hass: HomeAssistant, rt) -> str | None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, rt.device_id)})
    return device.id if device else None


def _describe(hass: HomeAssistant, rt) -> dict[str, Any]:
    prefix = f"{rt.device_id}_"
    entities = {
        e.unique_id[len(prefix) :]: e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), rt.entry.entry_id)
        if e.unique_id.startswith(prefix)
    }
    transport = rt.transport or "local"
    return {
        "device_id": _ha_device_id(hass, rt),
        "entry_id": rt.entry.entry_id,
        "name": rt.entry.title,
        "connected": rt.connected,
        "transport": rt.transport,
        "power_state": rt.power_state,
        "entities": entities,
        # Why a metric has no value (spec 08: unavailable vs unsupported vs disabled vs unknown).
        # Keyed like `entities` (metric key with "." -> "_").
        "reasons": {k.replace(".", "_"): m["reason"] for k, m in rt.metrics.items() if m.get("value") is None and m.get("reason")},
        "capabilities": [{k: c[k] for k in ("id", "status", "reason", "experimental") if k in c} for c in rt.capabilities],
        "can_launch": "game_management" in rt.granted(transport),
        "library_revision": rt.library_revision,
    }


@websocket_api.websocket_command({vol.Required("type"): "deckbeacon/devices"})
@callback
def ws_devices(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"devices": [_describe(hass, rt) for rt in _runtimes(hass).values()]})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "deckbeacon/library",
        vol.Required("device_id"): str,
        vol.Optional("include_tools", default=False): bool,
    }
)
@callback
def ws_library(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    rt = _by_device(hass, msg["device_id"])
    if rt is None:
        connection.send_error(msg["id"], "not_found", "Not a DeckBeacon device")
        return
    games = []
    for g in sorted(rt.library.values(), key=lambda g: g["name"].lower()):
        if g["kind"] == "tool" and not msg["include_tools"]:
            continue
        art = {k: signed_url(hass, rt.entry.entry_id, g["app_id"], k, connection.refresh_token_id) for k in ("capsule", "header")}
        games.append({k: g[k] for k in ("app_id", "name", "kind", "status", "size_on_disk", "last_played") if k in g} | {"artwork": art})
    connection.send_result(msg["id"], {"revision": rt.library_revision, "games": games, "can_launch": "game_management" in rt.granted(rt.transport or "local")})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "deckbeacon/artwork_url",
        vol.Required("device_id"): str,
        vol.Required("app_id"): vol.All(int, vol.Range(min=1, max=4294967295)),
        vol.Required("kind"): vol.In(KINDS),
    }
)
@callback
def ws_artwork_url(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    rt = _by_device(hass, msg["device_id"])
    if rt is None or msg["app_id"] not in rt.library:
        connection.send_error(msg["id"], "not_found", "Unknown device or game")
        return
    connection.send_result(msg["id"], {"url": signed_url(hass, rt.entry.entry_id, msg["app_id"], msg["kind"], connection.refresh_token_id)})
