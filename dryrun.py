#!/usr/bin/env python3
"""Standalone dry-run of the smart charging planner.

Loads a price JSON file (or falls back to a built-in sample) and prints the
plan. Requires only the Python standard library (no Home Assistant
dependencies).

The price fixture uses the compact format from the spot-price forecast
sensor: ``hours`` = [{"s": <unix epoch seconds|ms>, "p": <price per kWh>}],
optionally with a per-day ``days`` attribute.

Usage:
    python3 dryrun.py
    python3 dryrun.py --sample > dryrun_prices.json
    python3 dryrun.py --prices dryrun_prices.json
    python3 dryrun.py --soc 45 --max-soc 80 --weekly-full
    python3 dryrun.py --weekly-full --now 2026-09-17T00:00+00:00
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "custom_components", "smart_charging"))

import helper  # noqa: E402

UTC = timezone.utc


def _generate_sample() -> str:
    """Generate a 14-day sample price fixture (compact s/p + days)."""
    base = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    hours = []
    for d in range(14):
        for hh in range(24):
            is_night = hh >= 22 or hh <= 5
            is_boost_night = (d == 6 and hh >= 22) or (d == 7 and hh <= 3)
            price = 0.30 if is_boost_night else (0.8 if is_night else 1.4)
            hours.append(
                {
                    "s": int((base + timedelta(days=d, hours=hh)).timestamp()),
                    "p": price,
                }
            )
    days = []
    for d in range(14):
        prices = [h["p"] for h in hours[d * 24 : (d + 1) * 24]]
        days.append(
            {
                "date": (base + timedelta(days=d)).strftime("%Y-%m-%d"),
                "min_kwh": round(min(prices), 3),
                "max_kwh": round(max(prices), 3),
                "avg_kwh": round(sum(prices) / len(prices), 3),
            }
        )
    return json.dumps(
        {
            "hours": hours,
            "days": days,
            "horizon_days": 14,
            "days_count": len(days),
            "hours_count": len(hours),
            "generated_at": base.isoformat(),
        },
        indent=2,
    )


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _prices_from_data(data: object) -> list[helper.PriceHour]:
    if isinstance(data, dict):
        raw = data.get("hours", data)
    elif isinstance(data, list):
        raw = data
    else:
        raise ValueError("Unexpected JSON structure")
    return helper.parse_price_hours(raw)


def _mode(mode_str: str) -> str:
    """Map a command-line mode string to a helper mode constant."""
    lower = mode_str.strip().lower()
    if lower in ("off", "av"):
        return helper._MODE_OFF
    if lower in ("plan", "planlage", "planläge", "test"):
        return helper._MODE_PLAN
    if lower == "live":
        return helper._MODE_LIVE
    raise argparse.ArgumentTypeError(
        f"Invalid mode '{mode_str}'. Choose: off, plan (or test), live"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run the smart charging planner")
    parser.add_argument(
        "--prices", type=str, default="", help="Path to price JSON (default: sample)"
    )
    parser.add_argument(
        "--sample", action="store_true", help="Print a sample price JSON and exit"
    )
    parser.add_argument(
        "--mode", type=_mode, default=helper._MODE_PLAN, help="off | plan | live"
    )
    parser.add_argument("--soc", type=float, default=60.0, help="Battery level (%)")
    parser.add_argument("--min-soc", type=float, default=20.0, help="Battery floor (%)")
    parser.add_argument(
        "--max-soc", type=float, default=80.0, help="Normal charge target (%)"
    )
    parser.add_argument(
        "--capacity-kwh", type=float, default=77.0, help="Battery capacity (kWh)"
    )
    parser.add_argument(
        "--daily-consumption-pct", type=float, default=15.0, help="Daily use (%/day)"
    )
    parser.add_argument(
        "--charger-max-kw", type=float, default=11.0, help="Max charger power (kW)"
    )
    parser.add_argument(
        "--weekly-full", action="store_true", help="Allow the weekly 100 % boost"
    )
    parser.add_argument(
        "--min-days-between-full", type=float, default=5.0, help="100 % cooldown (d)"
    )
    parser.add_argument(
        "--last-full-charge", type=str, default="", help="ISO time of last 100 % charge"
    )
    parser.add_argument("--currency", type=str, default="SEK")
    parser.add_argument("--deadline", type=str, default="", help="Deadline ISO datetime")
    parser.add_argument(
        "--deadline-time",
        type=str,
        default="",
        help="Recurring 'HH:MM' deadline (interpreted in --timezone)",
    )
    parser.add_argument(
        "--deadline-restart-min",
        type=float,
        default=0.0,
        help="Minutes after a passed deadline before charging may restart (0 = never)",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="UTC",
        help="IANA timezone for the deadline (default: UTC)",
    )
    parser.add_argument(
        "--connected",
        type=lambda v: v.lower() in ("true", "1", "yes", "ja"),
        default=True,
        help="Is the charger connected? (default: true)",
    )
    parser.add_argument("--now", type=str, default="", help="Override 'now' ISO datetime")

    args = parser.parse_args()

    if args.sample:
        print(_generate_sample())
        return 0

    if args.prices:
        price_source = args.prices
        with open(args.prices, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        price_source = "built-in sample"
        data = json.loads(_generate_sample())
    price_hours = _prices_from_data(data)
    day_prices = (
        helper.parse_days(data.get("days") or []) if isinstance(data, dict) else []
    )

    now = _parse_iso(args.now) if args.now else datetime.now(UTC)
    deadline = _parse_iso(args.deadline) if args.deadline else None
    try:
        deadline_tz = ZoneInfo(args.timezone)
    except Exception:
        deadline_tz = UTC
    last_full = _parse_iso(args.last_full_charge) if args.last_full_charge else None

    plan = helper.compute_plan(
        price_hours=price_hours,
        day_prices=day_prices,
        connected=args.connected,
        mode=args.mode,
        soc_now=args.soc,
        min_soc=args.min_soc,
        max_soc=args.max_soc,
        daily_consumption_pct=args.daily_consumption_pct,
        charger_max_kw=args.charger_max_kw,
        battery_capacity_kwh=args.capacity_kwh,
        weekly_full_charge=args.weekly_full,
        last_full_charge=last_full,
        min_days_between_full=args.min_days_between_full,
        deadline=deadline,
        deadline_time=args.deadline_time or None,
        deadline_restart_minutes=args.deadline_restart_min,
        deadline_timezone=deadline_tz,
        now=now,
        currency=args.currency,
    )

    print(f"Mode:            {args.mode}")
    print(f"Connected:       {'yes' if args.connected else 'no'}")
    print(f"Currency:        {args.currency}")
    print(
        f"Battery:         {args.soc} %  (floor {args.min_soc} %, target {args.max_soc} %)"
    )
    print(f"Consumption:     {args.daily_consumption_pct} %/day")
    print(f"Charger max:     {args.charger_max_kw} kW on {args.capacity_kwh} kWh battery")
    print(
        f"Weekly 100 %:    {'on' if args.weekly_full else 'off'} "
        f"(cooldown {args.min_days_between_full} d)"
    )
    if last_full:
        print(f"Last full:       {last_full.isoformat()}")
    if deadline:
        print(f"Deadline:        {deadline.isoformat()}")
    if args.deadline_time:
        print(
            f"Deadline time:   {args.deadline_time} "
            f"(tz {args.timezone}, restart {args.deadline_restart_min} min)"
        )
    print(f"Now:             {now.isoformat()}")
    print(f"Price source:    {price_source}")
    print(f"Price hours:     {len(price_hours)}")
    print()
    print(f"Summary:         {plan.summary}")
    if plan.next_action:
        na = plan.next_action
        at_str = na.at.isoformat() if na.at else "(now)"
        print(f"Next action:     {na.action} at {at_str}  [{na.reason}]")
    if plan.threshold_start is not None:
        print(
            f"Auto thresholds: start <= {plan.threshold_start:.3f}  "
            f"stop >= {plan.threshold_stop:.3f} ({args.currency}/kWh)"
        )
    if plan.next_boost_after is not None:
        print(f"Next boost:      not before {plan.next_boost_after.isoformat()}")
    print(f"Sessions:        {len(plan.sessions)}")
    for i, s in enumerate(plan.sessions, 1):
        tag = "  [100 %]" if s.is_boost else ""
        print(
            f"  {i}. {s.start.isoformat()} -> {s.end.isoformat()}  "
            f"{s.power_kw} kW  avg {s.avg_price_kwh:.4f} {args.currency}/kWh"
            f"  ({len(s.hours)} h){tag}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())