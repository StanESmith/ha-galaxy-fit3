"""Samsung Accessory Protocol (SAP) constants for the Galaxy Fit3.

Wire-format facts (bit masks, UUIDs, message-type numbers) needed to interoperate with our own watch.

Grouped by protocol layer, bottom (BLE) to top (app messages).
"""

from __future__ import annotations

from uuid import UUID

# ---------------------------------------------------------------------------
# [1] BLE GATT pipe
# ---------------------------------------------------------------------------
# The watch's data-pipe service (16-bit UUID 0x1A1A) and its two characteristics.
PIPE_SERVICE = UUID("00001a1a-0000-1000-8000-00805f9b34fb")
PIPE_WRITE_CHAR = UUID("63e30bad-4206-4596-839f-e47cbf7a4b5d")  # central -> watch
PIPE_NOTIFY_CHAR = UUID("797ae4e9-2e58-4fe8-b48d-b5c79599fb9b")  # watch -> central (notify)
CCCD_UUID = UUID("00002902-0000-1000-8000-00805f9b34fb")  # standard client config descriptor

DEFAULT_ATT_WRITE_SIZE = 20  # ATT 23 - 3, until the MTU is negotiated
MIN_ATT_MTU = 23  # the BLE default ATT MTU; above this the exchange succeeded and frames fit one packet
MAX_BLE_PAYLOAD_SIZE = 512
CONNECT_TIMEOUT_S = 30.0

# ---------------------------------------------------------------------------
# [2] SAP "Lite" frame header -- the BLE framing
# ---------------------------------------------------------------------------
LITE_HEADER_LEN = 2
LITE_CONTROL_HEADER_LEN = 2
LITE_ACK_HEADER_LEN = 4
LITE_SEQ_WRAP = 16  # 4-bit sequence on continuation fragments

# byte 0
LITE_FRAME_TYPE_MASK = 0xC0
LITE_FRAME_TYPE_SHIFT = 6
LITE_MESSAGE_TYPE_MASK = 0x38
LITE_MESSAGE_TYPE_SHIFT = 3
LITE_FRAGMENT_TYPE_MASK = 0x06
LITE_FRAGMENT_TYPE_SHIFT = 1
LITE_RELIABILITY_MASK = 0x01

# byte 1 (data frame, first/single fragment)
LITE_TRANSACTION_ID_MASK = 0xE0
LITE_TRANSACTION_ID_SHIFT = 5
LITE_WINDOW_ID_MASK = 0x10
LITE_WINDOW_ID_SHIFT = 4
LITE_COMPRESSION_MASK = 0x08
LITE_ENCRYPTION_MASK = 0x04
LITE_RESERVED_MASK = 0x03
# byte 1 (data frame, continuation fragment 2/3): low nibble carries the 4-bit seq id
LITE_SEQ_ID_MASK = 0x0F

# Lite frame types (byte0 bits 7-6)
LITE_FRAME_DATA = 0
LITE_FRAME_CONTROL = 2

# Lite control subtypes
LITE_CONTROL_ACK = 1
LITE_CONTROL_ABORT = 2
# ACK body
LITE_ACK_STATUS_MASK = 0x0F
LITE_ACK_WINDOW_ID_MASK = 0x10
LITE_ACK_WINDOW_ID_SHIFT = 4
# ABORT body
LITE_ABORT_REASON_MASK = 0x0F
LITE_ABORT_INITIATOR_MASK = 0x10
LITE_ABORT_INITIATOR_SHIFT = 4
LITE_ABORT_INITIATOR_SENDER = 0
LITE_ABORT_INITIATOR_RECEIVER = 1
LITE_ABORT_REASON_MAX_RETRY = 0

# Fragment types (byte0 bits 2-1)
FRAG_SINGLE = 0
FRAG_FIRST = 1
FRAG_MIDDLE = 2
FRAG_LAST = 3

# ---------------------------------------------------------------------------
# Full / BR-EDR SAP header -- only if BR-EDR is used.
# Kept for completeness; the Fit3 (BLE) uses the Lite header above.
# ---------------------------------------------------------------------------
SAP_HEADER_LEN = 2
SAP_VERSION = 0
SAP_VERSION_MASK = 0xE0
SAP_VERSION_SHIFT = 5
SAP_FRAME_TYPE_MASK = 0x10
SAP_FRAME_TYPE_SHIFT = 4
SAP_FRAME_DATA = 0
SAP_FRAME_CONTROL = 1
SAP_SESSION_ID_MSB_MASK = 0x0F  # byte0 low nibble = sessionId bits 9-6
SAP_SESSION_ID_LSB_MASK = 0xFC  # byte1 bits 7-2 = sessionId bits 5-0
SAP_FRAGMENTATION_MASK = 0x03
SAP_SEQ_NUM_LEN = 2  # 16-bit big-endian on DATA frames
SAP_SEQ_WRAP = 65535
SAP_CONTROL_IMMEDIATE_ACK = 0
SAP_CONTROL_BLOCK_ACK = 1
SAP_CONTROL_NAK = 2

# ---------------------------------------------------------------------------
# [6] Capability discovery
# ---------------------------------------------------------------------------
CAP_MSG_TYPE_QUERY = 1
CAP_MSG_TYPE_RESPONSE = 2
CAP_MSG_TYPE_LEGACY_QUERY = 5
CAP_MSG_TYPE_LEGACY_RESPONSE = 6
CAP_QUERY_TYPE_NORMAL = 0
CAP_QUERY_TYPE_PERSISTENT = 2
CAP_QUERY_TYPE_PERSISTENT_WITH_CHECKSUM = 3
CAP_CHECKSUM_LEN = 4
CAP_ENCODING = "utf-8"

# SAP service-description string markers
SAP_STR_START = ord("/")  # 0x2F
SAP_STR_END = ord(";")  # 0x3B
SAP_FIXED_START = ord("=")  # 0x3D
SAP_FIXED_LEN_BYTES = 16
CHANNEL_RECORD_SIZE = 8

# ---------------------------------------------------------------------------
# [7] Fit3 app-message TLV
# ---------------------------------------------------------------------------
APP_MSG_FORMAT_FIXED = 0
APP_MSG_FORMAT_VARIABLE = 1
APP_MSG_TYPE_REQUEST = 0
APP_MSG_TYPE_RESPONSE = 1

# ---------------------------------------------------------------------------
# SAP service / channel map
# ---------------------------------------------------------------------------
# Each feature is a SAP service. The SAP "channel" for a feature is its service id; capability
# discovery matches the service *profile id* string. Only the features this integration targets are
# named here. The profile id strings are what the watch advertises, spelling included.
SERVICE_ID_SETUP = 1  # pairing / first-run setup
SERVICE_ID_WATCHFACE = 6  # watch faces
SERVICE_ID_HEALTH = 10  # heart rate / steps
SERVICE_ID_SETTINGS = 11  # time + battery
SERVICE_ID_FILE_TRANSFER = 30  # file transfer (watch-face install)

PROFILE_ID_WATCHFACE = "/system/WatchfaceSerevice"
PROFILE_ID_HEALTH = "/system/HealthService"
PROFILE_ID_SETTINGS = "/system/FullSettingService"

# Known app channels / message ids
CHANNEL_SET_TIME = SERVICE_ID_SETTINGS  # 11
CHANNEL_BATTERY = SERVICE_ID_SETTINGS  # 11 -- battery rides the settings service
CHANNEL_WATCHFACE = SERVICE_ID_WATCHFACE  # 6
MSG_ID_SET_TIME = 0
MSG_ID_BATTERY_RESPONSE = 2
