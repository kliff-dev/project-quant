"""Load and strictly validate daily, single-instrument market data."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import IO

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataValidationError(ValueError):
    """Market data cannot safely be used for a backtest."""


class DataDownloadError(RuntimeError):
    """The external provider failed to return usable market data."""


def validate_ohlcv(data: pd.DataFrame) -> pd.DataFrame:
    """Return a normalized copy; reject bad rows rather than fill or drop them.

    Index timestamps must be unique, increasing, timezone-naive daily dates.
    No exchange calendar is assumed: absent trading sessions cannot be detected.
    Extra columns are ignored explicitly; the five OHLCV columns are retained.
    """
    if not isinstance(data, pd.DataFrame) or data.empty:
        raise DataValidationError("Market data must be a non-empty DataFrame.")
    if isinstance(data.columns, pd.MultiIndex):
        raise DataValidationError("Expected one instrument with flat OHLCV columns.")
    result = data.copy()
    result.columns = [str(column).strip().lower() for column in result.columns]
    if result.columns.duplicated().any():
        raise DataValidationError("Duplicate column names are not allowed.")
    missing = set(OHLCV_COLUMNS) - set(result.columns)
    if missing:
        raise DataValidationError(f"Missing OHLCV columns: {', '.join(sorted(missing))}.")
    if not isinstance(result.index, pd.DatetimeIndex):
        raise DataValidationError("The index must be a DatetimeIndex.")
    if result.index.hasnans:
        raise DataValidationError("Missing or invalid timestamps are not allowed.")
    if result.index.has_duplicates:
        raise DataValidationError("Duplicate timestamps are not allowed.")
    if not result.index.is_monotonic_increasing:
        raise DataValidationError("Timestamps must be sorted in increasing order.")
    if result.index.tz is not None or not result.index.equals(result.index.normalize()):
        raise DataValidationError("Only timezone-naive daily dates at midnight are supported.")
    result = result[OHLCV_COLUMNS]
    for column in OHLCV_COLUMNS:
        if pd.api.types.is_bool_dtype(result[column]):
            raise DataValidationError(f"Boolean values are invalid in {column}.")
        try:
            result[column] = result[column].astype(float)
        except (ValueError, TypeError, OverflowError) as exc:
            raise DataValidationError(f"Non-numeric values in {column}.") from exc
    if not np.isfinite(result.to_numpy()).all():
        raise DataValidationError("OHLCV contains missing or non-finite values.")
    if (result[["open", "high", "low", "close"]] <= 0).any().any():
        raise DataValidationError("All prices must be greater than zero.")
    if (result["volume"] < 0).any():
        raise DataValidationError("Volume cannot be negative.")
    if (result["high"] < result[["open", "close", "low"]].max(axis=1)).any():
        raise DataValidationError("High must be at least open, close, and low.")
    if (result["low"] > result[["open", "close", "high"]].min(axis=1)).any():
        raise DataValidationError("Low must be at most open, close, and high.")
    result.index.name = "date"
    return result


def load_csv(path_or_buffer: str | Path | IO[str] | IO[bytes]) -> pd.DataFrame:
    """Read an OHLCV CSV with a date column. Dates are never silently repaired."""
    try:
        data = pd.read_csv(path_or_buffer, dtype=str)
        data.columns = [str(column).strip().lower() for column in data.columns]
        if "date" not in data.columns:
            raise DataValidationError("CSV requires a 'date' column.")
        data.index = pd.DatetimeIndex(pd.to_datetime(data.pop("date"), errors="raise"))
    except (ValueError, TypeError, pd.errors.ParserError) as exc:
        raise DataValidationError(f"Could not parse OHLCV CSV: {exc}") from exc
    return validate_ohlcv(data)


def download_yahoo(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Download daily adjusted OHLCV. Start is inclusive; end is exclusive.

    Adjusted OHLC keeps splits/dividends on the same scale. These are synthetic
    historical prices, not literal executable quotes; no separate dividend cash
    flows are credited. Provider revisions are isolated by local snapshots.
    """
    symbol = symbol.strip().upper()
    if not symbol or len(symbol.split()) != 1 or "," in symbol:
        raise ValueError("Provide exactly one Yahoo ticker symbol.")
    try:
        start_date, end_date = pd.Timestamp(start), pd.Timestamp(end)
    except (ValueError, TypeError) as exc:
        raise ValueError("Start and end must be valid daily dates.") from exc
    if pd.isna(start_date) or pd.isna(end_date) or start_date >= end_date:
        raise ValueError("Start date must precede end date (exclusive).")
    for value in (start_date, end_date):
        if value.tz is not None or value != value.normalize():
            raise ValueError("Start and end must be timezone-naive daily dates.")
    import yfinance as yf

    try:
        raw = yf.download(
            symbol, start=start_date.date().isoformat(), end=end_date.date().isoformat(),
            interval="1d", auto_adjust=True, actions=False, repair=False,
            keepna=True, progress=False, threads=False, ignore_tz=True,
            multi_level_index=False, timeout=20,
        )
    except Exception as exc:
        raise DataDownloadError(f"Yahoo download failed for {symbol}: {exc}") from exc
    if raw is None or raw.empty:
        raise DataDownloadError(f"Yahoo returned no data for {symbol}. Check ticker, dates, and connectivity.")
    if isinstance(raw.columns, pd.MultiIndex):
        if raw.columns.nlevels != 2 or len(raw.columns.get_level_values(1).unique()) != 1:
            raise DataValidationError("Yahoo returned data for multiple instruments.")
        raw = raw.copy()
        raw.columns = raw.columns.get_level_values(0)
    data = validate_ohlcv(raw)
    if (data.index < start_date).any() or (data.index >= end_date).any():
        raise DataValidationError("Provider returned dates outside the requested range.")
    return data


def demo_data(rows: int = 600, seed: int = 42) -> pd.DataFrame:
    """Deterministic, explicitly synthetic OHLCV for offline demos and tests."""
    if rows < 2:
        raise ValueError("Demo requires at least two bars.")
    rng = np.random.default_rng(seed)
    steps = np.arange(rows)
    close = 100 * np.exp(0.00025 * steps + 0.10 * np.sin(steps / 23) + np.cumsum(rng.normal(0, 0.006, rows)))
    previous = np.r_[close[0], close[:-1]]
    opening = previous * np.exp(rng.normal(0, 0.002, rows))
    data = pd.DataFrame({
        "open": opening,
        "high": np.maximum(opening, close) * (1 + rng.uniform(0.001, 0.012, rows)),
        "low": np.minimum(opening, close) * (1 - rng.uniform(0.001, 0.012, rows)),
        "close": close,
        "volume": rng.integers(100_000, 2_000_000, rows),
    }, index=pd.bdate_range("2022-01-03", periods=rows, name="date"))
    return validate_ohlcv(data)


def from_csv_text(text: str) -> pd.DataFrame:
    """Load an in-memory canonical CSV (also used by snapshot storage)."""
    return load_csv(StringIO(text))
