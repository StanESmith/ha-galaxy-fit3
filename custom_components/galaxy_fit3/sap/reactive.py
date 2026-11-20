"""Phone-side replies to requests the watch sends on its own during setup.

The protocol is request/reply in both directions: a reply's first byte is the request's first byte
with 0x40 set. During pairing the watch asks the phone for its timezone, config and so on, and the
phone answers; the replies are constants taken from a real capture, except the route-0a0a reply
which echoes the watch's own message counter (a fixed copy is wrong in every new session).

:func:`reply_for` is pure so it can be tested against the captured request/reply pairs.
"""

from __future__ import annotations

from . import healthsync, tzinfo

# Fixed replies keyed by (route, request opcode). The timezone reply (route 0808, ``0c``) is generated
# from the host's timezone by :mod:`.tzinfo`.
_FIXED: dict[tuple[str, int], bytes] = {
    ("0808", 0x0D): bytes.fromhex("cd2400000000"),
    ("0404", 0x86): bytes.fromhex("c60247024801"),
    ("0707", 0x16): bytes.fromhex("561600180119011500170020002100"),
}

# Window class (the b5 high bits) the phone used for a reply. In the capture the phone's capability-map
# acknowledgement (route 0f0f) uses 0x20/0x30 like the watch-face route, everything else 0x40/0x50.
REPLY_CLASS = 0x40
_CLASS_BY_ROUTE = {"0f0f": 0x20, "0606": 0x20}

# Claim that a health app IS installed on the phone in the health capability reply (route 0a0a); the
# watch then sends its heart-rate and step records (0x82 frames). The plain reply ends ``d0 01``
# (status 1, not installed); "installed" is ``d0 02 d2 <versionCode uint32 LE>`` (status 2). This is a
# deliberate statement to the watch about that one field; the version code is a plausible value (the
# watch keys off the status, not the code). Set to False to send the plain reply.
CLAIM_HEALTH_APP_INSTALLED = True
HEALTH_APP_VERSION_CODE = healthsync.HEALTH_APP_VERSION_CODE

# The watch resends its capability map until it is satisfied; in the capture the phone answered it
# twice and then stopped. Answering every repeat floods the link, so cap it per connection.
MAX_REPLIES = {("0f0f", 0x00): 2, ("0a0a", 0xA0): 3}


def reply_class(route: bytes) -> int:
    """Window class for a reply on ``route``."""
    return _CLASS_BY_ROUTE.get(route.hex(), REPLY_CLASS)


def _health_reply(app: bytes, claim_health_installed: bool, last_sync: int = 0) -> bytes | None:
    """Phone-side answers on the health route (0a0a); see :mod:`.healthsync` for the protocol.

    * ``a0`` capability request -> ``e0`` capability response. Only when claiming a health app is
      installed: the reply is that app's capability, which is the same claim.
    * ``82`` SYNC_DATA (the watch's health records) -> ``42`` success, echoing the sequence number.
    * ``03`` REQUEST_DATA -> ``43`` success.
    * ``01`` CHECK_STATUS -> ``41`` success.

    The watch's ``04`` wearable messages (exercise status) get no reply: a real health app would answer
    with a wearable message of its own, which is not implemented.
    """
    op = app[0]
    if op == healthsync.OP_CAPABILITY_REQUEST:
        if not claim_health_installed or len(app) < 5:
            return None
        return healthsync.build_capability_response(HEALTH_APP_VERSION_CODE)
    if op in (healthsync.OP_SYNC_DATA, healthsync.OP_REQUEST_DATA, healthsync.OP_CHECK_STATUS):
        seq = healthsync.sequence_of(app)
        if seq is None:
            return None
        if op == healthsync.OP_SYNC_DATA:
            return healthsync.build_sync_ack(seq, last_sync)
        reply = healthsync.build_request_data_response(seq, last_sync)
        if op == healthsync.OP_CHECK_STATUS:  # same body under the CHECK_STATUS response header, with a count
            return bytes([0x41, 0x03]) + reply[1:]
        return reply
    return None


def reply_for(
    route: bytes,
    app: bytes,
    tz: tuple[int, str] | None = None,
    claim_health_installed: bool = False,
    health_last_sync: int = 0,
) -> bytes | None:
    """Return the phone's reply payload for a watch request, or ``None`` if none is known.

    ``tz`` is ``(utc_offset_seconds, display_name)`` for the timezone reply; the default is the
    captured US Eastern one. ``claim_health_installed`` flips the health-capability reply's status to
    "health app installed" (see :data:`CLAIM_HEALTH_APP_INSTALLED`).
    """
    if not app:
        return None
    rhex = route.hex()
    op = app[0]
    if rhex == "0f0f" and op == 0x00 and len(app) > 10:
        return b"\x00"  # acknowledge the capability map (the watch resends it until answered)
    if rhex == "0a0a" and op != 0xA2:
        return _health_reply(app, claim_health_installed, health_last_sync)
    if rhex == "0a0a" and op == 0xA2 and len(app) >= 2:
        # GM capability reply: opcode|0x40, length+1, the watch's echoed body, then the GM_STATUS.
        # d0 01 = S Health uninstalled; d0 02 d2 <versionCode LE32> = installed (the experiment).
        if claim_health_installed:
            status = b"\xd0\x02\xd2" + int(HEALTH_APP_VERSION_CODE).to_bytes(4, "little")
            n_extra = 2  # d0 status + d2 version code
        else:
            status = b"\xd0\x01"
            n_extra = 1  # d0 status only
        return bytes([op | 0x40, app[1] + n_extra]) + app[2:] + status
    if rhex == "0808" and op == 0x0C:
        return tzinfo.build_timezone_reply(*(tz or (tzinfo.DEFAULT_OFFSET_S, tzinfo.DEFAULT_NAME)))
    if rhex == "0b0b" and op == 0x10 and len(app) == 3:
        return bytes.fromhex("01046400")  # answered identically in both captured sessions
    if rhex == "0909" and op == 0x01 and len(app) == 3 and app[1] == 0x01:
        return bytes([0x57, 0x01, 0x01, app[2]])  # echoes the watch's last byte (01->01, 02->02)
    return _FIXED.get((rhex, op))
