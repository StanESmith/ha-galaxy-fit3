"""Fit3 application-message TLV (layer [7]).

This is the watch's own message layer, which rides *inside* a SAP data session. It is a compact TLV:
a 1-byte header, an optional parameter count, then per-parameter ``[id][value]`` entries and bare
values, parsed against a per-message template the receiver already knows.

Two details are worth knowing. Multi-byte integers are big-endian (see ``BYTE_ORDER``), and a
fixed-size parameter is written as its 1-byte parameter id followed by the value.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

from . import constants as c

# Multi-byte integers are big-endian.
BYTE_ORDER = "big"


def build_header(msg_id: int, msg_type: int, fmt: int) -> int:
    """Compose the 1-byte app-message header.

    bit 7 = format (0 fixed, 1 variable), bit 6 = type (0 request, 1 response),
    bits 5-0 = message id (0-63).
    """
    if not 0 <= msg_id <= 0x3F:
        raise ValueError("message id must fit in 6 bits")
    return ((fmt & 1) << 7) | ((msg_type & 1) << 6) | (msg_id & 0x3F)


@dataclass(slots=True)
class AppMessageBuilder:
    """Builds an app message from parameters and bare values.

    ``emit_param_id`` writes each parameter as ``[id][value]`` (descriptor 2); it should stay True.
    """

    msg_id: int
    msg_type: int = c.APP_MSG_TYPE_REQUEST
    fmt: int = c.APP_MSG_FORMAT_VARIABLE
    emit_param_id: bool = True
    byte_order: str = BYTE_ORDER   # "big" or "little"; unconfirmed (see module docstring), selectable for experiments
    _params: list[tuple[int, int, int]] = field(default_factory=list)  # (id, value, size)
    _values: list[tuple[int, int]] = field(default_factory=list)  # (value, size)

    def add_param(self, param_id: int, value: int, size: int) -> "AppMessageBuilder":
        self._params.append((param_id, value, size))
        return self

    def add_value(self, value: int, size: int) -> "AppMessageBuilder":
        """A bare value with no parameter id (descriptor 4)."""
        self._values.append((value, size))
        return self

    def build(self) -> bytes:
        out = bytearray()
        out.append(build_header(self.msg_id, self.msg_type, self.fmt))
        out.append(len(self._params) & 0xFF)  # NO_OF_PARAM
        for param_id, value, size in self._params:
            if self.emit_param_id:
                out.append(param_id & 0xFF)
            out += int(value).to_bytes(size, self.byte_order, signed=value < 0)
        for value, size in self._values:
            out += int(value).to_bytes(size, self.byte_order, signed=value < 0)
        return bytes(out)


def build_set_time(
    epoch_utc_s: int | None = None,
    is_24h: bool = True,
    tz_offset_seconds: int = 0,
    roaming: bool = False,
    byte_order: str = BYTE_ORDER,
    offset_unit: str = "minutes",
) -> bytes:
    """Build the Fit3 "set time" app message (channel 11, msg id 0).

    Layout: type=request, format=variable, id=0, 4 params:
          param 0 = roaming flag   (byte)
          param 1 = epoch          (long, UTC seconds)
          param 4 = is24Hour       (boolean -> byte)
          param 2 = tz sign        (byte)
        + a bare short = tz offset

    ``tz_offset_seconds`` is split into a magnitude (bare short) and a sign byte. The offset short is
    in minutes by default (``offset_unit="seconds"`` selects seconds).
    """
    if epoch_utc_s is None:
        epoch_utc_s = int(_time.time())
    sign = 0 if tz_offset_seconds >= 0 else 1
    # Minutes by default; seconds are selectable with ``offset_unit``.
    offset_magnitude = abs(tz_offset_seconds) if offset_unit == "seconds" else abs(tz_offset_seconds) // 60
    return (
        AppMessageBuilder(
            msg_id=c.MSG_ID_SET_TIME,
            msg_type=c.APP_MSG_TYPE_REQUEST,
            fmt=c.APP_MSG_FORMAT_VARIABLE,
            byte_order=byte_order,
        )
        .add_param(0, 1 if roaming else 0, 1)
        .add_param(1, epoch_utc_s, 8)
        .add_param(4, 1 if is_24h else 0, 1)
        .add_param(2, sign, 1)
        .add_value(offset_magnitude & 0xFFFF, 2)
        .build()
    )
