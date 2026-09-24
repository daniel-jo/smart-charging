# Smart Charging — plan-based EV charging for Home Assistant

> ⚠️ This integration is the **brain** that decides *when* and *how much* to
> charge your EV based on spot prices and the car's battery state. It
> **never** writes the load current (`available_current`) — that is the sole
> domain of your load-balancing automation/blueprint (e.g. the svenakela
> charger-balancing blueprint).

Plan when and how much to charge your EV from hourly spot prices (from your
**spot price forecast** sensor) and the car's battery state. **No manual
price thresholds are needed** — the integration derives "cheap" from the
forecast itself and charges only up to the battery limits you set.

## How it works

- **Battery floor (`min_soc`)** — the plan never lets the *projected* charge
  level drop below this. If cheap hours aren't coming in time, charging is
  forced at whatever price is needed to protect the floor (you arrive at 25 %
  with a 20 % floor and a long trip coming up → it buys the cheapest hours it
  can afford).
- **Charge target (`max_soc`)** — normal upper limit. Regular charging stops
  here, even mid-window: no overcharging to battery-unhealthy levels.
- **Daily consumption (`%`/day)** — your average battery use (e.g. the daily
  commute). Together with the floor, it decides *when* charging is actually
  needed, so you buy as few kWh as possible.
- **Weekly 100 % boost** (optional) — tick the box and the integration
  schedules the **cheapest window in the whole forecast** (up to 14 days) to
  charge the battery to 100 %. It usually lands on the weekend's cheapest
  night; if a mid-week day is cheaper, it "seizes the opportunity" instead.
  A cooldown (default **5 days**) is enforced since the last 100 % charge —
  better to charge to 100 % less often than too often. The cooldown restarts
  automatically when the car reports ~100 %.
- **Derived thresholds** — "cheap hours" are the bottom ~25 % of the forecast
  (capped at the mean). The prices actually paid are exposed as
  `threshold_start` / `threshold_stop` on the plan sensor.

## Repository layout

```
custom_components/smart_charging/   the Home Assistant integration
tests/test_helper.py                unit tests (pytest)
dryrun.py                           standalone plan check (pure stdlib)
hacs.json                           HACS metadata
```

## Installation via HACS

1. Install **HACS** if you do not have it yet.
2. **HACS → ⋯ (top right) → Custom repositories** → add
   `https://github.com/daniel-jo/smart-charging` → category **Integration** →
   **Add**.
3. **HACS → Integrations → Smart Charging → Download** (use *Redownload* when
   updating) → **Restart Home Assistant**.
4. **Settings → Devices & Services → Add Integration → “Smart Charging”**.
5. Follow the two-step setup:
   1. **Entities** — pick the **Spot price forecast sensor**
      (`sensor.spot_price_<AREA>_forecast`), your **car battery SOC sensor**
      (0–100 %), the charger's operation mode switch, resume/stop buttons
      and the charger mode sensor.
   2. **Parameters** — set the battery limits and preferences below.
6. The integration starts in **Planläge (test)** mode — see below.

> ⚠️ v2.0 is a clean break: if you upgrade from 1.x, delete the old entry and
> add it again (only the entity IDs are preserved by the contract).

### Parameters

| Field | Meaning |
|---|---|
| Minimum battery level (%) | Floor — never plan below this |
| Maximum battery level (%) | Normal charging target (e.g. 80) |
| Battery capacity (kWh) | Needed to convert kW→% (e.g. 77) |
| Daily battery use (%/day) | Average daily consumption (e.g. 15) |
| Max charger power (kW) | e.g. 11 |
| Charge to 100 % weekly | Enable the cheapest-window boost |
| Min. days between 100 % charges | Cooldown, default 5 |
| Price currency | Labels the prices; nothing is converted |
| Ready by (time, optional) | Daily deadline — charging is complete *before* this, minute precise |
| Restart after deadline (min, 0 = never) | After the deadline passes, wait this long before charging may start again |

### Deadline (optional)

