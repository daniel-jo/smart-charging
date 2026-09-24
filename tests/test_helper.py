"""Unit tests for the pure logic in helper.py.

Run with pytest (preferred) or directly:  python3 tests/test_helper.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
        "smart_charging",
    ),
)

import helper  # noqa: E402

UTC = timezone.utc
EPSILON = 1e-9


def _pts(pairs: list[tuple[float, float]]) -> list[helper.PriceHour]:
    """Build PriceHours from (hour-offset, price) pairs relative to a fixed base."""
    base = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    return [helper.PriceHour(base + timedelta(hours=h), p) for h, p in pairs]


def _now(hour: float = 9, minute: int = 0) -> datetime:
    """Convenience: now at the given hour/min on the base date."""
    hours = int(hour)
    mins = minute if minute else int((hour % 1) * 60)
    return datetime(2026, 9, 17, hours, mins, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Mode guards
# ---------------------------------------------------------------------------


def test_off_mode_returns_empty_plan():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.5)]),
        connected=True,
        mode=helper._MODE_OFF,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(),
    )
    assert plan.sessions == []
    assert plan.next_action.action == "none"
    assert plan.next_action.reason == "off"
    assert plan.summary == "Av"


def test_disconnected_returns_empty():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.5)]),
        connected=False,
        mode=helper._MODE_LIVE,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(),
    )
    assert plan.sessions == []
    assert plan.next_action.action == "stop"
    assert plan.next_action.reason == "disconnected"
    assert plan.summary == "Ej inkopplad"


def test_no_price_data_returns_no_data():
    plan = helper.compute_plan(
        price_hours=[],
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(),
    )
    assert plan.sessions == []
    assert plan.next_action.reason == "no_data"
    assert plan.summary == "Inga data"


def test_no_future_hours_returns_no_data():
    plan = helper.compute_plan(
        price_hours=_pts([(1, 0.5), (2, 0.6)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(12),
    )
    assert plan.sessions == []
# ---------------------------------------------------------------------------
# Hysteresis — block building
# ---------------------------------------------------------------------------


def test_block_starts_at_threshold_start_and_stops_at_threshold_stop():
    """Hour 10 <= 0.80 start; hour 11 > 0.95 stop -> block ends after hour 10."""
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70), (11, 1.20)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert len(plan.sessions) == 1
    assert plan.sessions[0].start == _now(10)
    assert plan.sessions[0].end == _now(10) + timedelta(hours=1)
    assert len(plan.sessions[0].hours) == 1


def test_block_continues_through_stop_threshold():
    """Hour 10 <= 0.80 start; hour 11 <= 0.95 stop -> block continues."""
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70), (11, 0.90), (12, 1.20)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert len(plan.sessions) == 1
    assert len(plan.sessions[0].hours) == 2


def test_block_ends_above_threshold_stop():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70), (11, 0.90), (12, 1.00)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert len(plan.sessions) == 1
    assert len(plan.sessions[0].hours) == 2


def test_multiple_disjoint_blocks():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70), (11, 1.20), (12, 0.75), (13, 0.85)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert len(plan.sessions) == 2


def test_no_cheap_blocks():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.81), (11, 0.96)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert plan.sessions == []
    assert plan.next_action.reason == "threshold"
    assert "dyrt" in plan.summary


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


def test_deadline_past():
    """All future hours are after deadline so nothing is planned."""
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=_now(9),  # already passed
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(10),
    )
    # hour 10 is NOT < deadline (9), so it's filtered out
    assert plan.sessions == []
    assert plan.next_action.reason == "deadline"


def test_deadline_imminent_force_on():
    """Remaining hours (9-10 = 1h) < needed (ceil(20/11)=2h) -> force all."""
    plan = helper.compute_plan(
        price_hours=_pts([(9, 0.81), (10, 0.96)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=_now(11),
        charger_max_kw=11.0,
        battery_need_kwh=20.0,
        now=_now(9),
    )
    assert len(plan.sessions) == 1
    assert len(plan.sessions[0].hours) == 2  # hours 9 and 10 forced


# Deadline forcing only applies when battery_need is set
def test_deadline_no_battery_need_no_force():
    """Without battery_need, deadline forcing doesn't kick in."""
    plan = helper.compute_plan(
        price_hours=_pts([(9, 0.81), (10, 0.96)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=_now(11),
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    # No cheap blocks -> empty plan
    assert plan.sessions == []
    assert plan.next_action.reason == "threshold"
# ---------------------------------------------------------------------------
# Battery need
# ---------------------------------------------------------------------------


def test_battery_need_selects_cheapest_blocks():
    """Two blocks: [10h]@0.50, [12h,13h]@0.40,0.45. Need 11 kWh (1h @ 11 kW).
    Cheapest block is [12,13], truncated to 1h -> picks hour 12 @ 0.40."""
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.50), (11, 1.00), (12, 0.40), (13, 0.45)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=11.0,
        now=_now(9),
    )
    assert len(plan.sessions) == 1
    assert len(plan.sessions[0].hours) == 1
    assert abs(plan.sessions[0].avg_price_kwh - 0.40) < EPSILON


def test_battery_need_none_takes_all_cheap():
    """Without battery_need, keep all cheap blocks (two blocks)."""
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70), (11, 0.90), (14, 0.50)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert len(plan.sessions) == 2


