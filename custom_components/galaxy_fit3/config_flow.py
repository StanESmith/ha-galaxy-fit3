"""Config flow for the Galaxy Fit3 integration.

Supports Bluetooth auto-discovery (service 0x1A1A) and manual entry by MAC address.

Setup pairs with the watch. The Fit3 bonds over Bluetooth LE Secure Connections with a numeric
comparison: the watch shows a 6-digit code and the host must confirm it matches. The flow starts the
bond, shows that code here, and only tells BlueZ to accept once the user has confirmed it equals the
one on the watch (a mismatch rejects the pairing). The client then finishes the SAP hello and setup
exchange, and the entry is created once the watch is happy. A "skip pairing" option registers the
entry without pairing (it then pairs, auto-confirming, on its first connection) for scripted setup.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from . import ble_support
from .bluez_agent import PairingAgent
from .const import (
    CONF_ADDRESS,
    CONF_DISTANCE_UNIT,
    CONF_IS_24H,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_IS_24H,
    DEFAULT_SCAN_INTERVAL_MIN,
    DISTANCE_UNIT_AUTO,
    DOMAIN,
    MAX_SCAN_INTERVAL_MIN,
    MIN_SCAN_INTERVAL_MIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_SKIP_PAIRING = "skip_pairing"
CONF_MATCHES = "matches"

_CODE_TIMEOUT_S = 120  # how long the user has to confirm the code
_CONNECT_TIMEOUT_S = 240  # whole bond + hello + setup exchange


class _PairingSession:
    """Runs the bond in the background and exposes the numeric-comparison code to the flow."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        loop = asyncio.get_running_loop()
        self._hass = hass
        self._address = address
        self.passkey: int | None = None
        self.rejected = False
        self.error: Exception | None = None
        self._code_ready: asyncio.Future[bool] = loop.create_future()
        self._decision: asyncio.Future[bool] = loop.create_future()
        self.task = hass.async_create_task(self._run(), "galaxy_fit3_pairing")
        # completes as soon as there is a code to show, or the pairing ended without one
        self.wait_code_task = hass.async_create_task(self._wait_code(), "galaxy_fit3_wait_code")

    async def _confirm(self, _device: str, passkey: int) -> bool:
        """Called by the BlueZ agent: surface the code, then wait for the user's yes/no."""
        self.passkey = passkey
        if not self._code_ready.done():
            self._code_ready.set_result(True)
        try:
            ok = await asyncio.wait_for(self._decision, _CODE_TIMEOUT_S)
        except (TimeoutError, asyncio.TimeoutError):
            ok = False
        self.rejected = not ok
        return ok

    def decide(self, matches: bool) -> None:
        if not self._decision.done():
            self._decision.set_result(matches)

    async def _run(self) -> None:
        client = ble_support.build_client(self._hass, self._address)
        try:
            # Start from a clean BlueZ record: keys left by an earlier (failed) pairing make BlueZ try to encrypt
            # with a key the freshly reset watch no longer has, and the watch hangs up during service discovery.
            await asyncio.wait_for(ble_support.forget_bond(self._address), 8)
            await asyncio.sleep(2)  # let BlueZ rediscover the advertising watch
            await ble_support.prefer_le_bearer(self._address)
            async with PairingAgent(confirm=self._confirm):
                await asyncio.wait_for(client.connect(), _CONNECT_TIMEOUT_S)
        except Exception as err:  # noqa: BLE001 - reported to the user via the abort reason
            self.error = err
            _LOGGER.warning("Fit3 %s: pairing failed: %s", self._address, err)
        finally:
            await client.disconnect()
            if not self._code_ready.done():
                self._code_ready.set_result(False)  # no code was ever needed (already bonded) or failed

    async def _wait_code(self) -> None:
        await asyncio.wait({self._code_ready, self.task}, return_when=asyncio.FIRST_COMPLETED)

    def cancel(self) -> None:
        self.decide(False)
        for task in (self.task, self.wait_code_task):
            task.cancel()


