"""Samsung Accessory Protocol (SAP) client for the Galaxy Fit3.

Pure-Python, written for interoperability with our own watch. Layered bottom-to-top:

  constants   -- wire constants (UUIDs, bit masks, message ids)
  ble         -- [1] BLE GATT data-pipe transport (bleak)
  frame       -- [2] SAP Lite frame codec
  capability  -- [6] capability discovery / session negotiation
  app_message -- [7] Fit3 application-message TLV (set time)
"""

from __future__ import annotations

from . import app_message, capability, constants, frame

__all__ = ["app_message", "capability", "constants", "frame"]
