# Switcher Cloud for Home Assistant

**Control Switcher devices that only work through the vendor cloud, straight from Home Assistant.**

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/shaulbarlev/switcher-cloud)](https://github.com/shaulbarlev/switcher-cloud/releases)
[![Min HA version](https://img.shields.io/badge/Home%20Assistant-2025.3%2B-blue.svg)](https://www.home-assistant.io/)
[![License](https://img.shields.io/github/license/shaulbarlev/switcher-cloud)](LICENSE)

> **Beta.** Only the **On Wall boiler** is verified on real hardware. Every other device family was transcribed from the official app and has not been run against a device yet. See the [support matrix](#device-support) and [how to get your device verified](#help-verify-a-device).

---

## What it is

Switcher is an Israeli brand of Wi-Fi boiler switches, plugs, wall switches and shutter controllers. Home Assistant already supports many of them through the built-in `switcher_kis` integration, which talks to devices on your LAN over UDP.

Some Switcher products, most notably the **On Wall** boiler switch, have **no local API**. They talk only to the Switcher cloud, so `switcher_kis` and the `aioswitcher` library cannot see them.

This integration fills that gap. It speaks the same binary protocol the Switcher Smart Home app uses with the vendor cloud, reverse-engineered from the app, and exposes those devices as normal Home Assistant entities.

- Unofficial, not affiliated with Switcher.
- Cloud only. If your device works with `switcher_kis`, use that instead. It is local and faster.

## Features

- **Switch** for every device with on/off, one entity per gang on multi-gang wall switches
- **Water heater** entity for boiler-type devices (On Wall, Bath heater)
- **Run timer** (minutes) for boilers: the device turns itself off after the set time, even if Home Assistant goes down
- **Cover** for Runner shutters: open, close, stop, set position
- **Light** with brightness, RGB, colour temperature and effects (beta)
- **Sensors** for current, energy and time-on where the device reports them
- **Live updates**: a persistent connection receives the cloud's push notifications and refreshes state within a few seconds of a change made in the app
- **Confirmed control**: the UI flips only after the device reports the new state, never optimistically
- **Reauthentication** flow when your Switcher password changes
- **Diagnostics** download with credentials and identifiers redacted
- Config flow only, no YAML

## Device support

Status is per protocol family. `DeviceType` is the four-hex-digit code you will find in the diagnostics download.

| Switcher product | DeviceType | Status | Entities |
|---|---|---|---|
| On Wall boiler (S216) | `0321` | ✅ **Verified** on hardware, including run timer | switch, water_heater, number, sensors |
| Bath heater / Touch boiler | `031F` | 🟡 Beta | switch, water_heater, number, sensors |
| Power plugs / smart plugs | `030B` `030F` `0317` `01A1` `01A7` `01A8` | 🟡 Beta | switch, sensors |
| Wall switches, multi-gang | other `01xx` / `03xx` | 🟡 Beta | one switch per gang, sensors |
| Runner shutters / curtains | `0Cxx` | 🟡 Beta | cover |
| Mix panels (plug and runner lines) | `0Fxx` | 🟡 Beta | switch per plug line, cover per runner line, sensors |
| Lights | `08xx` | 🟡 Beta, on/off high confidence, colour unverified | light |
| Breeze (AC / IR blaster) | `0Exx` | ❌ Not supported | none |

**What Beta means.** The command bytes were transcribed from the decompiled official app and match what the app sends byte for byte, but nobody has run them against the real device. The worst case is "nothing happens" or an error toast, not damage. If you own one of these devices, please [help verify it](#help-verify-a-device).

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=shaulbarlev&repository=switcher-cloud&category=integration)

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. In HACS, open the menu (⋮) → **Custom repositories**.
3. Add `https://github.com/shaulbarlev/switcher-cloud` with category **Integration**.
4. Search for **Switcher Cloud** and install it.
5. Restart Home Assistant.

### Manual

1. Download the latest release.
2. Copy `custom_components/switcher_cloud` into your Home Assistant `custom_components` directory.
3. Restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=switcher_cloud)

1. Go to **Settings → Devices & services → Add integration**.
2. Search for **Switcher Cloud**.
3. Enter the **email and password of your Switcher Smart Home app account**.
4. All devices in the account are added. Use the **Name and assign** screen to put them in areas.

Notes:

- Email and password must each be **32 characters or fewer**. This is a limit of the vendor protocol.
- One config entry per Switcher account. Adding the same account twice is rejected.
- Devices added to the account later appear after you reload the integration.
- State is polled every 60 seconds. Pushes from the cloud trigger an extra refresh a few seconds after a change.

### Reauthentication

If you change your Switcher password, Home Assistant shows a **Re-authenticate** notification on the integration. Enter the new password and the integration reloads. The email cannot be changed in that flow. To switch accounts, remove the entry and add it again.

## Entities

Entity ids are derived from the device name in the Switcher app. Each cloud device becomes one Home Assistant device with manufacturer **Switcher**, the model name, and the firmware version. Unverified families carry "(beta, unverified)" in the model name so you can tell at a glance.

| Entity | Created for | Notes |
|---|---|---|
| `switch` | all controllable families | Main on/off. One per gang on wall switches, one per plug line on mix panels. |
| `water_heater` | On Wall, Bath heater | Operation modes `electric` / `off`. No temperature, the cloud does not report one. |
| `number` "Run timer" | On Wall, Bath heater | Minutes, slider 0 to 180 in steps of 5. Setting N turns the device on and it switches itself off after N minutes. Setting 0 turns it off. The device does not report the remaining time, so the number shows the requested value until the device turns off. The device may clamp the value to its own maximum. |
| `sensor` "Current" | energy-reporting families | Amperes, vendor convention. |
| `sensor` "Energy" | energy-reporting families | kWh, total increasing, suitable for the Energy dashboard. |
| `sensor` "Time on" | energy-reporting families | Seconds the device has been on in the current session. |
| `cover` | Runner shutters, mix-panel runner lines | Open / close / stop / position. Device class shutter. |
| `light` | Lights | Brightness, RGB, colour temperature (2700 to 6500 K nominal), effects. Colour temperature direction unverified. |

A device that is offline in the Switcher cloud shows its entities as **unavailable**.

### Dashboard tip

For a boiler, a tile card on the `switch` entity plus the `number` run timer gives a one-tap "boost for 30 minutes" control:

```yaml
type: tile
entity: number.boiler_run_timer
features:
  - type: numeric-input
    style: slider
```

Automations can call `number.set_value` with a minutes value to start a timed run and never worry about turning it off.

## Troubleshooting

**"Invalid email or password" during setup**

- Check that you can log in to the Switcher Smart Home app with the same email and password.
- Email and password are limited to 32 characters each.
- The cloud login error codes map as follows:

| Code | Meaning |
|---|---|
| 5 | Account not registered |
| 11 | Wrong password |
| 13 | Account error |
| 6, 12, 1xx | Transient server-side errors. Setup retries automatically. |

**"Failed to connect to the Switcher cloud"**

- Home Assistant needs outbound TCP to `appserver.switcher.co.il` on port 9091. Check firewalls and ad-blocking DNS.
- The Switcher cloud sometimes rejects logins for a while after the app logs in. Wait a minute and retry.

**Entities unavailable**

- The device is offline in the Switcher app too: check its Wi-Fi.
- The cloud connection dropped: the integration reconnects with backoff and logs a warning. Entities come back on their own.

**A command does nothing or shows "The Switcher cloud rejected the command"**

- For a Beta family this most likely means the transcribed command does not match your device. Please follow [Help verify a device](#help-verify-a-device).
- The message includes the cloud's error code. Include it in the issue.

**Debug logging**

```yaml
logger:
  logs:
    custom_components.switcher_cloud: debug
```

Debug output includes decoded push messages and poll failures. It never logs your password.

**Diagnostics download**

Settings → Devices & services → Switcher Cloud → ⋮ → **Download diagnostics**. The file contains the raw `WorkStatus` bytes and `DeviceType` for every device, with account, password, device passwords, MAC addresses, names and device ids redacted. Attach it to any issue.

### Help verify a device

To move a family from Beta to Verified, a maintainer needs to compare what the official app sends with what this integration sends. You can provide that without root:

1. Install [PCAPdroid](https://github.com/emanuele-f/PCAPdroid) on an Android phone with the Switcher Smart Home app.
2. In PCAPdroid, select only the Switcher app as the target and start the capture.
3. In the Switcher app, turn the device **on**, wait ten seconds, turn it **off**, and if it has one, set a timer or move a shutter.
4. Stop the capture and export it as a PCAP file.
5. Open an issue with the PCAP, your diagnostics download, and the exact product name.

The capture contains your session token and the device password for that session, both of which expire and can be rotated by changing your Switcher password afterwards. Do not post captures publicly if you are not comfortable with that. A private message to the maintainer works too.

## Privacy and security

- Your Switcher email and password are stored in the Home Assistant config entry, like every other cloud integration. They are redacted in diagnostics and never logged.
- The integration talks **only** to `appserver.switcher.co.il:9091`, the same server the official app uses. No third party, no telemetry.
- The vendor protocol is plain TCP. Message bodies are scrambled with AES-ECB using a fixed key that ships inside the Switcher app. This is **not** equivalent to TLS: treat the link as unencrypted. This is a property of the Switcher cloud, not something the integration can change.
- The integration polls once a minute and keeps one idle TCP connection open with a heartbeat. The official app polls far more aggressively.

## Limitations and not supported

- **Breeze** (AC / IR blaster) is not supported and will not get entities.
- **Schedules and timers** configured in the app are not exposed. The run timer here is a one-shot "turn on for N minutes".
- No temperature readings. The cloud does not provide any.
- Mix panel runner lines and the colour/white channels of lights are the least certain parts of the transcription.
- Local control is out of scope. For LAN devices use the built-in `switcher_kis` integration.
- Polling interval is fixed at 60 seconds.

## Contributing

Issues and pull requests are welcome.

- Bug reports: attach the diagnostics download and, for Beta devices, a packet capture (see above).
- The protocol transcription lives in `custom_components/switcher_cloud/devices.py`. Each builder and parser is annotated with the app class and method it was taken from, so a reviewer can check it against the decompiled source.
- Run the unit tests with:

```bash
python -m pytest tests
```

They need only Python and `pycryptodome`, not Home Assistant.

## Disclaimer

This is an unofficial, community-made integration. It is not affiliated with, endorsed by or supported by Switcher. It was built by reverse-engineering the public Switcher Smart Home app. Use it at your own risk. Switcher may change its cloud at any time and break it.

## License

See [LICENSE](LICENSE).
