# EV Charge Optimizer for Home Assistant

A custom Home Assistant integration that uses the [AgilePredict](https://agilepredict.com) API to give you intelligent, forward-looking recommendations on **when to charge your EV** on an Octopus Agile tariff.

## How It Works

AgilePredict forecasts Octopus Agile electricity prices **up to 14 days ahead** (updated 4x per day). This integration fetches that forecast and computes whether you should:

| Recommendation | Meaning |
|---|---|
| `charge_now_fully` | Prices are cheap **and** a long expensive period is coming — charge to your target % now |
| `charge_daily` | Prices are consistently low — just charge what you need each day during the cheapest window |
| `wait` | Current price is high but a cheap window is coming within a few hours — your battery has enough range to wait |
| `charge_now_minimum` | Battery is low or prices won't improve soon — charge to the minimum safety level now |

### Decision Logic

```
1. Battery at or below minimum?          →  charge_now_minimum  (always)
2. Cheap now + long expensive run ahead? →  charge_now_fully
3. Cheap now + future prices reasonable? →  charge_daily
4. Not cheap + cheap window within 8h + battery has range?  →  wait
5. Not cheap + cheap window within 24h + battery has range? →  wait
6. Expensive / battery running low?      →  charge_now_minimum
7. Moderate + expensive period coming?   →  charge_now_minimum
8. Default                               →  charge_daily
```

Thresholds are based on **percentiles of the 14-day forecast**, so they adapt as the market changes. If the next two weeks are uniformly expensive, the bands shift accordingly.

## Installation

### HACS (recommended)

1. Add this repo as a custom HACS repository (Integration type)
2. Install **EV Charge Optimizer**
3. Restart Home Assistant
4. Go to **Settings > Devices & Services > Add Integration** and search for *EV Charge Optimizer*

### Manual

1. Copy `custom_components/ev_charge_optimizer/` into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Add via **Settings > Devices & Services**

## Configuration

During setup you will be asked for:

| Field | Description | Example |
|---|---|---|
| **Region** | Your Octopus DNO region code | `G` - North Western England |
| **Battery capacity (kWh)** | Total usable battery | `77` |
| **Daily usage (kWh)** | Average kWh used per day | `15` |
| **Charger power (kW)** | Your wallbox/EVSE rating | `7.4` |
| **Minimum battery %** | Lowest acceptable charge | `20` |
| **Target charge %** | Charge ceiling (80% for longevity) | `80` |
| **Battery sensor** | HA entity reporting current % (optional) | `sensor.my_car_battery` |
| **Cheap percentile** | Prices below this percentile = "cheap" | `30` |
| **Expensive percentile** | Prices above this percentile = "expensive" | `70` |

### Region Codes

| Code | Region | Code | Region |
|---|---|---|---|
| A | Eastern England | H | Southern England |
| B | East Midlands | J | South Eastern England |
| C | London | K | Southern Wales |
| D | Merseyside and Northern Wales | L | South Western England |
| E | West Midlands | M | Yorkshire |
| F | North Eastern England | N | Southern Scotland |
| G | North Western England | P | Northern Scotland |

## Entities Created

### Sensors

| Entity | Description | Unit |
|---|---|---|
| `sensor.charging_recommendation` | Current strategy | text |
| `sensor.recommendation_reason` | Human-readable explanation | text |
| `sensor.current_electricity_price` | Price right now | p/kWh |
| `sensor.14_day_average_price` | Mean price over forecast | p/kWh |
| `sensor.cheap_price_threshold` | Upper bound of cheap band | p/kWh |
| `sensor.expensive_price_threshold` | Lower bound of expensive band | p/kWh |
| `sensor.next_cheap_window` | Timestamp of next cheap slot | datetime |
| `sensor.next_cheap_window_price` | Price at next cheap slot | p/kWh |
| `sensor.hours_until_cheap_window` | Hours until cheap electricity | h |
| `sensor.ev_battery_level` | Battery % | % |
| `sensor.energy_needed_to_charge` | kWh to reach target % | kWh |
| `sensor.hours_to_charge` | How long charging will take | h |
| `sensor.cost_to_charge_now` | Cost of charging right now | £ |
| `sensor.cost_at_cheapest_today` | Cost at today's cheapest slot | £ |
| `sensor.estimated_savings` | Savings from waiting | £ |
| `sensor.days_of_battery_range` | Days before hitting minimum | days |
| `sensor.expensive_period_next_7_days` | % of next 7 days that are expensive | % |
| `sensor.cheapest_price_today` | Lowest forecast price today | p/kWh |
| `sensor.cheapest_window_today` | When today's cheapest slot starts | datetime |
| `sensor.cheapest_upcoming_price` | Lowest price in 14-day window | p/kWh |
| `sensor.cheapest_upcoming_window` | When that slot starts | datetime |
| `sensor.longest_upcoming_expensive_period` | Longest run of expensive days ahead | days |

### Binary Sensors

| Entity | On when... |
|---|---|
| `binary_sensor.charge_ev_now` | Recommendation is to charge now |
| `binary_sensor.cheap_electricity_period` | Current slot is below cheap threshold |
| `binary_sensor.expensive_electricity_period` | Current slot is above expensive threshold |

## Example Automations

### Start/stop charger based on recommendation

```yaml
automation:
  - alias: "Start EV charging when recommended"
    trigger:
      - platform: state
        entity_id: binary_sensor.charge_ev_now
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.ev_charger

  - alias: "Stop EV charging when not recommended"
    trigger:
      - platform: state
        entity_id: binary_sensor.charge_ev_now
        to: "off"
    condition:
      - condition: state
        entity_id: sensor.charging_recommendation
        state: "wait"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.ev_charger
```

### Notification when recommendation changes

```yaml
automation:
  - alias: "EV charge recommendation changed"
    trigger:
      - platform: state
        entity_id: sensor.charging_recommendation
    action:
      - service: notify.mobile_app
        data:
          title: "EV Charging Update"
          message: >
            {{ states('sensor.charging_recommendation') | replace('_', ' ') | title }}:
            {{ state_attr('sensor.charging_recommendation', 'reason') }}
```

## Data Source

Forecasts provided by [AgilePredict](https://agilepredict.com) — a machine-learning model trained on BMRS, National Grid ESO, and weather data. Updated at **06:15, 10:15, 16:15, and 22:15** daily.

API: `https://agilepredict.com/api/{REGION}?days=14&high_low=True`
