"""Pure planning logic for the Smart Charging integration.

This module intentionally has NO Home Assistant imports so it can be unit
tested and dry-run outside of HA (see ``tests/test_helper.py`` and
``dryrun.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Optional


@dataclass(frozen=True)
class PriceHour:
    """One hourly price per kWh, in the configured currency.

    ``start`` is a timezone-aware UTC datetime, on the hour.
    """

    start: datetime
    price_kwh: float


@dataclass
class ChargingSession:
    """A contiguous block of planned charging hours."""

    start: datetime
    end: datetime  # exclusive end (start + 1h * len(hours))
    power_kw: float
    avg_price_kwh: float
    hours: list[dict] = field(default_factory=list)


@dataclass
class NextAction:
    """What the integration should do next."""

    action: str  # "resume" | "stop" | "none"
    at: Optional[datetime] = None  # when to act
    reason: str = ""  # cheap_window | threshold | deadline | disconnected | no_data | off


@dataclass
class Plan:
    """Complete plan output from ``compute_plan``."""

    sessions: list[ChargingSession] = field(default_factory=list)
    next_action: Optional[NextAction] = None
    summary: str = ""
    battery_need_kwh: Optional[float] = None
    updated: Optional[datetime] = None
    currency: str = "EUR"
_MODE_OFF = "off"
_MODE_PLAN = "plan"
_MODE_LIVE = "live"


def floor_hour(dt: datetime) -> datetime:
    """Round down to the start of the hour (preserving tzinfo)."""
    return dt.replace(minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_price_hours(
    raw_hours: list[dict], currency: str = "EUR"
) -> list[PriceHour]:
    """Parse a spot-price sensor's ``hours`` attribute into ``PriceHour``.

    The price is read from the generic ``currency_kwh`` key. If that key is
    absent, the per-currency key matching the *configured* currency is used
    (e.g. ``sek_kwh`` only when ``currency="SEK"``). There is deliberately no
    implicit fallback to any single currency: prices in a currency the user did
    not select are never trusted.

    ``start`` may be a naive ISO string (assumed UTC) or timezone-aware; the
    result is always normalised to a timezone-aware UTC datetime.
    """
    currency_key = f"{currency.lower()}_kwh"
    out: list[PriceHour] = []
    for hour in raw_hours:
        try:
            start = _parse_iso_dt(hour["start"])
            price = hour.get("currency_kwh")
            if price is None:
                price = hour.get(currency_key)
            price = float(price)
        except (KeyError, TypeError, ValueError):
            continue
        if start is None:
            continue
        out.append(PriceHour(start=start, price_kwh=price))
    return out


def compute_plan(
    *,
    price_hours: list[PriceHour],
    connected: bool,
    mode: str,
    threshold_start: float,
    threshold_stop: float,
    deadline: Optional[datetime],
    charger_max_kw: float,
    battery_need_kwh: Optional[float],
    now: datetime,
    currency: str = "EUR",
) -> Plan:
    """Produce a charging plan from price forecasts and configuration.

    Parameters
    ----------
    price_hours
        Hourly prices (future + recent past).
    connected
        Whether the charger reports a connection.
    mode
        ``"off"``, ``"plan"`` or ``"live"``.
    threshold_start
        Only *start* charging in hours at or below this price.
    threshold_stop
        Continue while price <= this threshold, stop the block above.
    deadline
        Optional deadline datetime.
    charger_max_kw
        Maximum charger power (kW).
    battery_need_kwh
        Energy needed (kWh). ``None`` means "charge all cheap hours".
    now
        Current time (timezone-aware).
    currency
        Configured price currency (e.g. ``"EUR"``). Used only for display —
        prices are never converted, they are treated as already being in this
        currency. Defaults to ``"EUR"``.

    Returns
    -------
    Plan
    """
    # --- Guard: off mode ---------------------------------------------------
    if mode == _MODE_OFF:
        return Plan(
            summary="Av",
            next_action=NextAction(action="none", reason="off"),
            currency=currency,
        )

    # --- Guard: not connected ----------------------------------------------
    if not connected:
        return Plan(
            summary="Ej inkopplad",
            next_action=NextAction(action="stop", reason="disconnected"),
            currency=currency,
        )

    # --- Guard: no price data ----------------------------------------------
    if not price_hours:
        return Plan(
            summary="Inga data",
            next_action=NextAction(action="stop", reason="no_data"),
            currency=currency,
        )

    now_hour = floor_hour(now)

    # Filter future hours (including the current hour).
    future = sorted(
        [h for h in price_hours if h.start >= now_hour],
        key=lambda h: h.start,
    )
    if not future:
        return Plan(
            summary="Inga data",
            next_action=NextAction(action="stop", reason="no_data"),
        )

    # Apply deadline filter.
    if deadline is not None:
        future = [h for h in future if h.start < deadline]
        if not future:
            return Plan(
                summary="Deadline passerad",
                next_action=NextAction(action="stop", reason="deadline"),
                currency=currency,
            )
# ------------------------------------------------------------------
    # 1. Build cheap blocks with hysteresis.
    #    Block starts at hour <= threshold_start, continues while
    #    price <= threshold_stop AND the hour is contiguous (exactly 1h after
    #    the previous selected hour).
    # ------------------------------------------------------------------
    raw_blocks: list[list[PriceHour]] = []
    current_block: list[PriceHour] = []
    in_block = False

    for h in future:
        if in_block:
            prev = current_block[-1]
            contiguous = prev.start + timedelta(hours=1) == h.start
            if contiguous and h.price_kwh <= threshold_stop:
                current_block.append(h)
                continue
            raw_blocks.append(current_block)
            current_block = []
            in_block = False
        if h.price_kwh <= threshold_start:
            current_block = [h]
            in_block = True

    if current_block:
        raw_blocks.append(current_block)

    # ------------------------------------------------------------------
    # 2. Deadline forcing — if remaining hours can't deliver the required
    #    energy, force-ON everything until the deadline.
    # ------------------------------------------------------------------
    forced_by_deadline = False
    if not raw_blocks and deadline is not None:
        if battery_need_kwh is not None and battery_need_kwh > 0:
            remaining_hours = len(future)
            needed_hours = (
                ceil(battery_need_kwh / charger_max_kw)
                if charger_max_kw > 0
                else remaining_hours
            )
            if needed_hours >= remaining_hours:
                raw_blocks = [future]
                forced_by_deadline = True

    if not raw_blocks:
        return Plan(
            summary="Ingen plan (för dyrt)",
            next_action=NextAction(action="stop", reason="threshold"),
            currency=currency,
        )
# ------------------------------------------------------------------
    # 3. Select blocks to satisfy battery need.
    # ------------------------------------------------------------------
    if battery_need_kwh is not None and battery_need_kwh > 0 and not forced_by_deadline:
        needed_kwh = battery_need_kwh
        sorted_blocks = sorted(
            raw_blocks,
            key=lambda b: sum(h.price_kwh for h in b) / max(len(b), 1),
        )
        selected: list[list[PriceHour]] = []
        accumulated = 0.0
        for block in sorted_blocks:
            block_kwh = len(block) * charger_max_kw
            if accumulated >= needed_kwh:
                break
            selected.append(block)
            accumulated += block_kwh
            if accumulated >= needed_kwh:
                excess_kwh = accumulated - needed_kwh
                excess_hours = (
                    ceil(excess_kwh / charger_max_kw) if charger_max_kw > 0 else 0
                )
                if excess_hours > 0 and len(selected[-1]) > excess_hours:
                    selected[-1] = selected[-1][:-excess_hours]
                break
        selections = selected
    else:
        selections = list(raw_blocks)

    if not selections:
        return Plan(
            summary="Ingen plan (för dyrt)",
            next_action=NextAction(action="stop", reason="threshold"),
            currency=currency,
        )

    # Sort selections by time.
    selections.sort(key=lambda b: b[0].start)
# ------------------------------------------------------------------
    # 4. Build ChargingSession objects.
    # ------------------------------------------------------------------
    sessions: list[ChargingSession] = []
    for block in selections:
        if not block:
            continue
        avg_price = sum(h.price_kwh for h in block) / len(block)
        start = block[0].start
        end = block[-1].start + timedelta(hours=1)
        sessions.append(
            ChargingSession(
                start=start,
                end=end,
                power_kw=charger_max_kw,
                avg_price_kwh=round(avg_price, 4),
                hours=[{"start": h.start, "price_kwh": h.price_kwh} for h in block],
            )
        )

    # ------------------------------------------------------------------
    # 5. Determine next_action.
    # ------------------------------------------------------------------
    next_action: Optional[NextAction] = None
    if sessions:
        first = sessions[0]
        last = sessions[-1]
        if first.start <= now_hour < first.start + timedelta(hours=1):
            next_action = NextAction(action="none", at=None, reason="charging")
        elif now_hour < first.start:
            reason = "deadline" if forced_by_deadline else "cheap_window"
            next_action = NextAction(action="resume", at=first.start, reason=reason)
        elif now_hour >= last.end:
            next_action = NextAction(action="stop", at=now_hour, reason="threshold")
        else:
            next_action = NextAction(action="none", at=None, reason="charging")
    else:
        next_action = NextAction(action="stop", reason="threshold")

    # ------------------------------------------------------------------
    # 6. Summary (human-readable state text).
    # ------------------------------------------------------------------
    summary = _build_summary(sessions, currency)

    return Plan(
        sessions=sessions,
        next_action=next_action,
        summary=summary,
        battery_need_kwh=battery_need_kwh,
        updated=now,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp string into a timezone-aware UTC datetime.

    A trailing ``Z`` (UTC) is accepted, and naive timestamps are assumed to be
    in UTC.
    """
    try:
        start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if start.tzinfo is None:
        return start.replace(tzinfo=timezone.utc)
    return start.astimezone(timezone.utc)


def _build_summary(sessions: list[ChargingSession], currency: str) -> str:
    """Short human-readable state text."""
    if not sessions:
        return "Ingen plan (för dyrt)"

    parts: list[str] = []
    for s in sessions[:2]:
        start_local = s.start.strftime("%H:%M")
        end_local = s.end.strftime("%H:%M")
        parts.append(
            f"Laddning {start_local}–{end_local} "
            f"({s.power_kw:.0f} kW, {s.avg_price_kwh:.2f} {currency}/kWh)"
        )
    if len(sessions) > 2:
        parts.append(f"+{len(sessions) - 2} till")
    return " / ".join(parts)