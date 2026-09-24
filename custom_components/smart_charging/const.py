"""Constants for the Smart Charging custom integration."""

DOMAIN = "smart_charging"
NAME = "Smart Charging"
VERSION = "1.0.0"
PLATFORMS = ["sensor", "calendar", "select"]

# --- modes ------------------------------------------------------------------
MODE_OFF = "off"
MODE_PLAN = "plan"
MODE_LIVE = "live"
MODE_OPTIONS = [MODE_OFF, MODE_PLAN, MODE_LIVE]

MODE_LABELS = {
    MODE_OFF: "Av",
    MODE_PLAN: "Planläge (test)",
    MODE_LIVE: "Live",
}

# --- config keys ------------------------------------------------------------
CONF_SPOT_PRICES_ENTITY = "spot_prices_entity"
CONF_CHARGER_OPERATION_MODE = "charger_operation_mode_entity"
CONF_CHARGER_RESUME_BUTTON = "charger_resume_button_entity"
CONF_CHARGER_STOP_BUTTON = "charger_stop_button_entity"
CONF_CHARGER_MODE_SENSOR = "charger_mode_sensor_entity"
CONF_DEADLINE_ENTITY = "deadline_entity"

CONF_THRESHOLD_START = "threshold_start"
CONF_THRESHOLD_STOP = "threshold_stop"
CONF_CHARGER_MAX_KW = "charger_max_kw"
CONF_BATTERY_NEED_KWH = "battery_need_kwh"
CONF_CURRENCY = "currency"

# --- currency ---------------------------------------------------------------
# The price currency is a *required* user choice; EUR is only a safety-net
# default (e.g. legacy config entries that predate the setting). Prices are
# never converted — they are assumed to already be in the configured currency.
CURRENCY_OPTIONS = ["EUR", "SEK", "NOK", "DKK", "GBP", "USD"]
DEFAULT_CURRENCY = "EUR"

# --- defaults ---------------------------------------------------------------
DEFAULT_THRESHOLD_START = 0.80
DEFAULT_THRESHOLD_STOP = 0.95
DEFAULT_CHARGER_MAX_KW = 11.0
DEFAULT_BATTERY_NEED_KWH = None
DEFAULT_UPDATE_INTERVAL_MINUTES = 15
DEFAULT_THROTTLE_SECONDS = 60

# --- charger_mode sensor states that mean "connected" -----------------------
# The zaptec sensor.*_charger_mode uses the native Zaptec values in lower
# case (Zaptec integration >= 0.8):
#   disconnected, connected_requesting, connected_charging,
#   connected_finished, unknown, ...
CHARGER_CONNECTED_STATES = {"connected_requesting", "connected_charging"}
