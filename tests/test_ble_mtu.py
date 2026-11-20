"""ATT MTU handling in the BLE transport (sap/ble.py).

bleak is not installed for the unit tests, so a stub ``bleak`` module stands in; the transport's
client is replaced with a fake that exposes what bleak's BlueZ backend does: ``mtu_size`` (a
hard-coded 23 unless AcquireWrite ran) and the write characteristic's live
``max_write_without_response_size`` (BlueZ's ``MTU`` property minus 3).
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

if "bleak" not in sys.modules:
    _stub = types.ModuleType("bleak")

    class _StubClient:  # only constructed, never used
        def __init__(self, *_a, **_k) -> None:
            pass

    _stub.BleakClient = _StubClient
    sys.modules["bleak"] = _stub

from sap import constants as c
from sap.ble import BleTransport


class _Char:
    def __init__(self, size: int) -> None:
        self.max_write_without_response_size = size


class _Services:
    def __init__(self, char: _Char | None) -> None:
        self._char = char

    def get_characteristic(self, uuid: str):
        return self._char if uuid == str(c.PIPE_WRITE_CHAR) else None


class _FakeClient:
    """bleak BlueZ client as the transport sees it after the connector returned it."""

    def __init__(self, sizes: list[int], refuse_over: int | None = None) -> None:
        self._sizes = sizes  # successive max_write_without_response_size readings
        self.char = _Char(sizes[0])
        self.services = _Services(self.char)
        self.mtu_size = 23  # what bleak reports without AcquireWrite
        self.is_connected = True
        self.refuse_over = refuse_over
        self.writes: list[bytes] = []

    async def start_notify(self, *_a) -> None:
        pass

    def tick(self) -> None:
        if len(self._sizes) > 1:
            self._sizes.pop(0)
        self.char.max_write_without_response_size = self._sizes[0]

    async def write_gatt_char(self, _uuid, data, response=False) -> None:
        if self.refuse_over is not None and len(data) > self.refuse_over:
            raise RuntimeError("org.bluez.Error.Failed")
        self.writes.append(bytes(data))


def _transport(client: _FakeClient) -> BleTransport:
    async def connector(_device, _name):
        return client

    return BleTransport("AA:BB:CC:DD:EE:FF", resolve_device=lambda: object(), connector=connector)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast(_delay, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr("sap.ble.asyncio.sleep", fast)


def test_uses_bluez_mtu_not_bleak_default():
    client = _FakeClient([509])
    t = _transport(client)
    asyncio.run(t.connect(lambda _d: None))
    assert t.mtu_ok
    assert t._write_chunk == 509


def test_waits_for_a_lagging_mtu_property():
    client = _FakeClient([20, 20, 509])
    t = _transport(client)

    async def run():
        task = asyncio.ensure_future(t.connect(lambda _d: None))
        for _ in range(10):
            await asyncio.sleep(0)
            client.tick()
        await task

    asyncio.run(run())
    assert t._write_chunk == 509


def test_stays_safe_when_bluez_really_has_23():
    client = _FakeClient([20])
    t = _transport(client)
    asyncio.run(t.connect(lambda _d: None))
    assert not t.mtu_ok
    assert t._write_chunk == c.DEFAULT_ATT_WRITE_SIZE


def test_refused_big_write_falls_back_and_stays_safe_on_reconnect():
    client = _FakeClient([509], refuse_over=20)  # stale property: promises 509, link takes 20
    t = _transport(client)
    asyncio.run(t.connect(lambda _d: None))
    with pytest.raises(RuntimeError):
        asyncio.run(t.write_frame(bytes(40)))
    assert not t.mtu_ok
    assert client.writes == []  # nothing of the refused frame went out
    asyncio.run(t.connect(lambda _d: None))  # next poll reads the same stale value
    assert not t.mtu_ok
    asyncio.run(t.write_frame(bytes(40)))
    assert [len(w) for w in client.writes] == [20, 20]


def test_write_failure_after_link_drop_keeps_big_writes():
    client = _FakeClient([509], refuse_over=20)
    t = _transport(client)
    asyncio.run(t.connect(lambda _d: None))
    client.is_connected = False  # the link dropped mid-write
    with pytest.raises(RuntimeError):
        asyncio.run(t.write_frame(bytes(40)))
    client.is_connected = True
    asyncio.run(t.connect(lambda _d: None))
    assert t.mtu_ok


def _bluez_reads(monkeypatch, t: BleTransport, values: list[int]) -> list[int]:
    """Make the D-Bus read of BlueZ's MTU property return ``values`` in turn (last one repeats)."""
    calls: list[int] = []

    async def fake():
        calls.append(1)
        return values.pop(0) if len(values) > 1 else values[0]

    monkeypatch.setattr(t, "_bluez_mtu", fake)
    return calls


def test_falls_back_to_bluez_over_dbus_when_bleak_has_no_mtu(monkeypatch):
    client = _FakeClient([20])  # bleak's copy of the property never shows up
    t = _transport(client)
    _bluez_reads(monkeypatch, t, [0, 512])
    asyncio.run(t.connect(lambda _d: None))
    assert t._write_chunk == 509


def test_refresh_picks_up_an_mtu_published_after_connect(monkeypatch):
    client = _FakeClient([20])
    t = _transport(client)
    values = [0]
    _bluez_reads(monkeypatch, t, values)
    asyncio.run(t.connect(lambda _d: None))
    assert not t.mtu_ok
    values[:] = [512]  # BlueZ publishes the property a few seconds later
    assert asyncio.run(t.refresh_mtu())
    assert t._write_chunk == 509


def test_refresh_does_not_reread_once_big_writes_were_refused(monkeypatch):
    client = _FakeClient([509], refuse_over=20)
    t = _transport(client)
    calls = _bluez_reads(monkeypatch, t, [512])
    asyncio.run(t.connect(lambda _d: None))
    with pytest.raises(RuntimeError):
        asyncio.run(t.write_frame(bytes(40)))
    calls.clear()
    assert not asyncio.run(t.refresh_mtu())
    assert calls == []
