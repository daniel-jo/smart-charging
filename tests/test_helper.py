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


def test_disconnected_live_returns_empty():
    """Live must never build a plan for a detached car — guarded."""
    plan = _cp(connected=False, mode=helper._MODE_LIVE)
    assert plan.sessions == []
    assert plan.next_action.action == "stop"
    assert plan.next_action.reason == "disconnected"
    assert plan.summary == "Ej inkopplad"


def test_disconnected_plan_mode_still_plans():
    """Planläge (test) previews the plan even without a connection.

    The plan is information only; nothing is ever written to the charger in
    Plan mode, so a detached car must not block the preview.
    """
    plan = _cp(
        price_hours=_day_night_prices(days=2),
        connected=False,
        mode=helper._MODE_PLAN,
        now=_now(0),
    )
    assert plan.sessions
    assert plan.summary != "Ej inkopplad"
    assert all(h["price_kwh"] == 0.8 for s in plan.sessions for h in s.hours)


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


def test_unconstrained_plans_without_window():
    plan = _cp(price_hours=_day_night_prices(days=2))
    assert plan.usage_next is None
    assert plan.sessions


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


def test_planned_hours_flat_compact_sorted():
    """planned_hours flattens sessions into compact per-hour chart points."""
    plan = _cp(price_hours=_day_night_prices(days=2))
    hrs = helper.planned_hours(plan.sessions)
    assert len(hrs) == sum(len(s.hours) for s in plan.sessions)
    assert hrs == sorted(hrs, key=lambda pt: pt["s"])
    assert set(hrs[0]) == {"s", "p", "kw", "b"}
    assert hrs[0]["s"] == int(BASE.timestamp())
    assert hrs[0]["p"] == 0.8
    assert hrs[0]["kw"] == 11.0
    assert hrs[0]["b"] == 0


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
# Charging statistics (accumulators, plan-hour accrual)
# ---------------------------------------------------------------------------


def _stat_session(
    offsets: list[int],
    prices: list[float] | None = None,
    durations: list[float] | None = None,
    power: float = 11.0,
) -> helper.ChargingSession:
    """Build a ChargingSession with one hour per offset (relative to BASE)."""
    prices = prices or [0.8] * len(offsets)
    durations = durations or [1.0] * len(offsets)
    starts = [BASE + timedelta(hours=h) for h in offsets]
    hours = [
        {"start": starts[i], "price_kwh": prices[i], "duration_hours": durations[i]}
        for i in range(len(starts))
    ]
    return helper.ChargingSession(
        start=starts[0],
        end=starts[-1] + timedelta(hours=durations[-1]),
        power_kw=power,
        avg_price_kwh=round(sum(prices) / len(prices), 4),
        hours=hours,
    )


def test_stats_accrue_charging_and_derived_values():
    stats = helper.accrue_charging(helper.ChargingStats(), 11.0, 0.8, 1.0, at=BASE)
    assert stats.kwh == 11.0
    assert stats.cost == 8.8
    assert stats.cost_at_ref == 11.0
    assert stats.sessions == 0  # windows are bumped separately
    assert stats.first_at == BASE
    assert stats.last_at == BASE
    assert abs(stats.avg_price_kwh - 0.8) < EPSILON
    assert abs(stats.saved - 2.2) < EPSILON

    stats2 = helper.accrue_charging(stats, 11.0, 0.4, 1.0, at=BASE + timedelta(hours=1))
    assert stats2.kwh == 22.0
    assert abs(stats2.avg_price_kwh - 0.6) < EPSILON
    assert stats2.first_at == BASE  # first accrual is sticky
    assert stats2.last_at == BASE + timedelta(hours=1)


def test_stats_accrue_ignores_non_positive_energy():
    stats = helper.accrue_charging(helper.ChargingStats(), 5.0, 0.8, 1.0)
    unchanged = helper.accrue_charging(stats, 0.0, 0.8, 1.0)
    assert unchanged is stats
    assert helper.accrue_charging(stats, -2.0, 0.8, 1.0) is stats


def test_stats_empty_derived_values_are_none():
    empty = helper.ChargingStats()
    assert empty.avg_price_kwh is None
    assert empty.saved is None


