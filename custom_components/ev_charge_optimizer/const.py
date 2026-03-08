"""Constants for EV Charge Optimizer."""

DOMAIN = "ev_charge_optimizer"

# Config entry keys
CONF_REGION = "region"
CONF_BATTERY_CAPACITY = "battery_capacity_kwh"
CONF_DAILY_USAGE = "daily_usage_kwh"
CONF_CHARGE_RATE = "charge_rate_kw"
CONF_MIN_CHARGE_PCT = "min_charge_pct"
CONF_TARGET_CHARGE_PCT = "target_charge_pct"
CONF_BATTERY_ENTITY = "battery_entity"
CONF_CHEAP_PERCENTILE = "cheap_percentile"
CONF_EXPENSIVE_PERCENTILE = "expensive_percentile"

# API
API_BASE_URL = "https://agilepredict.com/api"
API_DAYS = 14
# AgilePredict updates 4x/day at 06:15, 10:15, 16:15, 22:15
UPDATE_INTERVAL_MINUTES = 60

# Recommendation states
REC_CHARGE_NOW_FULLY = "charge_now_fully"
REC_CHARGE_NOW_MINIMUM = "charge_now_minimum"
REC_CHARGE_DAILY = "charge_daily"
REC_WAIT = "wait"
REC_UNKNOWN = "unknown"

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

# Data coordinator keys (what sensors read)
DATA_RECOMMENDATION = "recommendation"
DATA_REASON = "reason"
DATA_CURRENT_PRICE = "current_price"
DATA_AVG_PRICE = "avg_price"
DATA_CHEAP_THRESHOLD = "cheap_threshold"
DATA_EXPENSIVE_THRESHOLD = "expensive_threshold"
DATA_IS_CHEAP_NOW = "is_cheap_now"
DATA_IS_EXPENSIVE_NOW = "is_expensive_now"
DATA_NEXT_CHEAP_WINDOW = "next_cheap_window"
DATA_NEXT_CHEAP_PRICE = "next_cheap_price"
DATA_HOURS_UNTIL_CHEAP = "hours_until_cheap"
DATA_CURRENT_BATTERY_PCT = "current_battery_pct"
DATA_KWH_NEEDED = "kwh_needed"
DATA_HOURS_TO_CHARGE = "hours_to_charge"
DATA_COST_NOW = "cost_to_charge_now"
DATA_COST_CHEAPEST = "cost_at_cheapest_today"
DATA_ESTIMATED_SAVINGS = "estimated_savings"
DATA_DAYS_OF_RANGE = "days_of_battery_range"
DATA_PCT_EXPENSIVE_7DAYS = "pct_expensive_next_7days"
DATA_CHEAPEST_TODAY_PRICE = "cheapest_today_price"
DATA_CHEAPEST_TODAY_START = "cheapest_today_start"
DATA_CHEAPEST_UPCOMING_PRICE = "cheapest_upcoming_price"
DATA_CHEAPEST_UPCOMING_START = "cheapest_upcoming_start"
DATA_PRICES = "prices"
DATA_LONG_EXPENSIVE_DAYS = "long_expensive_days"
DATA_CHARGE_NOW_BINARY = "charge_now"

# Multi-day charge planning
DATA_NEXT_CHARGE_DATETIME = "next_charge_datetime"
DATA_NEXT_CHARGE_PRICE = "next_charge_price"
DATA_NEXT_CHARGE_TARGET_PCT = "next_charge_target_pct"
DATA_SECOND_CHARGE_DATETIME = "second_charge_datetime"
