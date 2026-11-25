"""Run one read-only MT5 check. Never submits orders or accepts passwords."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.mt5_connector import DEFAULT_TERMINAL, MT5ConnectionError, MT5Connector

LOG_COLUMNS = ["timestamp_utc", "event", "status", "broker", "server", "account_type", "symbol", "bid", "ask", "order_ticket", "deal_ticket", "position_ticket", "volume", "price", "commission", "fee", "swap", "message"]


def write_diagnostics(report: dict[str, Any], directory: Path) -> None:
    """Persist allowlisted diagnostics and an audit event, with no credentials."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mt5_connection_status.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    account, quote = report.get("account") or {}, report.get("quote") or {}
    log_path = directory / "execution_log.csv"
    write_header = not log_path.exists() or log_path.stat().st_size == 0
    with log_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "timestamp_utc": report["checked_at_utc"], "event": "read_only_connection_check",
            "status": report["status"], "broker": account.get("broker", ""),
            "server": account.get("server", ""), "account_type": account.get("account_type", "UNVERIFIED"),
            "symbol": quote.get("symbol", ""), "bid": quote.get("bid", ""), "ask": quote.get("ask", ""),
            "message": report.get("error", "No order_check or order_send calls; read-only inspection."),
        })
    if report.get("symbols"):
        with (directory / "mt5_symbols.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["name", "description", "base_currency", "profit_currency"])
            writer.writeheader()
            writer.writerows(report["symbols"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MT5 DEMO account and EUR/USD connection check. No orders.")
    parser.add_argument("--terminal", type=Path, default=DEFAULT_TERMINAL)
    parser.add_argument("--symbol", help="Exact EUR/USD broker symbol; only needed if automatic discovery is ambiguous.")
    parser.add_argument("--timeout-ms", type=int, default=20_000)
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    args = parser.parse_args(argv)
    report: dict[str, Any]
    try:
        with MT5Connector(args.terminal, timeout_ms=args.timeout_ms) as connector:
            report = connector.inspect(args.symbol)
    except (MT5ConnectionError, ValueError, OSError) as exc:
        report = {
            "status": "connection_failed", "read_only": True,
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "terminal_path": str(args.terminal), "error": str(exc),
            "mt5_error": getattr(exc, "api_error", None), "account": getattr(exc, "account", None),
            "orders_sent": 0,
        }
    try:
        write_diagnostics(report, args.log_dir)
    except (OSError, ValueError) as exc:
        print(f"Could not write local diagnostics: {exc}", file=sys.stderr)
        return 1
    console = {key: value for key, value in report.items() if key != "symbols"}
    print(json.dumps(console, indent=2, allow_nan=False))
    print(f"Local diagnostics: {args.log_dir}")
    return 0 if report["status"] == "verified_demo_connection" else 1


if __name__ == "__main__":
    raise SystemExit(main())
