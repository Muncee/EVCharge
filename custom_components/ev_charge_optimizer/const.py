"""Constants for EV Charge Optimizer."""

DOMAIN = "ev_charge_optimizer"

# Config entry keys
CONF_REGION = "region"
CONF_BATTERY_CAPACITY = "battery_capacity_kwh"
CONF_DAILY_USAGE = "daily_usage_kwh"
CONF_CHARGE_RATE = "charge_rate_kw"
CONF_TARGET_CHARGE_PCT = "target_charge_pct"
CONF_BATTERY_ENTITY = "battery_entity"

# API
API_BASE_URL = "https://agilepredict.com/api"
API_DAYS = 14
UPDATE_INTERVAL_MINUTES = 60

# Octopus Agile DNO regions
OCTOPUS_REGIONS = {
    "A": "Eastern England",
    "B": "East Midlands",
    "C": "London",
    "D": "Merseyside and Northern Wales",
    "E": "West Midlands",
    "F": "North Eastern England",
    "G": "North Western England",
    "H": "Southern England",
    "J": "South Eastern England",
    "K": "Southern Wales",
    "L": "South Western England",
    "M": "Yorkshire",
    "N": "Southern Scotland",
    "P": "Northern Scotland",
}

# Coordinator data keys
DATA_CURRENT_PRICE = "current_price"
DATA_SCHEDULE_SLOTS = "schedule_slots"    # list of {datetime, price, predicted} — individual ON slots
DATA_SCHEDULE_SESSIONS = "schedule_sessions"  # grouped for display
DATA_SCHEDULE_ACTIVE = "schedule_active"  # bool — current slot is a scheduled charge slot
DATA_NEXT_START = "next_start"            # datetime: start of next charge slot
DATA_NEXT_END = "next_end"               # datetime: end of next charge slot
DATA_SUMMARY = "summary"                 # short human-readable text

# HA event fired daily after 16:30 local time when next-day prices arrive
EVENT_PRICES_UPDATED = "ev_charge_optimizer_prices_updated"
