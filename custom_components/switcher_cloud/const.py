"""Constants for the Switcher Cloud cloud integration."""
DOMAIN = "switcher_cloud"
PLATFORMS = ["switch", "water_heater", "sensor", "cover"]

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"

DEFAULT_SCAN_INTERVAL = 60  # seconds; vendor app polls every 5s, we are gentler
