"""Diagnostics: built from an allowlist so keys and secrets can never leak (AC10)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import DeckBeaconConfigEntry
from .const import CONF_METRIC_KEYS, CONF_MODEL, CONF_NAME, CONF_VENDOR


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: DeckBeaconConfigEntry) -> dict[str, Any]:
    rt = entry.runtime_data
    hello = rt.hello or {}
    device = hello.get("device", {})
    return {
        "entry": {k: entry.data.get(k) for k in (CONF_NAME, CONF_MODEL, CONF_VENDOR, CONF_METRIC_KEYS)},
        "connection": {
            "connected": rt.connected,
            "power_state": rt.power_state,
            "transport": rt.transport,
            "last_seen": rt.last_seen.isoformat() if rt.last_seen else None,
            "connected_since": rt.connected_since.isoformat() if rt.connected_since else None,
            "last_event_seq": rt.last_event_seq,
        },
        "agent": {
            "agent_version": hello.get("agent_version"),
            "os": device.get("os"),
            "arch": device.get("arch"),
            "session_mode": hello.get("session", {}).get("mode"),
        },
        "capabilities": [{k: c.get(k) for k in ("id", "status", "tier", "reason")} for c in rt.capabilities],
        "metrics": {k: {"value": m.get("value"), "source": m.get("source"), "sampled_at": m.get("sampled_at")} for k, m in rt.metrics.items()},
    }
