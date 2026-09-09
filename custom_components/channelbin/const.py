"""Constants for the ChannelBin integration."""

DOMAIN = "channelbin"

DEFAULT_PORT = 5000
DEFAULT_SCHEME = "http"

# homeassistant.const has no CONF_SCHEME - "scheme" isn't a standard HA config key -
# so it's defined here rather than borrowed from a constant that doesn't exist.
CONF_SCHEME = "scheme"

# The coordinator polls /api/ha/v1/status every ~45s.
SCAN_INTERVAL_SECONDS = 45

STATUS_PATH = "/api/ha/v1/status"
