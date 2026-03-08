# EV Smart Charge — Home Assistant Integration

An intelligent Home Assistant custom integration that uses [AgilePredict](https://agilepredict.com) 14-day ML price forecasts to decide the **optimal time and strategy** for charging your EV on the [Octopus Energy Agile](https://octopus.energy/agile/) tariff.

---

## Features

- **Live 14-day price forecasts** from the AgilePredict API (updated 4× daily)
- **Intelligent multi-night strategy engine:**
  - Charge tonight during the cheapest half-hour slots
  - Charge to 100% tonight if upcoming days are significantly more expensive
  - Wait and skip tonight if tomorrow (or soon) is meaningfully cheaper
  - Force a minimum charge immediately if battery is critically low
- **Historical SOC learning** — tracks your battery drain patterns to know whether you can safely wait for a cheaper night
- **9 sensor entities** and 1 binary sensor for automations
- Full **UI config flow** — set up through Home Assistant Settings → Devices & Services
- **Options flow** — adjust targets, departure time, etc. at any time without reinstalling

---

## Installation

### HACS (recommended)

1. Add this repository to HACS as a custom repository (Integration category)
2. Install **EV Smart Charge**
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/ev_smart_charge/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for **EV Smart Charge**.

| Field | Description |
|---|---|
| **Region** | Your UK DNO region letter (A–P). Find it on your electricity bill (MPAN digits 9–10) or the [Octopus region map](https://octopus.energy/agile/). |
| **Battery capacity (kWh)** | Total usable capacity of your EV battery |
| **Charger rate (kW)** | Maximum AC charge rate of your home charger |
| **Target SOC %** | The state of charge to aim for each night (default 80%) |
| **Minimum SOC %** | If battery drops here, charge starts immediately regardless of price (default 20%) |
| **Departure time** | HH:MM — integration ensures charging finishes before this time |
| **SOC entity** | HA sensor entity reporting current battery % (e.g. `sensor.my_car_battery_level`) |
| **Charger switch** | HA switch entity to control your charger (e.g. `switch.ev_charger`) — optional |

---

## Entities Created

### Sensors

| Entity | Description |
|---|---|
| `sensor.ev_smart_charge_charging_strategy` | Current strategy recommendation |
| `sensor.ev_smart_charge_charge_window_start` | Start of optimal charging window (timestamp) |
| `sensor.ev_smart_charge_charge_window_end` | End of optimal charging window (timestamp) |
| `sensor.ev_smart_charge_estimated_charge_cost` | Estimated cost (£) for the recommended session |
| `sensor.ev_smart_charge_cheapest_price_tonight` | Cheapest single slot tonight (p/kWh) |
| `sensor.ev_smart_charge_forecast_lowest_price_14_days` | Lowest predicted price in next 14 days (p/kWh) |
| `sensor.ev_smart_charge_forecast_lowest_price_time` | When that lowest price occurs |
| `sensor.ev_smart_charge_current_agile_price` | Live current half-hour price (p/kWh) |
| `sensor.ev_smart_charge_estimated_daily_usage` | Estimated daily kWh usage from SOC history |

### Binary Sensors

| Entity | Description |
|---|---|
| `binary_sensor.ev_smart_charge_charge_now` | **ON** during the optimal charging window — use this to trigger automations |

---

## Strategy Logic

The integration decides between four strategies each update cycle:

### 1. `no_charge_needed`
Battery is already at or above the target SOC. Nothing to do.

### 2. `minimum_charge_now`
Battery SOC has dropped to or below the minimum threshold. Charge starts immediately (to protect the battery), but the integration still picks the cheapest available slots going forward.

### 3. `charge_full_tonight`
Tonight's cheapest window is **≥20% cheaper** than the average of the next 7 nights. Rather than charging only to target, the integration recommends charging to **100%** to bank cheap energy and avoid having to charge during expensive upcoming periods.

### 4. `wait_for_cheaper`
An upcoming night (within the next 3 days) has prices **≥10% cheaper** than tonight, AND the current charge is estimated to last that long based on your historical driving pattern. The integration skips tonight and targets the future cheap window.

### 5. `charge_tonight`
Default — tonight's window is reasonably priced and the right choice. Schedule charging for the cheapest half-hour slots before departure time.

---

## Example Automations

### Automatically start/stop charger

```yaml
# automation.yaml

- alias: "EV — Start charging in optimal window"
  trigger:
    - platform: state
      entity_id: binary_sensor.ev_smart_charge_charge_now
      to: "on"
  action:
    - service: switch.turn_on
      target:
        entity_id: switch.ev_charger

- alias: "EV — Stop charging after optimal window"
  trigger:
    - platform: state
      entity_id: binary_sensor.ev_smart_charge_charge_now
      to: "off"
  action:
    - service: switch.turn_off
      target:
        entity_id: switch.ev_charger
```

### Send a strategy notification each evening

```yaml
- alias: "EV — Evening charge briefing"
  trigger:
    - platform: time
      at: "18:00:00"
  action:
    - service: notify.mobile_app_your_phone
      data:
        title: "EV Charging Tonight"
        message: >
          Strategy: {{ states('sensor.ev_smart_charge_charging_strategy') }}.
          Window: {{ state_attr('sensor.ev_smart_charge_charging_strategy', 'window_start') | as_timestamp | timestamp_custom('%H:%M') }}
          to {{ state_attr('sensor.ev_smart_charge_charging_strategy', 'window_end') | as_timestamp | timestamp_custom('%H:%M') }}.
          Est. cost: £{{ state_attr('sensor.ev_smart_charge_charging_strategy', 'estimated_cost_gbp') }}.
```

---

## How the Algorithm Works

```
Every update (default: hourly):
│
├─ Fetch 14-day half-hourly price forecast from AgilePredict API
├─ Read current SOC from HA entity
├─ Record SOC to rolling history (used to estimate daily usage)
│
└─ Strategy decision:
   │
   ├─ SOC ≥ target?                  → no_charge_needed
   ├─ SOC ≤ minimum?                 → minimum_charge_now
   │
   ├─ Find tonight's cheapest N slots (N = kWh needed ÷ 0.5h × kW)
   ├─ Find each future night's cheapest N slots
   │
   ├─ Can we safely wait? (daily usage history × days until cheaper night)
   │   └─ Yes + future night ≥10% cheaper within 3 days  → wait_for_cheaper
   │
   ├─ Tonight ≥20% cheaper than next-7-day average?      → charge_full_tonight
   │
   └─ Default                                            → charge_tonight
```

---

## Notes

- AgilePredict is a community project — forecasts are best-effort ML predictions, not guaranteed prices. Always verify with [Octopus Energy](https://octopus.energy) for billing purposes.
- The integration uses no API key — the AgilePredict API is free and open.
- Price data is in **pence per kWh** inclusive of VAT (matching the Octopus Agile tariff display).
- SOC history is held in memory and lost on HA restart. After a restart, the daily usage estimate will return to unknown until enough readings are collected (~4 entries, typically a few hours).

---

## Region Codes

| Code | Region |
|---|---|
| A | Eastern England |
| B | East Midlands |
| C | London |
| D | Merseyside and Northern Wales |
| E | West Midlands |
| F | North Eastern England |
| G | North Western England |
| H | Southern England |
| J | South Eastern England |
| K | Southern Wales |
| L | South Western England |
| M | Yorkshire |
| N | Southern Scotland |
| P | Northern Scotland |
