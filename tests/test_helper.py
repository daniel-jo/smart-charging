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
EPSILON = 1e-6
BASE = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)  # a Thursday
RATE = 100.0 * 11.0 / 77.0  # SOC % per hour at 11 kW on a 77 kWh battery


def _pts(pairs: list[tuple[float, float]]) -> list[helper.PriceHour]:
    """Build PriceHours from (hour-offset, price) pairs relative to BASE."""
    return [helper.PriceHour(BASE + timedelta(hours=h), p) for h, p in pairs]


def _now(hour: float = 0) -> datetime:
    hours = int(hour)
    mins = int((hour % 1) * 60)
    return datetime(2026, 9, 17, hours, mins, tzinfo=UTC)


def _day_night_prices(
    days: int = 14,
    night: float = 0.8,
    day: float = 1.4,
    override: dict | None = None,
) -> list[helper.PriceHour]:
    """Night (22:00-06:00) / day price rhythm; optional {index: price}."""
    pts: list[tuple[int, float]] = []
    for d in range(days):
        for hh in range(24):
            is_night = hh >= 22 or hh <= 5
            pts.append((d * 24 + hh, night if is_night else day))
    if override:
        for idx, price in override.items():
            pts[idx] = (idx, price)
    return _pts(pts)


def _cp(**kw) -> helper.Plan:
    """compute_plan with practical defaults, overridable per test."""
    defaults = dict(
        price_hours=_pts([]),
        day_prices=None,
        connected=True,
        mode=helper._MODE_PLAN,
        soc_now=50.0,
        min_soc=20.0,
        max_soc=80.0,
        daily_consumption_pct=0.0,
        charger_max_kw=11.0,
        battery_capacity_kwh=77.0,
        weekly_full_charge=False,
        last_full_charge=None,
        min_days_between_full=5.0,
        deadline=None,
        now=_now(0),
        currency="SEK",
    )
    defaults.update(kw)
    return helper.compute_plan(**defaults)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_off_mode_returns_empty_plan():
    plan = _cp(mode=helper._MODE_OFF)
    assert plan.sessions == []
    assert plan.next_action.action == "none"
    assert plan.next_action.reason == "off"
    assert plan.summary == "Av"


def test_disconnected_returns_empty():
    plan = _cp(connected=False)
    assert plan.sessions == []
    assert plan.next_action.action == "stop"
    assert plan.next_action.reason == "disconnected"
    assert plan.summary == "Ej inkopplad"


def test_no_price_data_returns_no_data():
    plan = _cp(price_hours=[])
    assert plan.sessions == []
    assert plan.next_action.reason == "no_data"
    assert plan.summary == "Inga data"


def test_no_future_hours_returns_no_data():
    plan = _cp(price_hours=_pts([(1, 0.5), (2, 0.6)]), now=_now(12))
    assert plan.sessions == []
    assert plan.next_action.reason == "no_data"


def test_no_soc_returns_no_soc():
    plan = _cp(price_hours=_pts([(0, 0.5)]), soc_now=None)
    assert plan.summary == "Ingen SOC-data"
    assert plan.next_action.reason == "no_soc"


def test_invalid_capacity_returns_config_error():
    plan = _cp(price_hours=_pts([(0, 0.5)]), battery_capacity_kwh=0.0)
    assert plan.summary == "Felaktig konfiguration"


def test_min_soc_above_max_soc_returns_config_error():
    plan = _cp(price_hours=_pts([(0, 0.5)]), min_soc=90.0, max_soc=80.0)
    assert plan.summary == "Felaktig konfiguration"


def test_deadline_past():
    plan = _cp(price_hours=_pts([(10, 0.5)]), deadline=_now(9), now=_now(10))
    assert plan.summary == "Deadline passerad"
    assert plan.next_action.reason == "deadline"


# ---------------------------------------------------------------------------
# Recurring time-of-day deadline (minute precise)
# ---------------------------------------------------------------------------


