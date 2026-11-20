"""scripts/release_check.py: a tag must match the manifest and have changelog notes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release_check", Path(__file__).resolve().parents[1] / "scripts" / "release_check.py"
)
rc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rc)

CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-12-01

- Added a thing.
- Fixed a bug.

## [0.1.0] - 2026-11-20

- First release.
"""


def _project(tmp_path: Path, version: str, changelog: str | None = CHANGELOG) -> Path:
    manifest = tmp_path / "custom_components" / "galaxy_fit3" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": version}))
    if changelog is not None:
        (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


def test_a_matching_tag_returns_that_versions_notes_only(tmp_path: Path) -> None:
    version, prerelease, notes = rc.check("v0.2.0", _project(tmp_path, "0.2.0"))
    assert (version, prerelease) == ("0.2.0", False)
    assert notes == "- Added a thing.\n- Fixed a bug."


def test_a_suffix_marks_a_prerelease(tmp_path: Path) -> None:
    changelog = CHANGELOG.replace("## [0.2.0] - 2026-12-01", "## [0.2.0] - rc")
    _, prerelease, _ = rc.check("v0.2.0-rc1", _project(tmp_path, "0.2.0-rc1", changelog))
    assert prerelease


@pytest.mark.parametrize("tag", ["0.2.0", "v1.2", "release-1", "v1.2.3.4"])
def test_a_malformed_tag_is_refused(tmp_path: Path, tag: str) -> None:
    with pytest.raises(SystemExit, match="not a release tag"):
        rc.check(tag, _project(tmp_path, "0.2.0"))


def test_a_tag_that_differs_from_the_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="does not match manifest"):
        rc.check("v0.2.0", _project(tmp_path, "0.1.0"))


def test_a_version_without_changelog_notes_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no filled-in"):
        rc.check("v0.3.0", _project(tmp_path, "0.3.0"))


def test_missing_changelog_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no filled-in"):
        rc.check("v0.2.0", _project(tmp_path, "0.2.0", changelog=None))


def test_the_repository_manifest_version_is_a_plain_release_number() -> None:
    assert rc.TAG.match("v" + rc.manifest_version())
