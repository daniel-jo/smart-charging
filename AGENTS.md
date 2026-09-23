# Smart Charging — Home Assistant integration

Rules for agents working in this repository (the standalone, public **Smart
charging** HA integration). Source, tests and docs all live here; releases are
tags pushed to this repo (`v<version>`).

## Scope

- **Never read or modify files outside this repository's root directory.**
  The integration's code, tests, docs and config all live under
  `custom_components/smart_charging/`, `tests/`, `dryrun.py` and other files in
  this repo — everything you need is here. Files in sibling repos or outside
  paths (e.g. `~/projects/daniel-jo/de-server/`) are off-limits unless the user
  explicitly puts you in context and asks you to work there.

## Plan mode

- In **plan mode** the agent is strictly read-only: explore, analyze and
  present a plan only. Do **not** edit or create files and do **not** run any
  state-changing commands (including `git`).
- Stop after presenting the plan and wait for the user to switch to **act
  mode** before making changes.
- Read-only inspection and validation (reads, searches, `pytest`,
  `node --check`) are fine in plan mode; anything that writes is not.

## Layout

```
custom_components/smart_charging/  # the HA integration
├── __init__.py                    # platform setup & config entry lifecycle
├── config_flow.py                 # setup UI & options flow
├── const.py                       # constants, defaults, modes
├── coordinator.py                 # event-driven recompute, throttle, logbook
├── helper.py                      # pure logic — compute_plan (no HA imports)
├── sensor.py                      # sensor.smart_charging_plan + _decision
├── calendar.py                    # calendar.smart_charging_plan (HA calendar view)
├── select.py                      # select.smart_charging_mode (Av/Planläge/Live)
├── translations/                  # localised strings (en.json, sv.json)
├── strings.json                   # config-flow translation placeholders
├── manifest.json                  # HA/HACS metadata
dryrun.py                          # standalone plan check (pure stdlib)
tests/
└── test_helper.py                 # unit tests for helper.py (pytest)
.env.example                       # dry-run price JSON template (no secrets)
hacs.json                          # HACS metadata
README.md
```

## Commands

- Tests: `python3 -m pytest tests/ -v`
- Run tests standalone (no pytest): `python3 tests/test_helper.py`
- Dry-run against a price JSON fixture:
  `python3 dryrun.py --prices dryrun_prices.json --mode plan --threshold-start 0.80 --threshold-stop 0.95 --charger-max-kw 11`
- Generate a sample price fixture:
  `python3 dryrun.py --sample > dryrun_prices.json`

## Git hygiene

- Never run state-changing git commands (commit, push, tag, rebase, force-push,
  branch, …) on your own initiative. Stop after the change is made and verified;
  committing and pushing are up to the user. Do it only when the user
  explicitly asks — the "Releasing" steps below are no exception.

## Releasing

1. Bump `version` in **both** `custom_components/smart_charging/manifest.json` and
   `custom_components/smart_charging/const.py` (`VERSION` constant) — the two must
   always match.
2. Commit, tag `v<version>` and push: `git push origin main --tags`.
3. Create a GitHub Release for the tag (gh CLI or the web UI).

## Contracts you must not break

- Sensor entity IDs: `sensor.smart_charging_plan` (state: human-readable plan
  summary, attributes: `planned_sessions`, `next_action`, `mode`, …)
  and `sensor.smart_charging_decision` (state: `resume`/`stop`/`none`).
- Select entity: `select.smart_charging_mode` with options
  `["Av", "Planläge (test)", "Live"]`.
- Calendar entity: `calendar.smart_charging_plan` — every planned session
  becomes a calendar event visible in HA's built-in Calendar view.
- The integration **writes** only `switch.*_charger_operation_mode` (turn_on/off)
  and `button.*_resume_charging` / `button.*_stop_charging_final` (press).
- The integration **never** writes `number.*_available_current` (that is the
  load-balancing blueprint's sole domain).
- `helper.py` must stay free of Home Assistant imports (unit-testable / dry-run
  without HA). Use `datetime`, `dataclasses`, pure Python types only.

## Style

- Pure stdlib, async patterns, type hints.
- Manifest stays `requirements: []` unless there is a real need — say so loudly
  if you ever need to add a dependency.
- Formatting follows HA core conventions: black (88‑column line length), no
  trailing whitespace. There is no formatter config in this repository — adhere
  to the implied style of the existing code.
- Prefer one clear responsibility per file; if a module passes roughly 400–500
  lines, consider extracting focused logic into a helper (e.g. `helper.py`)
  rather than letting the module keep growing.

## Secrets

- This integration has no API keys or secrets. `.env` does not belong here and
  must never be committed. The `dryrun.py` reads a price JSON file, not a `.env`.
- Keep `dryrun_prices.json` or similar fixture files gitignored if they contain
  real price data; check in `dryrun_prices.json.sample` instead.