def test_deadline_time_session_ends_exact_minute():
    """A 05:45 deadline cuts the 05:00-06:00 hour short — session ends at 05:45."""
    plan = _cp(
        price_hours=_pts([(h, 0.5) for h in range(0, 8)]),  # 00:00-08:00 cheap
        soc_now=15.0,
        max_soc=100.0,
        deadline_time="05:45",
        now=_now(0),
    )
    assert plan.sessions
    last = plan.sessions[-1]
    assert last.end == BASE + timedelta(hours=5, minutes=45)
    assert abs(last.hours[-1]["duration_hours"] - 0.75) < EPSILON
    assert all(h["duration_hours"] == 1.0 for h in last.hours[:-1])


def test_no_deadline_unconstrained():
    plan = _cp(price_hours=_day_night_prices(days=2))
    assert plan.deadline_next is None
    assert plan.deadline_restart_at is None
    assert plan.deadline_time is None
    assert plan.sessions


def test_deadline_time_passed_no_restart():
    """Deadline already passed today and no restart minutes: plan stays stopped."""
    plan = _cp(
        price_hours=_day_night_prices(days=2),
        deadline_time="05:45",
        deadline_restart_minutes=0,
        now=_now(7),
    )
    assert plan.summary == "Deadline passerad"
    assert plan.sessions == []
    assert plan.next_action.action == "stop"
    assert plan.next_action.reason == "deadline"
    assert plan.deadline_restart_at is None


def test_deadline_time_blocked_then_rearm():
    """05:45 + 60 min: blocked until 06:45, then the plan targets tomorrow."""
    blocked = _cp(
        price_hours=_day_night_prices(days=3),
        deadline_time="05:45",
        deadline_restart_minutes=60,
        now=BASE.replace(hour=5, minute=50),
    )
    assert blocked.summary == "Deadline passerad"
    assert blocked.sessions == []
    assert blocked.deadline_restart_at == BASE.replace(hour=6, minute=45)

    # 06:46 — re-armed: the plan may charge again, but must end by tomorrow 05:45.
    rearmed = _cp(
        price_hours=_day_night_prices(days=3),
        deadline_time="05:45",
        deadline_restart_minutes=60,
        soc_now=30.0,
        now=BASE.replace(hour=6, minute=46),
    )
    assert rearmed.summary != "Deadline passerad"
    assert rearmed.sessions
    tomorrow = (BASE + timedelta(days=1)).replace(hour=5, minute=45)
    assert rearmed.deadline_next == tomorrow
    assert max(s.end for s in rearmed.sessions) <= tomorrow
    # Charging is scheduled again *today* (the 22:00 cheap night window).
    assert any(s.start >= BASE.replace(hour=12) for s in rearmed.sessions)


def test_deadline_time_fresh_cycle_next_day():
    """No restart (0 min): a passed deadline blocks the rest of the day, but the
    next day's pre-deadline window is planned normally."""
    plan = _cp(
        price_hours=_day_night_prices(days=3),
        deadline_time="05:45",
        deadline_restart_minutes=0,
        soc_now=30.0,
        now=(BASE + timedelta(days=1)).replace(hour=3),
    )
    assert plan.summary != "Deadline passerad"
    assert plan.deadline_next == (BASE + timedelta(days=1)).replace(hour=5, minute=45)
    assert plan.sessions
    assert max(s.end for s in plan.sessions) <= (BASE + timedelta(days=1)).replace(
        hour=5, minute=45
    )


# ---------------------------------------------------------------------------
# Parsing — compact hours format ({"s": epoch, "p": price})
# ---------------------------------------------------------------------------


def test_parse_price_hours_seconds():
    raw = [
        {"s": int(BASE.timestamp()), "p": 0.5},
        {"s": int((BASE + timedelta(hours=1)).timestamp()), "p": 0.9},
    ]
    out = helper.parse_price_hours(raw)
    assert [h.price_kwh for h in out] == [0.5, 0.9]
    assert out[0].start == BASE


def test_parse_price_hours_milliseconds():
    raw = [{"s": int(BASE.timestamp()) * 1000, "p": 0.4}]
    out = helper.parse_price_hours(raw)
    assert len(out) == 1
    assert abs(out[0].price_kwh - 0.4) < EPSILON
    assert out[0].start == BASE