class Fit3ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Galaxy Fit3."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "Fit3OptionsFlow":
        return Fit3OptionsFlow()

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._pairing: _PairingSession | None = None

    # -- discovery / manual entry -----------------------------------------------------------
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered over Bluetooth (advertising service 0x1A1A)."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._name = discovery_info.name or "Galaxy Fit3"
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered device, then start pairing."""
        assert self._address is not None
        if user_input is not None:
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._name or "Galaxy Fit3"},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual setup by MAC address."""
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS].strip().upper()
            self._name = user_input.get(CONF_NAME) or "Galaxy Fit3"
            await self.async_set_unique_id(self._address)
            self._abort_if_unique_id_configured()
            if user_input.get(CONF_SKIP_PAIRING):
                return self._create_entry()
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_NAME, default="Galaxy Fit3"): str,
                    vol.Optional(CONF_SKIP_PAIRING, default=False): bool,
                }
            ),
        )

    # -- pairing ----------------------------------------------------------------------------
    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the bond and wait (with a progress indicator) until the watch shows a code."""
        assert self._address is not None
        if self._pairing is None:
            self._pairing = _PairingSession(self.hass, self._address)
        if not self._pairing.wait_code_task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="wait_code",
                progress_task=self._pairing.wait_code_task,
            )
        if self._pairing.passkey is None:  # already bonded, or it failed before any code appeared
            return self.async_show_progress_done(next_step_id="finish")
        return self.async_show_progress_done(next_step_id="confirm_code")

    async def async_step_confirm_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the code; the pairing is only accepted if the user says it matches the watch."""
        assert self._pairing is not None and self._pairing.passkey is not None
        if user_input is not None:
            self._pairing.decide(bool(user_input.get(CONF_MATCHES)))
            return await self.async_step_finish()
        return self.async_show_form(
            step_id="confirm_code",
            data_schema=vol.Schema({vol.Required(CONF_MATCHES, default=False): bool}),
            description_placeholders={"code": f"{self._pairing.passkey:06d}"},
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait (with progress) for the bond, SAP hello and setup exchange to complete."""
        assert self._pairing is not None
        if not self._pairing.task.done():
            return self.async_show_progress(
                step_id="finish",
                progress_action="finishing",
                progress_task=self._pairing.task,
            )
        return self.async_show_progress_done(next_step_id="done")

    async def async_step_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._pairing is not None
        if self._pairing.rejected:
            return self.async_abort(reason="code_mismatch")
        if self._pairing.error is not None:
            return self.async_abort(reason="cannot_pair")
        return self._create_entry()

    # -- helpers ----------------------------------------------------------------------------
    def _create_entry(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._name or "Galaxy Fit3",
            data={CONF_ADDRESS: self._address, CONF_NAME: self._name},
        )

    @callback
    def async_remove(self) -> None:
        """The flow was closed (finished or abandoned): stop any pairing still in the background."""
        if self._pairing is not None:
            self._pairing.cancel()


class Fit3OptionsFlow(OptionsFlow):
    """Options for an existing Galaxy Fit3 entry (the integration's "Configure" button)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = {**user_input, CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN))}
            return self.async_create_entry(title="", data=data)
        current = self.config_entry.options.get(CONF_IS_24H, DEFAULT_IS_24H)
        minutes = int(self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN))
        unit = self.config_entry.options.get(CONF_DISTANCE_UNIT, DISTANCE_UNIT_AUTO)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_IS_24H, default=current): bool,
                    vol.Optional(CONF_DISTANCE_UNIT, default=unit): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[DISTANCE_UNIT_AUTO, "km", "mi"],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_DISTANCE_UNIT,
                        )
                    ),
                    vol.Optional(CONF_SCAN_INTERVAL, default=minutes): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL_MIN, max=MAX_SCAN_INTERVAL_MIN, step=1,
                            mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min",
                        )
                    ),
                }
            ),
        )
