import csv
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from engine.demo_executor import DemoExecutionDisabledError, PLACE_CONFIRMATION, place_demo_test_order
from engine.mt5_connector import DemoAccountRequiredError, MT5ConnectionError, MT5Connector
from scripts.check_mt5_connection import write_diagnostics


@pytest.fixture
def api():
    fake = SimpleNamespace(
        __version__="test", ACCOUNT_TRADE_MODE_DEMO=0, ACCOUNT_TRADE_MODE_CONTEST=1, ACCOUNT_TRADE_MODE_REAL=2,
    )
    account = SimpleNamespace(login=12345678, company="Test Broker", server="Test-Demo", currency="USD", trade_mode=0, trade_allowed=True, trade_expert=False)
    terminal = SimpleNamespace(connected=True, build=6295, trade_allowed=False, tradeapi_disabled=True)
    eurusd = SimpleNamespace(name="EURUSD", description="Euro vs US Dollar", currency_base="EUR", currency_profit="USD", visible=True, point=.00001, digits=5, volume_min=.01, volume_max=100., volume_step=.01, filling_mode=1, trade_mode=4, trade_exemode=2)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    fake.initialize = Mock(return_value=True)
    fake.shutdown = Mock()
    fake.last_error = Mock(return_value=(-6, "Terminal: Authorization failed"))
    fake.terminal_info = Mock(return_value=terminal)
    fake.account_info = Mock(return_value=account)
    fake.symbols_get = Mock(return_value=(eurusd,))
    fake.symbol_select = Mock(return_value=True)
    fake.symbol_info = Mock(return_value=eurusd)
    fake.symbol_info_tick = Mock(return_value=SimpleNamespace(bid=1.1, ask=1.10012, time_msc=now))
    fake.order_send = Mock(side_effect=AssertionError("Must never send an order"))
    fake.order_check = Mock(side_effect=AssertionError("Stage 1 must never prepare an order"))
    fake.login = Mock(side_effect=AssertionError("Must never handle credentials"))
    return fake


def test_read_only_demo_account_and_quote(api):
    with MT5Connector(api=api) as connector:
        result = connector.inspect()
    assert result["account"]["account_type"] == "DEMO"
    assert result["account"]["account_id_masked"] == "****5678"
    assert result["quote"]["bid"] == 1.1
    assert result["quote"]["spread_pips"] == pytest.approx(1.2)
    assert result["symbol_count"] == 1
    assert result["orders_sent"] == 0
    assert "not verified" in result["market_session"].lower()
    api.initialize.assert_called_once()
    assert set(api.initialize.call_args.kwargs) == {"timeout"}
    api.order_send.assert_not_called()
    api.order_check.assert_not_called()
    api.login.assert_not_called()
    api.shutdown.assert_called_once()


@pytest.mark.parametrize("mode", [1, 2, 3, None, False, 0.0, "0"])
def test_rejects_every_non_demo_or_unknown_account(api, mode):
    api.account_info.return_value.trade_mode = mode
    # A misleading server/name cannot override the trade_mode field.
    api.account_info.return_value.server = "Definitely-Demo"
    with pytest.raises(DemoAccountRequiredError):
        MT5Connector(api=api).connect()
    api.symbols_get.assert_not_called()
    api.order_send.assert_not_called()
    api.shutdown.assert_called_once()


def test_missing_account_fails_closed(api):
    api.account_info.return_value = None
    with pytest.raises(DemoAccountRequiredError, match="cannot be verified"):
        MT5Connector(api=api).connect()
    api.order_send.assert_not_called()


def test_authorization_failure_is_preserved_and_not_retried(api):
    api.initialize.return_value = False
    with pytest.raises(MT5ConnectionError) as caught:
        MT5Connector(api=api).connect()
    assert caught.value.api_error == (-6, "Terminal: Authorization failed")
    api.initialize.assert_called_once()
    api.account_info.assert_not_called()
    api.shutdown.assert_called_once()


@pytest.mark.parametrize("terminal", [None, SimpleNamespace(connected=False)])
def test_terminal_information_must_be_available_and_connected(api, terminal):
    api.terminal_info.return_value = terminal
    with pytest.raises(MT5ConnectionError):
        MT5Connector(api=api).connect()


def test_ambiguous_broker_suffix_requires_explicit_selection(api):
    first = api.symbols_get.return_value[0]
    first.name = "EURUSD.a"
    second = SimpleNamespace(**vars(first))
    second.name = "EURUSD.b"
    api.symbols_get.return_value = (first, second)
    with MT5Connector(api=api) as connector:
        with pytest.raises(MT5ConnectionError, match="ambiguous"):
            connector.inspect()
        result = connector.inspect("EURUSD.a")
    assert result["quote"]["symbol"] == "EURUSD.a"


def test_missing_quote_is_not_fabricated(api):
    api.symbol_info_tick.return_value = None
    with MT5Connector(api=api) as connector:
        with pytest.raises(MT5ConnectionError, match="No EUR/USD tick"):
            connector.inspect()
    api.order_send.assert_not_called()


@pytest.mark.parametrize("bid,ask", [(0, 1.1), (1.2, 1.1), (float("nan"), 1.1)])
def test_invalid_quotes_fail(api, bid, ask):
    api.symbol_info_tick.return_value.bid = bid
    api.symbol_info_tick.return_value.ask = ask
    with MT5Connector(api=api) as connector:
        with pytest.raises(MT5ConnectionError, match="bid/ask"):
            connector.inspect()


def test_stale_quote_has_a_warning(api):
    api.symbol_info_tick.return_value.time_msc -= 600_000
    with MT5Connector(api=api) as connector:
        report = connector.inspect()
    assert report["warnings"]
    assert report["quote"]["age_seconds"] >= 600


def test_account_switch_is_detected_even_when_masked_ids_match(api):
    original = api.account_info.return_value
    changed = SimpleNamespace(**vars(original))
    changed.login += 10_000
    api.account_info.side_effect = [original, original, original, changed]
    with MT5Connector(api=api) as connector:
        with pytest.raises(DemoAccountRequiredError, match="changed"):
            connector.inspect()


def test_diagnostic_log_records_failure_without_fake_execution(tmp_path):
    report = {"status": "connection_failed", "checked_at_utc": "2026-11-13T03:00:00+00:00", "error": "Authorization failed", "orders_sent": 0}
    write_diagnostics(report, tmp_path)
    stored = json.loads((tmp_path / "mt5_connection_status.json").read_text())
    assert stored["orders_sent"] == 0
    with (tmp_path / "execution_log.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["account_type"] == "UNVERIFIED"
    assert rows[0]["order_ticket"] == ""
    assert rows[0]["deal_ticket"] == ""


@pytest.mark.parametrize("phrase", ["", "yes", PLACE_CONFIRMATION])
def test_demo_execution_is_disabled_even_with_confirmation(phrase):
    with pytest.raises(DemoExecutionDisabledError):
        place_demo_test_order(phrase)