def test_parse_price_hours_skips_bad_rows():
    raw = [
        {"s": int(BASE.timestamp()), "p": 0.3},
        {"s": "not-a-number", "p": 0.4},
        {"s": 0, "p": "bad"},
        {"p": 0.5},  # missing s
        "garbage",
    ]
    out = helper.parse_price_hours(raw)
    assert len(out) == 1
    assert out[0].price_kwh == 0.3


def test_parse_days_best_effort():
    raw = [
        {"date": "2026-09-17", "min_kwh": 0.5, "max_kwh": 1.3, "avg_kwh": 0.9},
        {"date": "2026-09-18", "min_kwh": "bad"},
    ]
    out = helper.parse_days(raw)
    assert len(out) == 2
    assert out[0].date == "2026-09-17"
    assert out[0].max_kwh == 1.3
    assert out[1].date == "2026-09-18"
    assert out[1].avg_kwh is None


# ---------------------------------------------------------------------------
# Core planning — top-up, stop at target, floor forcing
# ---------------------------------------------------------------------------


def test_cheap_top_up_reaches_target_and_stops():
    """50 % -> 80 % needs ceil(30/14.29) = 3 cheap hours, then stop at target."""
    plan = _cp(price_hours=_day_night_prices(days=2))
    assert len(plan.sessions) == 1
    sess = plan.sessions[0]
    assert sess.start == BASE
    assert sess.end == BASE + timedelta(hours=3)
    assert len(sess.hours) == 3
    assert all(h["price_kwh"] == 0.8 for h in sess.hours)
    # Derived thresholds — from the cheapest/expensive selected hours.
    assert plan.threshold_start == 0.8
    assert plan.threshold_stop == 0.8
    assert plan.summary.startswith("Laddning 00:00")
    # Now is inside the first session hour -> keep charging.
    assert plan.next_action.action == "none"
    assert plan.next_action.reason == "charging"


def test_no_charging_when_battery_at_target():
    plan = _cp(price_hours=_day_night_prices(days=2), soc_now=80.0)
    assert plan.sessions == []
    assert plan.summary == "Redan laddad (80 %)"
    assert plan.next_action.action == "stop"
    assert plan.next_action.reason == "complete"
    assert plan.threshold_start is None


def test_top_up_skips_expensive_hours():
    plan = _cp(price_hours=_day_night_prices(days=2))
    assert all(
        h["price_kwh"] == 0.8 for s in plan.sessions for h in s.hours
    )


def test_sessions_are_contiguous_and_sorted():
    plan = _cp(price_hours=_day_night_prices(days=2))
    starts = [s.start for s in plan.sessions]
    assert starts == sorted(starts)
    for s in plan.sessions:
        hours = [h["start"] for h in s.hours]
        assert all(
            b == a + timedelta(hours=1)
            for a, b in zip(hours, hours[1:])
        )


def test_floor_forcing_buys_expensive_hours():
    """Heavy use (80 %/day) drains the battery to the floor, so expensive
    hours must be bought to protect it — even above the auto cheap cut."""
    prices = _pts([(0, 0.5), (1, 0.5), (2, 0.5)] + [(h, 1.5) for h in range(3, 24)])
    plan = _cp(price_hours=prices, soc_now=25.0, daily_consumption_pct=80.0)
    assert plan.sessions
    expensive = [
        h["price_kwh"]
        for s in plan.sessions
        for h in s.hours
        if h["price_kwh"] > 1.3
    ]
    assert expensive, "expected forced charging during expensive hours"
    assert plan.threshold_start == 0.5
    assert plan.threshold_stop == 1.5

    # Simulate the trajectory from the plan: SOC must never drop below floor.
    selected_starts = {h["start"] for s in plan.sessions for h in s.hours}
    soc = 25.0
    min_soc_seen = soc
    for h in prices:
        if h.start in selected_starts:
            soc = min(80.0, soc + RATE)
        else:
            soc -= 80.0 / 24.0
        min_soc_seen = min(min_soc_seen, soc)
    assert min_soc_seen >= 20.0 - EPSILON


