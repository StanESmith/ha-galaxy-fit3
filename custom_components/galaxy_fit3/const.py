"""Constants for the Galaxy Fit3 Home Assistant integration."""

from __future__ import annotations

DOMAIN = "galaxy_fit3"

CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_SAS_CONFIRMED = "sas_confirmed"
CONF_IS_24H = "is_24h"  # options flow: 24-hour clock (False = 12-hour)
DEFAULT_IS_24H = False

# Default polling cadence for status (battery/HR/steps) once the session is operational.
DEFAULT_SCAN_INTERVAL_S = 300

# False: each poll connects, syncs and disconnects, so the host's Bluetooth adapter is free between
# polls (a watch holding the link all day starved a second Bluetooth sensor, see the wiki note).
# True: keep one connection open, which also receives the watch's own unprompted pushes.
HOLD_CONNECTION = False



STATUS_AWAITING_CAPTURE = "awaiting_pairing_capture"
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
