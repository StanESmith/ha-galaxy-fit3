"""Errors raised by the SAP client."""

from __future__ import annotations


class NotConnected(RuntimeError):
    """The operation needs an open link to the watch; call ``connect()`` first."""
