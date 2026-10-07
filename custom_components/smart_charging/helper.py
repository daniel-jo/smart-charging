"""Pure planning logic for the Smart Charging integration.

This module intentionally has NO Home Assistant imports so it can be unit
tested and dry-run outside of HA (see ``tests/test_helper.py`` and
``dryrun.py``).

Planning model
--------------
The plan is built from the car's battery state instead of manual price
thresholds:

- ``soc_now`` (``%``) — current battery level, from the car integration.
- ``min_soc`` (``%``) — floor: the plan never lets the *projected* SOC go
  below this. If the battery would run dry before a cheap window, charging is
  forced at the cheapest hour within the survival window (floor forcing).
- ``max_soc`` (``%``) — normal upper target. Regular charging stops here,
  even mid-window (no overcharging to battery-healthy levels).
- ``daily_consumption_pct`` (``%/day``) — average battery use (e.g. the daily
  commute). Turns into a per-hour drain that decides *when* charging is
  actually needed.
- ``weekly_full_charge`` + ``min_days_between_full`` — schedule the weekly
  100 % boost: the cheapest contiguous window (on/after the cooldown bound)
  that tops the battery up to 100 %.

Price thresholds are *derived*, never configured:

- ``cheap_cut`` — bottom price quantile of the forecast (capped at the mean);
  hours at or below it are candidates for topping up to ``max_soc``.
- The plan also buys the cheapest hour inside the window the battery can
  survive before hitting the floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from math import ceil, isfinite
from statistics import mean
from typing import Any, Optional

_MODE_OFF = "off"
_MODE_PLAN = "plan"
_MODE_LIVE = "live"

# Share of the forecast hours treated as "cheap" for topping up.
CHEAP_QUANTILE = 0.25


@dataclass(frozen=True)
class PriceHour:
    """One hourly price per kWh, in the configured currency.

    ``start`` is a timezone-aware UTC datetime, on the hour.
    """

    start: datetime
    price_kwh: float
    duration_hours: float = 1.0  # < 1.0 when an away-window cut shortens the hour


@dataclass(frozen=True)
class DayPrice:
    """Per-day price summary (from the sensor's ``days`` attribute, if any)."""

    date: str
    min_kwh: Optional[float]
    max_kwh: Optional[float]
    avg_kwh: Optional[float]


@dataclass
class ChargingSession:
    """A contiguous block of planned charging hours."""

    start: datetime
    end: datetime  # exclusive end (start + 1h * len(hours))
    power_kw: float
    avg_price_kwh: float
    hours: list[dict] = field(default_factory=list)
    is_boost: bool = False  # True for the weekly 100 % boost window


@dataclass
class NextAction:
    """What the integration should do next."""

    action: str  # "resume" | "stop" | "none"
    at: Optional[datetime] = None  # when to act
    reason: str = ""


@dataclass
class Plan:
    """Complete plan output from ``compute_plan``."""

    sessions: list[ChargingSession] = field(default_factory=list)
    next_action: Optional[NextAction] = None
    summary: str = ""
    updated: Optional[datetime] = None
    currency: str = "SEK"
    # Battery state the plan was built from (exposed as sensor attributes).
    soc_now: Optional[float] = None
    # Where ``soc_now`` came from: "sensor" | "remembered" | "projected" | "none".
    soc_source: str = "sensor"
    min_soc: float = 0.0
    max_soc: float = 100.0
    daily_consumption_pct: float = 0.0
    # Derived price thresholds (informative only — never configured).
    threshold_start: Optional[float] = None
    threshold_stop: Optional[float] = None
    # Weekly full charge.
    last_full_charge: Optional[datetime] = None
    boost_scheduled: bool = False
    next_boost_after: Optional[datetime] = None
    # Per-day price stats for display (best-effort from `days` attribute).
    day_prices: list[DayPrice] = field(default_factory=list)
    # Continuous usage ("away-window") model — when enabled, replaces the
    # The car is away on the configured days in the
    # ``usage_away_start`` → ``usage_away_end`` window and drains only then.
    usage_enabled: bool = False
    usage_days: str = "weekdays"
    usage_away_start: str = ""
    usage_away_end: str = ""
    usage_next: Optional[datetime] = None


def floor_hour(dt: datetime) -> datetime:
    """Round down to the start of the hour (preserving tzinfo)."""
    return dt.replace(minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Price parsing — compact `hours` format: {"s": unix epoch, "p": price/kWh}
# ---------------------------------------------------------------------------


def _epoch_to_dt(value: Any) -> Optional[datetime]:
    """Convert a unix epoch (seconds or milliseconds) to a UTC datetime."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:  # milliseconds
        ts /= 1000.0
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_price_hours(raw_hours: list[dict]) -> list[PriceHour]:
    """Parse a spot-price sensor's ``hours`` attribute into ``PriceHour``.

    Each entry is ``{"s": <unix epoch seconds|ms>, "p": <price per kWh>}``.
    Unparseable entries are skipped; results are normalised to
    timezone-aware UTC datetimes, on the hour.
    """
    out: list[PriceHour] = []
    for hour in raw_hours:
        if not isinstance(hour, dict):
            continue
        start = _epoch_to_dt(hour.get("s"))
        if start is None:
            continue
        try:
            price = float(hour.get("p"))
        except (TypeError, ValueError):
            continue
        out.append(PriceHour(start=start, price_kwh=price))
    return out


def parse_days(raw_days: list[dict]) -> list[DayPrice]:
    """Best-effort parse of the sensor's per-day ``days`` attribute."""
    out: list[DayPrice] = []
    for day in raw_days:
        if not isinstance(day, dict):
            continue

        def _num(key: str) -> Optional[float]:
            try:
                return float(day[key])
            except (KeyError, TypeError, ValueError):
                return None

        out.append(
            DayPrice(
                date=str(day.get("date", "")),
                min_kwh=_num("min_kwh"),
                max_kwh=_num("max_kwh"),
                avg_kwh=_num("avg_kwh"),
            )
        )
    return out


def _parse_time_of_day(value: Optional[str]) -> Optional[tuple[int, int]]:
    """Parse ``"HH:MM"`` (or ``"H:MM"`` / ``"HH:MM:SS"``) into (hour, minute)."""
    if not value:
        return None
    try:
        parts = str(value).strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return (hour, minute)


def compute_plan(
    *,
    price_hours: list[PriceHour],
    day_prices: Optional[list[DayPrice]] = None,
    connected: bool,
    mode: str,
    soc_now: Optional[float],
    soc_source: str = "sensor",
    min_soc: float,
    max_soc: float,
    daily_consumption_pct: float = 0.0,
    charger_max_kw: float = 11.0,
    battery_capacity_kwh: float = 77.0,
    weekly_full_charge: bool = False,
    last_full_charge: Optional[datetime] = None,
    min_days_between_full: float = 5.0,
    # --- continuous usage ("away-window") model -------------------------------
    usage_enabled: bool = False,
    usage_days: str = "weekdays",  # "weekdays" | "all_days"
    usage_away_start: str = "",  # "HH:MM" — car leaves (the new "ready by")
    usage_away_end: str = "",  # "HH:MM" — car returns
    usage_timezone: Optional[tzinfo] = None,
    now: datetime,
    currency: str = "SEK",
) -> Plan:
    """Produce a charging plan from price forecasts and car battery state.

    Parameters
    ----------
    price_hours
        Hourly prices (future + recent past), UTC, on the hour.
    day_prices
        Optional per-day price summaries, used for display only.
    connected
        Whether the charger reports a connection.
    mode
        ``"off"``, ``"plan"`` or ``"live"``.
    soc_now
        Current battery level in percent (0-100). ``None`` if unavailable —
        the coordinator may substitute the last known reading (projected
        forward through the away-window) so the plan survives commuting gaps.
    soc_source
        Where ``soc_now`` came from: ``"sensor"`` | ``"remembered"`` |
        ``"projected"``. Exposed as a sensor attribute for transparency.
    min_soc
        Battery floor (percent). The plan never lets the projected SOC drop
        below this — charging is forced when needed.
    max_soc
        Normal upper target (percent). Regular charging stops here.
    daily_consumption_pct
        Average battery use per day (percent of the battery).
    charger_max_kw
        Maximum charger power (kW).
    battery_capacity_kwh
        Battery capacity (kWh) — converts charging power (kW) to SOC percent.
    weekly_full_charge
        Allow the weekly 100 % boost.
    last_full_charge
        Timestamp of the last 100 % charge (cooldown anchor).
    min_days_between_full
        Minimum days between two 100 % charges.
    usage_enabled
        When ``True``, the plan uses the "continuous usage"/away-window model:
        on the selected days (``usage_days``) the car is *away* between
        ``usage_away_start`` and ``usage_away_end``. It can only charge while
        home (``usage_away_end`` → ``usage_away_start``) and drains the battery
        only while away. ``usage_away_start`` acts as the "ready by" —
        charging must be complete before it. ``daily_consumption_pct`` is
        spread over the away hours.
    usage_days
        ``"weekdays"`` (default) — the away-window applies Mon–Fri only; on
        weekends the car is home all day (no drain). ``"all_days"``
        — the window applies every day.
    usage_away_start / usage_away_end
        Times of day (``"HH:MM"``) when the car leaves and returns, interpreted
        in ``usage_timezone``. ``usage_away_start`` is the new "ready by".
    usage_timezone
        Timezone the away-window is interpreted in (defaults to UTC).
    now
        Current time (timezone-aware).
    currency
        Configured price currency, used only for display — prices are never
        converted.

    Returns
    -------
    Plan
    """

    def _plan() -> Plan:
        return Plan(
            updated=now,
            currency=currency,
            soc_now=soc_now,
            soc_source=soc_source,
            min_soc=min_soc,
            max_soc=max_soc,
            daily_consumption_pct=daily_consumption_pct,
            last_full_charge=last_full_charge,
            day_prices=list(day_prices) if day_prices else [],
            usage_enabled=usage_enabled,
            usage_days=usage_days,
            usage_away_start=usage_away_start if usage_enabled else "",
            usage_away_end=usage_away_end if usage_enabled else "",
        )

    # --- Guards -------------------------------------------------------------
    if mode == _MODE_OFF:
        plan = _plan()
        plan.summary = "Av"
        plan.next_action = NextAction(action="none", reason="off")
        return plan

    # A plan is pure information — only Live mode is allowed to write to the
    # charger, so every other mode may preview the plan even when the car is
    # not connected. Live keeps the guard so it never acts on a detached car.
    if not connected and mode == _MODE_LIVE:
        plan = _plan()
        plan.summary = "Ej inkopplad"
        plan.next_action = NextAction(action="stop", reason="disconnected")
        return plan

    if not price_hours:
        plan = _plan()
        plan.summary = "Inga data"
        plan.next_action = NextAction(action="stop", reason="no_data")
        return plan

    if soc_now is None or not isfinite(soc_now):
        plan = _plan()
        plan.summary = "Ingen SOC-data"
        plan.soc_source = "none"
        plan.next_action = NextAction(action="stop", reason="no_soc")
        return plan

    if battery_capacity_kwh <= 0 or charger_max_kw <= 0:
        plan = _plan()
        plan.summary = "Felaktig konfiguration"
        plan.next_action = NextAction(action="stop", reason="no_data")
        return plan

    soc = max(0.0, min(100.0, soc_now))
    floor = max(0.0, min(100.0, min_soc))
    cap = max(0.0, min(100.0, max_soc))
    if floor > cap:
        plan = _plan()
        plan.summary = "Felaktig konfiguration"
        plan.next_action = NextAction(action="stop", reason="no_data")
        return plan

    rate = charger_max_kw / battery_capacity_kwh * 100.0  # SOC % per hour
    pct_cons_h = max(0.0, daily_consumption_pct) / 24.0  # SOC % per hour

    now_hour = floor_hour(now)
    future = sorted(
        [h for h in price_hours if h.start >= now_hour],
        key=lambda h: h.start,
    )
    if not future:
        plan = _plan()
        plan.summary = "Inga data"
        plan.next_action = NextAction(action="stop", reason="no_data")
        return plan

    plan = _plan()
    n = len(future)

    # --- Away-window availability & drain profile ("continuous usage") --------
    # When enabled, the car can charge only while home (usage_away_end →
    # usage_away_start on the selected days) and drains only while away, so the
    # constant per-hour drain (pct_cons_h) is replaced by a per-hour profile.
    # When disabled the profile degenerates to the legacy behaviour: every hour
    # chargeable at its full duration, uniform drain.
    usage_next: Optional[datetime] = None
    away_start_min = away_end_min = 0
    away_minutes = 0
    if usage_enabled:
        away_start_hm = _parse_time_of_day(usage_away_start)
        away_end_hm = _parse_time_of_day(usage_away_end)
        if away_start_hm is not None and away_end_hm is not None:
            away_start_min = away_start_hm[0] * 60 + away_start_hm[1]
            away_end_min = away_end_hm[0] * 60 + away_end_hm[1]
            away_minutes = (away_start_min - away_end_min) % (24 * 60)
            tz = usage_timezone if usage_timezone is not None else timezone.utc
            usage_next = _usage_next_transition(
                now, tz, usage_days, away_start_min, away_end_min
            )

    if usage_enabled and away_minutes > 0:
        tz = usage_timezone if usage_timezone is not None else timezone.utc
        drain_per_min = max(0.0, daily_consumption_pct) / away_minutes  # SOC %/min
        charge_frac = [0.0] * n
        drain = [0.0] * n
        for idx, h in enumerate(future):
            local = h.start.astimezone(tz)
            if not _usage_day_applies(usage_days, local):
                charge_frac[idx] = 1.0
                drain[idx] = 0.0
                continue
            minutes_of_day = local.hour * 60 + local.minute
            away_frac = _usage_away_fraction(minutes_of_day, away_start_min, away_end_min)
            charge_frac[idx] = 1.0 - away_frac
            drain[idx] = drain_per_min * away_frac * 60.0
    else:
        charge_frac = [h.duration_hours for h in future]
        drain = [pct_cons_h] * n

    plan.usage_next = usage_next

    # Prefix sums of the drain (SOC %): drain_cum[k] = sum(drain[:k]).
    drain_cum = [0.0] * (n + 1)
    for _k in range(n):
        drain_cum[_k + 1] = drain_cum[_k] + drain[_k]

    # --- Weekly 100 % boost window -----------------------------------------
    # Eligible on/after `bound` (cooldown since the last full charge). Picks
    # the cheapest *contiguous* stretch long enough to reach 100 %, so a
    # cheap mid-week day beats an expensive weekend, but never more often
    # than `min_days_between_full`.
    boost_range: Optional[range] = None
    if weekly_full_charge and min_days_between_full > 0 and rate > 0:
        bound = now_hour
        if last_full_charge is not None:
            bound = max(
                bound, last_full_charge + timedelta(days=float(min_days_between_full))
            )
        plan.next_boost_after = bound
        starts = [i for i in range(n) if future[i].start >= bound]
        best: Optional[tuple[float, int, int]] = None
        for i in starts:
            est = max(floor, soc - drain_cum[i])
            if est >= 100.0:
                continue
            # Smallest window whose *accumulated chargeable duration* reaches
            # 100 % (the final hour may be partial on an away-window cut).
            need = 100.0 - est
            j = i
            acc = 0.0
            while j < n and acc * rate + 1e-9 < need:
                acc += charge_frac[j]
                j += 1
            if acc * rate + 1e-9 < need:
                continue
            window = future[i:j]
            avg = sum(h.price_kwh for h in window) / (j - i)
            if best is None or avg < best[0]:
                best = (avg, i, j)
        if best is not None:
            _, start_idx, end_idx = best
            boost_range = range(start_idx, end_idx)
            plan.boost_scheduled = True

    # --- Auto cheap threshold (documented, never configured) ----------------
    prices_sorted = sorted(h.price_kwh for h in future)
    quant_idx = max(0, ceil(len(prices_sorted) * CHEAP_QUANTILE) - 1)
    cheap_cut = min(
        prices_sorted[min(len(prices_sorted) - 1, quant_idx)],
        mean(prices_sorted),
    )

    boost_starts = (
        {future[i].start for i in boost_range} if boost_range is not None else set()
    )


    # --- Chronological greedy ------------------------------------------------
    # Walk the forecast hour by hour. Charge when:
    #   1. it is a boost hour (mandatory, capped at 100 %) and the car is home;
    #   2. the hour is cheap (<= auto cheap_cut), the car is home and the battery
    #      is below the normal cap (top-up, capped at max_soc);
    #   3. the floor is threatened before a chargeable hour is available — then
    #      buy the cheapest chargeable hour inside the survival window (floor
    #      forcing), even if that hour is not "cheap".
    selected: list[PriceHour] = []
    selected_frac: list[float] = []

    def _charge_hour(hour: PriceHour, session_cap: float, frac: float) -> None:
        """Append one charging hour (partial on an away-window cut)."""
        nonlocal soc
        soc = min(session_cap, soc + rate * frac)
        selected.append(hour)
        selected_frac.append(frac)

    def _drain_span(i: int, j: int) -> float:
        """Cumulative SOC % drained over future hours [i, j)."""
        return drain_cum[j] - drain_cum[i]

    i = 0
    while i < n:
        hour = future[i]
        is_boost_hour = hour.start in boost_starts
        session_cap = 100.0 if is_boost_hour else cap

        if charge_frac[i] > 1e-9 and (
            is_boost_hour or (hour.price_kwh <= cheap_cut and soc < session_cap)
        ):
            _charge_hour(hour, session_cap, charge_frac[i])
            i += 1
            continue

        # Not charging this hour. Skip it — unless the floor would be breached
        # before any chargeable hour is available (the car may be away).
        if (
            soc < session_cap
            and drain_cum[i] < drain_cum[n]
            and drain_cum[n] - drain_cum[i] > (soc - floor) + 1e-9
        ):
            win_end = i + 1
            while win_end <= n and soc - _drain_span(i, win_end) >= floor - 1e-9:
                win_end += 1
            # Cheapest hour the car can actually charge within the survival window.
            j = min(
                (k for k in range(i, win_end) if charge_frac[k] > 1e-9),
                key=lambda k: future[k].price_kwh,
                default=None,
            )
            if j is not None:
                if j > i:
                    # Cheapest usable hour is later — wait for it (drain the way).
                    soc = max(floor, soc - _drain_span(i, j))
                    i = j
                    continue
                # j == i — cheapest hour we can use in time; charge it even though
                # it is above the cheap cut.
                _charge_hour(hour, session_cap, charge_frac[i])
                i += 1
                continue

        # Skip this hour: the battery drains a little, nothing to do.
        soc = max(floor, soc - drain[i])
        i += 1

    # --- Sessions -------------------------------------------------------------
    sessions: list[ChargingSession] = []
    if selected:
        runs: list[list[int]] = []
        run = [0]
        for k in range(1, len(selected)):
            if selected[k].start == selected[k - 1].start + timedelta(hours=1):
                run.append(k)
            else:
                runs.append(run)
                run = [k]
        runs.append(run)
        for run in runs:
            run_hours = [selected[k] for k in run]
            is_boost = any(h.start in boost_starts for h in run_hours)
            avg_price = round(mean(h.price_kwh for h in run_hours), 4)
            total_h = sum(selected_frac[k] for k in run)
            sessions.append(
                ChargingSession(
                    start=run_hours[0].start,
                    end=run_hours[0].start + timedelta(hours=total_h),
                    power_kw=charger_max_kw,
                    avg_price_kwh=avg_price,
                    hours=[
                        {
                            "start": selected[k].start,
                            "price_kwh": selected[k].price_kwh,
                            "duration_hours": selected_frac[k],
                        }
                        for k in run
                    ],
                    is_boost=is_boost,
                )
            )

    plan.sessions = sessions
    if selected:
        plan.threshold_start = min(h.price_kwh for h in selected)
        plan.threshold_stop = max(h.price_kwh for h in selected)

    # --- Next action & summary -------------------------------------------------
    if not sessions:
        if soc >= cap - 1e-9 and not plan.boost_scheduled:
            plan.summary = f"Redan laddad ({soc:.0f} %)"
            plan.next_action = NextAction(action="stop", at=now_hour, reason="complete")
        else:
            plan.summary = "Ingen plan"
            plan.next_action = NextAction(action="stop", at=now_hour, reason="no_schedule")
    else:
        plan.summary = _build_summary(sessions, currency)
        first = sessions[0]
        last = sessions[-1]
        # Minute-precise: a session may end at an arbitrary minute
        # (away-window cut or SOC cap), so compare against ``now``, not the floored hour.
        if first.start <= now < first.end:
            plan.next_action = NextAction(
                action="none",
                reason="boost" if first.is_boost else "charging",
            )
        elif now < first.start:
            plan.next_action = NextAction(
                action="resume",
                at=first.start,
                reason="boost" if first.is_boost else "cheap_window",
            )
        elif now_hour >= last.end:
            plan.next_action = NextAction(action="stop", at=now_hour, reason="complete")
        else:
            plan.next_action = NextAction(action="stop", at=now_hour, reason="gap")

    # Continuous usage: when the car is currently away, tell when it returns.
    if usage_enabled and away_minutes > 0:
        tz = usage_timezone if usage_timezone is not None else timezone.utc
        if _usage_is_away(now, tz, usage_days, away_start_min, away_end_min):
            until = usage_next.strftime("%H:%M") if usage_next is not None else ""
            prefix = f"Borta t.o.m. {until}" if until else "Borta"
            plan.summary = f"{prefix} — {plan.summary}" if plan.summary else prefix

    return plan


def planned_hours(sessions: list[ChargingSession]) -> list[dict]:
    """Flatten planned sessions into per-hour chart points.

    One entry per selected hour, compact: ``{"s": <unix epoch seconds>,
    "p": <price/kWh>, "kw": <charging power kW>, "b": <1 if boost else 0>}``.
    Uses the same ``{"s", "p"}`` shape as the spot-price forecast sensor so
    any chart card (ApexCharts, a custom card, ...) can plot the plan over
    time — including future hours — straight from the sensor attributes.
    """
    out: list[dict] = []
    for sess in sessions:
        for hour in sess.hours:
            start = hour.get("start")
            out.append(
                {
                    "s": int(start.timestamp()) if start is not None else 0,
                    "p": hour.get("price_kwh", 0.0),
                    "kw": sess.power_kw,
                    "b": 1 if sess.is_boost else 0,
                }
            )
    return sorted(out, key=lambda pt: pt["s"])


# ---------------------------------------------------------------------------
# Charging statistics — energy, cost and savings (no HA imports)
#
# These are pure accumulators used by the coordinator for both the simulated
# Plan-mode ledger and the measured Live-mode ledger. The coordinator decides
# *when* to accrue (which planned hours passed / how much the energy sensor
# measured); this module only does the math.
# ---------------------------------------------------------------------------


@dataclass
class ChargingStats:
    """Accumulated charging statistics for one mode (plan / live).

    ``cost`` is what was paid (or would have been paid): the sum of
    ``price_kwh * kwh`` over every accrued charging hour. ``cost_at_ref`` is
    what the same energy would have cost at the *reference* price (the
    day's average) — the difference is ``saved``. ``sessions`` counts separate
    charging windows (plan-mode test runs / live charging sessions), not
    individual accrual events.
    """

    kwh: float = 0.0
    cost: float = 0.0
    cost_at_ref: float = 0.0
    sessions: int = 0
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None

    @property
    def avg_price_kwh(self) -> Optional[float]:
        """Energy-weighted average price paid (currency per kWh)."""
        if self.kwh <= 1e-9:
            return None
        return self.cost / self.kwh

    @property
    def saved(self) -> Optional[float]:
        """Money saved versus the reference price (negative = lost)."""
        if self.kwh <= 1e-9:
            return None
        return self.cost_at_ref - self.cost


def accrue_charging(
    stats: ChargingStats,
    kwh: float,
    price_kwh: float,
    ref_price_kwh: float,
    at: Optional[datetime] = None,
) -> ChargingStats:
    """Return a new ``ChargingStats`` with ``kwh`` accrued at the given prices.

    ``price_kwh`` is what was (or would have been) paid per kWh,
    ``ref_price_kwh`` the reference the savings are measured against. Negative
    ``kwh`` is ignored — energy is never un-accrued. ``at`` marks the accrual
    timestamp (first/last are kept for display).
    """
    if kwh <= 0:
        return stats
    return ChargingStats(
        kwh=stats.kwh + kwh,
        cost=stats.cost + kwh * price_kwh,
        cost_at_ref=stats.cost_at_ref + kwh * ref_price_kwh,
        sessions=stats.sessions,
        first_at=stats.first_at if stats.first_at is not None else at,
        last_at=at,
    )


def start_stat_session(stats: ChargingStats) -> ChargingStats:
    """Mark a new charging window (increments the session counter)."""
    return ChargingStats(
        kwh=stats.kwh,
        cost=stats.cost,
        cost_at_ref=stats.cost_at_ref,
        sessions=stats.sessions + 1,
        first_at=stats.first_at,
        last_at=stats.last_at,
    )


def reference_price_kwh(
    day_prices: list[DayPrice],
    at: datetime,
    fallback: Optional[float] = None,
    local_tz: Optional[tzinfo] = None,
) -> Optional[float]:
    """Day-average reference price for ``at`` (best-effort).

    Looks the forecast sensor's ``days`` attribute up by date; ``local_tz``
    (the HA timezone) determines which date an instantaneous ``at`` belongs to.
    Falls back to ``fallback`` (e.g. the mean of all forecast prices) when the
    day is missing.
    """
    local = at.astimezone(local_tz) if local_tz is not None else at
    day_str = local.strftime("%Y-%m-%d")
    for day in day_prices:
        if day.date == day_str and day.avg_kwh is not None:
            return day.avg_kwh
    return fallback


def price_at(price_hours: list[PriceHour], at: datetime) -> Optional[float]:
    """Price per kWh of the forecast hour containing ``at``.

    The price sensor's hours sit on the hour, so the returned price applies to
    the whole clock hour. Returns ``None`` when the hour is outside the
    forecast (e.g. after a long outage).
    """
    hour_start = floor_hour(at)
    for hour in price_hours:
        if hour.start == hour_start:
            return hour.price_kwh
    return None


def hour_key(dt: datetime) -> str:
    """Stable UTC string key identifying an hourly charging slot."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00")


def _parse_optional_dt(value: Any) -> Optional[datetime]:
    """Best-effort parse of an ISO timestamp to an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def accrue_plan_hours(
    stats: ChargingStats,
    sessions: list[ChargingSession],
    now: datetime,
    day_prices: list[DayPrice],
    fallback_ref: Optional[float],
    accrued_hours: set,
    partial_hours: dict,
    local_tz: Optional[tzinfo] = None,
) -> ChargingStats:
    """Accrue the plan's elapsed hours into ``stats`` (Plan-mode simulation).

    Fully-past planned hours are accrued whole (``power_kw * duration``);
    the currently ongoing hour is accrued minute-precise through
    ``partial_hours`` (``{"until": <ISO>, "price": ..., "power": ...}`` per
    hourly key) so the simulated total grows in real time. Hour keys in
    ``accrued_hours`` are never accrued twice, which keeps the totals stable
    across recomputes and restarts.

    ``accrued_hours`` and ``partial_hours`` are updated in place; a new
    ``ChargingStats`` is returned (or the same instance when nothing accrued).
    """
    ongoing_keys = set()
    for sess in sessions:
        power = sess.power_kw or 0.0
        for hour in sess.hours:
            start: datetime = hour["start"]
            duration = float(hour.get("duration_hours", 1.0))
            end = start + timedelta(hours=duration)
            key = hour_key(start)
            price = float(hour["price_kwh"])
            if end <= now:
                # Fully-past hour.
                if key in accrued_hours:
                    continue
                partial = partial_hours.pop(key, None)
                if partial is not None:
                    # Close the minute-precise track: accrue the remainder.
                    until = _parse_optional_dt(partial.get("until"))
                    price = float(partial.get("price", price))
                    power = float(partial.get("power", power))
                    if until is not None and until < end:
                        kwh = power * ((end - until).total_seconds() / 3600.0)
                        if kwh > 1e-9:
                            ref = reference_price_kwh(
                                day_prices, until, fallback_ref, local_tz
                            )
                            stats = accrue_charging(stats, kwh, price, ref or 0.0, at=until)
                else:
                    kwh = power * duration
                    if kwh > 1e-9:
                        ref = reference_price_kwh(
                            day_prices, start, fallback_ref, local_tz
                        )
                        stats = accrue_charging(stats, kwh, price, ref or 0.0, at=start)
                accrued_hours.add(key)
            elif start <= now < end:
                # Ongoing hour: accrue minute-precise up to now.
                ongoing_keys.add(key)
                partial = partial_hours.get(key)
                accrue_from = start
                if partial is not None:
                    parsed = _parse_optional_dt(partial.get("until"))
                    if parsed is not None and parsed > accrue_from:
                        accrue_from = parsed
                    price = float(partial.get("price", price))
                    power = float(partial.get("power", power))
                if now > accrue_from:
                    kwh = power * ((now - accrue_from).total_seconds() / 3600.0)
                    if kwh > 1e-9:
                        ref = reference_price_kwh(
                            day_prices, accrue_from, fallback_ref, local_tz
                        )
                        stats = accrue_charging(stats, kwh, price, ref or 0.0, at=accrue_from)
                partial_hours[key] = {
                    "until": now.isoformat(),
                    "price": price,
                    "power": power,
                }

    # Hours that vanished from the plan mid-window: stop at the last accrued
    # point (the simulation reports what the plan intended at the time).
    for key in [k for k in partial_hours if k not in ongoing_keys]:
        partial_hours.pop(key, None)
    return stats


def stats_to_dict(stats: ChargingStats) -> dict:
    """Serialise a ``ChargingStats`` for the HA storage helper."""
    return {
        "kwh": stats.kwh,
        "cost": stats.cost,
        "cost_at_ref": stats.cost_at_ref,
        "sessions": stats.sessions,
        "first_at": stats.first_at.isoformat() if stats.first_at else None,
        "last_at": stats.last_at.isoformat() if stats.last_at else None,
    }


def stats_from_dict(data: Optional[dict]) -> ChargingStats:
    """Rebuild a ``ChargingStats`` from ``stats_to_dict`` output (or ``None``)."""
    if not isinstance(data, dict):
        return ChargingStats()

    def _ts(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    return ChargingStats(
        kwh=float(data.get("kwh") or 0.0),
        cost=float(data.get("cost") or 0.0),
        cost_at_ref=float(data.get("cost_at_ref") or 0.0),
        sessions=int(data.get("sessions") or 0),
        first_at=_ts(data.get("first_at")),
        last_at=_ts(data.get("last_at")),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_summary(sessions: list[ChargingSession], currency: str) -> str:
    """Short human-readable state text."""
    if not sessions:
        return "Ingen plan"

    parts: list[str] = []
    for s in sessions[:2]:
        start_local = s.start.strftime("%H:%M")
        end_local = s.end.strftime("%H:%M")
        if s.is_boost:
            day = s.start.strftime("%a %d/%m")
            parts.append(
                f"100 %-laddning {day} {start_local}-{end_local} "
                f"({s.power_kw:.0f} kW, {s.avg_price_kwh:.2f} {currency}/kWh)"
            )
        else:
            parts.append(
                f"Laddning {start_local}-{end_local} "
                f"({s.power_kw:.0f} kW, {s.avg_price_kwh:.2f} {currency}/kWh)"
            )
    if len(sessions) > 2:
        parts.append(f"+{len(sessions) - 2} till")
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# Continuous usage ("away-window") helpers
# ---------------------------------------------------------------------------


def _usage_day_applies(usage_days: str, local_dt: datetime) -> bool:
    """True when the away-window applies on the day of ``local_dt``."""
    return usage_days == "all_days" or local_dt.weekday() < 5


def _usage_away_fraction(
    minutes_of_day: int, away_start_min: int, away_end_min: int
) -> float:
    """Fraction of the hour starting at ``minutes_of_day`` that the car is away.

    The away window is ``[away_start, away_end)`` and may cross midnight
    (e.g. away 22:00→06:00, or the common day window 07:00→17:00).
    """
    if away_start_min == away_end_min:
        return 0.0
    if away_start_min < away_end_min:
        intervals = [(away_start_min, away_end_min)]
    else:
        intervals = [(away_start_min, 24 * 60), (0, away_end_min)]
    total = 0.0
    for a, b in intervals:
        total += max(0.0, min(minutes_of_day + 60, b) - max(minutes_of_day, a))
    return total / 60.0


def _usage_is_away(
    now: datetime,
    tz: tzinfo,
    usage_days: str,
    away_start_min: int,
    away_end_min: int,
) -> bool:
    """True when ``now`` falls inside the away-window on an applicable day."""
    local = now.astimezone(tz)
    if not _usage_day_applies(usage_days, local):
        return False
    if away_start_min == away_end_min:
        return False
    minutes_of_day = local.hour * 60 + local.minute
    if away_start_min < away_end_min:
        return away_start_min <= minutes_of_day < away_end_min
    return minutes_of_day >= away_start_min or minutes_of_day < away_end_min


def _usage_next_transition(
    now: datetime,
    tz: tzinfo,
    usage_days: str,
    away_start_min: int,
    away_end_min: int,
) -> Optional[datetime]:
    """Next moment (leave or return) the away-window state changes, or ``None``.

    Used by the coordinator to recompute exactly at the transition.
    """
    local_now = now.astimezone(tz)
    candidates: list[datetime] = []
    for offset in range(14):
        day = local_now + timedelta(days=offset)
        if not _usage_day_applies(usage_days, day):
            continue
        for minutes in (away_start_min, away_end_min):
            candidate = day.replace(
                hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0
            )
            if candidate > now:
                candidates.append(candidate)
    return min(candidates) if candidates else None


def _away_minutes_between(
    start: datetime,
    end: datetime,
    tz: tzinfo,
    usage_days: str,
    away_start_min: int,
    away_end_min: int,
) -> float:
    """Away-minutes spent in ``[start, end)`` across days and midnight.

    Mirrors the away-window the forecast drain model uses (day-filtered
    ``[away_start, away_end)``, which may cross midnight). Pure — the
    projection of a stale SOC reading forward through a commuting gap.
    """
    if end <= start or away_start_min == away_end_min:
        return 0.0
    if away_start_min < away_end_min:
        spans = [(away_start_min, away_end_min)]
    else:
        spans = [(away_start_min, 24 * 60), (0, away_end_min)]
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    total = 0.0
    day = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < local_end:
        if _usage_day_applies(usage_days, day):
            for a_min, b_min in spans:
                span_start = day + timedelta(minutes=a_min)
                span_end = day + timedelta(minutes=b_min)
                lo = max(span_start, local_start)
                hi = min(span_end, local_end)
                if hi > lo:
                    total += (hi - lo).total_seconds() / 60.0
        day += timedelta(days=1)
    return total


def project_soc(
    last_soc: float,
    last_seen: datetime,
    now: datetime,
    *,
    usage_enabled: bool = False,
    usage_days: str = "weekdays",
    usage_away_start: str = "",
    usage_away_end: str = "",
    daily_consumption_pct: float = 0.0,
    usage_timezone: Optional[tzinfo] = None,
) -> float:
    """Project a stale SOC reading forward to ``now``.

    Subtracts the modelled drain accrued between ``last_seen`` and ``now``
    (the daily consumption spread over the away-minutes — the same model the
    forecast uses). Without a usable away-window the reading is returned
    unchanged. Clamped at 0; charging while home cannot raise the reading, so
    the projection never exceeds ``last_soc``.
    """
    soc = max(0.0, last_soc)
    if now <= last_seen or daily_consumption_pct <= 0:
        return soc
    away_start = _parse_time_of_day(usage_away_start)
    away_end = _parse_time_of_day(usage_away_end)
    if not usage_enabled or away_start is None or away_end is None:
        return soc
    away_start_min = away_start[0] * 60 + away_start[1]
    away_end_min = away_end[0] * 60 + away_end[1]
    if away_start_min == away_end_min:
        return soc
    tz = usage_timezone if usage_timezone is not None else timezone.utc
    # Same divisor the forecast drain model uses (see compute_plan) — mirrored
    # on purpose so the projected start and the forecast drain agree exactly.
    away_minutes = (away_start_min - away_end_min) % (24 * 60)
    if away_minutes <= 0:
        return soc
    drain_per_min = max(0.0, daily_consumption_pct) / away_minutes  # SOC %/min
    elapsed = _away_minutes_between(
        last_seen, now, tz, usage_days, away_start_min, away_end_min
    )
    return max(0.0, soc - drain_per_min * elapsed)