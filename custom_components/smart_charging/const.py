"""Constants for the Smart Charging custom integration."""

DOMAIN = "smart_charging"
NAME = "Smart Charging"
VERSION = "1.1.0"
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
CONF_SOC_ENTITY = "soc_entity"
CONF_CHARGER_OPERATION_MODE = "charger_operation_mode_entity"
CONF_CHARGER_RESUME_BUTTON = "charger_resume_button_entity"
CONF_CHARGER_STOP_BUTTON = "charger_stop_button_entity"
CONF_CHARGER_MODE_SENSOR = "charger_mode_sensor_entity"
CONF_DEADLINE_ENTITY = "deadline_entity"  # legacy — only its clock time is used
CONF_DEADLINE_TIME = "deadline_time"
CONF_DEADLINE_RESTART_MINUTES = "deadline_restart_minutes"

CONF_MIN_SOC = "min_soc"
CONF_MAX_SOC = "max_soc"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_DAILY_CONSUMPTION_PCT = "daily_consumption_pct"
CONF_CHARGER_MAX_KW = "charger_max_kw"
CONF_WEEKLY_FULL_CHARGE = "weekly_full_charge"
CONF_MIN_DAYS_BETWEEN_FULL = "min_days_between_full"
CONF_LAST_FULL_CHARGE = "last_full_charge"  # persisted ISO timestamp
CONF_CURRENCY = "currency"

# --- currency ---------------------------------------------------------------
# The price currency is a required user choice. Prices are never converted —
# they are assumed to already be in the configured currency.
CURRENCY_OPTIONS = ["EUR", "SEK", "NOK", "DKK", "GBP", "USD"]
DEFAULT_CURRENCY = "SEK"

# --- defaults ---------------------------------------------------------------
DEFAULT_MIN_SOC = 20.0
DEFAULT_MAX_SOC = 80.0
DEFAULT_BATTERY_CAPACITY_KWH = 77.0
DEFAULT_DAILY_CONSUMPTION_PCT = 15.0
DEFAULT_CHARGER_MAX_KW = 11.0
DEFAULT_WEEKLY_FULL_CHARGE = False
DEFAULT_MIN_DAYS_BETWEEN_FULL = 5.0
DEFAULT_UPDATE_INTERVAL_MINUTES = 15
# 0 = never restart charging after a passed deadline.
DEFAULT_DEADLINE_RESTART_MINUTES = 0.0

# --- charger_mode sensor states that mean "connected" -----------------------
# The zaptec sensor.*_charger_mode uses the native Zaptec values in lower
# case (Zaptec integration >= 0.8):
#   disconnected, connected_requesting, connected_charging,
#   connected_finished, unknown, ...
CHARGER_CONNECTED_STATES = {"connected_requesting", "connected_charging"}
