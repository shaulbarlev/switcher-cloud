# Switcher Cloud — Home Assistant custom integration

Unofficial cloud control for **Switcher devices that only work through the
vendor cloud** — e.g. the "On Wall" boiler (type `0x0321`, S216), which
aioswitcher / switcher_kis do not support (they cover local-UDP devices only).

## Features
- Power **switch** for every switchable Switcher cloud device (per-gang for wall switches)
- **Water heater** entity for boiler-type devices (On Wall, Bath heater)
- **Run timer** (number, minutes) for boilers — the device turns itself off after N minutes, even if HA is down
- **Cover** entity for Runner shutters (open / close / stop / position)
- **Light** entity (on/off, brightness, RGB, colour temperature, effects) — beta
- **Sensors**: current (A), energy (kWh), time-on (s) where the device reports them
- Live updates: a persistent connection receives the cloud's push notifications
- Re-authentication flow when the password changes
- Diagnostics dump (Settings → Devices → ⋮ → Download diagnostics) for issue reports

## Device support

| Switcher product | DeviceType | Status |
|---|---|---|
| On Wall boiler (S216) | `0321` | ✅ **Verified** on real hardware (incl. run timer) |
| Bath heater / Touch boiler | `031F` | 🟡 Beta — same command as On Wall, unverified |
| Power plugs / Smart plug | `030B` `030F` `0317` `01A1` `01A7` `01A8` | 🟡 Beta — unverified |
| Wall switches (multi-gang) | other `01xx` / `03xx` | 🟡 Beta — unverified, one switch per gang |
| Runner shutters / curtains | `0Cxx` | 🟡 Beta — unverified, position reported |
| Mix panels (plug/light lines + runner lines) | `0Fxx` | 🟡 Beta — unverified, one entity per line |
| Lights | `08xx` | 🟡 Beta — on/off high-confidence; colour/CT unverified (Switcher's app ships no colour UI) |
| Breeze (AC / IR) | `0Exx` | ❌ Not supported |

**Beta means:** the command bytes were transcribed from the official app's
decompiled code and are byte-exact to what it sends, but nobody has run them
against the real device. They are the same commands the app uses, so the worst
case is "nothing happens / an error toast", not damage. If you own one of these,
please open an issue with your diagnostics dump — a packet capture of the
official app controlling the device (PCAPdroid, no root needed) lets it be
promoted to Verified.

## Setup (HACS)
1. Add this repository to HACS as a custom repository (category: Integration).
2. Install "Switcher Cloud".
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → **Switcher Cloud**.
5. Enter the email + password you use in the Switcher Smart Home app.

## Notes
- Talks to `appserver.switcher.co.il:9091` (Switcher IL region cloud) over raw TCP.
- Requires `pycryptodome`.
