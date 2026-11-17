"""Agent update entity (ADR-0010): installed vs latest, release notes, install
with explicit approval, progress and the last error."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeckBeaconConfigEntry
from .entity import DeckBeaconEntity

_IN_PROGRESS = ("waiting", "downloading", "installing")


async def async_setup_entry(hass: HomeAssistant, entry: DeckBeaconConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([AgentUpdate(entry.runtime_data)])


class AgentUpdate(DeckBeaconEntity, UpdateEntity):
    _attr_translation_key = "agent_update"
    _attr_title = "DeckBeacon agent"
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "agent_update")

    def _m(self, key: str) -> Any:
        m = self.runtime.metrics.get(f"agent.update.{key}")
        return m.get("value") if m else None

    @property
    def available(self) -> bool:
        return bool(self.runtime.hello)

    @property
    def installed_version(self) -> str | None:
        return self.runtime.hello.get("agent_version")

    @property
    def latest_version(self) -> str | None:
        latest = self._m("latest")
        if latest is None and self._m("state") in ("up_to_date", None):
            return self.installed_version  # nothing newer (or not checked yet)
        return latest or self.installed_version

    @property
    def release_url(self) -> str | None:
        return self._m("notes_url")

    @property
    def in_progress(self) -> bool:
        return self._m("state") in _IN_PROGRESS

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return {"update_state": self._m("state"), "channel": self._m("channel"), "detail": self._m("reason")}

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        rt = self.runtime
        target = version or self._m("latest")
        if not target:
            raise HomeAssistantError("No agent update is available")
        if not rt.connected:
            raise HomeAssistantError(f"{rt.entry.title} is offline; approve the update when it is online")
        if "updates" not in rt.granted(rt.transport or "local"):
            raise HomeAssistantError(f"Agent updates are not allowed over the {rt.transport} connection (DeckBeacon options)")
        try:
            # Pressing Install is the user's explicit approval (spec 12).
            result = await rt.async_send_command("approve_agent_update", {"version": target}, ttl_s=86400, confirmed=True)
        except TimeoutError as err:
            raise HomeAssistantError("The device did not answer in time") from err
        except ConnectionError as err:
            raise HomeAssistantError(str(err)) from err
        if result["state"] in ("rejected", "failed", "expired"):
            raise HomeAssistantError(result.get("error", {}).get("message", result["state"]))
