# Smart Charging — plan-based EV charging for Home Assistant

> ⚠️ This integration is the **brain** that decides *when* to charge your EV
> based on spot prices. It **never** writes the load current
> (`available_current`) — that is the sole domain of your load-balancing
> automation/blueprint (e.g. the svenakela charger-balancing blueprint).

Plan when and how to charge your EV based on hourly spot prices (from the
**Spot prices** HA integration), configurable price thresholds, and an
optional deadline.

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
   1. **Entities** — pick the **Spot prices forecast sensor**
      (`sensor.spot_prices_<AREA>_forecast`), your charger's operation mode
      switch, resume/stop buttons, charger mode sensor, and an optional
      deadline `input_datetime`.
   2. **Parameters** — set the price thresholds, charger max power and
      optional battery energy need.
6. The integration starts in **Planläge (test)** mode — see below.

Updates arrive as HACS update notifications whenever a new release is tagged
in this repository.

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
| `sensor.smart_charging_plan` | sensor | Human-readable summary + `planned_sessions` (list), `next_action`, `mode` |
| `sensor.smart_charging_decision` | sensor | `resume`/`stop`/`none` + `reason` attribute |
| `select.smart_charging_mode` | select | Av / Planläge (test) / Live |
| `calendar.smart_charging_plan` | calendar | Every planned session as a calendar event |

### `planned_sessions` attribute

```json
[
  {
    "start": "2026-09-23T22:00:00+02:00",
    "end": "2026-09-24T02:00:00+02:00",
    "power_kw": 11.0,
    "avg_price_sek_kwh": 0.567,
    "hours": [{"start": "2026-09-23T22:00:00+02:00", "sek_kwh": 0.50}, ...]
  }
]
```

Use this in any chart card (e.g. ApexCharts) to overlay the planned charging
windows on the price forecast graph.

## Development & testing

```bash
python3 -m pytest tests/ -v              # unit tests
python3 tests/test_helper.py              # standalone (no pytest)
python3 dryrun.py --sample > prices.json  # sample price fixture
python3 dryrun.py --prices prices.json    # dry-run the planner
```

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
  - entity: sensor.spot_prices_SE3_forecast
    attribute: hours
    type: line
    data_generator: |
      return entity.attributes.hours.map(h => ({
        x: new Date(h.start).getTime(),
        y: h.sek_kwh
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