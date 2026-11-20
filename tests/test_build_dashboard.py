"""The dashboard generator in scripts/build_dashboard.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "build_dashboard", Path(__file__).resolve().parents[1] / "scripts" / "build_dashboard.py"
)
bd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bd)

FACES = [
    {"face": 16, "name": "Progress gauge 1", "icon": "https://img/16.png"},
    {"face": 31, "name": "Casual Minimalist", "icon": "https://img/31.png"},
]


def test_main_grid_and_store_page_show_opposite_faces(tmp_path: Path) -> None:
    (tmp_path / "face_16.png").write_bytes(b"")
    config = bd.build(FACES, "office_galaxy_fit3", "lovelace", tmp_path, bd.PREVIEW_URL)
    main, store = config["views"]
    grid = main["sections"][0]["cards"][3:]
    shop = store["sections"][0]["cards"][3:]
    assert [c["variables"]["name"] for c in grid] == ["Casual Minimalist", "Progress gauge 1"]  # by name
    assert grid[1]["visibility"][0]["value_template"] == (
        "{{ 'Progress gauge 1' in (state_attr('select.office_galaxy_fit3_watch_face', 'options') or []) "
        "and not is_state('select.office_galaxy_fit3_watch_face', 'Progress gauge 1') }}"
    )
    assert shop[1]["visibility"][0]["value_template"].startswith("{{ not 'Progress gauge 1' in ")
    assert shop[1]["variables"] == {"name": "Progress gauge 1", "id": 16, "image": "/local/galaxy_fit3/faces/face_16.png"}
    assert shop[0]["variables"]["image"] == "https://img/31.png"  # no preview in the package: store icon
    assert store["back_path"] == "/lovelace/galaxy-fit3"
    assert config["streamline_templates"]["fit3_store_face"]["card"]["tap_action"]["data"] == {
        "face_id": "[[id]]", "wait": False,
    }
