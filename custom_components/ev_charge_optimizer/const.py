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
DATA_CURRENT_PRICE = "current_price"         # float p/kWh
DATA_AVG_14DAY = "avg_14day_price"           # float — 14-day average for context
DATA_SCHEDULE_SLOTS = "schedule_slots"       # list of {datetime, price, predicted} — individual ON slots
DATA_SCHEDULE_SESSIONS = "schedule_sessions" # consecutive slot groups (for binary sensor attrs)
DATA_WEEKLY_PLAN = "weekly_plan"             # list of 7 day-plan dicts (the main schedule)
DATA_SCHEDULE_ACTIVE = "schedule_active"     # bool — current slot is a scheduled charge slot
DATA_NEXT_START = "next_start"               # datetime: start of next charge slot
DATA_NEXT_END = "next_end"                   # datetime: end of next charge slot
DATA_SUMMARY = "summary"                     # one-line summary string
DATA_NOTIFICATION_TEXT = "notification_text" # multi-line schedule for notifications
DATA_WEEKLY_COST_GBP = "weekly_cost_gbp"     # total estimated charge cost this week
DATA_WEEKLY_KWH = "weekly_kwh"               # total estimated kWh this week

# HA event fired daily after 16:30 local time when next-day prices arrive
EVENT_PRICES_UPDATED = "ev_charge_optimizer_prices_updated"