def test_start_stat_session_bumps_counter_without_energy():
    stats = helper.start_stat_session(helper.ChargingStats())
    assert stats.sessions == 1 and stats.kwh == 0.0
    stats = helper.accrue_charging(stats, 11.0, 0.8, 1.0)
    stats = helper.start_stat_session(stats)
    assert stats.sessions == 2 and stats.kwh == 11.0


def test_reference_price_kwh_day_lookup_and_fallback():
    days = [helper.DayPrice("2026-09-17", 0.5, 1.3, 0.9)]
    # Matching day -> day average.
    assert helper.reference_price_kwh(days, BASE) == 0.9
    # Unknown day -> fallback.
    assert helper.reference_price_kwh(days, BASE + timedelta(days=1), fallback=1.1) == 1.1
    # No days at all -> fallback.
    assert helper.reference_price_kwh([], BASE, fallback=0.7) == 0.7


def test_price_at_hour_lookup():
    prices = _pts([(0, 0.8), (1, 0.4), (2, 0.6)])
    assert helper.price_at(prices, BASE + timedelta(minutes=30)) == 0.8
    assert helper.price_at(prices, BASE + timedelta(hours=1, minutes=59)) == 0.4
    assert helper.price_at(prices, BASE + timedelta(days=2)) is None


def test_hour_key_is_utc_floor():
    assert helper.hour_key(BASE) == "2026-09-17T00:00"
    assert helper.hour_key(BASE + timedelta(hours=3, minutes=45)) == "2026-09-17T03:00"


def test_stats_serialization_roundtrip():
    stats = helper.start_stat_session(
        helper.accrue_charging(helper.ChargingStats(), 22.0, 0.6, 1.0, at=BASE)
    )
    restored = helper.stats_from_dict(helper.stats_to_dict(stats))
    assert restored.kwh == stats.kwh
    assert restored.cost == stats.cost
    assert restored.cost_at_ref == stats.cost_at_ref
    assert restored.sessions == stats.sessions
    assert restored.first_at == stats.first_at
    assert restored.last_at == stats.last_at
    # Tolerant of missing / malformed payloads.
    assert helper.stats_from_dict(None).kwh == 0.0
    assert helper.stats_from_dict({}).kwh == 0.0


def test_accrue_plan_hours_fully_past_once_and_idempotent():
    stats = helper.ChargingStats()
    accrued = set()
    partial = {}
    sessions = [
        _stat_session([0, 1], prices=[0.8, 0.4]),
    ]
    stats = helper.accrue_plan_hours(
        stats, sessions, BASE + timedelta(hours=2), [], None, accrued, partial
    )
    assert abs(stats.kwh - 22.0) < EPSILON
    assert abs(stats.cost - (11.0 * 0.8 + 11.0 * 0.4)) < EPSILON
    assert accrued == {helper.hour_key(BASE), helper.hour_key(BASE + timedelta(hours=1))}
    assert partial == {}

    # A later recompute must not double-count.
    again = helper.accrue_plan_hours(
        stats, sessions, BASE + timedelta(hours=2), [], None, accrued, partial
    )
    assert again.kwh == stats.kwh
    assert again.cost == stats.cost


def test_accrue_plan_hours_ongoing_minute_precise():
    stats = helper.ChargingStats()
    accrued = set()
    partial = {}
    sessions = [_stat_session([5])]

    stats = helper.accrue_plan_hours(
        stats, sessions, BASE + timedelta(hours=5, minutes=30), [], None, accrued, partial
    )
    assert abs(stats.kwh - 5.5) < EPSILON  # 11 kW * 0.5 h
    assert helper.hour_key(BASE + timedelta(hours=5)) in partial

    stats = helper.accrue_plan_hours(
        stats, sessions, BASE + timedelta(hours=5, minutes=45), [], None, accrued, partial
    )
    assert abs(stats.kwh - 8.25) < EPSILON  # 11 kW * 0.75 h

    # The hour passes: the remainder is closed and the key archived.
    stats = helper.accrue_plan_hours(
        stats, sessions, BASE + timedelta(hours=6, minutes=5), [], None, accrued, partial
    )
    assert abs(stats.kwh - 11.0) < EPSILON
    assert partial == {}
    assert helper.hour_key(BASE + timedelta(hours=5)) in accrued


