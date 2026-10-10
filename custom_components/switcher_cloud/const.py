"""Constants for the Switcher Cloud integration."""
from homeassistant.const import Platform

DOMAIN = "switcher_cloud"
PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.WATER_HEATER,
    Platform.SENSOR,
    Platform.COVER,
    Platform.LIGHT,
    Platform.NUMBER,
]

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"

DEFAULT_SCAN_INTERVAL = 60  # seconds; vendor app polls every 5 s, we are gentler
