# Switcher Cloud for Home Assistant

Switcher devices in Home Assistant, through the Switcher cloud.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/shaulbarlev/switcher-cloud)](https://github.com/shaulbarlev/switcher-cloud/releases)
[![Min HA version](https://img.shields.io/badge/Home%20Assistant-2025.3%2B-blue.svg)](https://www.home-assistant.io/)
[![License](https://img.shields.io/github/license/shaulbarlev/switcher-cloud)](LICENSE)

> Only the On Wall boiler has been tested on real hardware. The other device families were taken from the official app's code and have not been run against a device yet. See the [support table](#device-support).

---

## Why this exists

Home Assistant ships `switcher_kis`, which talks to Switcher devices directly over your LAN. That is the right choice when it works, and it does for most models.

It does not work for the On Wall boiler. The `aioswitcher` maintainers tried in [PR #847](https://github.com/TomerFi/aioswitcher/pull/847). The tracking issue, [aioswitcher #889](https://github.com/TomerFi/aioswitcher/issues/889), ends with "nothing we can do right now", waiting on a Switcher firmware update with no ETA. The same wall came up in [homebridge-switcher-platform #75](https://github.com/nitaybz/homebridge-switcher-platform/issues/75). I own one of these boilers, so I was stuck.

So this integration does what the app does. It logs in to the Switcher cloud with your app account and speaks the app's own binary protocol, which I reverse-engineered from the app. Every device on the account shows up, whether or not it has a local interface. If a device of yours already works with `switcher_kis`, keep using that for it; local is faster and does not depend on Switcher's servers.

The protocol is reverse engineered from the Switcher Android app and follows its patterns.

This is unofficial and not affiliated with Switcher.

## Features

- Every device with on/off gets a switch. Multi-gang wall switches get one per gang.
- Boilers get a water heater entity, a run timer and the auto shutdown setting.
- Runner shutters get a cover with open, close, stop and set position.
- Lights get brightness, RGB, colour temperature and effects.
- Current, energy and time remaining sensors, where the device reports them.
- Live updates. One connection to the cloud stays open, so a change made in the app shows up a few seconds later.

## Device support

Only my own model, the On Wall boiler, is verified as of today. Nothing else in this table has been tested on a real device.

Status is per protocol family. The DeviceType is the four hex digit code in the diagnostics download.

| Switcher product | DeviceType | Status | Entities |
|---|---|---|---|
| On Wall boiler (S216) | `0321` | Verified, including the run timer | switch, water_heater, number, sensors |
| Bath heater / Touch boiler | `031F` | Unverified | switch, water_heater, number, sensors |
| Power plugs / smart plugs | `030B` `030F` `0317` `01A1` `01A7` `01A8` | Unverified | switch, sensors |
| Wall switches, multi-gang | other `01xx` / `03xx` | Unverified | one switch per gang, sensors |
| Runner shutters / curtains | `0Cxx` | Unverified | cover |
| Mix panels (plug and runner lines) | `0Fxx` | Unverified | switch per plug line, cover per runner line, sensors |
| Lights | `08xx` | Unverified. On/off is solid, colour is a best guess | light |
| Breeze (AC / IR blaster) | `0Exx` | Not supported | none |

Unverified means the command bytes were copied from the decompiled official app and match what the app sends, byte for byte, but nobody has run them against the real device yet. If something is off, the device ignores the command or you get an error toast. It will not damage anything. If you own one of these and it works, or does not, open an issue and say so.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=shaulbarlev&repository=switcher-cloud&category=integration)

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. In HACS, open the three dot menu and pick Custom repositories.
3. Add `https://github.com/shaulbarlev/switcher-cloud` with the category set to Integration.
4. Search for Switcher Cloud and install it.
5. Restart Home Assistant.

### Manual

1. Download the latest release.
2. Copy `custom_components/switcher_cloud` into your Home Assistant `custom_components` directory.
3. Restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=switcher_cloud)

1. Go to Settings, then Devices & services, then Add integration.
2. Search for Switcher Cloud.
3. Enter the email and password you use in the Switcher Smart Home app.
4. Every device in the account gets added. The Name and assign screen lets you put them in areas.

Notes:

- Email and password must each be 32 characters or fewer. The vendor protocol packs them into fixed 32 byte fields.
- One config entry per Switcher account. Adding the same account twice is rejected.
- Devices added to the account later appear after you reload the integration.
- State is polled every 60 seconds, or every 15 seconds while a boiler is on. When the cloud pushes a change, the integration refreshes a few seconds later instead of waiting for the next poll.

### Reauthentication

If you change your Switcher password, Home Assistant shows a Re-authenticate notification on the integration. Enter the new password and it reloads. You cannot change the email in that flow. To switch accounts, remove the entry and add it again.

## Entities

Entity ids come from the device name in the Switcher app. Each cloud device becomes one Home Assistant device with the manufacturer, model and firmware version filled in. Unverified families say "(unverified)" in the model field so you can tell at a glance.

| Entity | Created for | Notes |
|---|---|---|
| `switch` | all controllable families | Main on/off. One per gang on wall switches, one per plug line on mix panels. |
| `water_heater` | On Wall, Bath heater | Shows On / Off. No temperature, the cloud does not report one. |
| `number` "Run timer" | On Wall, Bath heater | Minutes, slider 0 to 180 in steps of 5. Setting N turns the device on and it switches itself off after N minutes. Setting 0 turns it off. The device does not report the remaining time, so the number shows the requested value until the device turns off. The device may clamp the value to its own maximum. |
| `number` "Auto shutdown" | On Wall, Bath heater | The device's own auto-shutdown limit in minutes, the same setting as in the app. Read from the device every poll. 0 means unlimited; values from 1 to 60 are raised to 61 by the device. |
| `sensor` "Current" | energy-reporting families | Amperes. The device reports a raw value and the app divides by 220, so this follows the same convention. |
| `sensor` "Energy" | energy-reporting families | kWh, total increasing, suitable for the Energy dashboard. |
| `sensor` "Time remaining" | On Wall, Bath heater | Seconds until the device switches itself off. Ticks every second, seeded from the device and re-synced on each poll, the way the app does it. 0 when off. |
| `cover` | Runner shutters, mix-panel runner lines | Open / close / stop / position. Device class shutter. |
| `light` | Lights | Brightness, RGB, colour temperature (2700 to 6500 K nominal), effects. Colour temperature direction unverified. |

When a device is offline in the Switcher cloud its entities go unavailable.

### Dashboard tip

For a boiler, a tile card on the run timer gives you a "boost for 30 minutes" slider:

```yaml
type: tile
entity: number.boiler_run_timer
features:
  - type: numeric-input
    style: slider
```

Automations can call `number.set_value` with a number of minutes to start a timed run. The device handles turning itself off.

## Privacy and security

- Your Switcher email and password are stored in the Home Assistant config entry, like every other cloud integration. They are redacted in diagnostics and never logged.
- The integration only talks to `appserver.switcher.co.il:9091`, the same server the official app uses. There is no third party involved and nothing is sent anywhere else.
- The vendor protocol is plain TCP. Message bodies are scrambled with AES-ECB using a fixed key that ships inside the Switcher app. That is not the same as TLS, so treat the link as unencrypted. This is how the Switcher cloud works and the integration cannot change it.
- The integration polls once a minute and keeps one idle TCP connection open with a heartbeat. The official app polls far more aggressively.

## Limitations and not supported

- Breeze (AC / IR blaster) is not supported and gets no entities.
- Schedules and timers you set up in the app are not exposed. The run timer here is a one-shot "turn on for N minutes".
- No temperature readings. The cloud does not provide any.
- Mix panel runner lines and the colour/white channels of lights are the parts I am least sure about.
- Polling interval is 60 seconds, 15 seconds while a boiler is on. Not configurable.

## Contributing

Issues and pull requests are welcome. Bug reports are most useful with the diagnostics download attached (Settings, Devices & services, Switcher Cloud, three dot menu, Download diagnostics). For an unverified device, a packet capture of the official app controlling it is what lets me confirm the bytes; [PCAPdroid](https://github.com/emanuele-f/PCAPdroid) on Android does that without root.

The protocol code lives in `custom_components/switcher_cloud/devices.py`. Each builder and parser names the app class and method it came from, so a reviewer can check it against the decompiled source. Unit tests run with `python -m pytest tests` and need only Python and `pycryptodome`.

## Disclaimer

This is an unofficial integration. It is not affiliated with, endorsed by or supported by Switcher. I built it by reverse-engineering the public Switcher Smart Home app. Use it at your own risk; Switcher can change its cloud at any time and break it.

## License

See [LICENSE](LICENSE).
