"""Timezone fields the watch is told about during setup.

Two places carry the phone's UTC offset, both as the triple ``<sign> <uint16 LE seconds>`` where the
sign byte is 1 for west of UTC (``01 50 46`` = 0x4650 = 18000 s = UTC-5):

* the reply to the watch's route-0808 request ``0c``: ``4c 27 01 09 <triple> 28 <len> <name>``
* the tail of the route-0101 time frame: ``... 04 <triple> 05 00``

Only one real sample exists (US Eastern, standard time), so the layout is inferred from it. What is confirmed:
the Eastern bytes are reproduced exactly. What is not: the meaning of ``01 09`` and ``05 00`` (likely
DST related, only ever seen with DST off), the sign for zones east of UTC, whether the name switches
to a daylight name in summer, and whether the watch uses the name for anything beyond display. The
offset is the current one including DST, which is what makes the watch's clock right.
"""

from __future__ import annotations

# Default = the captured sample (US Eastern, UTC-5).
DEFAULT_OFFSET_S = -18000
DEFAULT_NAME = "Eastern Standard Time"


def offset_triple(offset_s: int) -> bytes:
    """``<sign> <abs(offset) uint16 LE>``; sign 1 means west of UTC."""
    mag = abs(int(offset_s))
    if mag > 0xFFFF:
        raise ValueError("UTC offset out of range")
    return bytes([1 if offset_s < 0 else 0]) + mag.to_bytes(2, "little")


def build_timezone_reply(offset_s: int = DEFAULT_OFFSET_S, name: str = DEFAULT_NAME) -> bytes:
    """The phone's reply payload to the watch's ``0c`` request (route 0808)."""
    raw = name.encode("ascii", "ignore")[:255]
    return bytes.fromhex("4c270109") + offset_triple(offset_s) + bytes([0x28, len(raw)]) + raw


# English long *standard-time* names (what Java/ICU prints for en_US), best effort and cosmetic.
_NAMES: dict[str, str] = {}
for _name, _zones in {
    "Eastern Standard Time": "America/New_York America/Toronto America/Detroit America/Montreal US/Eastern",
    "Central Standard Time": "America/Chicago America/Mexico_City America/Winnipeg US/Central",
    "Mountain Standard Time": "America/Denver America/Phoenix America/Edmonton US/Mountain",
    "Pacific Standard Time": "America/Los_Angeles America/Vancouver America/Tijuana US/Pacific",
    "Alaska Standard Time": "America/Anchorage",
    "Hawaii-Aleutian Standard Time": "Pacific/Honolulu US/Hawaii",
    "Atlantic Standard Time": "America/Halifax",
    "Brasilia Standard Time": "America/Sao_Paulo",
    "Greenwich Mean Time": "Europe/London Europe/Dublin Atlantic/Reykjavik Africa/Abidjan",
    "Western European Standard Time": "Europe/Lisbon",
    "Central European Standard Time": (
        "Europe/Berlin Europe/Paris Europe/Madrid Europe/Rome Europe/Amsterdam Europe/Brussels "
        "Europe/Vienna Europe/Zurich Europe/Stockholm Europe/Oslo Europe/Copenhagen Europe/Prague "
        "Europe/Warsaw Europe/Budapest"
    ),
    "Eastern European Standard Time": "Europe/Helsinki Europe/Athens Europe/Kiev Europe/Kyiv Europe/Bucharest",
    "Moscow Standard Time": "Europe/Moscow",
    "Gulf Standard Time": "Asia/Dubai",
    "India Standard Time": "Asia/Kolkata Asia/Calcutta",
    "China Standard Time": "Asia/Shanghai",
    "Hong Kong Standard Time": "Asia/Hong_Kong",
    "Singapore Standard Time": "Asia/Singapore",
    "Japan Standard Time": "Asia/Tokyo",
    "Korean Standard Time": "Asia/Seoul",
    "Australian Western Standard Time": "Australia/Perth",
    "Australian Eastern Standard Time": "Australia/Sydney Australia/Melbourne Australia/Brisbane",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Coordinated Universal Time": "UTC Etc/UTC",
}.items():
    for _z in _zones.split():
        _NAMES[_z] = _name


def timezone_name(tz_id: str, offset_s: int) -> str:
    """Display name for a zone id; zones not in the table get a ``GMT+hh:mm`` style name."""
    if tz_id in _NAMES:
        return _NAMES[tz_id]
    mag = abs(int(offset_s)) // 60
    sign = "-" if offset_s < 0 else "+"
    return f"GMT{sign}{mag // 60:02d}:{mag % 60:02d}"
