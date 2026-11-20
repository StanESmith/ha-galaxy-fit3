"""Phone-side SAP setup exchange replayed after the hello (what the phone's companion app does at pairing).

After the SAP hello pair, the phone's companion app sends ~35 frames (quick-reply templates, device
info, timezone, the current time, ...) and the watch stays on its pairing screen until it has
received them. This is that sequence, taken verbatim from a real capture (the second, cleaner
pairing session) as ``(h2, h3, b4, b5, route_hex, app_hex)`` tuples for
:func:`appframe.encode_app_frame`. The watch acknowledges each frame on route ``8000``.

Only the frames the PHONE initiates are here. Replies to requests the watch makes (timezone, config,
the capability-map acknowledgement, the route-0a0a echo, ...) are produced on demand by
:mod:`.reactive`, because the watch asks for them at its own pace and a fixed copy of the 0a0a reply is
wrong in every new session (it echoes the watch's message counter). The header bytes ``h2 h3`` are
recomputed (:func:`appframe.build_frame`) and ``b5`` is only used for its window class.

Two entries are not verbatim: the ``0101`` time frame (``app_hex`` is ``None``; built from the live
clock by :func:`build_time_app`), and the timezone text on route ``0808`` is replayed as captured
("Eastern Standard Time").

Whether the watch needs *all* of these is unverified -- this is a full replay first.
"""

from __future__ import annotations

import time

from . import tzinfo

_TIME_PREFIX = bytes.fromhex("830164000200030a")  # tag 03, len 0x0a: epoch seconds as ASCII


def build_time_app(
    epoch_seconds: int | None = None, utc_offset_s: int = tzinfo.DEFAULT_OFFSET_S, is_24h: bool = False
) -> bytes:
    """App payload of the route-0101 time frame (10-digit epoch seconds, so the length is fixed).

    The tail ``04 <sign> <uint16 LE seconds> 05 <is_24h>`` carries the UTC offset (default: the captured
    US Eastern one; the watch applies it to the epoch, so it must match the host's timezone) and the
    clock format (param 5: 1 = 24-hour, 0 = 12-hour).
    """
    secs = int(time.time() if epoch_seconds is None else epoch_seconds)
    text = str(secs).encode("ascii")
    if len(text) != 10:
        raise ValueError("epoch seconds must be 10 digits")
    fmt = b"\x05\x01" if is_24h else b"\x05\x00"
    return _TIME_PREFIX + text + b"\x04" + tzinfo.offset_triple(utc_offset_s) + fmt


TIME_VARIANTS = ("standard", "tz_zero", "local_epoch", "millis", "no_tz")


def build_time_app_variant(variant: str, epoch_seconds: int | None = None, utc_offset_s: int = tzinfo.DEFAULT_OFFSET_S, is_24h: bool = False) -> bytes:
    """Experimental encodings of the route-0101 time frame (diagnostics only; see ``set_time``'s ``variant``).

    ``standard`` is :func:`build_time_app`; ``tz_zero`` sends a 0 offset triple; ``local_epoch`` sends
    epoch minus the west-positive offset with a zero triple; ``millis`` sends 13 digits (length byte 0x0d);
    ``no_tz`` leaves the ``04 ...`` offset parameter out.
    """
    secs = int(time.time() if epoch_seconds is None else epoch_seconds)
    zero = tzinfo.offset_triple(0)
    if variant == "standard":
        return build_time_app(secs, utc_offset_s, is_24h)
    if variant == "tz_zero":
        return _TIME_PREFIX + str(secs).encode("ascii") + b"\x04" + zero + b"\x05\x00"
    if variant == "local_epoch":
        return _TIME_PREFIX + str(secs + int(utc_offset_s)).encode("ascii") + b"\x04" + zero + b"\x05\x00"
    if variant == "millis":
        return _TIME_PREFIX[:-1] + b"\x0d" + str(secs * 1000).encode("ascii") + b"\x04" + tzinfo.offset_triple(utc_offset_s) + b"\x05\x00"
    if variant == "no_tz":
        return _TIME_PREFIX + str(secs).encode("ascii") + b"\x05\x00"
    raise ValueError(f"unknown time variant {variant!r}")


# (h2, h3, b4, b5, route_hex, app_hex | None)
SETUP_FRAMES: list[tuple[int, int, int, int, str, str | None]] = [
    (0xc2, 0x41, 0x01, 0x40, "1212", "020301"),
    (0x64, 0x80, 0x01, 0x50, "1212", "0000100103596573010b5965732c20706c6561736501024f4b01024e6f01094e6f207468616e6b7301044e6f706501065468616e6b7301104d75636820617070726563696174656401094f6e206d7920776179010a5768617427732075703f01054c6174657201044e69636501074920616772656501064772656174210105576861743f01044f4d4721"),
    (0x32, 0x40, 0x01, 0x40, "1212", "010003010f506c656173652074657874206d652e011843616e20796f752063616c6c206261636b206c617465723f011349276c6c2063616c6c20796f75206261636b2e"),
    (0x03, 0xc0, 0x01, 0x50, "0707", "0d"),
    (0x07, 0x80, 0x01, 0x40, "0101", "01040205000e"),
    (0xc2, 0x41, 0x01, 0x50, "0101", "020100"),
    (0xc9, 0x01, 0x01, 0x50, "0101", None),  # TIME: epoch seconds as ASCII
    (0x03, 0xc0, 0x01, 0x40, "0b0b", "02"),
    (0x03, 0xc0, 0x01, 0x30, "0606", "00"),
    (0x03, 0xc0, 0x01, 0x40, "0b0b", "22"),
    (0x03, 0xc0, 0x01, 0x50, "1313", "00"),
    (0x03, 0xc0, 0x01, 0x40, "0b0b", "20"),
    (0xc2, 0x41, 0x01, 0x50, "0707", "151400"),
    (0x03, 0xc0, 0x01, 0x40, "0707", "0e"),
    (0xcc, 0x01, 0x01, 0x40, "0b0b", "8e0500010100020003000400"),
    (0x0e, 0x40, 0x01, 0x40, "0909", "d1050100000000000000000200030004000500"),
    (0xc9, 0x01, 0x01, 0x50, "0909", "d204010202000000000000000004ffffffffffffffff0300"),
    (0xc5, 0xc1, 0x01, 0x40, "0909", "550201080000000201"),
    (0x04, 0x40, 0x01, 0x50, "0909", "e003010002000300000000"),
    (0x03, 0xc0, 0x01, 0x40, "1111", "06"),
    (0xc2, 0x41, 0x01, 0x50, "0b0b", "102000"),
    (0xc2, 0x41, 0x01, 0x50, "0101", "840101"),
    (0xc4, 0x81, 0x01, 0x40, "1919", "09370038013401000000"),
    (0xc2, 0x41, 0x01, 0x50, "1919", "1a2a00"),
    (0xc2, 0x41, 0x01, 0x40, "1919", "1b2c01"),
    (0x07, 0x80, 0x01, 0x50, "1919", "050a02000000"),
    (0xc2, 0x41, 0x01, 0x40, "1919", "010100"),
]
