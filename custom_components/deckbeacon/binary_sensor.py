"""Binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeckBeaconConfigEntry
from .entity import DeckBeaconEntity


async def async_setup_entry(hass: HomeAssistant, entry: DeckBeaconConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    async_add_entities(
        [
            ConnectedSensor(rt),
            MetricBinary(rt, "battery.charging", BinarySensorDeviceClass.BATTERY_CHARGING, None),
            MetricBinary(rt, "power.ac_online", BinarySensorDeviceClass.PLUG, "ac_power"),
        ]
    )


class ConnectedSensor(DeckBeaconEntity, BinarySensorEntity):
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "connected")

    @property
    def is_on(self) -> bool:
        return self.runtime.connected


class MetricBinary(DeckBeaconEntity, BinarySensorEntity):
    def __init__(self, runtime, key: str, device_class, translation_key: str | None) -> None:
        super().__init__(runtime, key.replace(".", "_"))
        self._key = key
        self._attr_device_class = device_class
        if translation_key:
            self._attr_translation_key = translation_key

    @property
    def available(self) -> bool:
        m = self.runtime.metrics.get(self._key)
        return self.runtime.connected and m is not None and m.get("value") is not None

    @property
    def is_on(self) -> bool | None:
        m = self.runtime.metrics.get(self._key)
        return None if m is None else bool(m.get("value"))
