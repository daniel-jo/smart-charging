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
| Max charger power (kW) | e.g. 11 |
| Price currency | Labels the prices; nothing is converted |
| Charge to 100 % weekly | Enable the cheapest-window boost |
| Min. days between 100 % charges | Cooldown, default 5 |
| Continuous usage (checkbox) | The car has regular away-times — see below |
| Applies to | `weekdays` (Mon–Fri) or `all days` — only used when Continuous usage is on |
| Usually away from / until | The window the car is normally gone — the **from** time is the new ready-by |
| Daily battery use (%/day) | Average daily consumption (e.g. 15); spread over the away hours only |
| Use last known battery level (h) | Max age of a remembered SOC (default 12): the plan survives while the car reports no SOC |

### Continuous usage (optional)

Ticking **"Continuous usage"** (`Kontinuerlig körning`) replaces the daily
ready-by model with your weekly rhythm. All fields live on the **same
parameters page** (no extra step) — the away-fields are only required and
only used while the checkbox is on:

- **Applies to** — `weekdays` (Mon–Fri) or `all days`.
- **Usually away from / until** — the window the car is normally gone.

- While the car is **home** (`away until` → `away from`) it may charge; while
  it is **away** it never charges.
- The **"from" time is the new "ready by"**: charging must be *complete* before
  it (minute precise), because that is when the car leaves.
- **The daily consumption is used only while the car is away** — parked at home
  the battery stays put, so `daily battery use` is spread over the away hours.
- On **non-applicable days** (weekends with `weekdays`) the car is home all day:
  no consumption.
- While away, the summary reads `Borta t.o.m. HH:MM — …`.

### SOC gaps (car away, sensor offline)

Many car integrations report `unknown`/`unavailable` while the car is gone —
instead of dropping the plan ("Ingen SOC-data"), the integration remembers the
last valid SOC reading and projects it forward through the away-window (the
same drain model as above). The plan — in **both** Plan and Live mode — stays
intact during the daily commute. The new `soc_source` attribute on
`sensor.smart_charging_plan` tells you where the value came from: `sensor`,
`remembered` (unchanged) or `projected` (drained through the away-window).

- The reading expires after **Use last known battery level (h)** (default 12 h);
  with no fresh *and* no recent reading the plan still fails safe (`stop`).
- The weekly-100 % detection and the measured Live statistics always use the
  fresh sensor reading — only the *planning* input falls back.

## Modes

| Mode | State | Plan calc | Zaptec calls | Use case |
|---|---|---|---|---|
| **Av** | `sensor.smart_charging_plan` = `"Av"` | No | Never | Manual/off |
| **Planläge (test)** | Plan + logbook | Yes | **No** | Verify decisions risk-free; a plan is also built while the car is disconnected |
| **Live** | Plan + logbook | Yes | Yes | Enable charging |

The plan is always only *information* — it never touches the charger by
itself. Only **Live** writes to the charger (operation-mode switch +
resume/stop buttons), and only for a car that reports as connected.

Switch modes via `select.smart_charging_mode` or the HA Services → Developer
Tools.

## Entities

| Entity ID | Type | Purpose |
|---|---|---|
| `sensor.smart_charging_plan` | sensor | Human-readable summary + attributes below |
| `sensor.smart_charging_decision` | sensor | `resume`/`stop`/`none` + `reason` |
| `select.smart_charging_mode` | select | Av / Planläge (test) / Live |
| `calendar.smart_charging_plan` | calendar | Every planned session as a calendar event |
| `sensor.smart_charging_plan_energy` | sensor | kWh "charged" in Plan mode (**simulated**) |
| `sensor.smart_charging_plan_saving` | sensor | Money saved vs the day average (simulated) |
| `sensor.smart_charging_plan_avg_price` | sensor | Average price paid per kWh (simulated) |
| `sensor.smart_charging_live_energy` | sensor | kWh actually charged in Live mode |
| `sensor.smart_charging_live_saving` | sensor | Money saved vs the day average (Live) |
| `sensor.smart_charging_live_avg_price` | sensor | Average price paid per kWh (Live) |

### `sensor.smart_charging_plan` attributes

```json
{
  "planned_sessions": [{"start": "...", "end": "...", "power_kw": 11.0,
                          "avg_price_kwh": 0.567, "is_boost": false, "hours": [...]}],
  "planned_hours": [{"s": 1789603200, "p": 0.56, "kw": 11.0, "b": 0},
                    {"s": 1789606800, "p": 0.61, "kw": 11.0, "b": 0}],
  "next_action": {"action": "resume", "at": "...", "reason": "cheap_window"},
  "mode": "plan",
  "currency": "SEK",
  "soc_now": 60.0,
  "soc_source": "sensor",
  "min_soc": 20.0,
  "max_soc": 80.0,
  "daily_consumption_pct": 15.0,
  "threshold_start": 0.55,
  "threshold_stop": 0.95,
  "boost_scheduled": true,
  "last_full_charge": "...",
  "next_boost_after": "...",
  "usage_enabled": true,
  "usage_days": "weekdays",
  "usage_away_start": "07:00",
  "usage_away_end": "17:00",
  "usage_next": "...",
  "day_prices": [{"date": "2026-09-17", "min_kwh": 0.5, "max_kwh": 1.3, "avg_kwh": 0.9}],
  "updated": "..."
}
```

`planned_hours` is a flat per-hour list of the hours the plan intends to
charge — one entry per hour with the same compact `{"s": epoch, "p": price}`
shape as the spot-price forecast sensor, plus `kw` (charging power) and `b`
(`1` for the weekly boost). It makes the *future* plan trivial to chart.

`threshold_start`/`threshold_stop` are **derived** from the hours actually
selected — they are information, never input.

