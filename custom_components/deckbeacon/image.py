"""Now-playing artwork as a native image entity (works without the custom cards)."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DeckBeaconConfigEntry
from .entity import DeckBeaconEntity


async def async_setup_entry(hass: HomeAssistant, entry: DeckBeaconConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([NowPlayingArtwork(hass, entry.runtime_data)])


class NowPlayingArtwork(DeckBeaconEntity, ImageEntity):
    _attr_translation_key = "now_playing_artwork"

    def __init__(self, hass: HomeAssistant, runtime) -> None:
        DeckBeaconEntity.__init__(self, runtime, "now_playing_artwork")
        ImageEntity.__init__(self, hass)
        self._app_id: int | None = None
        self._attr_content_type = "image/jpeg"

    def _current(self) -> int | None:
        m = self.runtime.metrics.get("game.current.app_id")
        v = m.get("value") if m else None
        return int(v) if isinstance(v, int) else None

    @property
    def available(self) -> bool:
        return self._current() is not None

    @callback
    def _updated(self) -> None:
        app_id = self._current()
        if app_id != self._app_id:
            self._app_id = app_id
            self._attr_image_last_updated = dt_util.utcnow()
            self._cached_image = None
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        app_id = self._current()
        if app_id is None:
            return None
        for kind in ("capsule", "header"):
            got = await self.runtime.artwork.async_get(app_id, kind)
            if got is not None:
                if got[1] != self._attr_content_type:
                    self._attr_content_type = got[1]
                    self.__dict__.pop("content_type", None)  # cached_property on newer HA
                return got[0]
        return None
