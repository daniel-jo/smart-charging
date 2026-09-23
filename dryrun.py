#!/usr/bin/env python3
"""Standalone dry-run of the smart charging planner.

Loads a price JSON file and prints the plan. Requires only the Python standard
library (no Home Assistant dependencies).

Usage:
    python3 dryrun.py --prices dryrun_prices.json
    python3 dryrun.py --prices dryrun_prices.json --mode live --threshold-start 0.50
    python3 dryrun.py --sample > dryrun_prices.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "custom_components", "smart_charging"))

import helper  # noqa: E402

UTC = timezone.utc


def _generate_sample() -> str:
    """Generate a sample price JSON so users can test without real data."""
    base = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
    hours = [
        {
            "start": (base + timedelta(hours=h)).isoformat(),
            "sek_kwh": round(
                0.30
                if 4 <= (h % 24) <= 6
                else 0.70
                if 22 <= (h % 24) or (h % 24) <= 3
                else 1.10,
                3,
            ),
        }
        for i in range(7)
        for h in range(i * 24, i * 24 + 24)
    ]
    return json.dumps({"hours": hours, "generated_at": base.isoformat()}, indent=2)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_prices(path: str) -> list[helper.PriceHour]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        raw = data.get("hours", data)
    elif isinstance(data, list):
        raw = data
    else:
        raise ValueError("Unexpected JSON structure")
    return [helper.PriceHour(_parse_iso(h["start"]), h["sek_kwh"]) for h in raw]


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
    parser.add_argument("--prices", type=str, default="", help="Path to price JSON file")
    parser.add_argument(
        "--sample", action="store_true", help="Print a sample price JSON and exit"
    )
    parser.add_argument(
        "--mode",
        type=_mode,
        default=helper._MODE_PLAN,
        help="Mode: off, plan (default), or live",
    )
    parser.add_argument(
        "--threshold-start", type=float, default=0.80, help="Start threshold (kr/kWh)"
    )
    parser.add_argument(
        "--threshold-stop", type=float, default=0.95, help="Stop threshold (kr/kWh)"
    )
    parser.add_argument(
        "--deadline", type=str, default="", help="Deadline ISO datetime"
    )
    parser.add_argument(
        "--charger-max-kw", type=float, default=11.0, help="Max charger power (kW)"
    )
    parser.add_argument(
        "--battery-need-kwh",
        type=float,
        default=None,
        help="Required energy (kWh); default = all cheap hours",
    )
    parser.add_argument(
        "--connected",
        type=lambda v: v.lower() in ("true", "1", "yes", "ja"),
        default=True,
        help="Is the charger connected? (default: true)",
    )
    parser.add_argument(
        "--now", type=str, default="", help="Override 'now' ISO datetime"
    )

    args = parser.parse_args()

    if args.sample:
        print(_generate_sample())
        return 0

    if not args.prices:
        parser.print_help()
        print("\nProvide --prices or --sample.")
        return 1

    prices = load_prices(args.prices)
    now = _parse_iso(args.now) if args.now else datetime.now(UTC)
    deadline = _parse_iso(args.deadline) if args.deadline else None

    plan = helper.compute_plan(
        price_hours=prices,
        connected=args.connected,
        mode=args.mode,
        threshold_start=args.threshold_start,
        threshold_stop=args.threshold_stop,
        deadline=deadline,
        charger_max_kw=args.charger_max_kw,
        battery_need_kwh=args.battery_need_kwh,
        now=now,
    )

    print(f"Mode:           {args.mode}")
    print(f"Connected:      {'yes' if args.connected else 'no'}")
    print(f"Thresholds:     start <= {args.threshold_start}  stop >= {args.threshold_stop}")
    print(f"Charger max:    {args.charger_max_kw} kW")
    if args.battery_need_kwh:
        print(f"Battery need:   {args.battery_need_kwh} kWh")
    else:
        print("Battery need:   all cheap hours")
    if deadline:
        print(f"Deadline:       {deadline.isoformat()}")
    print(f"Now:            {now.isoformat()}")
    print(f"Price hours:    {len(prices)}")
    print()
    print(f"Summary:        {plan.summary}")
    if plan.next_action:
        na = plan.next_action
        at_str = na.at.isoformat() if na.at else "(now)"
        print(f"Next action:    {na.action} at {at_str}  [{na.reason}]")
    print(f"Sessions:       {len(plan.sessions)}")
    for i, s in enumerate(plan.sessions, 1):
        print(
            f"  {i}. {s.start.isoformat()} -> {s.end.isoformat()}  "
            f"{s.power_kw} kW  avg {s.avg_price_sek_kwh:.4f} kr/kWh  ({len(s.hours)} h)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())