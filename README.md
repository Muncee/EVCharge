# EVCharge — Home Assistant Integrations

Two Home Assistant custom integrations for intelligently charging your EV on the [Octopus Energy Agile](https://octopus.energy/agile/) tariff, powered by [AgilePredict](https://agilepredict.com) 14-day price forecasts.

---

## EV Charge Optimizer (`ev_charge_optimizer`)

Uses the 14-day price forecast to dynamically recommend **when and how much** to charge, taking into account upcoming expensive periods across the full two-week window.

### Recommendations

| State | Meaning |
|---|---|
| `charge_now_fully` | Prices are cheap now and a long expensive period is coming — charge to target % now |
| `charge_daily` | Prices are consistently reasonable — charge what you need each day during cheapest windows |
| `wait` | Current price is high but a cheap window is coming soon — battery has enough range to wait |
| `charge_now_minimum` | Battery is low or prices won't improve — charge to minimum safety level now |

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

Thresholds are percentile-based on the 14-day forecast — they shift as market prices change.

### Installation via HACS

1. Add this repo as a custom HACS repository (Integration category)
2. Install **EV Charge Optimizer**
3. Restart Home Assistant
4. **Settings > Devices & Services > Add Integration** → search *EV Charge Optimizer*

### Manual Installation

Copy `custom_components/ev_charge_optimizer/` into your HA `config/custom_components/` directory and restart.

### Configuration

| Field | Description | Example |
|---|---|---|
| **Region** | Octopus DNO region code | `G` - North Western England |
| **Battery capacity (kWh)** | Total usable battery | `77` |
| **Daily usage (kWh)** | Average kWh used per day | `15` |
| **Charger power (kW)** | Wallbox/EVSE rating | `7.4` |
| **Minimum battery %** | Lowest acceptable charge | `20` |
| **Target charge %** | Charge ceiling | `80` |
| **Battery sensor** | HA entity reporting current % (optional) | `sensor.my_car_battery` |
| **Cheap percentile** | Prices below this = cheap | `30` |
| **Expensive percentile** | Prices above this = expensive | `70` |

### Entities

**Sensors:** recommendation, reason, current price, 14-day average, cheap/expensive thresholds, next cheap window, battery %, kWh needed, cost now, cost at cheapest, estimated savings, days of range, % expensive next 7 days, cheapest today, cheapest upcoming, longest expensive run ahead.

**Binary Sensors:** `charge_ev_now`, `cheap_electricity_period`, `expensive_electricity_period`

---

## EV Smart Charge (`ev_smart_charge`)

A nightly scheduling integration that finds the **cheapest consecutive window** before your departure time and can defer charging to a cheaper night if one is coming soon.

### Strategies

| Strategy | When used |
|---|---|
| `no_charge_needed` | Already at or above target SOC |
| `minimum_charge_now` | Battery at or below minimum — charges immediately |
| `charge_full_tonight` | Tonight is ≥20% cheaper than the next 7-night average |
| `wait_for_cheaper` | A night within 3 days is ≥10% cheaper and battery will last |
| `charge_tonight` | Default — charges during cheapest slots before departure |

### Installation via HACS

1. Add this repo as a custom HACS repository (Integration category)
2. Install **EV Smart Charge**
3. Restart Home Assistant
4. **Settings > Devices & Services > Add Integration** → search *EV Smart Charge*

### Manual Installation

Copy `custom_components/ev_smart_charge/` into your HA `config/custom_components/` directory and restart.

### Configuration

| Field | Description |
|---|---|
| **Region** | UK DNO region letter (A–P) |
| **Battery capacity (kWh)** | Total usable capacity |
| **Charger rate (kW)** | Maximum AC charge rate |
| **Target SOC %** | Charge level to aim for each night (default 80%) |
| **Minimum SOC %** | Emergency threshold — charge starts immediately (default 20%) |
| **Departure time** | HH:MM — charging finishes before this time |
| **SOC entity** | HA sensor reporting current battery % |
| **Charger switch** | HA switch entity to control charger (optional) |

### Entities

**Sensors:** charging strategy, charge window start/end, estimated charge cost, cheapest price tonight, 14-day lowest price, current Agile price, estimated daily usage.

**Binary Sensors:** `ev_smart_charge_charge_now` — ON during optimal charging window.

---

## Example Automation (works with either integration)

```yaml
- alias: "Start EV charging when recommended"
  trigger:
    - platform: state
      entity_id: binary_sensor.charge_ev_now   # or binary_sensor.ev_smart_charge_charge_now
      to: "on"
  action:
    - service: switch.turn_on
      target:
        entity_id: switch.ev_charger
```

---

## Region Codes

| Code | Region | Code | Region |
|---|---|---|---|
| A | Eastern England | H | Southern England |
| B | East Midlands | J | South Eastern England |
| C | London | K | Southern Wales |
| D | Merseyside and Northern Wales | L | South Western England |
| E | West Midlands | M | Yorkshire |
| F | North Eastern England | N | Southern Scotland |
| G | North Western England | P | Northern Scotland |

---

## Data Source

Forecasts from [AgilePredict](https://agilepredict.com) — ML model trained on BMRS, National Grid ESO, and weather data. Updated at **06:15, 10:15, 16:15, and 22:15** daily. No API key required.
