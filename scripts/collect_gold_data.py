"""Explicit read-only Gold DEMO data collection; no order or login path."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.gold_connector import GOLD_SYMBOL, GoldConnector
from engine.mt5_connector import DEFAULT_TERMINAL, MT5ConnectionError

DEFAULT_OUTPUT = ROOT / "data/raw/gold_intraday_2026-11-15"
COLUMNS = ["timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]


def _iso(value: Any) -> str:
    return pd.Timestamp(value).isoformat().replace("+00:00", "Z")


def validate_rates(rates: Any, *, start: datetime, end: datetime, now: datetime) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Retain actual complete bars only; reject duplicate/invalid OHLC evidence."""
    if rates is None or len(rates) == 0:
        raise MT5ConnectionError("No Gold M5 history returned; check terminal history limits and connection.")
    frame = pd.DataFrame(rates)
    required = {"time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"}
    if not required <= set(frame.columns):
        raise MT5ConnectionError("Gold history lacks required native OHLCV/spread columns.")
    times = pd.to_datetime(frame["time"], unit="s", utc=True, errors="coerce")
    if times.isna().any() or times.duplicated().any():
        raise MT5ConnectionError("Gold history has invalid or duplicate timestamps; raw history was not saved.")
    if any(t.second != 0 or t.minute % 5 for t in times):
        raise MT5ConnectionError("Gold history contains timestamps off the M5 grid.")
    frame.insert(0, "timestamp_utc", times)
    frame = frame.sort_values("timestamp_utc").reset_index(drop=True)
    complete = frame["timestamp_utc"] + pd.Timedelta(minutes=5) <= pd.Timestamp(now)
    forming_removed = int((~complete).sum())
    within_range = (frame["timestamp_utc"] >= pd.Timestamp(start)) & (frame["timestamp_utc"] <= pd.Timestamp(end))
    frame = frame.loc[complete & within_range].copy()
    if frame.empty:
        raise MT5ConnectionError("No complete Gold M5 bars fall in the requested range.")
    for column in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if not frame[column].map(lambda value: math.isfinite(float(value))).all():
            raise MT5ConnectionError(f"Gold history has non-finite {column}.")
    if ((frame[["open", "high", "low", "close"]] <= 0).any().any() or
            (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any() or
            (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any() or
            (frame[["tick_volume", "spread", "real_volume"]] < 0).any().any()):
        raise MT5ConnectionError("Gold history has invalid OHLC prices, volumes, or spread.")
    gaps = []
    for index in range(1, len(frame)):
        previous, current = frame.iloc[index - 1]["timestamp_utc"], frame.iloc[index]["timestamp_utc"]
        seconds = (current - previous).total_seconds()
        if seconds > 300:
            crosses_weekend = any((previous + pd.Timedelta(days=day)).weekday() >= 5
                                  for day in range(int(seconds // 86400) + 1))
            category = "possible_weekend_closure" if crosses_weekend else (
                "possible_daily_session_break" if seconds >= 3600 else "unexplained_intraday_gap")
            gaps.append({"previous_open_utc": _iso(previous), "next_open_utc": _iso(current),
                         "missing_m5_slots": int(seconds / 300) - 1, "classification": category})
    jumps = frame["open"].div(frame["close"].shift()).sub(1).abs()
    unusual = [{"timestamp_utc": _iso(frame.iloc[i]["timestamp_utc"]), "absolute_open_gap_fraction": float(jumps.iloc[i])}
               for i in range(1, len(frame)) if jumps.iloc[i] > .01]
    return frame[COLUMNS], {"duplicate_timestamps": 0, "invalid_prices": 0, "forming_bars_removed": forming_removed,
                            "gap_count": len(gaps), "gaps": gaps, "unusual_open_gaps_over_1_percent": unusual,
                            "gap_caveat": "Classification is diagnostic only: broker session schedules were not verified; gaps were not filled."}


def collect(connector: GoldConnector, output: Path, *, now: datetime | None = None, days: int = 180) -> dict[str, Any]:
    """Download once, validate native DEMO identity before/after, save immutable CSV."""
    if type(days) is not int or not 1 <= days <= 730:
        raise ValueError("days must be an integer from 1 to 730.")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Collection clock must be timezone-aware.")
    now = now.astimezone(timezone.utc)
    start = now - timedelta(days=days)
    csv_path, manifest_path = output / f"{GOLD_SYMBOL}_M5.csv", output / "manifest.json"
    if csv_path.exists() or manifest_path.exists():
        raise FileExistsError("Gold immutable cache already exists. Use --status; choose a new --output for a fresh download.")
    snapshot = connector.inspect(GOLD_SYMBOL)
    if snapshot["instrument"]["chart_mode"] != 0:
        raise MT5ConnectionError("Cannot collect research history without verified bid chart_mode=0.")
    identity = connector.account_identity
    api = connector.terminal_api
    rates = api.copy_rates_range(GOLD_SYMBOL, api.TIMEFRAME_M5, start, now)
    # No retry or fallback: a partial result is documented as actual coverage.
    after = connector.verify_pinned_account()
    if identity != connector.account_identity or snapshot["account"] != after:
        raise MT5ConnectionError("Account changed during Gold history download; discard data.")
    terminal = api.terminal_info()
    if terminal is None or not getattr(terminal, "connected", False):
        raise MT5ConnectionError("Terminal disconnected during Gold history download; discard data.")
    frame, quality = validate_rates(rates, start=start, end=now, now=now)
    actual_start, actual_end = frame.iloc[0]["timestamp_utc"], frame.iloc[-1]["timestamp_utc"]
    warnings = list(snapshot["warnings"])
    if actual_start > pd.Timestamp(start) + pd.Timedelta(days=4):
        warnings.append("Available history begins later than requested; broker retention or terminal Max. bars may limit coverage.")
    if actual_end + pd.Timedelta(days=4) < pd.Timestamp(now):
        warnings.append("Latest historical bar is more than four days old.")
    manifest = {
        "schema_version": 1, "status": "collected_read_only", "created_at_utc": now.isoformat(),
        "source": "MetaTrader5 native copy_rates_range through local terminal; existing DEMO account login",
        "account": after, "instrument": snapshot["instrument"], "timeframe": "M5", "timestamp_basis": "UTC bar open",
        "requested_start_utc": start.isoformat(), "requested_end_utc": now.isoformat(), "requested_calendar_days": days,
        "actual_first_open_utc": _iso(actual_start), "actual_last_open_utc": _iso(actual_end),
        "actual_last_close_utc": _iso(actual_end + pd.Timedelta(minutes=5)), "rows": len(frame),
        "actual_covered_calendar_days": (actual_end - actual_start).total_seconds() / 86400,
        "quality": quality, "warnings": warnings, "connection": {key: value for key, value in snapshot.items() if key != "symbols"},
        "volume_caveat": "tick_volume counts price updates; real_volume may be zero. Neither is consolidated global gold volume.",
        "spread_caveat": "Native historical spread is in broker points; it is not a complete historical bid/ask tick path.",
        "derivation": "M5 only. Derive M15 only from three consecutive complete M5 bars; never fill absent candles.",
        "orders_sent": 0, "order_checks": 0, "retries": 0,
    }
    saved = frame.copy()
    saved["timestamp_utc"] = saved["timestamp_utc"].map(_iso)
    payload = saved.to_csv(index=False, lineterminator="\n").encode("utf-8")
    manifest["csv_file"] = csv_path.name
    manifest["csv_sha256"] = hashlib.sha256(payload).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents an accidental overwrite, even on simultaneous runs.
    with csv_path.open("xb") as handle:
        handle.write(payload)
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, allow_nan=False)
        handle.write("\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download", action="store_true", help="Explicit read-only native DEMO history request; no orders.")
    mode.add_argument("--status", action="store_true", help="Inspect local cache only; never connects to MT5.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--terminal", type=Path, default=DEFAULT_TERMINAL)
    args = parser.parse_args(argv)
    if not args.download:
        manifest_path = args.output / "manifest.json"
        report = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {
            "status": "not_collected", "read_only": True, "orders_sent": 0,
            "message": "Offline status. Use --download to request actual broker data; no process is running or scheduled."}
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    if (args.output / "manifest.json").exists() or (args.output / f"{GOLD_SYMBOL}_M5.csv").exists():
        print("Immutable Gold cache already exists; use --status or a new --output.", file=sys.stderr)
        return 1
    try:
        with GoldConnector(args.terminal) as connector:
            report = collect(connector, args.output, days=args.days)
    except (MT5ConnectionError, OSError, ValueError) as exc:
        # Exception messages can originate inside native APIs; retain only type and
        # controlled diagnostics, never a native credential-bearing response.
        failure = {"status": "collection_failed", "read_only": True,
                   "checked_at_utc": datetime.now(timezone.utc).isoformat(), "error_type": type(exc).__name__,
                   "message": "Gold data was not collected. Verify terminal connection, pinned DEMO account and XAUUSDm contract.",
                   "orders_sent": 0, "retries": 0}
        args.output.mkdir(parents=True, exist_ok=True)
        with (args.output / "last_collection_failure.json").open("w", encoding="utf-8") as handle:
            json.dump(failure, handle, indent=2, allow_nan=False)
        print(json.dumps(failure, indent=2))
        return 1
    print(json.dumps({key: value for key, value in report.items() if key not in ("quality", "connection")}, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
