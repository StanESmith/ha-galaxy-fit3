"""The pairing agent waits for the user's answer and rejects by raising (private D-Bus session).

Skipped unless dbus-fast is installed and a session bus is available, e.g.
``dbus-run-session -- python -m pytest tests/test_bluez_agent.py`` inside an env with dbus-fast.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("dbus_fast")
if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
    pytest.skip("no D-Bus session bus", allow_module_level=True)

from dbus_fast import BusType, DBusError  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402

import bluez_agent  # noqa: E402


async def _call_agent(confirm, verdict_after: float = 0.05):
    """Export the agent on the session bus, call RequestConfirmation like BlueZ would."""
    server = await MessageBus(bus_type=BusType.SESSION).connect()
    server.export("/agent", bluez_agent._Agent(confirm))
    await server.request_name("test.galaxyfit3.agent")
    client = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        intro = await client.introspect("test.galaxyfit3.agent", "/agent")
        iface = client.get_proxy_object("test.galaxyfit3.agent", "/agent", intro).get_interface("org.bluez.Agent1")
        return await iface.call_request_confirmation("/org/bluez/hci0/dev_X", 123456)
    finally:
        client.disconnect()
        server.disconnect()


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=15))


def test_no_callback_auto_accepts() -> None:
    assert _run(_call_agent(None)) is None  # returns normally


def test_callback_gets_the_passkey_and_can_accept_after_waiting() -> None:
    seen = {}

    async def confirm(device, passkey):
        seen["key"] = passkey
        await asyncio.sleep(0.2)  # the "user" takes a moment
        return True

    _run(_call_agent(confirm))
    assert seen["key"] == 123456


def test_callback_false_rejects_the_pairing() -> None:
    async def confirm(device, passkey):
        return False

    with pytest.raises(DBusError) as exc:
        _run(_call_agent(confirm))
    assert exc.value.type == "org.bluez.Error.Rejected"
