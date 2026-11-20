"""SAP capability discovery (layer [6]).

After authentication the central sends a capability QUERY; the watch replies with a RESPONSE that
lists its service records (profileId strings + channel/component records). Matching the Fit3's
own service yields the channel/component ids that bind to a 10-bit
session id (used by every subsequent data frame).

The fixed message header is byte-exact; the per-record body layout is only partly decoded, so records
are returned raw.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import constants as c


def build_query(query_type: int = c.CAP_QUERY_TYPE_NORMAL) -> bytes:
    """Build a capability-discovery QUERY message body.

    Layout: ``[message type (1)][query type (1)]`` for a normal query.
    """
    return bytes((c.CAP_MSG_TYPE_QUERY & 0xFF, query_type & 0xFF))


@dataclass(slots=True)
class ServiceRecord:
    """One advertised SAP service from a capability RESPONSE."""

    profile_id: str
    raw: bytes


@dataclass(slots=True)
class CapabilityResponse:
    msg_type: int
    num_records: int
    services: list[ServiceRecord]


def parse_response(body: bytes) -> CapabilityResponse:
    """Parse a capability RESPONSE message body.

    The header (message type, record count) is byte-exact. The service records use the SAP
    service-description string format (markers ``/`` ``;`` ``=``), but the per-record field order is
    not fully decoded, so records are returned raw.
    """
    if not body:
        raise ValueError("empty capability response")
    msg_type = body[0]
    if msg_type != c.CAP_MSG_TYPE_RESPONSE:
        raise ValueError(f"not a capability RESPONSE (message type {msg_type})")
    num_records = body[1] if len(body) > 1 else 0
    services = _split_service_strings(body[2:])
    return CapabilityResponse(msg_type=msg_type, num_records=num_records, services=services)


def _split_service_strings(data: bytes) -> list[ServiceRecord]:
    """Best-effort extraction of ``/profileId;`` tokens from a record blob."""
    services: list[ServiceRecord] = []
    i = 0
    n = len(data)
    while i < n:
        if data[i] == c.SAP_STR_START:
            end = data.find(c.SAP_STR_END, i + 1)
            if end == -1:
                break
            profile = data[i + 1 : end].decode(c.CAP_ENCODING, errors="replace")
            services.append(ServiceRecord(profile_id=profile, raw=data[i : end + 1]))
            i = end + 1
        else:
            i += 1
    return services


def find_service(resp: CapabilityResponse, profile_id: str) -> ServiceRecord | None:
    """Locate a service by profile id (e.g. the Fit3's own service)."""
    for svc in resp.services:
        if svc.profile_id == profile_id:
            return svc
    return None


# ---------------------------------------------------------------------------
# Capability map as the watch sends it over BLE
# ---------------------------------------------------------------------------
# The Fit3's actual capability RESPONSE over BLE is a NUMERIC service->channel
# map (no profile-id strings on the wire -- the string-based parser above is a
# different/older form). After a 0x0f 0x0f 0x00 0x01 marker it carries a run of
# 4-byte records [channelId][0x03][serviceId][0x02], then a 2-byte trailer.
# channelId 0xFF means the service is advertised but not bound to a channel.
CAP_MAP_MARKER = b"\x0f\x0f\x00\x01"
_REC_TAG_A = 0x03  # follows the channel id
_REC_TAG_B = 0x02  # follows the service id
CHANNEL_UNBOUND = 0xFF


@dataclass(slots=True)
class ServiceChannelMap:
    """Parsed service<->channel bindings from a real Fit3 capability RESPONSE."""

    bound: dict[int, int]  # serviceId -> channelId, for services with a real channel
    services: list[int]  # every advertised serviceId (bound or not), in wire order

    def channel_for(self, service_id: int) -> int | None:
        return self.bound.get(service_id)


def parse_service_channel_map(payload: bytes) -> ServiceChannelMap:
    """Parse the numeric service->channel map from a capability-RESPONSE payload.

    ``payload`` is the SAP message body (after the Lite header / seq). Finds the
    ``0f 0f 00 01`` marker and reads ``[channel][0x03][serviceId][0x02]`` records.
    """
    i = payload.find(CAP_MAP_MARKER)
    if i == -1:
        raise ValueError("capability map marker 0f0f0001 not found")
    i += len(CAP_MAP_MARKER)
    bound: dict[int, int] = {}
    services: list[int] = []
    while i + 3 < len(payload) and payload[i + 1] == _REC_TAG_A and payload[i + 3] == _REC_TAG_B:
        channel, service_id = payload[i], payload[i + 2]
        services.append(service_id)
        if channel != CHANNEL_UNBOUND:
            bound[service_id] = channel
        i += 4
    if not services:
        raise ValueError("no service records after the capability map marker")
    return ServiceChannelMap(bound=bound, services=services)
