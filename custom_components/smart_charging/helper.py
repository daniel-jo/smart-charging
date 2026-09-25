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
    duration_hours: float = 1.0  # < 1.0 when a deadline cuts the hour short


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
    # Recurring time-of-day deadline (informative; resolution happens in
    # ``compute_plan``).
    deadline_time: Optional[str] = None
    deadline_next: Optional[datetime] = None
    deadline_restart_at: Optional[datetime] = None


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


def _truncate_at_deadline(
    hours: list[PriceHour], deadline: datetime
) -> list[PriceHour]:
    """Keep hours that start before ``deadline``; the containing hour is cut short."""
    out: list[PriceHour] = []
    for h in hours:
        if h.start >= deadline:
            continue
        end = h.start + timedelta(hours=1)
        if end <= deadline:
            out.append(h)
            continue
        duration = (deadline - h.start).total_seconds() / 3600.0
        if duration > 0:
            out.append(PriceHour(h.start, h.price_kwh, duration_hours=duration))
    return out


def compute_plan(
    *,
    price_hours: list[PriceHour],
    day_prices: Optional[list[DayPrice]] = None,
    connected: bool,
    mode: str,
    soc_now: Optional[float],
    min_soc: float,
    max_soc: float,
    daily_consumption_pct: float = 0.0,
    charger_max_kw: float = 11.0,
    battery_capacity_kwh: float = 77.0,
    weekly_full_charge: bool = False,
    last_full_charge: Optional[datetime] = None,
    min_days_between_full: float = 5.0,
    deadline: Optional[datetime] = None,
    deadline_time: Optional[str] = None,
    deadline_restart_minutes: float = 0.0,
    deadline_timezone: Optional[tzinfo] = None,
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
        Current battery level in percent (0-100). ``None`` if unavailable.
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
    deadline
        Optional legacy absolute deadline datetime — no charging planned at or
        after it (kept for backward compatibility).
    deadline_time
        Optional recurring time-of-day ``"HH:MM"`` (interpreted in
        ``deadline_timezone``). Charging is planned so it is *complete* before
        this time each day — minute precise: the hour that contains the
        deadline is cut short.
    deadline_restart_minutes
        Minutes to wait after a passed deadline before the plan may be
        recomputed against the *next* day's deadline. ``0`` (default) — after
        the deadline has passed, charging must not start again that day.
    deadline_timezone
        Timezone the recurring deadline is interpreted in (defaults to UTC).
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
            min_soc=min_soc,
            max_soc=max_soc,
            daily_consumption_pct=daily_consumption_pct,
            last_full_charge=last_full_charge,
            day_prices=list(day_prices) if day_prices else [],
            deadline_time=deadline_time or None,
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

    # --- Deadline -------------------------------------------------------------
    # Two modes:
    #  * ``deadline`` (legacy) — an absolute datetime; everything at/after it
    #    is cut.
    #  * ``deadline_time`` — a recurring time-of-day, minute precise. Once the
    #    deadline passes, charging is blocked; after ``deadline_restart_minutes``
    #    (``0`` = never) it re-arms and the plan runs against the *next* day's
    #    deadline.
    eff_deadline: Optional[datetime] = None
    tod = _parse_time_of_day(deadline_time)
    if tod is not None:
        tz = deadline_timezone if deadline_timezone is not None else timezone.utc
        local_now = now.astimezone(tz)
        today_dl = local_now.replace(
            hour=tod[0], minute=tod[1], second=0, microsecond=0
        )
        if today_dl > now:
            eff_deadline = today_dl
        else:
            restart_min = max(0.0, float(deadline_restart_minutes))
            rearm_at = today_dl + timedelta(minutes=restart_min)
            if restart_min > 0 and now >= rearm_at:
                eff_deadline = today_dl + timedelta(days=1)
            else:
                plan = _plan()
                plan.deadline_restart_at = rearm_at if restart_min > 0 else None
                plan.summary = "Deadline passerad"
                plan.next_action = NextAction(
                    action="stop", at=now_hour, reason="deadline"
                )
                return plan
    elif deadline is not None:
        eff_deadline = deadline

    if eff_deadline is not None:
        if tod is not None:
            future = _truncate_at_deadline(future, eff_deadline)
        else:
            future = [h for h in future if h.start < eff_deadline]
        if not future:
            plan = _plan()
            plan.summary = "Deadline passerad"
            plan.next_action = NextAction(action="stop", at=now_hour, reason="deadline")
            return plan

    plan = _plan()
    plan.deadline_next = eff_deadline
    n = len(future)

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
            hours_elapsed = (future[i].start - now_hour) / timedelta(hours=1)
            est = max(floor, soc - pct_cons_h * hours_elapsed)
            if est >= 100.0:
                continue
            # Smallest window whose *accumulated duration* reaches 100 % (the
            # final hour may be partial when a deadline cuts the horizon).
            need = 100.0 - est
            j = i
            acc = 0.0
            while j < n and acc * rate + 1e-9 < need:
                acc += future[j].duration_hours
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
    #   1. it is a boost hour (mandatory, capped at 100 %);
    #   2. the hour is cheap (<= auto cheap_cut) and the battery is below the
    #      normal cap (top-up, capped at max_soc);
    #   3. the floor is threatened before the horizon ends — then buy the
    #      cheapest hour inside the survival window (floor forcing), even if
    #      that hour is not "cheap".
    selected: list[PriceHour] = []

    def _charge_hour(hour: PriceHour, session_cap: float) -> None:
        """Append one charging hour (partial only when a deadline cuts it)."""
        nonlocal soc
        soc = min(session_cap, soc + rate * hour.duration_hours)
        selected.append(hour)

    i = 0
    while i < n:
        hour = future[i]
        is_boost_hour = hour.start in boost_starts
        session_cap = 100.0 if is_boost_hour else cap

        if is_boost_hour or (hour.price_kwh <= cheap_cut and soc < session_cap):
            _charge_hour(hour, session_cap)
            i += 1
            continue

        # Not charging this hour. Skip it — unless the floor would be breached
        # before the horizon ends without a charge.
        if (
            pct_cons_h > 0
            and soc < session_cap
            and (soc - floor) / pct_cons_h < (n - i)
        ):
            win_end = min(n, i + max(1, int((soc - floor) / pct_cons_h) + 1))
            if win_end > i:
                j = min(range(i, win_end), key=lambda k: future[k].price_kwh)
                if j > i:
                    # Cheapest usable hour is later — wait for it.
                    soc = max(floor, soc - pct_cons_h * (j - i))
                    i = j
                    continue
                # j == i — this is the cheapest hour we can use in time;
                # charge it even though it is above the cheap cut.
                _charge_hour(hour, session_cap)
                i += 1
                continue

        # Skip this hour: the battery drains a little, nothing to do.
        soc = max(floor, soc - pct_cons_h)
        i += 1

    # --- Sessions -------------------------------------------------------------
    sessions: list[ChargingSession] = []
    if selected:
        runs: list[list[PriceHour]] = []
        run = [selected[0]]
        for prev, cur in zip(selected, selected[1:]):
            if cur.start == prev.start + timedelta(hours=1):
                run.append(cur)
            else:
                runs.append(run)
                run = [cur]
        runs.append(run)
        for run in runs:
            is_boost = any(h.start in boost_starts for h in run)
            avg_price = round(mean(h.price_kwh for h in run), 4)
            sessions.append(
                ChargingSession(
                    start=run[0].start,
                    end=run[-1].start + timedelta(hours=run[-1].duration_hours),
                    power_kw=charger_max_kw,
                    avg_price_kwh=avg_price,
                    hours=[
                        {
                            "start": h.start,
                            "price_kwh": h.price_kwh,
                            "duration_hours": h.duration_hours,
                        }
                        for h in run
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
        # Minute-precise: a session may end at an arbitrary minute (deadline
        # cut or SOC cap), so compare against ``now``, not the floored hour.
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