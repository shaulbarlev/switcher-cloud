# Switcher Cloud for Home Assistant

Control the Switcher devices that only work through the vendor cloud, from Home Assistant.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/shaulbarlev/switcher-cloud)](https://github.com/shaulbarlev/switcher-cloud/releases)
[![Min HA version](https://img.shields.io/badge/Home%20Assistant-2025.3%2B-blue.svg)](https://www.home-assistant.io/)
[![License](https://img.shields.io/github/license/shaulbarlev/switcher-cloud)](LICENSE)

> Beta. Only the On Wall boiler has been tested on real hardware. Every other device family was copied from the official app's code and has not been run against a device yet. See the [support matrix](#device-support) and [how to get your device verified](#help-verify-a-device).

---

## What it is

Switcher is an Israeli brand of Wi-Fi boiler switches, plugs, wall switches and shutter controllers. Home Assistant already supports many of them through the built-in `switcher_kis` integration, which talks to devices on your LAN over UDP.

Some Switcher products have no local API at all. The On Wall boiler switch is the main one. They only talk to the Switcher cloud, so `switcher_kis` and the `aioswitcher` library cannot see them.

This integration speaks the same binary protocol the Switcher Smart Home app uses with the vendor cloud. I reverse-engineered it from the app and wrapped it in normal Home Assistant entities.

It is unofficial and cloud only. If your device already works with `switcher_kis`, use that. It is local and faster.

## Features

- Every device with on/off gets a switch. Multi-gang wall switches get one per gang.
- Boilers (On Wall, Bath heater) also get a water heater entity and a run timer in minutes. Set the timer and the device turns itself off when the time is up, which still works if Home Assistant goes down halfway through.
- Runner shutters get a cover with open, close, stop and set position.
- Lights get brightness, RGB, colour temperature and effects. This part is beta.
- Current, energy and time-on sensors, where the device reports them.
- Live updates. One connection to the cloud stays open, so a change made in the app shows up a few seconds later.
- Control is confirmed, not assumed. When you flip a switch the UI waits for the device to report the new state before it changes.
- A reauthentication prompt when your Switcher password changes.
- A diagnostics download with credentials and identifiers redacted.
- Setup is a config flow. There is no YAML.

## Device support

Status is per protocol family. The DeviceType is the four hex digit code in the diagnostics download.

| Switcher product | DeviceType | Status | Entities |
|---|---|---|---|
| On Wall boiler (S216) | `0321` | Verified on hardware, including the run timer | switch, water_heater, number, sensors |
| Bath heater / Touch boiler | `031F` | Beta | switch, water_heater, number, sensors |
| Power plugs / smart plugs | `030B` `030F` `0317` `01A1` `01A7` `01A8` | Beta | switch, sensors |
| Wall switches, multi-gang | other `01xx` / `03xx` | Beta | one switch per gang, sensors |
| Runner shutters / curtains | `0Cxx` | Beta | cover |
| Mix panels (plug and runner lines) | `0Fxx` | Beta | switch per plug line, cover per runner line, sensors |
| Lights | `08xx` | Beta. On/off is solid, colour is unverified | light |
| Breeze (AC / IR blaster) | `0Exx` | Not supported | none |

Beta means the command bytes were copied from the decompiled official app and match what the app sends, byte for byte, but nobody has run them against the real device yet. If something is off, the device ignores the command or you get an error toast. It will not damage anything. If you own one of these, please [help verify it](#help-verify-a-device).

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
- State is polled every 60 seconds. When the cloud pushes a change, the integration refreshes a few seconds later instead of waiting for the next poll.

### Reauthentication

If you change your Switcher password, Home Assistant shows a Re-authenticate notification on the integration. Enter the new password and it reloads. You cannot change the email in that flow. To switch accounts, remove the entry and add it again.

## Entities

Entity ids come from the device name in the Switcher app. Each cloud device becomes one Home Assistant device with the manufacturer, model and firmware version filled in. Unverified families say "(beta, unverified)" in the model field so you can tell at a glance.

| Entity | Created for | Notes |
|---|---|---|
| `switch` | all controllable families | Main on/off. One per gang on wall switches, one per plug line on mix panels. |
| `water_heater` | On Wall, Bath heater | Operation modes `electric` / `off`. No temperature, the cloud does not report one. |
| `number` "Run timer" | On Wall, Bath heater | Minutes, slider 0 to 180 in steps of 5. Setting N turns the device on and it switches itself off after N minutes. Setting 0 turns it off. The device does not report the remaining time, so the number shows the requested value until the device turns off. The device may clamp the value to its own maximum. |
| `sensor` "Current" | energy-reporting families | Amperes. The device reports a raw value and the app divides by 220, so this follows the same convention. |
| `sensor` "Energy" | energy-reporting families | kWh, total increasing, suitable for the Energy dashboard. |
| `sensor` "Time on" | energy-reporting families | Seconds the device has been on in the current session. |
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

## Troubleshooting

### "Invalid email or password" during setup

- Check that you can log in to the Switcher Smart Home app with the same email and password.
- Email and password are limited to 32 characters each.
- The cloud's login error codes:

| Code | Meaning |
|---|---|
| 5 | Account not registered |
| 11 | Wrong password |
| 13 | Account error |
| 6, 12, 1xx | Temporary server-side errors. Setup retries on its own. |

### "Failed to connect to the Switcher cloud"

- Home Assistant needs outbound TCP to `appserver.switcher.co.il` on port 9091. Check firewalls and ad-blocking DNS.
- The Switcher cloud sometimes rejects logins for a while after the app logs in. Wait a minute and retry.

### Entities unavailable

- The device is offline in the Switcher app too: check its Wi-Fi.
- The cloud connection dropped. The integration reconnects with backoff and logs one warning. Entities come back on their own.

### A command does nothing, or shows "The Switcher cloud rejected the command"

- For a Beta family this most likely means the command copied from the app does not match your device. See [Help verify a device](#help-verify-a-device).
- The message includes the cloud's error code. Include it in the issue.

### Debug logging

```yaml
logger:
  logs:
    custom_components.switcher_cloud: debug
```

Debug output includes decoded push messages and poll failures. It never logs your password.

### Diagnostics download

Settings, Devices & services, Switcher Cloud, then the three dot menu and Download diagnostics. The file has the raw WorkStatus bytes and DeviceType for every device. Account, password, device passwords, MAC addresses, names and device ids are redacted. Attach it to any issue.

### Help verify a device

To move a family from Beta to Verified, a maintainer needs to compare what the official app sends with what this integration sends. You can provide that without root:

1. Install [PCAPdroid](https://github.com/emanuele-f/PCAPdroid) on an Android phone with the Switcher Smart Home app.
2. In PCAPdroid, select only the Switcher app as the target and start the capture.
3. In the Switcher app, turn the device on, wait ten seconds, turn it off. If it has a timer or a shutter, use that too.
4. Stop the capture and export it as a PCAP file.
5. Open an issue with the PCAP, your diagnostics download, and the exact product name.

The capture contains your session token and the device password for that session. Both expire, and changing your Switcher password afterwards rotates them. If you would rather not post it publicly, send it to the maintainer privately.

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
- Local control is out of scope. For LAN devices use the built-in `switcher_kis` integration.
- Polling interval is fixed at 60 seconds.

## Contributing

Issues and pull requests are welcome.

- Bug reports: attach the diagnostics download and, for Beta devices, a packet capture (see above).
- The protocol code lives in `custom_components/switcher_cloud/devices.py`. Each builder and parser names the app class and method it came from, so a reviewer can check it against the decompiled source.
- Run the unit tests with:

```bash
python -m pytest tests
```

They need only Python and `pycryptodome`, not Home Assistant.

## Disclaimer

This is an unofficial integration. It is not affiliated with, endorsed by or supported by Switcher. I built it by reverse-engineering the public Switcher Smart Home app. Use it at your own risk; Switcher can change its cloud at any time and break it.

## License

See [LICENSE](LICENSE).
