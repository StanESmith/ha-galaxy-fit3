[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]
[![Home Assistant][ha-shield]][ha]
[![CI][ci-shield]][ci]
![Project Maintenance][maintenance-shield]
[![BuyMeCoffee][buymecoffee-shield]][buymecoffee]

[![Add to Home Assistant][add-to-ha-shield]][add-to-ha-url]

# Samsung Galaxy Fit3

A Home Assistant custom integration that talks to a **Samsung Galaxy Fit3** directly over Bluetooth:
no phone, no Samsung Health, no cloud.

[![Open your Home Assistant instance and start setting up Samsung Galaxy Fit3.][config-flow-shield]][config-flow-url]

> [!NOTE]
> This project is not affiliated with or endorsed by Samsung. It is an independent implementation for
> using your own watch with your own Home Assistant; see [How it works](#how-it-works).

## Features

- **Battery** level and charging state.
- **Heart rate** and **steps**, with heart-rate history kept as hourly long-term statistics (the watch
  itself holds only about a day and a half).
- **Clock sync**: the watch's time is set from Home Assistant.
- **Watch faces**: switch between the faces on the watch, and install any of the ~100 free faces from
  Samsung's store.
- A **dashboard** to switch and install faces by picture ([Dashboard](#dashboard)).

Everything runs locally (`iot_class: local_push`). See [Known limitations](#known-limitations) for what
it does not do yet.

## Requirements

- Home Assistant **2026.10** or newer.
- A **Bluetooth adapter in the Home Assistant machine** (built in or USB), driven by BlueZ. ESPHome and
  Shelly Bluetooth proxies are not enough: the watch is paired through BlueZ, and installing a face needs
  Bluetooth Classic, which proxies do not carry.
- For **installing faces**, that adapter must be **Bluetooth 4.1 or newer**. See
  [Bluetooth adapter](#bluetooth-adapter).
- The watch must not be paired to a phone at the same time.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.][add-to-ha-shield]][add-to-ha-url]

1. HACS -> **Integrations** -> three dots -> **Custom repositories**
2. Add `https://github.com/StanESmith/ha-galaxy-fit3`, category **Integration**
3. Search **Samsung Galaxy Fit3** -> **Download** -> **Restart Home Assistant**
4. Set it up as in [Configuration](#configuration)

### Manual

```bash
# Copy to your config directory
cp -r custom_components/galaxy_fit3 /config/custom_components/
# Restart Home Assistant
```

## Configuration

1. **Unpair the watch from your phone** (in the Galaxy Wearable app, or factory reset the watch).
2. Wake the watch and keep it near the Home Assistant host.
3. Add the integration: click the **Add integration** button at the top, or go to **Settings -> Devices &
   services**. The watch is usually discovered on its own (Bluetooth service `0x1A1A`); otherwise
   click **Add integration**, search for **Samsung Galaxy Fit3** and enter its Bluetooth address.
4. Setup pairs the watch by itself; confirm the pairing when Home Assistant asks.

The bond belongs to one Bluetooth adapter: after changing adapters, factory reset the watch and set it
up again.

## Entities

`<device>` is `galaxy_fit3` for a device named "Galaxy Fit3".

| Entity | Description |
|---|---|
| `sensor.<device>_battery` | Battery level. |
| `sensor.<device>_heart_rate` | Latest heart rate; also feeds the hourly statistic `galaxy_fit3:heart_rate`. Attributes `last_seen` and `stored_records`. |
| `sensor.<device>_steps` | Steps today. |
| `select.<device>_watch_face` | The faces on the watch; pick one to switch. Its picture is the current face's preview. |
| `select.<device>_store_face` | A face from Samsung's store, for the install button. |
| `button.<device>_install_watch_face` | Installs the face chosen in the store select. |
| `sensor.<device>_face_install` | Install progress: state plus `face` and `progress` attributes. |

## Actions

| Action | Description |
|---|---|
| `galaxy_fit3.install_watch_face` | Download a face from the store and send it to the watch (about a minute). |
| `galaxy_fit3.set_watch_face` | Switch to a face that is already on the watch. |
| `galaxy_fit3.fetch_face_previews` | Save the preview pictures of store faces for the dashboard (in the background). |
| `galaxy_fit3.list_watch_faces` | Return the faces on the watch. |
| `galaxy_fit3.set_time` | Set the watch's clock now. |
| `galaxy_fit3.reconnect` | Drop and re-open the Bluetooth connection. |

Every field is described in **Tools -> Actions**. Example:

```yaml
action: galaxy_fit3.install_watch_face
data:
  face_id: 16      # the face number (the store's app id ends in it: ...sm_r390_0016)
  wait: false      # return at once and report on the Face install sensor (default: wait)
```

## Dashboard

The integration ships a dashboard view that builds itself, so nothing else needs installing:

- **Galaxy Fit3**: the current face with its picture, a grid of the faces on the watch (tap one to switch,
  after a confirmation), a **Browse the face store** button, install progress and health graphs.
- **Face store** (a subview): a picture of every free face that is not on the watch yet. Tap one to install
  it; it takes about a minute, and then the face moves to the grid.

The face list comes from Samsung's store when the dashboard loads, so new faces appear on their own.
Add the two views to a dashboard (Edit dashboard -> three dots -> **Raw configuration editor**) under
`views:`. View strategies cannot be added from the visual editor.

```yaml
  - title: Galaxy Fit3
    path: galaxy-fit3
    icon: mdi:watch-variant
    strategy:
      type: custom:galaxy-fit3
  - title: Face store
    path: galaxy-fit3-store
    icon: mdi:storefront-outline
    subview: true
    back_path: /lovelace/galaxy-fit3     # your dashboard's path, then this view's path
    strategy:
      type: custom:galaxy-fit3
      page: store
```

| Option | Default | |
|---|---|---|
| `page` | `faces` | `faces` or `store` |
| `entry_id` | the first watch | which watch, when you have more than one |
| `store_path` | `galaxy-fit3-store` | the store view's `path`, if you named it differently |
| `show_health` | `true` | `false` leaves out the heart-rate and steps graphs |

**Pictures.** Faces show Samsung's small store icon at first. A face's real 256x402 preview is saved when
you install it. To save the rest, call the `galaxy_fit3.fetch_face_previews` action once (or list `face_ids`
to fetch only some). Each preview is inside the face's store package, about 4 MB, so all of them is a few
hundred MB, fetched one at a time in the background. They are saved under `/config/www/galaxy_fit3/faces/`
and served as `/local/galaxy_fit3/faces/face_<number>.png`. Photo faces (Photos, Casual Minimalist) have no
preview in their package and keep the icon.

**Coming from the generated dashboard (0.1.0).** Replace its two views with the ones above, remove the
`fit3_face` and `fit3_store_face` entries from `streamline_templates`, and uninstall Streamline Card if nothing
else uses it. The preview pictures you already have are used as they are.

## Bluetooth adapter

| Feature | Needs | Why |
|---|---|---|
| Battery, heart rate, steps, clock, switching faces | any Bluetooth LE adapter (4.0+) | These run over a BLE connection with an LE Secure Connections bond; BlueZ does the Secure Connections part in software. |
| **Installing watch faces** | **Bluetooth 4.1 or newer with Classic (BR/EDR) Secure Connections** | The face file goes over a Bluetooth Classic RFCOMM link. BlueZ derives the Classic key from the BLE pairing, and Linux only uses such a key with AES-CCM encryption, which needs a 4.1+ controller. |

- A **Bluetooth 5.x USB dongle** with an in-kernel Linux driver works, for example one on the Realtek
  RTL8761B (TP-Link UB500, ASUS USB-BT500), or an **Intel AX200/AX210** card. Check the Home Assistant OS
  release notes for the chip you pick.
- **Avoid Bluetooth 4.0 adapters** (CSR8510, Broadcom BCM20702), even though Home Assistant lists them as
  high-performance: fine for BLE sensors, but they cannot install faces.
- Place the adapter where the watch spends its time (a USB extension cable helps): at about -88 dBm or
  weaker, polls start failing.

Check an adapter on the host with `bluetoothctl mgmt.info`: `version 7` or higher means Bluetooth 4.1+
(6 = 4.0, 8 = 4.2, 9 = 5.0).

## Troubleshooting

- **Face install fails, kernel logs `Invalid security: Missing AES-CCM usage`**: the adapter is
  Bluetooth 4.0. Use a 4.1+ adapter ([Bluetooth adapter](#bluetooth-adapter)).
- **Readings stop or polls time out**: the signal is too weak; move the adapter closer to the watch.
- **Setup does not find the watch**: unpair it from the phone, wake it, and add it by Bluetooth address.
- **The Galaxy Fit3 view is empty, or says the view could not be built**: the message says why, usually that
  the integration is not set up or still loading. If you see a missing custom element
  (`ll-strategy-view-galaxy-fit3`), the dashboard loaded before the integration's script did: reload the
  page (in the companion app, Settings -> Companion app -> Debugging -> Reset frontend cache).

### Debug logging

Turn on debug logging from the integration's page (**Settings -> Devices & services -> Samsung Galaxy
Fit3 -> three dots -> Enable debug logging**), or in `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.galaxy_fit3: debug
```

When opening an issue, include the log, but remove your watch's Bluetooth address from it.

## Known limitations

- **Only battery, heart rate and steps are read.** The watch also records sleep, stress, SpO2 and
  exercise; those are not decoded yet.
- **Free store faces only.** Custom (non-store) faces cannot be installed.
- **Polling, not streaming.** Every 5 minutes the integration connects, syncs, reads and disconnects; holding
  the link open interfered with a neighbouring Bluetooth sensor. The watch keeps about a day and a half of
  data, so a long outage can leave a gap in the history.
- **Bluetooth range and adapters.** At about -88 dBm or weaker, roughly half the polls fail. The bond belongs
  to one adapter, and only the bonded adapter is used.
- **Rare install hang.** After a face install the link release has occasionally not returned; teardown is
  time-limited, so it recovers on its own.
- **Tested on one watch and one Home Assistant OS host.** Reports from other setups are welcome.

## How it works

The Fit3 does not expose a plain GATT API; it speaks **Samsung Accessory Protocol (SAP)**. Over BLE the
session is secured by an ordinary **LE Secure Connections bond** made by BlueZ, and SAP itself runs in
plaintext, so no vendor key or encryption of ours is involved. The protocol was worked out by analysis,
for interoperability with our own device, and the integration is written from that description. It
contains no Samsung program code, keys or encryption routines. It does send a few fixed messages the
watch expects from a phone (such as the setup exchange and a health capability reply), which are
protocol data.

```
[7] Fit3 app messages   per-feature TLV: battery, clock, watch faces, health
[6] Capability/session  capability discovery and session setup
[3] SAP reassembly/ack  fragments, 4-bit sequence, ACK/ABORT, window
[2] SAP Lite frame      2-byte header codec
[1] BLE GATT pipe       service 0x1A1A, write/notify characteristics
[0] BLE link            bleak GATT client
```

The BLE pipe: service `00001a1a-0000-1000-8000-00805f9b34fb`, write characteristic
`63e30bad-4206-4596-839f-e47cbf7a4b5d`, notify characteristic `797ae4e9-2e58-4fe8-b48d-b5c79599fb9b`.
(Layers 4 and 5, authentication and record encryption, are not used over BLE: the bond secures the link.)

## Development

```
custom_components/galaxy_fit3/   the integration
  sap/                           SAP protocol stack (pure Python)
  face_store.py  store_catalog.py   Samsung store catalogue, face downloads, preview pictures
  frontend/galaxy-fit3.js        the dashboard view strategy (plain JavaScript, no build step)
  brand/                         the integration's icon, shown by Home Assistant 2026.3 and newer
scripts/
  fetch_store_faces.py   list the store, download face packages, extract preview pictures
  make_brand_images.py   draw the icon (original artwork; needs Pillow)
  release_check.py       check a release tag against the manifest and the changelog
tests/                   unit tests: no Home Assistant needed
tests_ha/                integration tests inside the real Home Assistant core
tests_js/                tests of the dashboard strategy (Node)
CHANGELOG.md             release notes, one section per version
```

### Tests

Two suites, run in separate processes: the unit tests install stand-in `homeassistant` modules, which would
get in the way of the real one.

```bash
# unit tests: the protocol codec, the client against a fake watch, the config-flow state machine
uv venv --python 3.14
uv pip install -r requirements-test.txt dbus-fast   # dbus-fast is optional: the pairing-agent tests skip without it
dbus-run-session -- .venv/bin/pytest

# Home Assistant tests: the real core (pytest-homeassistant-custom-component), a fake watch
uv venv --python 3.14 .venv-ha
uv pip install --python .venv-ha/bin/python -r tests_ha/requirements.txt
.venv-ha/bin/python tests_ha/ha_requirements.py | uv pip install --python .venv-ha/bin/python -r -
.venv-ha/bin/pytest tests_ha
```

The Home Assistant tests set up the config entry, check the entities, translations and service
descriptions, call the actions, and drive the config and options flows. The watch is replaced by an
autospecced `GalaxyFit3Client`, which can only offer methods the real client has, so a renamed method
fails the test. Bluetooth itself (BlueZ, bonding, the link) is not covered; that needs the hardware.

### Releasing

1. Raise `version` in `custom_components/galaxy_fit3/manifest.json` and fill in a `## [X.Y.Z]` section of
   `CHANGELOG.md`.
2. `python3 scripts/release_check.py vX.Y.Z` confirms the tag, manifest and changelog agree.
3. Commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`. The release workflow runs both test suites and
   publishes the GitHub release with that changelog section as its notes; HACS then offers the update to
   everyone who installed through it. A tag with a suffix (`v0.2.0-rc1`) is published as a pre-release.

After copying changed files into `custom_components/` on your own system, restart Home Assistant: reloading
the config entry does not re-import changed Python modules.

**CI.** GitHub Actions (`.github/workflows/ci.yml`) runs the unit tests, the Home Assistant tests, hassfest
and HACS validation, and `release.yml` publishes releases.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests (`.venv/bin/pytest`)
5. Open a Pull Request

Keep device identifiers (Bluetooth addresses, captures) and Samsung's face packages out of commits.

## License

Apache-2.0 - see [LICENSE](LICENSE)

## Attribution

This integration was built with the help of **Claude Code** (Anthropic).

## Links

- **Repository:** [github.com/StanESmith/ha-galaxy-fit3](https://github.com/StanESmith/ha-galaxy-fit3)
- **HACS:** Add as custom repository (Integration category)

[releases-shield]: https://img.shields.io/github/v/release/StanESmith/ha-galaxy-fit3?style=for-the-badge
[releases]: https://github.com/StanESmith/ha-galaxy-fit3/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/StanESmith/ha-galaxy-fit3?style=for-the-badge
[commits]: https://github.com/StanESmith/ha-galaxy-fit3/commits/main
[license-shield]: https://img.shields.io/github/license/StanESmith/ha-galaxy-fit3?style=for-the-badge
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[hacs]: https://hacs.xyz/docs/faq/custom_repositories/
[ha-shield]: https://img.shields.io/badge/Home%20Assistant-2026.10%2B-41BDF5.svg?style=for-the-badge&logo=homeassistant&logoColor=white
[ha]: https://www.home-assistant.io/
[ci-shield]: https://img.shields.io/github/actions/workflow/status/StanESmith/ha-galaxy-fit3/ci.yml?branch=main&style=for-the-badge&label=CI
[ci]: https://github.com/StanESmith/ha-galaxy-fit3/actions/workflows/ci.yml
[maintenance-shield]: https://img.shields.io/maintenance/yes/2026?style=for-the-badge
[buymecoffee-shield]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[buymecoffee]: https://www.buymeacoffee.com/StanESmith

[add-to-ha-shield]: https://my.home-assistant.io/badges/hacs_repository.svg
[add-to-ha-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=StanESmith&repository=ha-galaxy-fit3&category=integration
[config-flow-shield]: https://my.home-assistant.io/badges/config_flow_start.svg
[config-flow-url]: https://my.home-assistant.io/redirect/config_flow_start/?domain=galaxy_fit3
