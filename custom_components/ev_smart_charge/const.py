"""Constants for the EV Smart Charge integration."""

DOMAIN = "ev_smart_charge"
PLATFORMS = ["sensor", "binary_sensor"]

# Config entry keys
CONF_REGION = "region"
CONF_BATTERY_CAPACITY = "battery_capacity"
CONF_CHARGE_RATE = "charge_rate"
CONF_TARGET_SOC = "target_soc"
CONF_MIN_SOC = "min_soc"
CONF_DEPARTURE_TIME = "departure_time"
CONF_SOC_ENTITY = "soc_entity"
CONF_CHARGER_SWITCH = "charger_switch"
CONF_SCAN_INTERVAL = "scan_interval"

# Agile UK DNO region codes
REGION_CODES = {
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

# AgilePredict API
AGILE_PREDICT_API_BASE = "https://agilepredict.com/api"
API_TIMEOUT = 30
DEFAULT_FORECAST_DAYS = 14

# Defaults
DEFAULT_TARGET_SOC = 80
DEFAULT_MIN_SOC = 20
DEFAULT_DEPARTURE_TIME = "07:30"
DEFAULT_SCAN_INTERVAL = 3600  # 1 hour in seconds

# Strategy types
STRATEGY_CHARGE_TONIGHT = "charge_tonight"
STRATEGY_CHARGE_FULL_TONIGHT = "charge_full_tonight"
STRATEGY_WAIT_FOR_CHEAPER = "wait_for_cheaper"
STRATEGY_MINIMUM_CHARGE = "minimum_charge_now"
STRATEGY_NO_CHARGE_NEEDED = "no_charge_needed"

STRATEGY_DESCRIPTIONS = {
    STRATEGY_CHARGE_TONIGHT: "Charge tonight during optimal low-price window",
    STRATEGY_CHARGE_FULL_TONIGHT: "Charge to full tonight — upcoming prices are higher",
    STRATEGY_WAIT_FOR_CHEAPER: "Wait — cheaper prices forecast in the next few days",
    STRATEGY_MINIMUM_CHARGE: "Charging minimum now (low battery) — optimal window follows",
    STRATEGY_NO_CHARGE_NEEDED: "No charging needed — target SOC already met",
}

# Thresholds for strategy decisions
WAIT_IF_TOMORROW_CHEAPER_BY = 0.10   # Wait if tomorrow is 10%+ cheaper than tonight
CHARGE_FULL_IF_TONIGHT_CHEAPER_BY = 0.20  # Charge full if tonight is 20%+ cheaper than next 7-day avg
TONIGHT_VS_FUTURE_TOLERANCE = 0.10  # Tonight is "good enough" if within 10% of cheapest upcoming
EXPENSIVE_PERIOD_DAYS = 7            # Days to look ahead when deciding to charge full

# Sensor/entity names
SENSOR_CHARGE_START = "charge_start"
SENSOR_CHARGE_END = "charge_end"
SENSOR_STRATEGY = "strategy"
SENSOR_CHEAPEST_TONIGHT = "cheapest_price_tonight"
SENSOR_ESTIMATED_COST = "estimated_charge_cost"
SENSOR_FORECAST_LOW = "forecast_lowest_price"
SENSOR_FORECAST_LOW_TIME = "forecast_lowest_price_time"
SENSOR_CURRENT_PRICE = "current_price"
SENSOR_DAILY_USAGE = "estimated_daily_usage"
BINARY_SENSOR_CHARGE_NOW = "charge_now"
