"""Launch button: starts the game chosen in the game picker (M6)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeckBeaconConfigEntry
from .entity import DeckBeaconEntity
from .services import async_close, async_launch, current_game


async def async_setup_entry(hass: HomeAssistant, entry: DeckBeaconConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    async_add_entities([LaunchButton(rt), CloseGameButton(rt), ForceCloseGameButton(rt)])


class LaunchButton(DeckBeaconEntity, ButtonEntity):
    _attr_translation_key = "launch_game"
    _attr_icon = "mdi:play"

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "launch_game")

    @property
    def available(self) -> bool:
        return self.runtime.connected and "game_management" in self.runtime.granted(self.runtime.transport or "local")

    async def async_press(self) -> None:
        picker = getattr(self.runtime, "game_picker", None)
        app_id = picker.selected_app_id if picker is not None else None
        if app_id is None:
            raise ServiceValidationError("Choose a game in “Game to launch” first")
        await async_launch(self.runtime, app_id)


class CloseGameButton(DeckBeaconEntity, ButtonEntity):
    """Gracefully close the running game."""

    _attr_translation_key = "close_game"
    _attr_icon = "mdi:stop-circle-outline"
    _force = False

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "force_close_game" if self._force else "close_game")

    @property
    def available(self) -> bool:
        rt = self.runtime
        return rt.connected and "game_management" in rt.granted(rt.transport or "local") and current_game(rt) is not None

    async def async_press(self) -> None:
        await async_close(self.runtime, force=self._force)


class ForceCloseGameButton(CloseGameButton):
    """Kill the running game. Disabled by default: an HA button cannot ask for
    confirmation, so enabling it is the user's opt-in (spec 08)."""

    _attr_translation_key = "force_close_game"
    _attr_icon = "mdi:close-octagon"
    _attr_entity_registry_enabled_default = False
    _force = True
