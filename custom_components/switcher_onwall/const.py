"""Constants for the Switcher On Wall cloud integration."""
DOMAIN = "switcher_onwall"
PLATFORMS = ["switch", "water_heater", "sensor"]

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"

DEFAULT_SCAN_INTERVAL = 60  # seconds; vendor app polls every 5s, we are gentler
