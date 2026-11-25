"""Gold collection is tested entirely against a native-API-shaped fake."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from engine.gold_connector import GoldConnector
from engine.mt5_connector import DemoAccountRequiredError, MT5ConnectionError
from scripts.collect_gold_data import collect, main, validate_rates


@pytest.fixture
def gold_api():
    account = SimpleNamespace(login=464192812, company="Exness Technologies Ltd", server="Exness-MT5Trial17",
                              currency="USD", trade_mode=0, trade_allowed=True, trade_expert=True)
    info = SimpleNamespace(name="XAUUSDm", currency_base="XAU", currency_profit="USD", visible=True,
                           description="Gold vs US Dollar", trade_contract_size=100., volume_min=.01,
                           volume_max=200., volume_step=.01, point=.001, trade_tick_size=.001,
                           chart_mode=0, digits=3, filling_mode=1, trade_mode=4, trade_exemode=2,
                           trade_stops_level=0, trade_freeze_level=0)
    terminal = SimpleNamespace(connected=True, build=6295, trade_allowed=True, tradeapi_disabled=False)
    fake = SimpleNamespace(__version__="fake", ACCOUNT_TRADE_MODE_DEMO=0, ACCOUNT_TRADE_MODE_CONTEST=1,
                           ACCOUNT_TRADE_MODE_REAL=2, TIMEFRAME_M5=5)
    fake.initialize = Mock(return_value=True)
    fake.shutdown = Mock()
    fake.last_error = Mock(return_value=(-1, "no history"))
    fake.account_info = Mock(return_value=account)
    fake.terminal_info = Mock(return_value=terminal)
    fake.symbols_get = Mock(return_value=(info,))
    fake.symbol_select = Mock(return_value=True)
    fake.symbol_info = Mock(return_value=info)
    fake.symbol_info_tick = Mock(return_value=SimpleNamespace(bid=3920., ask=3920.24,
                                  time_msc=int(datetime.now(timezone.utc).timestamp() * 1000)))
    fake.copy_rates_range = Mock()
    for name in ("order_send", "order_check", "login"):
        setattr(fake, name, Mock(side_effect=AssertionError("Forbidden native mutation")))
    return fake


def bars(start, count=5):
    return [{"time": int((start + timedelta(minutes=5 * i)).timestamp()), "open": 3900. + i,
             "high": 3902. + i, "low": 3899. + i, "close": 3901. + i,
             "tick_volume": 100, "spread": 240, "real_volume": 0} for i in range(count)]


def test_exact_gold_contract_and_native_units(gold_api):
    with GoldConnector(api=gold_api) as connector:
        result = connector.inspect()
    assert result["quote"]["symbol"] == "XAUUSDm"
    assert result["quote"]["spread_price"] == pytest.approx(.24)
    assert result["quote"]["spread_points"] == pytest.approx(240)
    assert result["quote"]["spread_pips"] is None
    assert result["instrument"]["trade_contract_size"] == 100
    assert "CFD" in result["instrument"]["instrument_type"]
    assert result["account"]["account_id_masked"] == "****2812"
    assert result["orders_sent"] == 0
    for name in ("order_send", "order_check", "login", "copy_rates_range"):
        getattr(gold_api, name).assert_not_called()


@pytest.mark.parametrize("symbol", ["GC=F", "EURUSDm", "XAUUSD", "XAUUSDm.other"])
def test_no_gold_substitution(gold_api, symbol):
    with GoldConnector(api=gold_api) as connector:
        with pytest.raises(MT5ConnectionError, match="exact XAUUSDm"):
            connector.inspect(symbol)
    gold_api.symbol_info_tick.assert_not_called()


@pytest.mark.parametrize("field,value", [("trade_contract_size", 1), ("trade_contract_size", float("nan")),
    ("volume_min", .1), ("volume_step", .1), ("point", 0), ("trade_tick_size", float("inf")),
    ("chart_mode", 1), ("chart_mode", False), ("currency_base", "EUR"), ("currency_profit", "EUR"),
    ("digits", None), ("volume_max", .001)])
def test_contract_mismatch_fails_closed(gold_api, field, value):
    setattr(gold_api.symbol_info.return_value, field, value)
    with GoldConnector(api=gold_api) as connector:
        with pytest.raises(MT5ConnectionError):
            connector.inspect()


@pytest.mark.parametrize("mode", [1, 2, 3, False, "0", 0.0])
def test_non_demo_rejected(gold_api, mode):
    gold_api.account_info.return_value.trade_mode = mode
    with pytest.raises(DemoAccountRequiredError):
        GoldConnector(api=gold_api).connect()
    gold_api.symbols_get.assert_not_called()


@pytest.mark.parametrize("field,value", [("login", 123456), ("server", "Another-Demo"),
                                       ("company", "Another Broker"), ("currency", "EUR")])
def test_other_demo_account_is_rejected(gold_api, field, value):
    setattr(gold_api.account_info.return_value, field, value)
    with GoldConnector(api=gold_api) as connector:
        with pytest.raises(DemoAccountRequiredError, match="pinned"):
            connector.inspect()


def test_permissions_change_during_snapshot_is_detected(gold_api):
    original = gold_api.account_info.return_value
    changed = SimpleNamespace(**vars(original))
    changed.trade_allowed = False
    gold_api.account_info.side_effect = [original, original, original, changed]
    with GoldConnector(api=gold_api) as connector:
        with pytest.raises(DemoAccountRequiredError, match="changed"):
            connector.inspect()


def test_stale_quote_is_descriptive_not_a_session_claim(gold_api):
    gold_api.symbol_info_tick.return_value.time_msc -= 86400000
    with GoldConnector(api=gold_api) as connector:
        result = connector.inspect()
    assert result["quote"]["age_seconds"] > 86300
    assert "older than 120" in result["warnings"][0]
    assert "Not verified" in result["market_session"]


def test_different_valid_tick_sizes_are_recorded_not_assumed(gold_api):
    gold_api.symbol_info.return_value.point = .01
    gold_api.symbol_info.return_value.trade_tick_size = .01
    with GoldConnector(api=gold_api) as connector:
        result = connector.inspect()
    assert result["quote"]["spread_points"] == pytest.approx(24)
    assert any("differs from 0.001" in warning for warning in result["warnings"])


def test_collect_closed_bars_hash_and_immutable_cache(gold_api, tmp_path):
    start = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
    now = start + timedelta(minutes=22)
    gold_api.copy_rates_range.return_value = bars(start)
    with GoldConnector(api=gold_api) as connector:
        result = collect(connector, tmp_path, now=now)
        with pytest.raises(FileExistsError, match="immutable"):
            collect(connector, tmp_path, now=now)
    assert result["rows"] == 4
    assert result["quality"]["forming_bars_removed"] == 1
    assert result["actual_last_close_utc"] == "2026-11-13T10:20:00Z"
    assert result["requested_calendar_days"] == 180
    assert result["csv_sha256"] == hashlib.sha256((tmp_path / "XAUUSDm_M5.csv").read_bytes()).hexdigest()
    assert json.loads((tmp_path / "manifest.json").read_text())["orders_sent"] == 0
    assert any("later than requested" in warning for warning in result["warnings"])
    gold_api.copy_rates_range.assert_called_once()
    gold_api.order_send.assert_not_called()
    gold_api.order_check.assert_not_called()
    gold_api.login.assert_not_called()


@pytest.mark.parametrize("field,value", [("high", 1), ("low", 5000), ("open", float("nan")),
                                        ("close", 0), ("tick_volume", -1), ("spread", -1)])
def test_invalid_history_is_not_saved(gold_api, tmp_path, field, value):
    start = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
    rows = bars(start)
    rows[1][field] = value
    gold_api.copy_rates_range.return_value = rows
    with GoldConnector(api=gold_api) as connector:
        with pytest.raises(MT5ConnectionError, match="Gold history"):
            collect(connector, tmp_path, now=start + timedelta(hours=1))
    assert not list(tmp_path.iterdir())


def test_duplicate_history_rejected(gold_api, tmp_path):
    start = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
    rows = bars(start)
    rows[1]["time"] = rows[0]["time"]
    gold_api.copy_rates_range.return_value = rows
    with GoldConnector(api=gold_api) as connector:
        with pytest.raises(MT5ConnectionError, match="duplicate timestamps"):
            collect(connector, tmp_path, now=start + timedelta(hours=1))
    assert not list(tmp_path.iterdir())


def test_history_gap_is_recorded_without_filling():
    start = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
    rows = bars(start)
    del rows[2]
    frame, quality = validate_rates(rows, start=start, end=start + timedelta(hours=1), now=start + timedelta(hours=1))
    assert len(frame) == 4
    assert quality["gap_count"] == 1
    assert quality["gaps"][0]["missing_m5_slots"] == 1
    assert quality["gaps"][0]["classification"] == "unexplained_intraday_gap"


def test_daily_break_and_weekend_gap_are_labeled_possible_only():
    friday = datetime(2026, 11, 13, 20, tzinfo=timezone.utc)
    rows = [bars(friday, 1)[0], bars(friday + timedelta(hours=2), 1)[0],
            bars(friday + timedelta(days=3), 1)[0]]
    _, quality = validate_rates(rows, start=friday, end=friday + timedelta(days=4), now=friday + timedelta(days=4))
    assert [gap["classification"] for gap in quality["gaps"]] == ["possible_daily_session_break", "possible_weekend_closure"]
    assert "schedules were not verified" in quality["gap_caveat"]


def test_account_switch_during_history_discards_download(gold_api, tmp_path):
    start = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
    def switch(*args):
        gold_api.account_info.return_value.login = 123456
        return bars(start)
    gold_api.copy_rates_range.side_effect = switch
    with GoldConnector(api=gold_api) as connector:
        with pytest.raises(DemoAccountRequiredError):
            collect(connector, tmp_path, now=start + timedelta(hours=1))
    assert not list(tmp_path.iterdir())


def test_default_and_status_are_offline(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("scripts.collect_gold_data.GoldConnector", Mock(side_effect=AssertionError("Offline command must not connect")))
    assert main(["--output", str(tmp_path)]) == 0
    assert "not_collected" in capsys.readouterr().out
    assert main(["--status", "--output", str(tmp_path)]) == 0
    assert not list(tmp_path.iterdir())
