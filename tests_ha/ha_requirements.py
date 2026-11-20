#!/usr/bin/env python3
"""Print the pip requirements of the Home Assistant components this integration depends on.

pytest-homeassistant-custom-component installs the Home Assistant core but not the packages behind
components such as ``bluetooth`` and ``recorder``. This reads them from the installed Home Assistant's own
manifests (following each component's dependencies), starting from this integration's manifest.

  python tests_ha/ha_requirements.py | uv pip install -r -
"""

from __future__ import annotations

import json
from pathlib import Path

import homeassistant

COMPONENTS = Path(homeassistant.__file__).parent / "components"
MANIFEST = Path(__file__).resolve().parents[1] / "custom_components" / "galaxy_fit3" / "manifest.json"


def requirements(roots: list[str]) -> list[str]:
    seen: set[str] = set()
    found: set[str] = set()

    def walk(domain: str) -> None:
        manifest = COMPONENTS / domain / "manifest.json"
        if domain in seen or not manifest.exists():
            return
        seen.add(domain)
        data = json.loads(manifest.read_text())
        found.update(data.get("requirements", []))
        for dep in data.get("dependencies", []) + data.get("after_dependencies", []):
            walk(dep)

    for root in roots:
        walk(root)
    return sorted(found)


if __name__ == "__main__":
    own = json.loads(MANIFEST.read_text())
    print("\n".join(requirements(own.get("dependencies", []) + own.get("after_dependencies", []))))