# ---------------------------------------------------------------------------
# Next action
# ---------------------------------------------------------------------------


def test_next_action_resume_before_session():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert plan.next_action.action == "resume"
    assert plan.next_action.at == _now(10)
    assert plan.next_action.reason == "cheap_window"


def test_next_action_none_during_session():
    """Now is 10:30 -> within hour 10 which is a cheap block."""
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(10, 30),
    )
    assert plan.next_action.action == "none"
    assert plan.next_action.reason == "charging"


def test_next_action_stop_after_session():
    """Now is 12:00, last cheap session ended at 11:00, hour 13 is expensive."""
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70), (11, 0.90), (13, 0.81)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(12),
    )
    assert plan.next_action.action == "stop"
    assert plan.next_action.reason == "threshold"
# ---------------------------------------------------------------------------
# Summary strings
# ---------------------------------------------------------------------------


def test_summary_off():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70)]),
        connected=True,
        mode=helper._MODE_OFF,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert plan.summary == "Av"


def test_summary_no_plan():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.81)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert "dyrt" in plan.summary


def test_summary_with_session():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70), (11, 0.90)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert "Laddning" in plan.summary
    assert "11 kW" in plan.summary


def test_summary_disconnected():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70)]),
        connected=False,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert plan.summary == "Ej inkopplad"


def test_summary_shows_configured_currency():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70), (11, 0.90)]),
        connected=True,
        mode=helper._MODE_PLAN,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
        currency="EUR",
    )
    assert plan.currency == "EUR"
    assert "EUR/kWh" in plan.summary


def test_plan_default_currency_is_eur():
    plan = helper.compute_plan(
        price_hours=_pts([(10, 0.70)]),
        connected=True,
        mode=helper._MODE_OFF,
        threshold_start=0.80,
        threshold_stop=0.95,
        deadline=None,
        charger_max_kw=11.0,
        battery_need_kwh=None,
        now=_now(9),
    )
    assert plan.currency == "EUR"


# ---------------------------------------------------------------------------
# Price parsing — currency handling
# ---------------------------------------------------------------------------


def test_parse_price_hours_uses_generic_key():
    """Prices are read from the currency-agnostic 'currency_kwh' key."""
    hours = [
        {"start": "2026-09-17T10:00:00+00:00", "currency_kwh": 0.40, "sek_kwh": 9.99},
        {"start": "2026-09-17T11:00:00+00:00", "currency_kwh": 0.50},
    ]
    out = helper.parse_price_hours(hours, currency="EUR")
    assert [h.price_kwh for h in out] == [0.40, 0.50]
    assert out[0].start == _now(10)


def test_parse_price_hours_uses_chosen_currency_key():
    """A per-currency key matching the configured choice is accepted.

    This is the *only* place SEK is used in the tests: it verifies the
    conversion of a SEK-keyed source when the user has explicitly chosen SEK.
    """
    hours = [{"start": "2026-09-17T10:00:00+00:00", "sek_kwh": 0.80}]
    out = helper.parse_price_hours(hours, currency="SEK")
    assert len(out) == 1
    assert abs(out[0].price_kwh - 0.80) < EPSILON


def test_parse_price_hours_no_implicit_currency_fallback():
    """Prices in a currency the user did NOT choose are never trusted."""
    hours = [{"start": "2026-09-17T10:00:00+00:00", "sek_kwh": 0.80}]
    assert helper.parse_price_hours(hours, currency="EUR") == []


def test_parse_price_hours_naive_start_assumed_utc():
    hours = [{"start": "2026-09-17T10:00:00", "currency_kwh": 0.30}]
    out = helper.parse_price_hours(hours, currency="EUR")
    assert len(out) == 1
    assert out[0].start == datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


def test_parse_price_hours_skips_bad_rows():
    hours = [
        {"start": "2026-09-17T10:00:00+00:00", "currency_kwh": 0.30},
        {"start": "not-a-date", "currency_kwh": 0.40},
        {"start": "2026-09-17T11:00:00+00:00"},  # no usable price key
    ]
    out = helper.parse_price_hours(hours, currency="EUR")
    assert len(out) == 1
    assert out[0].price_kwh == 0.30


# ---------------------------------------------------------------------------
# Run standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    failures = 0
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"PASS  {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failures} failed")
    sys.exit(1 if failures else 0)
# ---------------------------------------------------------------------------