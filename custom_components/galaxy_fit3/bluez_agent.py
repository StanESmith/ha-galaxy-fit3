"""Temporary BlueZ pairing agent (HA's BlueZ has none registered).

Without an agent, the LE Secure Connections bond that the watch triggers (when we enable
notifications on the SAP data pipe) has nobody to answer BlueZ's confirmation request, so the
GATT call hangs. This registers a throw-away agent for the duration of a connect and removes it
afterwards. By default it auto-accepts numeric-comparison requests; given a ``confirm`` coroutine
(``async (device, passkey) -> bool``) it hands the code to that instead, which is how the setup flow
shows the code and waits for the user to confirm it matches the watch.

NOTE: deliberately no ``from __future__ import annotations`` -- dbus_fast reads the method
annotations ('o', 'u', 's') as D-Bus signatures and breaks if they become strings.
"""

import logging

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/galaxy_fit3/agent"


class _Agent(ServiceInterface):
    def __init__(self, confirm=None):
        super().__init__("org.bluez.Agent1")
        # async (device_path, passkey) -> bool; None means accept without asking
        self._confirm = confirm

    @method()
    def Release(self):  # noqa: N802 - D-Bus method name
        return

    @method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
        if self._confirm is None:
            _LOGGER.debug("pairing agent: auto-confirming %s", device)
            return
        # BlueZ waits for this call to return, so awaiting the user's answer here is fine; raising
        # org.bluez.Error.Rejected makes BlueZ abort the pairing.
        if not await self._confirm(device, passkey):
            raise DBusError("org.bluez.Error.Rejected", "pairing code was not confirmed")

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
        _LOGGER.debug("pairing agent: authorizing %s", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
        return

    @method()
    def Cancel(self):  # noqa: N802
        return


class PairingAgent:
    """Async context manager: register the agent as default on enter, unregister on exit."""

    def __init__(self, capability: str = "DisplayYesNo", confirm=None) -> None:
        self._capability = capability
        self._confirm = confirm
        self._bus = None
        self._mgr = None

    async def __aenter__(self):
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self._bus.export(AGENT_PATH, _Agent(self._confirm))
            intro = await self._bus.introspect("org.bluez", "/org/bluez")
            self._mgr = self._bus.get_proxy_object("org.bluez", "/org/bluez", intro).get_interface(
                "org.bluez.AgentManager1"
            )
            await self._mgr.call_register_agent(AGENT_PATH, self._capability)
            await self._mgr.call_request_default_agent(AGENT_PATH)
            _LOGGER.debug("pairing agent registered (%s)", self._capability)
        except Exception as err:  # noqa: BLE001 - optional helper, never fatal
            _LOGGER.debug("could not register pairing agent: %s", err)
        return self

    async def __aexit__(self, *exc):
        try:
            if self._mgr is not None:
                await self._mgr.call_unregister_agent(AGENT_PATH)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("pairing agent unregister failed: %s", err)
        if self._bus is not None:
            self._bus.disconnect()
        return False
