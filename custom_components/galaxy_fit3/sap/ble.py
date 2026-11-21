"""BLE GATT transport (layer [1]) via bleak.

Opens the watch's data-pipe service (0x1A1A), enables notifications on the read characteristic, and
writes SAP Lite frames to the write characteristic (chunked to the negotiated ATT MTU). Callers receive
each notification chunk and reassemble at the SAP Lite fragment layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from bleak import BleakClient

from . import constants as c

_LOGGER = logging.getLogger(__name__)

NotifyCallback = Callable[[bytes], Awaitable[None] | None]

# Resolves the current LE ``BLEDevice`` for an address (HA's bluetooth stack in production); passing
# the discovered device (not a bare MAC) makes BlueZ use the LE bearer instead of guessing BR/EDR on
# this dual-mode watch ("org.bluez.Error.BREDR.ProfileUnavailable").
DeviceResolver = Callable[[], object | None]
# Connect function: ``async (device, name) -> BleakClient``, e.g. bleak_retry_connector's
# ``establish_connection`` partially applied with BleakClientWithServiceCache.
Connector = Callable[..., Awaitable[BleakClient]]


class BleTransport:
    """Thin async wrapper around a bleak GATT connection to the watch's data pipe."""

    def __init__(
        self,
        address: str,
        *,
        resolve_device: DeviceResolver | None = None,
        connector: Connector | None = None,
    ) -> None:
        self._address = address
        self._resolve_device = resolve_device
        self._connector = connector
        # Without an injected connector we fall back to a plain client (standalone/test use).
        self._client = BleakClient(address, timeout=c.CONNECT_TIMEOUT_S)
        self._on_notify: NotifyCallback | None = None
        self._write_chunk = c.DEFAULT_ATT_WRITE_SIZE
        # Set when BlueZ refused a write at the size its MTU property promised (e.g. a stale value from
        # a cached service table); from then on this transport stays at the safe 20-byte size.
        self._big_writes_refused = False

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    @property
    def mtu_ok(self) -> bool:
        """True once the ATT MTU exchange raised the write size above the 23-byte default, so a frame
        fits one BLE packet. The clock frames are gated on this: a split one corrupts the watch clock."""
        return self._write_chunk > c.DEFAULT_ATT_WRITE_SIZE

    async def connect(self, on_notify: NotifyCallback) -> None:
        """Connect, enable notifications, and record the usable write chunk size."""
        self._on_notify = on_notify
        device = self._resolve_device() if self._resolve_device else None
        if self._connector is not None and device is not None:
            self._client = await self._connector(device, self._address)
        else:
            await self._client.connect()
        await self._client.start_notify(str(c.PIPE_NOTIFY_CHAR), self._handle_notify)
        mtu = await self._negotiate_mtu()
        # negotiated ATT MTU minus the 3-byte ATT write header
        self._write_chunk = c.DEFAULT_ATT_WRITE_SIZE
        if mtu and not self._big_writes_refused:
            self._write_chunk = max(c.DEFAULT_ATT_WRITE_SIZE, mtu - 3)
        # If BlueZ really did stay at the 23-byte default we do NOT drop the link: battery still works at
        # MTU 23, and the caller gates the clock frames on `mtu_ok` so a small MTU can never split one and
        # corrupt the watch clock (the 2106 bug).
        level = logging.DEBUG if mtu > c.MIN_ATT_MTU else logging.WARNING
        _LOGGER.log(level, "connected to %s, ATT MTU=%s, write chunk=%d", self._address, mtu, self._write_chunk)

    def _link_mtu(self) -> int:
        """The ATT MTU BlueZ negotiated for this link, or 0 when it can't be read.

        BlueZ does the ATT MTU exchange itself while the connection comes up (it offers 517; the watch
        settles on 512) and publishes the result as the ``MTU`` property of every GATT characteristic,
        which bleak exposes as ``max_write_without_response_size`` (MTU - 3). bleak's ``mtu_size`` is NOT
        the link MTU: it returns a hard-coded 23 unless ``_acquire_mtu`` (AcquireWrite) has run, and
        AcquireWrite wedges the write characteristic ("Write acquired"). Reading ``mtu_size`` is why every
        connect used to report MTU 23 and fall back to 20-byte writes.
        """
        try:
            char = self._client.services.get_characteristic(str(c.PIPE_WRITE_CHAR))
            size = char.max_write_without_response_size if char is not None else 0
        except Exception:  # noqa: BLE001 - services not resolved yet, or a backend without the property
            return 0
        return size + 3 if size > 0 else 0

    async def _bluez_mtu(self) -> int:
        """The write characteristic's ``MTU`` property read straight from BlueZ over D-Bus, or 0.

        bleak's copy of the property can stay unset for the whole connection (seen live: BlueZ showed
        512 from a few seconds after connecting until the end of the poll while bleak still gave 23),
        so this asks BlueZ itself, on the D-Bus path bleak resolved for the characteristic.
        """
        try:
            char = self._client.services.get_characteristic(str(c.PIPE_WRITE_CHAR))
            obj = getattr(char, "obj", None)
            path = obj[0] if isinstance(obj, tuple) and obj else None
            if not isinstance(path, str) or not path.startswith("/org/bluez/"):
                return 0
            from dbus_fast import BusType, Message, MessageType
            from dbus_fast.aio import MessageBus

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                reply = await bus.call(
                    Message(
                        destination="org.bluez",
                        path=path,
                        interface="org.freedesktop.DBus.Properties",
                        member="Get",
                        signature="ss",
                        body=["org.bluez.GattCharacteristic1", "MTU"],
                    )
                )
            finally:
                bus.disconnect()
            if reply is None or reply.message_type != MessageType.METHOD_RETURN:
                return 0  # "No such property" until BlueZ has finished the exchange
            return int(reply.body[0].value)
        except Exception:  # noqa: BLE001 - no D-Bus (tests, other backends): fall back to bleak's value
            return 0

    async def _read_mtu(self) -> int:
        """Best reading of the link MTU: bleak's copy of the BlueZ property, else BlueZ over D-Bus."""
        mtu = self._link_mtu()
        return mtu if mtu > c.MIN_ATT_MTU else max(mtu, await self._bluez_mtu())

    async def _negotiate_mtu(self, attempts: int = 8) -> int:
        """Wait for the BlueZ ATT MTU to show up on the write characteristic and return it.

        BlueZ publishes the property a few seconds after the link comes up, so poll briefly; if it is
        still missing, :meth:`refresh_mtu` checks again before anything that needs the large MTU.
        Never calls the backend's ``_acquire_mtu`` (see :meth:`_link_mtu`).
        """
        best = await self._read_mtu()
        for _ in range(attempts):
            if best > c.MIN_ATT_MTU:
                return best
            await asyncio.sleep(0.4)
            best = max(best, await self._read_mtu())
        return best or c.MIN_ATT_MTU

    async def refresh_mtu(self) -> bool:
        """Re-read the link MTU if the write size is still the 20-byte default; returns :attr:`mtu_ok`.

        Called before the clock frames, which need a frame to fit one packet: by then BlueZ has usually
        published the MTU it negotiated even when it had not at connect time.
        """
        if self.mtu_ok or self._big_writes_refused or not self._client.is_connected:
            return self.mtu_ok
        mtu = await self._read_mtu()
        if mtu > c.MIN_ATT_MTU:
            self._write_chunk = mtu - 3
            _LOGGER.debug("%s: ATT MTU=%s published after connect, write chunk=%d", self._address, mtu, self._write_chunk)
        return self.mtu_ok

    async def disconnect(self) -> None:
        if self._client.is_connected:
            try:
                # bounded: a StopNotify that never returns once held the coordinator's link lock for good
                await asyncio.wait_for(self._client.stop_notify(str(c.PIPE_NOTIFY_CHAR)), timeout=5)
            except Exception:  # noqa: BLE001 - best effort on teardown (timeouts included)
                pass
            await asyncio.wait_for(self._client.disconnect(), timeout=15)

    async def write_char(self, uuid: str, data: bytes, response: bool = True) -> None:
        """Diagnostics: write an arbitrary GATT characteristic."""
        await self._client.write_gatt_char(uuid, data, response=response)

    async def write_frame(self, frame: bytes) -> None:
        """Write one SAP Lite frame, chunked to the ATT MTU."""
        for i in range(0, len(frame), self._write_chunk):
            chunk = frame[i : i + self._write_chunk]
            try:
                await self._client.write_gatt_char(str(c.PIPE_WRITE_CHAR), chunk, response=False)
            except Exception:
                # A dropped link (common at this range) is not a refusal: only a write that fails while
                # the link is still up says the size was wrong.
                if len(chunk) > c.DEFAULT_ATT_WRITE_SIZE and self._client.is_connected:
                    # BlueZ refuses a write-without-response larger than the link allows, so nothing of
                    # this chunk went out. Drop to the safe size from now on (which also
                    # turns `mtu_ok` off, so no clock frame is ever split) and let the caller retry.
                    _LOGGER.error(
                        "%s: a %d-byte write was refused; using %d-byte writes until Home Assistant restarts",
                        self._address, len(chunk), c.DEFAULT_ATT_WRITE_SIZE,
                    )
                    self._write_chunk = c.DEFAULT_ATT_WRITE_SIZE
                    self._big_writes_refused = True
                raise
            await asyncio.sleep(0)  # yield between chunks

    def _handle_notify(self, _sender: object, data: bytearray) -> None:
        if self._on_notify is None:
            return
        result = self._on_notify(bytes(data))
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)  # noqa: RUF006 - fire-and-forget notify handler
