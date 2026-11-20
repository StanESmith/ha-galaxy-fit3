"""Test bootstrap.

The protocol package ships inside the HA custom component at
``custom_components/galaxy_fit3/sap``. The component's own ``__init__`` imports Home Assistant,
which the unit tests do not need, so we put the component directory on ``sys.path`` and import the
pure-Python ``sap`` package as a top-level module -- exercising the protocol codec without pulling
in Home Assistant or bleak.
"""

from __future__ import annotations

import sys
from pathlib import Path

_COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "galaxy_fit3"
sys.path.insert(0, str(_COMPONENT_DIR))
