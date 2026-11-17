"""Launch button: starts the game chosen in the game picker (M6)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeckBeaconConfigEntry
from .entity import DeckBeaconEntity
from .services import async_launch


async def async_setup_entry(hass: HomeAssistant, entry: DeckBeaconConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([LaunchButton(entry.runtime_data)])


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
