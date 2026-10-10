# Switcher On Wall (cloud) — Home Assistant custom integration

Unofficial cloud control for the **Switcher "On Wall" water heater/boiler**
(device type `0x0321`, model S216) via the vendor cloud that the official
Switcher app uses. Upstream integrations (aioswitcher / switcher_kis) only
support local-UDP devices and do not support this model — it is cloud-only.

## Features
- Power **switch** (on/off) per device
- **Water heater** entity (operation mode on/off)
- **Sensor**: seconds-on counter (resets when the boiler turns off)

## Setup (HACS)
1. Add this repository to HACS as a custom repository (category: Integration).
2. Install "Switcher On Wall (cloud)".
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → **Switcher On Wall (cloud)**.
5. Enter the email + password you use in the Switcher Smart Home app.

## Notes
- Talks to `appserver.switcher.co.il:9091` (Switcher IL region cloud) over raw TCP.
- Requires `pycryptodome`.