# ---------------------------------------------------------------------------
# Weekly 100 % boost
# ---------------------------------------------------------------------------

CHEAP_WINDOW = {166: 0.3, 167: 0.3, 168: 0.3, 169: 0.3, 170: 0.3, 171: 0.3}


def test_boost_scheduled_on_cheapest_window():
    """Day-6 22:00-04:00 is the cheapest stretch in the 14-day forecast."""
    plan = _cp(
        price_hours=_day_night_prices(days=14, override=CHEAP_WINDOW),
        soc_now=60.0,
        daily_consumption_pct=15.0,
        weekly_full_charge=True,
    )
    assert plan.boost_scheduled is True
    boosts = [s for s in plan.sessions if s.is_boost]
    assert len(boosts) == 1
    boost = boosts[0]
    assert boost.start == BASE + timedelta(days=6, hours=22)
    assert boost.end == BASE + timedelta(days=6, hours=28)
    assert all(h["price_kwh"] == 0.3 for h in boost.hours)
    assert round(boost.avg_price_kwh, 4) == 0.3
    # No last full charge -> eligible immediately (cooldown bound = now).
    assert plan.next_boost_after == BASE


def test_boost_not_scheduled_when_disabled():
    plan = _cp(price_hours=_day_night_prices(days=14, override=CHEAP_WINDOW))
    assert plan.boost_scheduled is False
    assert plan.next_boost_after is None
    assert not any(s.is_boost for s in plan.sessions)


def test_boost_blocked_by_cooldown():
    """A full charge one day ago + 5-day cooldown pushes the bound past a
    short 3-day horizon, so no boost can be scheduled."""
    plan = _cp(
        price_hours=_day_night_prices(days=3),
        soc_now=60.0,
        daily_consumption_pct=15.0,
        weekly_full_charge=True,
        last_full_charge=BASE - timedelta(days=1),
    )
    assert plan.boost_scheduled is False
    assert plan.next_boost_after == BASE + timedelta(days=4)
    assert not any(s.is_boost for s in plan.sessions)


def test_boost_eligible_again_after_cooldown():
    plan = _cp(
        price_hours=_day_night_prices(days=14, override=CHEAP_WINDOW),
        soc_now=60.0,
        daily_consumption_pct=15.0,
        weekly_full_charge=True,
        last_full_charge=BASE - timedelta(days=6),
    )
    assert plan.boost_scheduled is True
    # Bound = max(now, last_full + 5 d) = BASE (the cooldown already elapsed).
    assert plan.next_boost_after == BASE


# ---------------------------------------------------------------------------
# Next action & summary
# ---------------------------------------------------------------------------


def test_next_action_resume_before_first_session():
    now = BASE - timedelta(hours=2)  # 2026-09-16 22:00
    plan = _cp(price_hours=_day_night_prices(days=2), now=now)
    assert plan.next_action.action == "resume"
    assert plan.next_action.at == BASE
    assert plan.next_action.reason == "cheap_window"


def test_next_action_resume_boost_reason():
    # Day-6 08:00 — before the boost window (day-6 22:00), after all earlier
    # cheap nights, so the boost is the first scheduled session.
    plan = _cp(
        price_hours=_day_night_prices(days=14, override=CHEAP_WINDOW),
        weekly_full_charge=True,
        now=BASE + timedelta(days=6, hours=8),
    )
    boosts = [s for s in plan.sessions if s.is_boost]
    assert boosts
    assert plan.sessions[0].is_boost is True
    assert plan.next_action.action == "resume"
    assert plan.next_action.reason == "boost"
    assert plan.next_action.at == boosts[0].start


def test_summary_shows_currency():
    plan = _cp(price_hours=_day_night_prices(days=2), currency="SEK")
    assert "SEK/kWh" in plan.summary


def test_summary_boost_marker():
    plan = _cp(
        price_hours=_day_night_prices(days=14, override=CHEAP_WINDOW),
        weekly_full_charge=True,
    )
    assert "100 %-laddning" in plan.summary


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