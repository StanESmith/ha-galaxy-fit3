# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Each release's section becomes its GitHub release notes,
which HACS shows when it offers the update.

## [Unreleased]

## [0.1.0] - Unreleased

First public release.

### Added

- Battery level and charging state, heart rate and steps read directly from the watch over Bluetooth.
- Heart-rate history kept as hourly long-term statistics.
- Clock sync from Home Assistant.
- Watch-face switching, and installing any free face from Samsung's store over Bluetooth Classic.
- Install progress sensor, a `wait: false` option for the install action, and an install button.
- A generated dashboard for switching and installing faces by picture, with a script to build it.
- Integration icon for Home Assistant 2026.3 and newer.
