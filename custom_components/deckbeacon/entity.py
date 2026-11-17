"""Base entity for DeckBeacon devices."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import CONF_MODEL, CONF_NAME, CONF_VENDOR, DOMAIN, SIGNAL_UPDATE
from .hub import DeviceRuntime


class DeckBeaconEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: DeviceRuntime, key: str) -> None:
        self.runtime = runtime
        data = runtime.entry.data
        self._attr_unique_id = f"{runtime.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.device_id)},
            name=data[CONF_NAME],
            manufacturer=data.get(CONF_VENDOR) or None,
            model=data.get(CONF_MODEL),
            sw_version=runtime.hello.get("agent_version"),
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE.format(self.runtime.entry.entry_id), self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
