"""Game picker for the Launch button (dashboard-friendly launch, M6)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import DeckBeaconConfigEntry
from .entity import DeckBeaconEntity


def launchable(runtime) -> dict[str, int]:
    """Label -> app_id for installed games and shortcuts. Shortcut names are not
    unique (three "Discord" entries on the G15), so duplicates get their id."""
    games = [g for g in runtime.library.values() if g.get("kind") in ("game", "shortcut") and g.get("status") == "installed"]
    counts: dict[str, int] = {}
    for g in games:
        counts[g["name"]] = counts.get(g["name"], 0) + 1
    out = {}
    for g in sorted(games, key=lambda g: (g["name"].lower(), g["app_id"])):
        label = g["name"] if counts[g["name"]] == 1 else f"{g['name']} ({g['app_id']})"
        out[label] = g["app_id"]
    return out


async def async_setup_entry(hass: HomeAssistant, entry: DeckBeaconConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([GamePicker(entry.runtime_data)])


class GamePicker(DeckBeaconEntity, SelectEntity, RestoreEntity):
    _attr_translation_key = "game_to_launch"
    _attr_icon = "mdi:gamepad-variant"

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "game_to_launch")
        self._app_id: int | None = None
        runtime.game_picker = self

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and (app_id := last.attributes.get("app_id")) is not None:
            self._app_id = int(app_id)

    @property
    def available(self) -> bool:
        return bool(launchable(self.runtime))

    @property
    def options(self) -> list[str]:
        return list(launchable(self.runtime))

    @property
    def current_option(self) -> str | None:
        for label, app_id in launchable(self.runtime).items():
            if app_id == self._app_id:
                return label
        return None

    @property
    def selected_app_id(self) -> int | None:
        return self._app_id if self.current_option is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return {"app_id": self._app_id} if self._app_id is not None else None

    async def async_select_option(self, option: str) -> None:
        self._app_id = launchable(self.runtime)[option]
        self.async_write_ha_state()