def test_accrue_plan_hours_cut_hour():
    """A cut hour (duration < 1) accrues only the cut portion."""
    stats = helper.ChargingStats()
    accrued = set()
    partial = {}
    sessions = [_stat_session([8], durations=[0.25])]
    stats = helper.accrue_plan_hours(
        stats, sessions, BASE + timedelta(hours=8, minutes=20), [], None, accrued, partial
    )
    assert abs(stats.kwh - 2.75) < EPSILON  # 11 kW * 0.25 h
    assert helper.hour_key(BASE + timedelta(hours=8)) in accrued


def test_accrue_plan_hours_dropped_partial_is_closed():
    """An ongoing hour that vanishes from the plan stops accruing — nothing more."""
    stats = helper.ChargingStats()
    accrued = set()
    partial = {}
    sessions = [_stat_session([10])]
    stats = helper.accrue_plan_hours(
        stats, sessions, BASE + timedelta(hours=10, minutes=30), [], None, accrued, partial
    )
    assert len(partial) == 1
    assert abs(stats.kwh - 5.5) < EPSILON

    stats = helper.accrue_plan_hours(
        stats, [], BASE + timedelta(hours=10, minutes=40), [], None, accrued, partial
    )
    assert partial == {}
    assert abs(stats.kwh - 5.5) < EPSILON


def test_accrue_plan_hours_uses_day_reference_price():
    stats = helper.ChargingStats()
    accrued = set()
    partial = {}
    days = [helper.DayPrice("2026-09-17", 0.5, 1.3, 1.0)]
    sessions = [_stat_session([3], prices=[0.8])]
    stats = helper.accrue_plan_hours(
        stats, sessions, BASE + timedelta(hours=4), days, None, accrued, partial
    )
    assert abs(stats.cost_at_ref - 11.0) < EPSILON
    assert abs(stats.saved - 2.2) < EPSILON


# ---------------------------------------------------------------------------
# Continuous usage ("away-window") model
# ---------------------------------------------------------------------------


def test_usage_no_charging_while_away():
    """While the car is away the plan must never schedule charging."""
    plan = _cp(
        price_hours=_day_night_prices(days=2),
        usage_enabled=True,
        usage_days="weekdays",
        usage_away_start="07:00",
        usage_away_end="17:00",
    )
    assert plan.sessions
    for s in plan.sessions:
        for h in s.hours:
            assert not (7 <= h["start"].hour < 17)


def test_usage_ready_by_from_time():
    """The "from" time acts as the ready-by: charging is complete before it."""
    plan = _cp(
        price_hours=_day_night_prices(days=1, night=0.5, day=0.5),
        soc_now=0.0,
        min_soc=20.0,
        max_soc=100.0,
        usage_enabled=True,
        usage_days="all_days",
        usage_away_start="07:00",
        usage_away_end="17:00",
    )
    assert plan.sessions
    # No charging is ever scheduled inside the away window.
    for s in plan.sessions:
        for h in s.hours:
            assert not (7 <= h["start"].hour < 17)
    # The morning session is cut exactly at the leave time.
    assert plan.sessions[0].end == BASE + timedelta(hours=7)


def test_usage_weekend_home_all_day():
    """On a weekend the car is home all day: daytime hours become chargeable."""
    saturday = BASE + timedelta(days=2)
    plan = _cp(
        price_hours=_day_night_prices(days=3, night=1.4, day=0.5),
        soc_now=15.0,
        max_soc=100.0,
        usage_enabled=True,
        usage_days="weekdays",
        usage_away_start="07:00",
        usage_away_end="17:00",
        now=saturday.replace(hour=8),
    )
    day_hours = [
        h
        for s in plan.sessions
        for h in s.hours
        if 9 <= h["start"].hour < 17 and h["start"].weekday() >= 5
    ]
    assert day_hours


def test_usage_next_transition_is_leave_time():
    plan = _cp(
        price_hours=_day_night_prices(days=1),
        usage_enabled=True,
        usage_days="weekdays",
        usage_away_start="07:30",
        usage_away_end="17:00",
        now=_now(0),
    )
    assert plan.usage_next == BASE + timedelta(hours=7, minutes=30)


def test_usage_summary_shows_away_until():
    plan = _cp(
        price_hours=_day_night_prices(days=1),
        usage_enabled=True,
        usage_days="weekdays",
        usage_away_start="07:00",
        usage_away_end="17:00",
        soc_now=50.0,
        now=_now(10),
    )
    assert plan.summary.startswith("Borta t.o.m. 17:00")


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