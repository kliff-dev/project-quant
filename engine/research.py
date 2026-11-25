"""Shared research workflow and strict, portable audit exports."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

from engine.backtester import Backtester, BacktestResult
from engine.config import BacktestConfig
from engine.data_loader import validate_ohlcv
from engine.storage import PROJECT_ROOT, DataStore, canonical_csv
from strategies.base_strategy import BaseStrategy

ASSUMPTIONS = {
    "performance": "Simulated historical research only; not actual trading performance.",
    "execution": "Close-time signals fill at next observed daily open with adverse costs. Last-bar signals remain unfilled.",
    "position_sizing": "Single instrument, long-only, whole shares; allocation budget includes entry commissions; no leverage or pyramiding.",
    "costs": "Full spread is halved per side; slippage is adverse per side; commission is fixed plus bps of filled notional.",
    "end_position": "Marked at final close without reserving future exit costs; excluded from closed-trade statistics.",
    "sharpe": "Mean per-bar excess simple return / sample standard deviation * sqrt(periods_per_year); includes warmup and flat bars; annual risk-free rate converted geometrically; cash earns zero.",
    "drawdown": "Equity / running peak - 1, seeded with initial capital. Average includes all bars, including zero drawdowns. Summary magnitudes are positive percentages.",
    "trade_metrics": "Completed round trips only, net of entry and exit costs. Losses are negative. Breakeven trades reset streaks and count in win-rate denominator and expectancy.",
    "undefined_metrics": "Sharpe null for insufficient/constant returns. Profit factor null with status infinite when wins and no losses, or undefined when neither wins nor losses.",
    "data_limitations": "Daily OHLCV does not prove next-open liquidity or fillability. No volume limit, exchange calendar validation, taxes, corporate-action cash ledger, or point-in-time universe.",
    "research_risks": "Current tickers can introduce survivorship bias. Repeated tuning can overfit; use chronological holdouts and record experiments.",
}


@dataclass
class ResearchRun:
    data: pd.DataFrame
    result: BacktestResult
    report: dict[str, Any]


def code_fingerprint() -> str:
    """Hash calculation source so edits are distinguishable from parameter changes."""
    digest = hashlib.sha256()
    for directory in ("engine", "strategies", "analytics"):
        for path in sorted((PROJECT_ROOT / directory).glob("*.py")):
            digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def run_research(data: pd.DataFrame, strategy: BaseStrategy, config: BacktestConfig, store: DataStore, metadata: dict[str, Any]) -> ResearchRun:
    """Snapshot validated inputs, run the engine, and capture all material assumptions."""
    data = validate_ohlcv(data)
    dataset_id = store.save(data, metadata)
    result = Backtester(strategy, config).run(data)
    versions = {name: importlib.metadata.version(name) for name in ("numpy", "pandas", "plotly", "streamlit", "yfinance")}
    identity = dict(dataset_id=dataset_id, strategy=strategy.parameters(), config=asdict(config), code_sha256=code_fingerprint(), python=platform.python_version(), packages=versions)
    run_id = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()[:20]
    report = dict(
        schema_version=1, run_id=run_id, created_at_utc=datetime.now(timezone.utc).isoformat(),
        **identity, source=metadata, bars=len(data), first_bar=data.index[0].isoformat(),
        last_bar=data.index[-1].isoformat(), assumptions=ASSUMPTIONS,
        metrics=result.metrics, open_position=result.open_position,
    )
    return ResearchRun(data.copy(), result, report)


def _json_default(value: Any) -> str:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def report_json(report: dict[str, Any]) -> str:
    """Portable JSON: no NaN/Infinity extensions that break other parsers."""
    return json.dumps(report, indent=2, sort_keys=True, allow_nan=False, default=_json_default)


def report_files(run: ResearchRun) -> dict[str, bytes]:
    """Return a portable bundle including the exact bars needed to replay a run."""
    result = run.result
    files = {
        "market_data.csv": canonical_csv(run.data).encode("utf-8"),
        "equity.csv": result.equity.to_csv(float_format="%.17g", lineterminator="\n").encode("utf-8"),
        "fills.csv": result.fills.to_csv(index=False, float_format="%.17g", lineterminator="\n").encode("utf-8"),
        "trades.csv": result.trades.to_csv(index=False, float_format="%.17g", lineterminator="\n").encode("utf-8"),
        "orders.csv": result.orders.to_csv(index=False, lineterminator="\n").encode("utf-8"),
        "run.json": report_json(run.report).encode("utf-8"),
    }
    checksums = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    files["checksums.json"] = report_json(checksums).encode("utf-8")
    return files


def report_zip(run: ResearchRun) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, content in report_files(run).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def export_run(run: ResearchRun, output_root: str | Path) -> Path:
    """Write one deterministic run directory; reruns with identical identity reuse it."""
    output_dir = Path(output_root) / run.report["run_id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in report_files(run).items():
        (output_dir / name).write_bytes(content)
    return output_dir
