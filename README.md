# Switcher Cloud for Home Assistant

Switcher devices in Home Assistant, through the Switcher cloud.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/shaulbarlev/switcher-cloud)](https://github.com/shaulbarlev/switcher-cloud/releases)
[![Min HA version](https://img.shields.io/badge/Home%20Assistant-2025.3%2B-blue.svg)](https://www.home-assistant.io/)
[![License](https://img.shields.io/github/license/shaulbarlev/switcher-cloud)](LICENSE)

> Only the On Wall boiler has been tested on real hardware. The other device families were taken from the official app's code and cross-checked against [aioswitcher](https://github.com/TomerFi/aioswitcher), the field-tested LAN library behind Home Assistant's `switcher_kis`, but have not been run against a device through this integration yet. See the [support table](#device-support).

---

## Why this exists

Home Assistant ships `switcher_kis`, which talks to Switcher devices directly over your LAN. That is the right choice when it works, and it does for most models.

It does not work for the On Wall boiler. The `aioswitcher` maintainers tried in [PR #847](https://github.com/TomerFi/aioswitcher/pull/847) (issue [aioswitcher #889](https://github.com/TomerFi/aioswitcher/issues/889)). Seems it is dependent on a Switcher firmware update with no ETA. I own one of these boilers, so I was stuck.

So this logs in to the Switcher cloud with your app account and speaks the app's own binary protocol, which I reverse-engineered from the app. If a device of yours already works with `switcher_kis`, keep using that for it; local is faster and does not depend on Switcher's servers.

The protocol is reverse engineered from the Switcher Android app and follows its patterns.

This is unofficial and not affiliated with Switcher.

## Features

- Every device with on/off gets a switch. Multi-gang wall switches get one per gang.
- Boilers (On Wall, V2, Touch, V4, Mini, Heater) get a water heater entity, a run timer and the auto shutdown setting.
- Runner shutters get a cover with open, close, stop and set position, plus a child lock switch. Runner S11 and S12 also get their light lines.
- Switcher Light SL01, SL02 and SL03 get one on/off light per line.
- Power, current, energy and auto-off-time sensors, where the device reports them. Breeze gets a temperature sensor.
- Live updates. One connection to the cloud stays open, so a change made in the app shows up a few seconds later.

## Device support

The only model tested is the On Wall, as it is the only Switcher product I have. Assume experimental support for all the rest.

Status is per protocol family. The DeviceType is the four hex digit code in the diagnostics download. "Evidence" says where the bytes come from: the decompiled app, and whether aioswitcher (which drives the same devices over the LAN with the same command and status layouts) agrees.

| Switcher product | DeviceType | Evidence | Entities |
|---|---|---|---|
| On Wall boiler (S216) | `0321` | Verified on hardware, including the run timer and auto shutdown | switch, water_heater, number, sensors |
| V2, Touch, V4, Mini boilers | `01A1` `01A7` `030B` `030F` `0317` | App + aioswitcher agree on on/off, timer, auto shutdown and the status layout | switch, water_heater, number, sensors |
| Power Plug | `01A8` | App + aioswitcher agree | switch, sensors |
| Heater | `031F` | App + aioswitcher agree on on/off, timer and status. No auto shutdown write in either | switch, water_heater, run timer, sensors |
| Runner, Runner Mini | `0C01` `0C02` | App + aioswitcher agree on position, stop, child lock and status | cover, child lock switch |
| Runner S11, Runner S12 | `0F01` `0F02` | App + aioswitcher agree, per line | cover and child lock per runner line, light per light line |
| Light SL01, SL02, SL03 (and Mini) | `0F04` `0F05` `0F06` `0F07` `0F08` | App + aioswitcher agree, per line | one on/off light per line |
| Breeze (AC / IR blaster) | `0E01` | Status layout agrees with aioswitcher. Control needs the IR code database, not included | temperature sensor only |
| Wall switches, multi-gang | other `01xx` / `03xx` | App only. Not a Switcher product, gang count is guessed from status | one switch per gang, sensors |
| Colour lights | `08xx` | App only. On/off is solid, colour is a best guess | light |

"App + aioswitcher agree" means the command bytes were copied from the decompiled official app and the same sub-operation and argument layout is what aioswitcher sends to that device over the LAN, where it is known to work. The cloud wraps the same device-side payload, so this is strong evidence, but nobody has run them through this integration yet. If something is off, the device ignores the command or you get an error toast. It will not damage anything. If you own one of these and it works, or does not, open an issue and say so.

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
| `switch` | boilers, plugs, wall switches | Main on/off. One per gang on wall switches. |
| `switch` "Child lock" | Runners, runner lines | The Runner's child lock, as in the app and `switcher_kis`. |
| `water_heater` | boilers | Shows On / Off. No temperature, the cloud does not report one. |
| `number` "Run timer" | boilers | Minutes, slider 0 to 180 in steps of 5. Setting N turns the device on and it switches itself off after N minutes. Setting 0 turns it off. The device may clamp the value to its own maximum. |
| `number` "Auto shutdown" | On Wall, V2, Touch, V4, Mini | The device's own auto-shutdown limit in minutes, the same setting as in the app. 0 means unlimited; values from 1 to 60 are raised to 61. The Heater reports this value but has no way to set it, so it gets a diagnostic sensor instead. |
| `sensor` "Power" | energy-reporting families | Watts. aioswitcher reads the same field as watts. |
| `sensor` "Current" | energy-reporting families | Amperes, watts divided by 220, the convention both the app and aioswitcher use. |
| `sensor` "Energy" | energy-reporting families | kWh, total increasing, suitable for the Energy dashboard. The On Wall reports 0 here. |
| `sensor` "Auto off at" | boilers | The moment the device will switch itself off, as a timestamp. Home Assistant shows it as a live countdown. Empty when the device is off. |
| `sensor` "Temperature" | Breeze | Room temperature from the Breeze's own sensor. |
| `cover` | Runners, runner lines | Open / close / stop / position. Open and close are position 100 and 0, like `switcher_kis`. Device class shutter. |
| `light` | Light SL01/02/03, S11/S12 light lines | On/off per line. |
| `light` | Colour lights (`08xx`) | Brightness, RGB, colour temperature (2700 to 6500 K nominal), effects. Colour temperature direction unverified. |

When a device is offline in the Switcher cloud its entities go unavailable.


## Privacy and security

- Your Switcher email and password are stored in the Home Assistant config entry, like every other cloud integration. They are redacted in diagnostics and never logged.
- The integration only talks to `appserver.switcher.co.il:9091`, the same server the official app uses. There is no third party involved and nothing is sent anywhere else.
- The vendor protocol is plain TCP. Message bodies are scrambled with AES-ECB using a fixed key that ships inside the Switcher app. That is not the same as TLS, so treat the link as unencrypted. This is how the Switcher cloud works and the integration cannot change it.
- The integration polls once a minute and keeps one idle TCP connection open with a heartbeat. The official app polls far more aggressively.

## Limitations and not supported

- Breeze (AC / IR blaster) only gets a temperature sensor. Sending IR commands needs the per-remote IR code set that the app downloads from Switcher (aioswitcher ships it as a 14 MB database); the command framing is known, the codes are not bundled here.
- Schedules and timers you set up in the app are not exposed. The run timer here is a one-shot "turn on for N minutes".
- The colour/white channels of `08xx` lights and the gang count of multi-gang wall switches are the parts I am least sure about. Neither is a Switcher product.

## Contributing

Issues and pull requests are welcome. Bug reports are most useful with the diagnostics download attached. For an unverified device, a packet capture of the official app controlling it is what lets me confirm the bytes. I use [PCAPdroid](https://github.com/emanuele-f/PCAPdroid) on Android.

The protocol code lives in `custom_components/switcher_cloud/devices.py`. Unit tests run with `python -m pytest tests` and need only Python and `pycryptodome`.

## Disclaimer

This is an unofficial integration. It is not affiliated with, endorsed by or supported by Switcher. I built it by reverse-engineering the public Switcher Smart Home app. Use it at your own risk; Switcher can change its cloud at any time and break it.

## License

See [LICENSE](LICENSE).
