from io import StringIO

import numpy as np
import pandas as pd
import pytest

from engine.data_loader import DataDownloadError, DataValidationError, demo_data, download_yahoo, load_csv, validate_ohlcv
from engine.storage import DataStore


@pytest.mark.parametrize("kind", ["nan", "inf", "negative", "zero", "volume", "high", "low", "duplicate", "order", "nat", "timezone", "intraday", "column", "boolean"])
def test_rejects_invalid_data(bars, kind):
    data = bars([10, 11, 12])
    if kind in {"nan", "inf", "negative", "zero"}:
        data["close"] = data["close"].astype(float)
        data.loc[data.index[1], "close"] = {"nan": np.nan, "inf": np.inf, "negative": -1, "zero": 0}[kind]
    elif kind == "volume":
        data.loc[data.index[0], "volume"] = -1
    elif kind == "high":
        data.loc[data.index[0], "high"] = 5
    elif kind == "low":
        data.loc[data.index[0], "low"] = 15
    elif kind == "duplicate":
        data.index = pd.DatetimeIndex([data.index[0], data.index[0], data.index[2]])
    elif kind == "order":
        data = data.iloc[::-1]
    elif kind == "nat":
        data.index = pd.DatetimeIndex([data.index[0], pd.NaT, data.index[2]])
    elif kind == "timezone":
        data.index = data.index.tz_localize("UTC")
    elif kind == "intraday":
        data.index = data.index + pd.Timedelta(hours=1)
    elif kind == "column":
        data = data.drop(columns="volume")
    elif kind == "boolean":
        data["close"] = True
    with pytest.raises(DataValidationError):
        validate_ohlcv(data)


def test_csv_roundtrip_and_snapshot_provenance(tmp_path):
    data = demo_data(80)
    store = DataStore(tmp_path)
    metadata = {"symbol": "SYNTHETIC", "source": "demo", "seed": 42}
    dataset_id = store.save(data, metadata)
    assert store.save(data, metadata) == dataset_id
    restored, provenance = store.load(dataset_id)
    pd.testing.assert_frame_equal(restored, data, check_freq=False, rtol=1e-14)
    assert provenance == metadata
    assert len(store.list_datasets()) == 1
    path = store.processed_dir / f"{dataset_id}.csv"
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        store.load(dataset_id)


def test_csv_requires_dates_and_reports_bad_dates():
    with pytest.raises(DataValidationError, match="date"):
        load_csv(StringIO("open,high,low,close,volume\n1,2,1,2,10\n"))
    with pytest.raises(DataValidationError):
        load_csv(StringIO("date,open,high,low,close,volume\nbad,1,2,1,2,10\n"))


def test_demo_is_reproducible():
    pd.testing.assert_frame_equal(demo_data(), demo_data())


def test_yahoo_download_contract(monkeypatch, bars):
    import yfinance
    calls = {}
    def fake_download(symbol, **kwargs):
        calls.update(kwargs)
        assert symbol == "SPY"
        return bars([10, 11, 12]).rename(columns=str.title)
    monkeypatch.setattr(yfinance, "download", fake_download)
    result = download_yahoo(" spy ", "2024-01-01", "2024-01-04")
    assert len(result) == 3
    assert calls["auto_adjust"] is True
    assert calls["keepna"] is True
    assert calls["repair"] is False
    monkeypatch.setattr(yfinance, "download", lambda *args, **kwargs: pd.DataFrame())
    with pytest.raises(DataDownloadError):
        download_yahoo("SPY", "2024-01-01", "2024-01-04")
