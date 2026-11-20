# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Each release's section becomes its GitHub release notes,
which HACS shows when it offers the update.

## [Unreleased]

## [0.2.0] - Unreleased

### Added

- A dashboard view that builds itself (`strategy: type: custom:galaxy-fit3`): the face grid and the Face store
  page follow Samsung's store and what is on the watch, with nothing to maintain. Streamline Card is no longer
  needed.
- `galaxy_fit3.fetch_face_previews` saves the real preview pictures of store faces in the background.
- The watch-face select lists the ids of the faces on the watch (`face_ids`).
- `galaxy_fit3/faces` WebSocket command, which the dashboard view uses.

### Changed

- The integration now depends on Home Assistant's `http` and `websocket_api` components (always present) and
  loads its dashboard script through the frontend.

### Removed

- The generated dashboard (`scripts/build_dashboard.py` and the example YAML). Move to the dashboard view
  described in the README.

## [0.1.0] - 2026-11-20

First public release.

### Added

- Battery level and charging state, heart rate and steps read directly from the watch over Bluetooth.
- Heart-rate history kept as hourly long-term statistics.
- Clock sync from Home Assistant.
- Watch-face switching, and installing any free face from Samsung's store over Bluetooth Classic.
- Install progress sensor, a `wait: false` option for the install action, and an install button.
- A generated dashboard for switching and installing faces by picture, with a script to build it.
- Integration icon for Home Assistant 2026.3 and newer.
