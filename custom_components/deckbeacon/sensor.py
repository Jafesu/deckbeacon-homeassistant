"""Sensors. Metric entities are unavailable while disconnected or when the device
reports ``null``; cached values are never shown as current (spec 04)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DeckBeaconConfigEntry
from .const import CONF_METRIC_KEYS, SIGNAL_NEW_METRIC
from .entity import DeckBeaconEntity


@dataclass(frozen=True, kw_only=True)
class MetricDescription(SensorEntityDescription):
    convert: Callable[[Any], Any] = lambda v: v


def _ts(v: Any) -> datetime | None:
    return dt_util.parse_datetime(v) if isinstance(v, str) else None


_M = SensorStateClass.MEASUREMENT
_DOWNLOAD_STATES = ("idle", "downloading", "paused", "installing", "updating", "verifying", "update_required", "uninstalling", "files_missing", "installed")
METRICS: dict[str, MetricDescription] = {
    d.key: d
    for d in (
        MetricDescription(key="cpu.usage", translation_key="cpu_usage", native_unit_of_measurement=PERCENTAGE, state_class=_M, suggested_display_precision=0),
        MetricDescription(
            key="memory.used_percent", translation_key="memory_used", native_unit_of_measurement=PERCENTAGE, state_class=_M, suggested_display_precision=0
        ),
        MetricDescription(
            key="memory.total_bytes",
            translation_key="memory_total",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
        MetricDescription(
            key="storage.home.used_percent",
            translation_key="storage_home_used",
            native_unit_of_measurement=PERCENTAGE,
            state_class=_M,
            suggested_display_precision=0,
        ),
        MetricDescription(
            key="storage.home.free_bytes",
            translation_key="storage_home_free",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            state_class=_M,
            suggested_display_precision=1,
        ),
        MetricDescription(
            key="storage.sd.used_percent",
            translation_key="storage_sd_used",
            native_unit_of_measurement=PERCENTAGE,
            state_class=_M,
            suggested_display_precision=0,
        ),
        MetricDescription(
            key="storage.sd.free_bytes",
            translation_key="storage_sd_free",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            state_class=_M,
            suggested_display_precision=1,
        ),
        MetricDescription(
            key="system.boot_time",
            translation_key="boot_time",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            convert=_ts,
        ),
        MetricDescription(key="battery.level", device_class=SensorDeviceClass.BATTERY, native_unit_of_measurement=PERCENTAGE, state_class=_M),
        MetricDescription(
            key="battery.power_w",
            translation_key="battery_power",
            device_class=SensorDeviceClass.POWER,
            native_unit_of_measurement=UnitOfPower.WATT,
            state_class=_M,
            suggested_display_precision=1,
        ),
        MetricDescription(
            key="battery.status",
            translation_key="battery_status",
            device_class=SensorDeviceClass.ENUM,
            options=["charging", "discharging", "full", "not_charging", "unknown"],
            convert=lambda v: v if v in {"charging", "discharging", "full", "not_charging"} else "unknown",
        ),
        MetricDescription(
            key="thermal.cpu_c",
            translation_key="cpu_temp",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=_M,
            suggested_display_precision=0,
        ),
        MetricDescription(
            key="thermal.gpu_c",
            translation_key="gpu_temp",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=_M,
            suggested_display_precision=0,
        ),
        MetricDescription(key="fan.rpm", translation_key="fan", native_unit_of_measurement=REVOLUTIONS_PER_MINUTE, state_class=_M),
        MetricDescription(key="gpu.busy", translation_key="gpu_busy", native_unit_of_measurement=PERCENTAGE, state_class=_M),
        MetricDescription(key="game.current.app_id", translation_key="current_app_id", entity_category=EntityCategory.DIAGNOSTIC),
        MetricDescription(key="game.session.started_at", translation_key="session_started", device_class=SensorDeviceClass.TIMESTAMP, convert=_ts),
        MetricDescription(
            key="game.session.duration_s",
            translation_key="session_duration",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            suggested_unit_of_measurement=UnitOfTime.MINUTES,
            suggested_display_precision=0,
        ),
        MetricDescription(key="library.games", translation_key="installed_games", state_class=_M),
        MetricDescription(key="library.updates", translation_key="updates_available", state_class=_M),
        MetricDescription(key="library.shortcuts", translation_key="shortcuts", state_class=_M),
        # ADR-0007 P1 (experimental, opt-in on the device): text set by the game, shown as-is.
        MetricDescription(key="game.activity.details", translation_key="activity_details"),
        MetricDescription(key="game.activity.state", translation_key="activity_state"),
        MetricDescription(key="game.activity.party_size", translation_key="activity_party_size"),
        MetricDescription(key="game.activity.party_max", translation_key="activity_party_max"),
        MetricDescription(key="game.activity.started_at", translation_key="activity_started", device_class=SensorDeviceClass.TIMESTAMP, convert=_ts),
        MetricDescription(
            key="steam.ui_mode",
            translation_key="steam_ui_mode",
            device_class=SensorDeviceClass.ENUM,
            options=["gamepad", "desktop", "unknown"],
            convert=lambda v: None if v is None else (v if v in ("gamepad", "desktop") else "unknown"),
        ),
        MetricDescription(
            key="session.type",
            translation_key="session_type",
            device_class=SensorDeviceClass.ENUM,
            options=["gaming", "desktop"],
            convert=lambda v: v if v in ("gaming", "desktop") else None,
        ),
        MetricDescription(
            key="downloads.status",
            translation_key="download_status",
            device_class=SensorDeviceClass.ENUM,
            options=list(_DOWNLOAD_STATES) + ["unknown"],
            convert=lambda v: v if v in _DOWNLOAD_STATES else "unknown",
        ),
        MetricDescription(key="downloads.name", translation_key="download_name"),
        MetricDescription(key="downloads.progress", translation_key="download_progress", native_unit_of_measurement=PERCENTAGE, suggested_display_precision=1),
        MetricDescription(
            key="downloads.rate_bps",
            translation_key="download_rate",
            device_class=SensorDeviceClass.DATA_RATE,
            native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
            suggested_unit_of_measurement=UnitOfDataRate.MEGABYTES_PER_SECOND,
            suggested_display_precision=1,
        ),
        MetricDescription(
            key="downloads.eta_s",
            translation_key="download_eta",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            suggested_unit_of_measurement=UnitOfTime.MINUTES,
            suggested_display_precision=0,
        ),
        MetricDescription(
            key="session.mode",
            translation_key="session_mode",
            device_class=SensorDeviceClass.ENUM,
            options=["gaming", "desktop", "none", "unknown"],
            convert=lambda v: v if v in {"gaming", "desktop", "none"} else "unknown",
        ),
    )
}


async def async_setup_entry(hass: HomeAssistant, entry: DeckBeaconConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    runtime = entry.runtime_data
    added: set[str] = set()

    @callback
    def add(keys: list[str]) -> None:
        new = [MetricSensor(runtime, METRICS[k]) for k in keys if k in METRICS and k not in added and k != "game.current.name"]
        added.update(e.entity_description.key for e in new)
        if new:
            async_add_entities(new)

    async_add_entities([ConnectionSensor(runtime), PowerStateSensor(runtime), LastSeenSensor(runtime), CurrentGameSensor(runtime)])
    add([k for k in entry.data.get(CONF_METRIC_KEYS, []) if k != "game.current.name"])
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_METRIC.format(entry.entry_id), add))


class MetricSensor(DeckBeaconEntity, SensorEntity):
    entity_description: MetricDescription

    def __init__(self, runtime, description: MetricDescription) -> None:
        super().__init__(runtime, description.key.replace(".", "_"))
        self.entity_description = description

    def _metric(self) -> dict[str, Any] | None:
        return self.runtime.metrics.get(self.entity_description.key)

    @property
    def available(self) -> bool:
        m = self._metric()
        return self.runtime.connected and m is not None and m.get("value") is not None

    @property
    def native_value(self) -> Any:
        m = self._metric()
        return None if m is None else self.entity_description.convert(m.get("value"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        m = self._metric()
        if m is None:
            return None
        attrs = {"source": m.get("source"), "sampled_at": m.get("sampled_at")}
        if m.get("confidence"):
            attrs["confidence"] = m["confidence"]
        return attrs


class ConnectionSensor(DeckBeaconEntity, SensorEntity):
    _attr_translation_key = "connection"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["local", "remote", "offline"]

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "connection")

    @property
    def native_value(self) -> str:
        return self.runtime.transport if self.runtime.connected and self.runtime.transport else "offline"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        # Which address the device is using: internal, external, cloud, ... (ADR-0011).
        return {"via": self.runtime.via} if self.runtime.connected and self.runtime.via else None


class PowerStateSensor(DeckBeaconEntity, SensorEntity):
    """Physical power state as announced by the device, independent of connection."""

    _attr_translation_key = "power_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["on", "suspended", "shutting_down", "rebooting", "off", "unknown"]

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "power_state")

    @property
    def native_value(self) -> str:
        return self.runtime.power_state


class CurrentGameSensor(DeckBeaconEntity, SensorEntity):
    """The game being played. "Not playing" is a known state, not "unavailable"."""

    _attr_translation_key = "current_game"
    _attr_icon = "mdi:gamepad-variant"

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "game_current_name")

    def _metric(self) -> dict[str, Any] | None:
        return self.runtime.metrics.get("game.current.name")

    @property
    def available(self) -> bool:
        m = self._metric()
        if not self.runtime.connected or m is None:
            return False
        return m.get("value") is not None or m.get("reason") == "no game running"

    @property
    def native_value(self) -> str | None:
        m = self._metric()
        if m is None:
            return None
        return m.get("value") if m.get("value") is not None else "Not playing"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        app = self.runtime.metrics.get("game.current.app_id", {}).get("value")
        started = self.runtime.metrics.get("game.session.started_at", {}).get("value")
        return {"app_id": app, "session_started": started} if app else None


class LastSeenSensor(DeckBeaconEntity, SensorEntity):
    """Always available, so staleness is visible even when metrics are not (spec 04)."""

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "last_seen")

    @property
    def native_value(self) -> datetime | None:
        return self.runtime.last_seen.replace(microsecond=0) if self.runtime.last_seen else None
