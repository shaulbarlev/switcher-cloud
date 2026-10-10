# Switcher Cloud — Home Assistant custom integration

Unofficial cloud control for **Switcher devices that only work through the
vendor cloud** — e.g. the "On Wall" boiler (type `0x0321`, S216), which
aioswitcher / switcher_kis do not support (they cover local-UDP devices only).

## Features
- Power **switch** (on/off) per device
- **Water heater** entity (operation mode on/off)
- **Sensor**: seconds-on counter (resets when the boiler turns off)

## Setup (HACS)
1. Add this repository to HACS as a custom repository (category: Integration).
2. Install "Switcher Cloud".
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → **Switcher Cloud**.
5. Enter the email + password you use in the Switcher Smart Home app.

## Notes
- Talks to `appserver.switcher.co.il:9091` (Switcher IL region cloud) over raw TCP.
- Requires `pycryptodome`.