## Charging statistics — how much you charge, save and at what price

Three numbers are tracked **separately per mode** (`sensor.smart_charging_plan_*`
for the simulated test and `sensor.smart_charging_live_*` for real charging):

- **Energy (kWh)** — with `device_class: energy` + `state_class: total_increasing`,
  so Home Assistant's recorder keeps the history and the Energy dashboard picks
  them up.
- **Saving** — money saved *versus the day's average price* of the same day
  (from the forecast sensor's `days` attribute; falls back to the mean of the
  whole forecast). A forced expensive hour (battery floor) correctly shows a
  *negative* contribution.
- **Average price** — the energy-weighted average of what was paid per kWh.

### Plan mode = simulation

While in **Planläge (test)** the accumulated totals are **simulated**: every
planned hour that passes is counted as charged (`power_kw × duration` at that
hour's price), minute-precise for the currently ongoing hour. This is fully
disconnected from the car — exactly the overview you want before going live.

### Live mode = measurement

In **Live** the energy is **measured**. Configure the optional
**Charging energy sensor** in the options (e.g. your Zaptec charging-energy
sensor in kWh, or a kW power sensor — both units are auto-detected). While the
integration drives a charging session it samples the sensor every 60 seconds
and accrues the measured kWh at the price of the clock hour it is measured in.

Without a configured energy sensor the SOC fallback is used (SOC delta ×
battery capacity per session, same architecture as the energy accrual). The
`source` attribute on the sensors tells you which it was (`energy_sensor`,
`soc` or `simulation`).

> **Note:** with a cumulative energy sensor, the first ~≤15 min of a session
> (between pressing *resume* and the next plan recompute) are usually included
> thanks to a baseline sampled at resume time — but energy consumed before the
> integration first detects the charge may be missed.

### Reset

Use the `smart_charging.reset_statistics` service to zero the counters
(`mode`: `plan`, `live` or `all`), e.g. at the start of every month or after a
test period. Reset does not re-accrue history.

### Dashboard example

```yaml
type: entities
entities:
  - entity: sensor.smart_charging_plan_energy
  - entity: sensor.smart_charging_plan_saving
  - entity: sensor.smart_charging_plan_avg_price
  - entity: sensor.smart_charging_live_energy
  - entity: sensor.smart_charging_live_saving
  - entity: sensor.smart_charging_live_avg_price
```

The `energy` sensors also appear in the Energy dashboard, and the `avg_price`
sensors render a price-per-kWh history from the recorder.

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
show:
  loading: false
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
      // Håll serien minst med en punkt så att "Loading…" inte fastnar när
      // planen är tom (se rubriken "Felsökning: Loading…").
      const planned = (entity && entity.attributes && entity.attributes.planned_sessions) || [];
      if (!Array.isArray(planned) || planned.length === 0) {
        return [{ x: Date.now(), y: null }];
      }
      try {
        const price = {}, boost = {};
        planned.forEach(h => { price[h.s] = h.p; if (h.b) boost[h.s] = true; });
        const start = Math.min(...planned.map(h => h.s));
        const end = Math.max(...planned.map(h => h.s)) + 3600;
        const pts = [];
        for (let s = start; s < end; s += 3600) {
          pts.push({
            x: new Date(s * 1000).getTime(),
            y: (price[s] !== undefined && !boost[s]) ? price[s] : null
          });
        }
        return pts;
      } catch (e) {
        return [{ x: Date.now(), y: null }];
      }
    name: Plan
    color: "#43A047"
```

The same idea with the flat `planned_hours` attribute — the price of every
planned future charging hour as bars (boost hours purple):

```yaml
type: custom:apexcharts-card
graph_span: 5d
now:
  show: true
header:
  show: true
  title: Planerade laddtimmar
show:
  loading: false
series:
  - entity: sensor.smart_charging_plan
    attribute: planned_hours
    type: column
    data_generator: |
      // Håll serien minst med en punkt så att "Loading…" inte fastnar när
      // planen är tom (se rubriken "Felsökning: Loading…").
      const planned = (entity && entity.attributes && entity.attributes.planned_hours) || [];
      if (!Array.isArray(planned) || planned.length === 0) {
        return [{ x: Date.now(), y: null }];
      }
      try {
        return planned.map(h => ({
          x: new Date(h.s * 1000).getTime(),
          y: h.b ? 15 : 14
        }));
      } catch (e) {
        return [{ x: Date.now(), y: null }];
      }
    name: Plan
```
> `show: { loading: false }` och en ifylld serie hindrar att kortet fastnar i
> "Loading…" när planen är tom (se nedan).
>
## Felsökning: kortet står fast i "Loading…"
apexcharts-card går ur "Loading…" först när **alla** serier har minst en datapunkt.
En tom serie (t.ex. plan-serien när `planned_hours` = `[]`, dvs ingen laddning
är planerad just nu) får kortet att aldrig klara avsluta laddningen.
- Kontrollera datat: Developer Tools → Template:
  `{{ state_attr('sensor.smart_charging_plan','planned_hours') | length }}`
- Sätt `show: { loading: false }` så grafen ritas ändå (priskurvan syns, och
  laddfönstret dyker upp först när en plan finns).
- Ett tomt `planned_hours` är normalt: ingen laddning behövs just nu (bilen redan
  laddad, inga billiga timmar). Se `sensor.smart_charging_plan`:s tillstånd och
  attributen `mode` / `next_action` / `soc_now`.
- Lägg aldrig bara en plan-serie i kortet — håll alltid prisserien som säkrare.


> Cards read the sensor's *current* attributes, so they show future hours too —
> Home Assistant's built-in history charts only show past states. Set
> `graph_span` long enough to cover today plus the planned horizon.

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