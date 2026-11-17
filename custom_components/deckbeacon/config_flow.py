"""Config flow: SAS pairing (ADR-0002).

1. ``user``: open a 10-minute pairing window and wait for the device
   (`deckbeacon pair --url <this HA>` on the SteamOS device).
2. ``confirm_code``: show the 6-digit code; the user accepts or rejects it.
3. wait for the device-side confirmation, then create one entry per device.
"""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_STATIC,
    CONF_GM_LOCAL,
    CONF_GM_REMOTE,
    CONF_METRIC_KEYS,
    CONF_MODEL,
    CONF_NAME,
    CONF_VENDOR,
    DOMAIN,
    PAIRING_WINDOW_SECONDS,
)
from .hub import PairingAborted, PairingWindow, async_get_hub


class DeckBeaconConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair one SteamOS device."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DeckBeaconOptionsFlow()

    def __init__(self) -> None:
        self._window: PairingWindow | None = None
        self._wait_device: asyncio.Task | None = None
        self._wait_agent: asyncio.Task | None = None
        self._abort_reason = "window_closed"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = await async_get_hub(self.hass)
        if self._window is None:
            self._window = hub.open_window(self.flow_id)
        if self._wait_device is None:
            self._wait_device = self.hass.async_create_background_task(
                self._await(self._window.device_arrived, PAIRING_WINDOW_SECONDS), "deckbeacon_wait_device"
            )
        if not self._wait_device.done():
            return self.async_show_progress(
                step_id="user",
                progress_action="wait_for_device",
                progress_task=self._wait_device,
                description_placeholders={"command": f"deckbeacon pair --url {self._suggested_url()}"},
            )
        if self._wait_device.result() is not True:
            return self.async_show_progress_done(next_step_id="failed")
        return self.async_show_progress_done(next_step_id="confirm_code")

    async def async_step_confirm_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._window is None or self._window.request is None or self._window.sas is None:
            return self.async_abort(reason="window_closed")
        req = self._window.request
        await self.async_set_unique_id(req["device"]["device_id"])
        if self._async_current_entries() and any(e.unique_id == self.unique_id for e in self._async_current_entries()):
            self._finish(False, "already_paired")
            return self.async_abort(reason="already_configured")
        sas = self._window.sas
        return self.async_show_menu(
            step_id="confirm_code",
            menu_options=["accept", "reject"],
            description_placeholders={
                "code": f"{sas[:3]} {sas[3:]}",
                "name": req["name"],
                "model": str(req["device"].get("model", "SteamOS device")),
            },
        )

    async def async_step_reject(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._finish(False, "sas_rejected")
        return self.async_abort(reason="code_rejected")

    async def async_step_accept(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._window is None:
            return self.async_abort(reason="window_closed")
        if not self._window.ha_decision.done():
            self._window.ha_decision.set_result(True)
        if self._wait_agent is None:
            self._wait_agent = self.hass.async_create_background_task(self._await(self._window.agent_decision, PAIRING_WINDOW_SECONDS), "deckbeacon_wait_agent")
        if not self._wait_agent.done():
            return self.async_show_progress(step_id="accept", progress_action="wait_for_device_confirm", progress_task=self._wait_agent)
        if self._wait_agent.result() is not True:
            self._abort_reason = "device_rejected" if self._wait_agent.result() is False else self._abort_reason
            return self.async_show_progress_done(next_step_id="failed")
        return self.async_show_progress_done(next_step_id="create")

    async def async_step_create(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._window is None or self._window.request is None or self._window.device_static is None:
            return self.async_abort(reason="window_closed")
        req = self._window.request
        dev = req["device"]
        self._abort_if_unique_id_configured()
        result = self.async_create_entry(
            title=req["name"],
            data={
                CONF_DEVICE_ID: dev["device_id"],
                CONF_DEVICE_STATIC: self._window.device_static,
                CONF_NAME: req["name"],
                CONF_MODEL: dev.get("model", "SteamOS device"),
                CONF_VENDOR: dev.get("vendor", ""),
                CONF_METRIC_KEYS: [],
            },
        )
        self._finish(True)
        return result

    async def async_step_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._finish(False, self._abort_reason)
        return self.async_abort(reason=self._abort_reason)

    async def _await(self, fut: asyncio.Future, timeout: float) -> bool | None:
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(fut)
        except TimeoutError:
            self._abort_reason = "timeout"
            return None
        except PairingAborted as err:
            self._abort_reason = str(err) if str(err) in {"timeout", "already_paired"} else "window_closed"
            return None

    def _finish(self, ok: bool, reason: str = "window_closed") -> None:
        win = self._window
        if win is None:
            return
        if not win.ha_decision.done():
            win.ha_decision.set_result(ok)
        if ok:
            if not win.finished.done():
                win.finished.set_result(True)
        hub = self.hass.data.get(DOMAIN)
        if hub is not None:
            if ok:
                if win in hub.windows:
                    hub.windows.remove(win)
            else:
                hub.close_window(win, reason)

    def _suggested_url(self) -> str:
        from homeassistant.helpers.network import NoURLAvailableError, get_url

        try:
            return get_url(self.hass, allow_external=False, allow_cloud=False)
        except NoURLAvailableError:
            return "http://<home-assistant>:8123"

    def async_remove(self) -> None:
        """Flow closed by the user: close the window."""
        if self._window is not None and not self._window.finished.done():
            self._finish(False, "user_cancelled")


class DeckBeaconOptionsFlow(OptionsFlow):
    """Permission tiers per transport. The agent enforces them too (spec 10)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GM_LOCAL, default=opts.get(CONF_GM_LOCAL, True)): bool,
                    vol.Required(CONF_GM_REMOTE, default=opts.get(CONF_GM_REMOTE, False)): bool,
                }
            ),
        )
