#!/usr/bin/env python3
"""Build the Galaxy Fit3 dashboard: a tap-to-switch face grid and a Face store page.

Writes YAML to paste into a dashboard's raw configuration editor (Edit dashboard ->
three dots -> Raw configuration editor):

  * ``streamline_templates:`` goes at the top level of the dashboard, next to ``views:``
    (merge it into an existing ``streamline_templates:`` block if there is one);
  * the two entries under ``views:`` are appended to the dashboard's own ``views:``.

The face cards need the streamline-card frontend plugin (HACS). Each face is one card;
a visibility condition shows it on the main page when it is on the watch and not the
current face, and on the store page when it is not on the watch yet.

Usage:
  fetch_store_faces.py list --out faces.json
  fetch_store_faces.py previews --dir previews          # copy previews/ to config/www/galaxy_fit3/faces/
  build_dashboard.py faces.json --previews previews > galaxy_fit3_dashboard.yaml

Entity IDs default to a device named "Galaxy Fit3"; pass --device with your device's
slug (e.g. office_galaxy_fit3) if you renamed it, or edit the output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PREVIEW_URL = "/local/galaxy_fit3/faces"
WATCH_FACE_HELP = (
    "<small>Tap a face below to switch. The watch changes on its next connection, a few seconds "
    "when it is near Home Assistant.</small>"
)
STORE_HELP = (
    "<small>Every free face in Samsung's store that is not on the watch yet. Tap one to install it: "
    "it takes about a minute, keep the watch near Home Assistant. When it is done it moves to the "
    "Watch face card.</small>"
)


def templates(select: str) -> dict:
    def card(tap: dict) -> dict:
        return {"card": {
            "type": "picture-entity", "entity": select, "image": "[[image]]", "name": "[[name]]",
            "show_name": True, "show_state": False, "tap_action": tap, "hold_action": {"action": "none"},
        }}

    return {
        "fit3_face": card({
            "action": "perform-action", "perform_action": "select.select_option",
            "target": {"entity_id": select}, "data": {"option": "[[name]]"},
            "confirmation": {"text": "Switch the watch to [[name]]?"},
        }),
        # wait: false returns at once; progress shows on the Face install sensor.
        "fit3_store_face": card({
            "action": "perform-action", "perform_action": "galaxy_fit3.install_watch_face",
            "data": {"face_id": "[[id]]", "wait": False},
            "confirmation": {"text": "Install [[name]] on the watch? It takes about a minute; "
                                     "keep the watch near Home Assistant."},
        }),
    }


def face_image(face: dict, previews: Path | None, url: str) -> str:
    # A face with no preview in its package (photo faces) falls back to the store icon.
    if previews is None or (previews / f"face_{face['face']}.png").exists():
        return f"{url}/face_{face['face']}.png"
    return face.get("icon") or face.get("screenshot") or f"{url}/face_{face['face']}.png"


def face_cards(faces: list[dict], select: str, previews: Path | None, url: str, store: bool) -> list[dict]:
    options = f"(state_attr('{select}', 'options') or [])"
    cards = []
    for face in sorted(faces, key=lambda f: f["name"].lower()):
        name = face["name"]
        if store:
            variables = {"name": name, "id": face["face"], "image": face_image(face, previews, url)}
            shown = f"{{{{ not '{name}' in {options} }}}}"
        else:
            variables = {"name": name, "image": face_image(face, previews, url)}
            shown = f"{{{{ '{name}' in {options} and not is_state('{select}', '{name}') }}}}"
        cards.append({
            "type": "custom:streamline-card", "template": "fit3_store_face" if store else "fit3_face",
            "variables": variables, "grid_options": {"columns": 4 if store else 3, "rows": "auto"},
            "visibility": [{"condition": "template", "value_template": shown}],
        })
    return cards


def build(faces: list[dict], device: str, dashboard: str, previews: Path | None, url: str) -> dict:
    select = f"select.{device}_watch_face"
    install = f"sensor.{device}_face_install"
    heart_rate, steps = f"sensor.{device}_heart_rate", f"sensor.{device}_steps"
    install_tile = {"type": "tile", "entity": install, "name": "Install", "vertical": False,
                    "state_content": ["state", "face", "progress"]}
    trend = [{"type": "trend-graph", "hours_to_show": 24}]
    current = (
        f"{{% set p = state_attr('{select}', 'entity_picture') %}}{{% if p %}}<img src=\"{{{{ p }}}}\" "
        f"width=\"96\" align=\"left\" style=\"margin-right:16px\">{{% endif %}}\n<small>Showing now</small>\n"
        f"### {{{{ states('{select}') if states('{select}') not in ['unknown', 'unavailable'] "
        f"else 'Waiting for the watch' }}}}\n{WATCH_FACE_HELP}"
    )
    last_seen = f"state_attr('{heart_rate}', 'last_seen')"
    main = {
        "title": "Galaxy Fit3", "path": "galaxy-fit3", "icon": "mdi:watch-variant", "type": "sections",
        "max_columns": 3,
        "sections": [
            {"type": "grid", "cards": [
                {"type": "heading", "heading": "Watch face", "icon": "mdi:watch-variant",
                 "badges": [{"type": "entity", "entity": f"sensor.{device}_battery", "show_state": True}]},
                {"type": "markdown", "grid_options": {"columns": 12, "rows": "auto"}, "content": current},
                {"type": "heading", "heading": "Tap to switch", "heading_style": "subtitle"},
                *face_cards(faces, select, previews, url, store=False),
            ]},
            {"type": "grid", "cards": [
                {"type": "heading", "heading": "Add a face", "icon": "mdi:storefront-outline"},
                {"type": "shortcut", "label": "Browse the face store", "icon": "mdi:storefront-outline",
                 "tap_action": {"action": "navigate", "navigation_path": f"/{dashboard}/galaxy-fit3-store"},
                 "grid_options": {"columns": 12, "rows": 1}},
                {**install_tile, "grid_options": {"columns": 12, "rows": 1}},
            ]},
            {"type": "grid", "cards": [
                {"type": "heading", "heading": "Health", "icon": "mdi:heart-pulse"},
                {"type": "tile", "entity": heart_rate, "name": "Heart rate", "vertical": False,
                 "features": trend, "grid_options": {"columns": 6, "rows": 2}},
                {"type": "tile", "entity": steps, "name": "Steps today", "vertical": False,
                 "features": trend, "grid_options": {"columns": 6, "rows": 2}},
                {"type": "statistics-graph", "title": "Heart rate, last 7 days (hourly)",
                 "entities": [{"entity": "galaxy_fit3:heart_rate", "name": "Heart rate"}],
                 "stat_types": ["mean", "min", "max"], "period": "hour", "days_to_show": 7,
                 "chart_type": "line", "grid_options": {"columns": "full", "rows": 5}},
                {"type": "statistics-graph", "title": "Steps per day",
                 "entities": [{"entity": steps, "name": "Steps"}], "stat_types": ["max"], "period": "day",
                 "days_to_show": 14, "chart_type": "bar", "grid_options": {"columns": "full", "rows": 4}},
                {"type": "markdown", "text_only": True, "grid_options": {"columns": "full", "rows": "auto"},
                 "content": f"<small>Last reading from the watch: {{{{ {last_seen} | as_datetime | as_local "
                            f"| relative_time if {last_seen} else 'not yet' }}}} ago. {{{{ state_attr('"
                            f"{heart_rate}', 'stored_records') or 0 }}}} heart-rate readings stored.</small>"},
            ]},
        ],
    }
    store = {
        "title": "Face store", "path": "galaxy-fit3-store", "icon": "mdi:storefront-outline",
        "type": "sections", "subview": True, "back_path": f"/{dashboard}/galaxy-fit3", "max_columns": 4,
        "sections": [{"type": "grid", "column_span": 4, "cards": [
            {"type": "heading", "heading": "Galaxy Fit3 face store", "icon": "mdi:storefront-outline"},
            {"type": "markdown", "text_only": True, "grid_options": {"columns": "full", "rows": "auto"},
             "content": STORE_HELP},
            {**install_tile, "grid_options": {"columns": "full", "rows": 1}},
            *face_cards(faces, select, previews, url, store=True),
        ]}],
    }
    return {"streamline_templates": templates(select), "views": [main, store]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("faces", type=Path, help="face list from `fetch_store_faces.py list --out`")
    ap.add_argument("--device", default="galaxy_fit3", help="device slug in the entity IDs (default galaxy_fit3)")
    ap.add_argument("--dashboard", default="lovelace", help="dashboard URL path (default lovelace)")
    ap.add_argument("--previews", type=Path, help="preview folder; faces missing from it use the store icon")
    ap.add_argument("--url", default=PREVIEW_URL, help=f"where the previews are served (default {PREVIEW_URL})")
    ap.add_argument("--json", action="store_true", help="write JSON instead of YAML")
    args = ap.parse_args()

    config = build(json.loads(args.faces.read_text()), args.device, args.dashboard, args.previews, args.url)
    if args.json:
        json.dump(config, sys.stdout, indent=1)
        return 0
    try:
        import yaml
    except ImportError:  # JSON is valid YAML, so the editor accepts it either way
        json.dump(config, sys.stdout, indent=1)
        return 0
    yaml.safe_dump(config, sys.stdout, sort_keys=False, allow_unicode=True, width=1000)
    return 0


if __name__ == "__main__":
    sys.exit(main())
