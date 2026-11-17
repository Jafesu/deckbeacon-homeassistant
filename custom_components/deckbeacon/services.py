"""DeckBeacon actions: launch_game, list_games, get_game, refresh_library."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

ATTR_DEVICE_ID = "device_id"
ATTR_APP_ID = "app_id"
ATTR_INCLUDE_TOOLS = "include_tools"
ATTR_FORCE = "force"

_DEVICE = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})
_GAME = _DEVICE.extend({vol.Required(ATTR_APP_ID): vol.All(vol.Coerce(int), vol.Range(min=1, max=4294967295))})
_CLOSE = _DEVICE.extend({vol.Optional(ATTR_FORCE, default=False): cv.boolean})
_LIST = _DEVICE.extend({vol.Optional(ATTR_INCLUDE_TOOLS, default=False): cv.boolean})


def _runtime(hass: HomeAssistant, device_id: str):
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device {device_id}")
    hub = hass.data.get(DOMAIN)
    for entry_id in device.config_entries:
        if hub is not None and entry_id in hub.runtimes:
            return hub.runtimes[entry_id]
    raise ServiceValidationError("That device is not a loaded DeckBeacon device")


def _public(game: dict[str, Any]) -> dict[str, Any]:
    return {k: game[k] for k in ("app_id", "name", "kind", "status", "size_on_disk", "last_played", "build_id") if k in game}


async def async_launch(rt, app_id: int) -> dict[str, Any]:
    """Launch an installed game or shortcut (shared by the action and the Launch button)."""
    if not rt.connected:
        raise HomeAssistantError(f"{rt.entry.title} is offline")
    if "game_management" not in rt.granted(rt.transport or "local"):
        raise ServiceValidationError(f"Game management is not allowed over the {rt.transport} connection (DeckBeacon options)")
    game = rt.library.get(app_id)
    if game is None or game.get("kind") == "tool":
        raise ServiceValidationError(f"App {app_id} is not an installed game on {rt.entry.title}")
    try:
        result = await rt.async_send_command("launch_game", {"app_id": app_id}, ttl_s=60)
    except TimeoutError as err:
        raise HomeAssistantError("The device did not answer in time") from err
    except ConnectionError as err:
        raise HomeAssistantError(str(err)) from err
    if result["state"] in ("rejected", "failed", "expired"):
        raise HomeAssistantError(result.get("error", {}).get("message", result["state"]))
    # "dispatched": Steam accepted the request; a deckbeacon_command_result event
    # with state "confirmed" follows once the game process is running.
    return {"state": result["state"], "app_id": app_id, "name": game["name"]}


def current_game(rt) -> tuple[int, str] | None:
    m = rt.metrics.get("game.current.app_id")
    app_id = m.get("value") if m else None
    if not isinstance(app_id, int):
        return None
    name = (rt.metrics.get("game.current.name") or {}).get("value") or (rt.library.get(app_id) or {}).get("name") or f"App {app_id}"
    return app_id, name


async def async_close(rt, force: bool = False) -> dict[str, Any]:
    """Close the running game (shared by the action and the buttons).

    Force close kills the game without letting it save; calling the action with
    force, or pressing the dedicated button, is the user's explicit confirmation."""
    if not rt.connected:
        raise HomeAssistantError(f"{rt.entry.title} is offline")
    if "game_management" not in rt.granted(rt.transport or "local"):
        raise ServiceValidationError(f"Game management is not allowed over the {rt.transport} connection (DeckBeacon options)")
    game = current_game(rt)
    if game is None:
        raise ServiceValidationError(f"No game is running on {rt.entry.title}")
    app_id, name = game
    try:
        result = await rt.async_send_command("close_game", {"app_id": app_id, "force": force}, ttl_s=30, confirmed=force)
    except TimeoutError as err:
        raise HomeAssistantError("The device did not answer in time") from err
    except ConnectionError as err:
        raise HomeAssistantError(str(err)) from err
    if result["state"] in ("rejected", "failed", "expired"):
        raise HomeAssistantError(result.get("error", {}).get("message", result["state"]))
    # "dispatched": the game was signalled; deckbeacon_command_result reports
    # "confirmed" once it has exited, or "failed" (suggesting force close).
    return {"state": result["state"], "app_id": app_id, "name": name, "force": force}


@callback
def async_register_services(hass: HomeAssistant) -> None:
    async def launch_game(call: ServiceCall) -> ServiceResponse:
        return await async_launch(_runtime(hass, call.data[ATTR_DEVICE_ID]), call.data[ATTR_APP_ID])

    async def list_games(call: ServiceCall) -> ServiceResponse:
        rt = _runtime(hass, call.data[ATTR_DEVICE_ID])
        games = [_public(g) for g in sorted(rt.library.values(), key=lambda g: g["name"].lower()) if call.data[ATTR_INCLUDE_TOOLS] or g["kind"] != "tool"]
        return {"games": games, "count": len(games), "revision": rt.library_revision}

    async def get_game(call: ServiceCall) -> ServiceResponse:
        rt = _runtime(hass, call.data[ATTR_DEVICE_ID])
        game = rt.library.get(call.data[ATTR_APP_ID])
        if game is None:
            raise ServiceValidationError(f"App {call.data[ATTR_APP_ID]} is not in the library")
        return _public(game)

    async def refresh_library(call: ServiceCall) -> None:
        rt = _runtime(hass, call.data[ATTR_DEVICE_ID])
        try:
            await rt.async_send_command("refresh_library", {}, ttl_s=30)
        except (TimeoutError, ConnectionError) as err:
            raise HomeAssistantError(f"Could not refresh: {err}") from err

    hass.services.async_register(DOMAIN, "launch_game", launch_game, schema=_GAME, supports_response=SupportsResponse.OPTIONAL)

    async def close_game(call: ServiceCall) -> ServiceResponse:
        return await async_close(_runtime(hass, call.data[ATTR_DEVICE_ID]), call.data[ATTR_FORCE])

    hass.services.async_register(DOMAIN, "close_game", close_game, schema=_CLOSE, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "list_games", list_games, schema=_LIST, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "get_game", get_game, schema=_GAME, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "refresh_library", refresh_library, schema=_DEVICE)