- **"Ready by"** is a clock time (HA's local timezone), **not a date** — it
  recurs every day and only the time matters.
- While the deadline is in the future, the plan schedules charging so it is
  **complete before** that time — **minute precise**: the hour that contains
  the deadline is cut short, so a 05:45 deadline ends charging at 05:45 (not
  06:00).
- Once the deadline passes, charging stops (`"Deadline passerad"`). It may
  start again only after **"Restart after deadline"** minutes (e.g. `60` → a
  05:45 deadline re-arms at 06:45) — and then only if the plan wants it,
  targeting the *next* day's deadline. With `0`/empty (default) charging does
  **not** restart that day; the next day's pre-deadline window is planned
  normally.
- Leaving the time empty disables the deadline entirely.
- Backwards compatibility: an old deadline `input_datetime` from the previous
  UI is still honoured for its clock time (the date is ignored). The picker has
  moved from the *entities* step into *parameters*.

## Modes

| Mode | State | Plan calc | Zaptec calls | Use case |
|---|---|---|---|---|
| **Av** | `sensor.smart_charging_plan` = `"Av"` | No | Never | Manual/off |
| **Planläge (test)** | Plan + logbook | Yes | **No** | Verify decisions risk-free |
| **Live** | Plan + logbook | Yes | Yes | Enable charging |

Switch modes via `select.smart_charging_mode` or the HA Services → Developer
Tools.

## Entities

| Entity ID | Type | Purpose |
|---|---|---|
| `sensor.smart_charging_plan` | sensor | Human-readable summary + attributes below |
| `sensor.smart_charging_decision` | sensor | `resume`/`stop`/`none` + `reason` |
| `select.smart_charging_mode` | select | Av / Planläge (test) / Live |
| `calendar.smart_charging_plan` | calendar | Every planned session as a calendar event |

### `sensor.smart_charging_plan` attributes

```json
{
  "planned_sessions": [{"start": "...", "end": "...", "power_kw": 11.0,
                          "avg_price_kwh": 0.567, "is_boost": false, "hours": [...]}],
  "next_action": {"action": "resume", "at": "...", "reason": "cheap_window"},
  "mode": "plan",
  "currency": "SEK",
  "soc_now": 60.0,
  "min_soc": 20.0,
  "max_soc": 80.0,
  "daily_consumption_pct": 15.0,
  "threshold_start": 0.55,
  "threshold_stop": 0.95,
  "boost_scheduled": true,
  "last_full_charge": "...",
  "next_boost_after": "...",
  "deadline_time": "05:45",
  "deadline_next": "...",
  "deadline_restart_at": "...",
  "day_prices": [{"date": "2026-09-17", "min_kwh": 0.5, "max_kwh": 1.3, "avg_kwh": 0.9}],
  "updated": "..."
}
```

`threshold_start`/`threshold_stop` are **derived** from the hours actually
selected — they are information, never input.

## Price data format

The integration reads the forecast sensor's `hours` attribute in compact
format, one entry per hour:

```json
{"s": 1789603200, "p": 0.8}
```

- `s` — unix epoch (seconds or milliseconds)
- `p` — price per kWh in your chosen currency (never converted)

The optional `days` attribute (`date`, `min_kwh`, `max_kwh`, `avg_kwh`) is
used for the per-day stats on the sensor. The forecast typically covers up to
14 days (a *long* horizon is what lets the boost pick the cheapest day of the
week — your sensor's "forecast period" option).

## Dashboard visualization (ApexCharts)

```yaml
type: custom:apexcharts-card
graph_span: 3d
now:
  show: true
header:
  show: true
  title: Elpris + plan
series:
  - entity: sensor.spot_price_SE3_forecast
    attribute: hours
    type: line
    data_generator: |
      return entity.attributes.hours.map(h => ({
        x: new Date(h.s * 1000).getTime(),
        y: h.p
      }));
    name: Spotpris
  - entity: sensor.smart_charging_plan
    attribute: planned_sessions
    type: range
    data_generator: |
      return entity.attributes.planned_sessions.map(s => ({
        x: [new Date(s.start).getTime(), new Date(s.end).getTime()],
        y: s.power_kw
      }));
    name: Plan
    color: "#43A047"
```

## Development & testing

```bash
python3 -m pytest tests/ -v              # unit tests
python3 tests/test_helper.py              # standalone (no pytest)
python3 dryrun.py                        # dry-run with a built-in sample
python3 dryrun.py --sample > prices.json  # sample price fixture
python3 dryrun.py --prices prices.json    # dry-run against price data
python3 dryrun.py --weekly-full           # demo the weekly 100 % boost
```

## Contract (do not break)

- Entity IDs: `sensor.smart_charging_plan`, `sensor.smart_charging_decision`,
  `select.smart_charging_mode`, `calendar.smart_charging_plan`.
- **Writes**: only `switch.*_charger_operation_mode` (turn_on/off) and
  `button.*_resume_charging` / `button.*_stop_charging_final` (press).
- **Never writes**: `number.*_available_current` — that belongs to the
  load-balancing blueprint.

## Releasing

1. Bump `version` in `custom_components/smart_charging/manifest.json` **and**
   `custom_components/smart_charging/const.py`.
2. Commit, tag `v<version>`, push, and create a GitHub Release.

## Support

Open an issue at <https://github.com/daniel-jo/smart-charging/issues>.