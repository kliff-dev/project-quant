"""Command-line entry point for reproducible historical research."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from engine.config import BacktestConfig, TransactionCosts
from engine.data_loader import DataDownloadError, demo_data, download_yahoo, load_csv
from engine.research import export_run, run_research
from engine.storage import PROJECT_ROOT, DataStore
from strategies.trend_following import MovingAverageCrossover


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project Quant — simulated daily SMA backtests; no live trading.")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--demo", action="store_true", help="Use deterministic synthetic data (default).")
    sources.add_argument("--csv", type=Path, help="CSV containing date, open, high, low, close, volume.")
    sources.add_argument("--symbol", help="Explicitly download one Yahoo ticker; requires --start and --end.")
    sources.add_argument("--dataset", help="Replay a saved immutable snapshot ID.")
    parser.add_argument("--start", help="Inclusive start date for Yahoo.")
    parser.add_argument("--end", help="Exclusive end date for Yahoo.")
    parser.add_argument("--label", default="CSV", help="Descriptive label for imported CSV data.")
    parser.add_argument("--short", type=int, default=20, help="Short SMA window.")
    parser.add_argument("--long", type=int, default=50, help="Long SMA window.")
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--allocation", type=float, default=.95, help="Fraction of cash allocated, including entry fees.")
    parser.add_argument("--commission-bps", type=float, default=5)
    parser.add_argument("--fixed-commission", type=float, default=0)
    parser.add_argument("--spread-bps", type=float, default=2, help="Full bid-ask spread in basis points.")
    parser.add_argument("--slippage-bps", type=float, default=2, help="Adverse slippage per side in basis points.")
    parser.add_argument("--periods-per-year", type=int, default=252)
    parser.add_argument("--risk-free-rate", type=float, default=0, help="Annual decimal rate used only in Sharpe.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.symbol and (not args.start or not args.end):
        parser.error("--symbol requires both --start and --end (exclusive).")
    if not args.symbol and (args.start or args.end):
        parser.error("--start and --end apply only to --symbol downloads.")
    try:
        strategy = MovingAverageCrossover(args.short, args.long)
        costs = TransactionCosts(args.commission_bps, args.fixed_commission, args.spread_bps, args.slippage_bps)
        config = BacktestConfig(args.capital, args.allocation, costs, args.periods_per_year, args.risk_free_rate)
        store = DataStore(args.data_dir)
        if args.dataset:
            data, metadata = store.load(args.dataset)
        elif args.csv:
            data = load_csv(args.csv)
            metadata = {"source": "csv", "symbol": args.label, "filename": args.csv.name, "adjustment": "user-supplied; not verified"}
        elif args.symbol:
            data = download_yahoo(args.symbol, args.start, args.end)
            metadata = {"source": "yahoo", "symbol": args.symbol.strip().upper(), "requested_start": args.start, "requested_end_exclusive": args.end, "adjustment": "auto_adjust=True; adjusted OHLC; no separate dividend cash flows"}
        else:
            data = demo_data()
            metadata = {"source": "synthetic", "symbol": "SYNTHETIC", "seed": 42, "rows": 600, "adjustment": "not applicable"}
        run = run_research(data, strategy, config, store, metadata)
        output_dir = export_run(run, args.output)
    except (ValueError, OSError, KeyError, sqlite3.Error, DataDownloadError) as exc:
        parser.exit(1, f"Backtest failed: {exc}\n")
    metrics = run.result.metrics
    print("SIMULATED RESEARCH — not actual trading performance")
    print(f"Source: {metadata['source']} | Dataset: {run.report['dataset_id']}")
    print(f"Final equity: {metrics['final_equity']:,.2f} | Return: {metrics['return_pct']:.2f}%")
    print(f"Closed trades: {metrics['number_of_trades']} | Open positions: {metrics['open_positions']} | Max drawdown: {metrics['max_drawdown_pct']:.2f}%")
    print(f"Report: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
