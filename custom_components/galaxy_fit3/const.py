"""Constants for the Galaxy Fit3 Home Assistant integration."""

from __future__ import annotations

DOMAIN = "galaxy_fit3"

CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_SAS_CONFIRMED = "sas_confirmed"
CONF_IS_24H = "is_24h"  # options flow: 24-hour clock (False = 12-hour)
DEFAULT_IS_24H = False
CONF_DISTANCE_UNIT = "distance_unit"  # options flow: the unit the watch shows distances in
DISTANCE_UNIT_AUTO = "auto"  # follow Home Assistant's unit system (miles for US customary, else km)
DISTANCE_UNITS = ("km", "mi")

# Default polling cadence for status (battery/HR/steps) once the session is operational.
CONF_SCAN_INTERVAL = "scan_interval"  # options flow: minutes between polls
DEFAULT_SCAN_INTERVAL_MIN = 5
MIN_SCAN_INTERVAL_MIN, MAX_SCAN_INTERVAL_MIN = 1, 60
DEFAULT_SCAN_INTERVAL_S = DEFAULT_SCAN_INTERVAL_MIN * 60

# False: each poll connects, syncs and disconnects, so the host's Bluetooth adapter is free between
# polls (a watch holding the link all day starved a second Bluetooth sensor, see the wiki note).
# True: keep one connection open, which also receives the watch's own unprompted pushes.
HOLD_CONNECTION = False



STATUS_AWAITING_CAPTURE = "awaiting_pairing_capture"
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
