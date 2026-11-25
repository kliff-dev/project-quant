import json
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pytest

from engine.demo_executor import CLOSE_CONFIRMATION, MAGIC, PLACE_CONFIRMATION, DemoExecutionDisabledError, DemoExecutor, DemoVerificationError
from engine.mt5_connector import DemoAccountRequiredError, MT5Connector
from engine.protection import RiskProtection


@pytest.fixture
def rig(tmp_path):
    """A complete in-memory broker: no MT5 package, terminal, or network access."""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    api = Record(
        __version__="fake", ACCOUNT_TRADE_MODE_DEMO=0, ACCOUNT_TRADE_MODE_CONTEST=1, ACCOUNT_TRADE_MODE_REAL=2,
        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, POSITION_TYPE_BUY=0,
        ORDER_FILLING_FOK=0, ORDER_FILLING_IOC=1, ORDER_FILLING_RETURN=2,
        SYMBOL_TRADE_EXECUTION_REQUEST=0, SYMBOL_TRADE_EXECUTION_INSTANT=1, SYMBOL_TRADE_EXECUTION_MARKET=2,
        SYMBOL_TRADE_MODE_DISABLED=0, SYMBOL_TRADE_MODE_LONGONLY=1, SYMBOL_TRADE_MODE_FULL=4,
        TRADE_ACTION_DEAL=1, ORDER_TIME_GTC=0, ORDER_STATE_FILLED=4,
        TRADE_RETCODE_DONE=10009, DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_SELL=1, DEAL_REASON_SL=4, DEAL_REASON_TP=5,
    )
    account = Record(login=12345678, company="Test Broker", server="Broker-Demo", currency="USD", trade_mode=0,
                     trade_allowed=True, trade_expert=True, balance=500., equity=500., margin_free=500., leverage=100)
    terminal = Record(connected=True, trade_allowed=True, tradeapi_disabled=False, build=6295)
    symbol = Record(name="EURUSDm", description="Euro versus USD", currency_base="EUR", currency_profit="USD", visible=True,
                    point=.00001, digits=5, volume_min=.01, volume_max=200., volume_step=.01, filling_mode=3,
                    trade_mode=4, trade_exemode=2, order_mode=127, trade_contract_size=100_000.,
                    trade_tick_size=.00001, trade_stops_level=0, trade_freeze_level=0)
    tick = Record(bid=1.1, ask=1.10012, time_msc=now)
    ledger = {"positions": [], "orders": {}, "deals": {}}
    api.initialize, api.shutdown = Mock(return_value=True), Mock()
    api.account_info = Mock(return_value=account)
    api.terminal_info = Mock(return_value=terminal)
    api.symbols_get = Mock(return_value=(symbol,))
    api.symbol_info = Mock(return_value=symbol)
    api.symbol_info_tick = Mock(return_value=tick)
    api.positions_get = Mock(side_effect=lambda: tuple(ledger["positions"]))
    api.orders_get = Mock(return_value=())
    api.order_calc_margin = Mock(return_value=11.06)
    def profit(side, name, volume, entry, exit_price):
        value = (Decimal(str(exit_price)) - Decimal(str(entry))) * Decimal(str(volume)) * 100_000
        if side == 1:
            value = -value
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    api.order_calc_profit = Mock(side_effect=profit)
    api.order_check = Mock(return_value=Record(retcode=0, comment="Done", margin=11.06, margin_free=488.94))
    api.last_error = Mock(return_value=(-1, "Test error"))
    api.history_orders_get = Mock(side_effect=lambda ticket: ledger["orders"].get(ticket, ()))
    api.history_deals_get = Mock(side_effect=lambda ticket: ledger["deals"].get(ticket, ()))

    def send(request):
        closing = "position" in request
        order, deal = (112, 223) if closing else (111, 222)
        price = 1.1 if closing else 1.10013
        ledger["orders"][order] = (Record(ticket=order, symbol="EURUSDm", magic=MAGIC, type=request["type"], state=4),)
        ledger["deals"][order] = (Record(ticket=deal, order=order, position_id=333, symbol="EURUSDm", magic=MAGIC,
                                             type=request["type"], entry=1 if closing else 0, volume=.01, price=price,
                                             profit=-.13 if closing else 0., commission=-.01, fee=0., swap=0., time_msc=now),)
        ledger["positions"] = [] if closing else [Record(ticket=444, identifier=333, symbol="EURUSDm", magic=MAGIC, type=0, volume=.01, price_open=price,
                                                        sl=request.get("sl", 0), tp=request.get("tp", 0))]
        return Record(retcode=10009, order=order, deal=deal, volume=.01, price=price, bid=1.1, ask=1.10012,
                      comment="Done", request_id=1, retcode_external=0)

    api.order_send = Mock(side_effect=send)
    connector = MT5Connector(api=api)
    connector.connect()
    executor = DemoExecutor(connector, tmp_path)
    yield Record(api=api, account=account, terminal=terminal, symbol=symbol, tick=tick, ledger=ledger, executor=executor, directory=tmp_path)
    connector.close()


def test_preview_uses_minimum_fok_and_estimates_without_order_calls(rig):
    preview = rig.executor.prepare_entry()
    assert preview["ready"]
    assert preview["funds"]["balance"] == 500
    assert preview["volume_lots"] == .01
    assert preview["estimated_margin"] == 11.06
    assert preview["estimated_spread_cost"] == .12
    assert preview["request"]["type_filling"] == 0  # FOK enum, not symbol bitmask 3
    assert "price" not in preview["request"]  # Market Execution
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize("condition", ["terminal", "external_api", "account_permission", "expert_permission", "future_tick", "stale_tick", "spread", "positions", "orders", "funds", "symbol_mode", "market_order"])
def test_blockers_never_reach_check_or_send(rig, condition):
    if condition == "terminal":
        rig.terminal.trade_allowed = False
    elif condition == "external_api":
        rig.terminal.tradeapi_disabled = True
    elif condition == "account_permission":
        rig.account.trade_allowed = False
    elif condition == "expert_permission":
        rig.account.trade_expert = False
    elif condition == "future_tick":
        rig.tick.time_msc += 20_000
    elif condition == "stale_tick":
        rig.tick.time_msc -= 200_000
    elif condition == "spread":
        rig.tick.ask = 1.101
    elif condition == "positions":
        rig.ledger["positions"] = [Record(ticket=999)]
    elif condition == "orders":
        rig.api.orders_get.return_value = (Record(ticket=999),)
    elif condition == "funds":
        rig.account.margin_free = 1.
    elif condition == "symbol_mode":
        rig.symbol.trade_mode = 3
    elif condition == "market_order":
        rig.symbol.order_mode = 0
    preview = rig.executor.prepare_entry()
    assert not preview["ready"]
    with pytest.raises(DemoExecutionDisabledError, match="blocked"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize("minimum,step", [(.1, .1), (.01, .003), (0, .01)])
def test_invalid_or_excessive_broker_minimum_stops(rig, minimum, step):
    rig.symbol.volume_min, rig.symbol.volume_step = minimum, step
    with pytest.raises(DemoExecutionDisabledError):
        rig.executor.prepare_entry()
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize("mode", [1, 2])
def test_contest_and_real_accounts_stop_even_when_server_says_demo(rig, mode):
    rig.account.trade_mode = mode
    with pytest.raises(DemoAccountRequiredError):
        rig.executor.prepare_entry()
    rig.api.order_send.assert_not_called()


def test_exact_confirmation_and_current_preview_are_required(rig):
    preview = rig.executor.prepare_entry()
    for phrase in ["yes", "place demo test order", "PLACE DEMO TEST ORDER "]:
        with pytest.raises(DemoExecutionDisabledError, match="confirmation"):
            rig.executor.open_test(preview["preview_id"], phrase)
    with pytest.raises(DemoExecutionDisabledError, match="identity/age"):
        rig.executor.open_test("wrong", PLACE_CONFIRMATION)
    stored = json.loads(rig.executor.preview_path.read_text())
    stored["created_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    rig.executor.preview_path.write_text(json.dumps(stored))
    with pytest.raises(DemoExecutionDisabledError, match="identity/age"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.api.order_send.assert_not_called()


def test_account_switch_after_order_check_stops_send(rig):
    preview = rig.executor.prepare_entry()
    def check(request):
        rig.account.trade_mode = 2
        return Record(retcode=0, comment="Done")
    rig.api.order_check.side_effect = check
    with pytest.raises(DemoAccountRequiredError):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.api.order_send.assert_not_called()


def test_permission_change_after_order_check_stops_send(rig):
    preview = rig.executor.prepare_entry()
    def check(request):
        rig.terminal.trade_allowed = False
        return Record(retcode=0, comment="Done")
    rig.api.order_check.side_effect = check
    with pytest.raises(DemoExecutionDisabledError, match="permission"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.api.order_send.assert_not_called()


def test_order_check_rejection_never_sends(rig):
    preview = rig.executor.prepare_entry()
    rig.api.order_check.return_value = Record(retcode=10018, comment="Market closed")
    with pytest.raises(DemoExecutionDisabledError, match="Market closed"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize("outcome", ["none", "rejected", "partial", "exception", "missing_deal"])
def test_uncertain_or_failed_send_is_never_repeated(rig, outcome):
    preview = rig.executor.prepare_entry()
    if outcome == "none":
        rig.api.order_send.side_effect = lambda request: None
    elif outcome in ("rejected", "partial"):
        rig.api.order_send.side_effect = lambda request: Record(retcode=10010 if outcome == "partial" else 10018, comment=outcome)
    elif outcome == "exception":
        rig.api.order_send.side_effect = RuntimeError("IPC response lost")
    else:
        rig.api.history_deals_get.side_effect = lambda ticket: ()
    with pytest.raises(DemoVerificationError):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert rig.executor.state_path.is_file()
    with pytest.raises(DemoExecutionDisabledError):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert rig.api.order_send.call_count == 1


def test_verified_open_and_separately_confirmed_close_reconcile_actual_deals(rig):
    preview = rig.executor.prepare_entry()
    opened = rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert opened["status"] == "open_verified"
    assert opened["position_ticket"] == 444
    assert opened["executed_volume"] == .01
    assert opened["executed_price"] == 1.10013
    assert opened["entry_slippage_points"] == pytest.approx(1)
    assert rig.api.order_send.call_count == 1
    close = rig.executor.prepare_close()
    assert close["request"]["position"] == 444
    assert close["side"] == "sell"
    assert rig.api.order_send.call_count == 1
    with pytest.raises(DemoExecutionDisabledError):
        rig.executor.close_test(close["preview_id"], PLACE_CONFIRMATION)
    closed = rig.executor.close_test(close["preview_id"], CLOSE_CONFIRMATION)
    assert closed["status"] == "closed_verified"
    assert closed["realized_net_pnl"] == pytest.approx(-.15)
    assert not rig.ledger["positions"]
    assert rig.api.order_send.call_count == 2
    assert rig.api.order_check.call_count == 2
    assert not rig.executor.prepare_entry()["ready"]


def test_broker_minimum_below_cap_is_used_without_rounding_up(rig):
    rig.symbol.volume_min = rig.symbol.volume_step = .001
    assert rig.executor.prepare_entry()["volume_lots"] == .001


def test_ioc_is_explicit_and_unsupported_filling_never_falls_back(rig):
    rig.symbol.filling_mode = 2
    preview = rig.executor.prepare_entry()
    assert preview["request"]["type_filling"] == 1
    assert "partial" in preview["filling_policy"]
    rig.symbol.filling_mode = 0
    with pytest.raises(DemoExecutionDisabledError, match="FOK/IOC"):
        rig.executor.prepare_entry()


def test_existing_lock_prevents_any_attempt(rig):
    rig.executor.lock_path.write_text("previous process")
    with pytest.raises(DemoExecutionDisabledError, match="lock"):
        rig.executor.prepare_entry()
    rig.api.order_send.assert_not_called()


def test_changed_test_position_is_never_closed(rig):
    preview = rig.executor.prepare_entry()
    rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    close = rig.executor.prepare_close()
    rig.ledger["positions"][0].volume = .02
    with pytest.raises(DemoExecutionDisabledError, match="changed"):
        rig.executor.close_test(close["preview_id"], CLOSE_CONFIRMATION)
    assert rig.api.order_send.call_count == 1


def test_order_result_alone_does_not_verify_position(rig):
    preview = rig.executor.prepare_entry()
    original_send = rig.api.order_send.side_effect
    def send_without_position(request):
        result = original_send(request)
        rig.ledger["positions"] = []
        return result
    rig.api.order_send.side_effect = send_without_position
    with pytest.raises(DemoVerificationError, match="unique open position"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert json.loads(rig.executor.state_path.read_text())["status"] == "open_unverified"


def test_uncertain_close_cannot_be_reopened_by_reverifying_original_entry(rig):
    preview = rig.executor.prepare_entry()
    rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    close = rig.executor.prepare_close()
    rig.api.order_send.side_effect = lambda request: None
    with pytest.raises(DemoVerificationError):
        rig.executor.close_test(close["preview_id"], CLOSE_CONFIRMATION)
    with pytest.raises(DemoVerificationError, match="phase"):
        rig.executor.verify(closing=False)
    with pytest.raises(DemoExecutionDisabledError):
        rig.executor.close_test(close["preview_id"], CLOSE_CONFIRMATION)
    assert rig.api.order_send.call_count == 2  # one open, one uncertain close


def test_cli_defaults_to_preview_without_order_calls(rig, monkeypatch, capsys):
    from scripts import test_demo_trade
    monkeypatch.setattr(test_demo_trade, "MT5Connector", lambda *args, **kwargs: MT5Connector(api=rig.api))
    assert test_demo_trade.main(["--log-dir", str(rig.directory)]) == 0
    assert '"ready": true' in capsys.readouterr().out
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


def test_cli_reports_blocked_preview_without_prompting(rig, monkeypatch):
    from scripts import test_demo_trade
    rig.terminal.trade_allowed = False
    monkeypatch.setattr(test_demo_trade, "MT5Connector", lambda *args, **kwargs: MT5Connector(api=rig.api))
    assert test_demo_trade.main(["--log-dir", str(rig.directory)]) == 2
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


def test_large_ticket_query_failure_uses_read_only_interval_and_exact_ticket_filter(rig):
    original_send = rig.api.order_send.side_effect
    def send(request):
        result = original_send(request)
        order = rig.ledger["orders"].pop(result.order)[0]
        deal = rig.ledger["deals"].pop(result.order)[0]
        result.order, result.deal = 2846998088, 2545515037
        order.ticket, deal.order, deal.ticket = result.order, result.order, result.deal
        rig.ledger["orders"][result.order] = (order,)
        rig.ledger["deals"][result.order] = (deal,)
        return result
    def lookup(*args, **kwargs):
        if kwargs:
            assert kwargs["ticket"] == 2846998088
            return None
        assert len(args) == 2 and args[0].tzinfo is not None
        return tuple(d for records in rig.ledger["deals"].values() for d in records)
    rig.api.order_send.side_effect = send
    rig.api.history_deals_get.side_effect = lookup
    preview = rig.executor.prepare_entry()
    result = rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert result["status"] == "open_verified"
    assert result["entry_verified_deals"][0]["ticket"] == 2545515037
    assert result["entry_deal_history_lookup"]["method"] == "time_range_after_ticket_error"
    assert rig.api.order_send.call_count == 1
    assert rig.api.history_deals_get.call_count == 2


def test_both_deal_history_query_modes_failing_never_resends(rig):
    rig.api.history_deals_get.side_effect = lambda *args, **kwargs: None
    preview = rig.executor.prepare_entry()
    with pytest.raises(DemoVerificationError, match="unavailable"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert rig.api.order_send.call_count == 1
    assert rig.api.history_deals_get.call_count == 2
    with pytest.raises(DemoExecutionDisabledError):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert rig.api.order_send.call_count == 1


def test_protected_preview_attaches_levels_and_checks_broker_without_sending(rig):
    rig.executor.protection = RiskProtection(5, 2)
    preview = rig.executor.prepare_entry()
    assert preview["ready"]
    assert preview["request"]["sl"] == 1.07512
    assert preview["request"]["tp"] == 1.15012
    assert preview["protection"]["estimated_loss_at_sl"] == 25
    assert preview["protection"]["estimated_profit_at_tp"] == 50
    assert preview["broker_check"]["retcode"] == 0
    rig.api.order_check.assert_called_once()
    rig.api.order_send.assert_not_called()


def test_protected_broker_rejection_never_substitutes_an_unprotected_order(rig):
    rig.executor.protection = RiskProtection(5, 2)
    rig.api.order_check.return_value = Record(retcode=10016, comment="Invalid stops")
    preview = rig.executor.prepare_entry()
    assert not preview["ready"]
    with pytest.raises(DemoExecutionDisabledError):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.api.order_send.assert_not_called()
    assert rig.api.order_check.call_count == 1


def test_actual_broker_position_levels_and_fill_risk_are_verified(rig):
    rig.executor.protection = RiskProtection(5, 2)
    preview = rig.executor.prepare_entry()
    result = rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert result["status"] == "open_verified"
    assert rig.ledger["positions"][0].sl == preview["request"]["sl"]
    assert rig.ledger["positions"][0].tp == preview["request"]["tp"]
    # Fake broker slips one point adversely: planned risk is not a fill guarantee.
    assert result["protection_at_fill"]["estimated_loss_at_sl"] == 25.01
    assert result["protection_at_fill"]["budget_exceeded_by_fill"] is True
    assert rig.api.order_send.call_count == 1


@pytest.mark.parametrize("missing", ["sl", "tp"])
def test_missing_actual_protection_fails_verification_without_modifying_or_resending(rig, missing):
    rig.executor.protection = RiskProtection(5, 2)
    preview = rig.executor.prepare_entry()
    send = rig.api.order_send.side_effect
    def missing_level(request):
        result = send(request)
        setattr(rig.ledger["positions"][0], missing, 0)
        return result
    rig.api.order_send.side_effect = missing_level
    with pytest.raises(DemoVerificationError, match="approved SL/TP"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    assert rig.api.order_send.call_count == 1
    assert json.loads(rig.executor.state_path.read_text())["status"] == "open_unverified"


def test_quote_movement_cannot_increase_checked_order_risk(rig):
    rig.executor.protection = RiskProtection(5, 2)
    preview = rig.executor.prepare_entry()
    def check(request):
        rig.tick.ask += .00001
        return Record(retcode=0, comment="Done")
    rig.api.order_check.side_effect = check
    with pytest.raises(DemoExecutionDisabledError, match="Quote moved"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.api.order_send.assert_not_called()


def test_balance_increase_cannot_raise_previously_approved_budget(rig):
    rig.executor.protection = RiskProtection(5, 2)
    preview = rig.executor.prepare_entry()
    rig.account.balance = rig.account.equity = 600
    with pytest.raises(DemoExecutionDisabledError, match="reviewed risk budget"):
        rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.api.order_send.assert_not_called()


def test_named_session_retains_closed_legacy_record_and_restores_policy(rig):
    legacy = rig.executor.state_path
    legacy.write_text(json.dumps({"status": "closed_verified", "old_order": 111}))
    previous_bytes = legacy.read_bytes()
    executor = DemoExecutor(rig.executor.connector, rig.directory, protection=RiskProtection(5, 2), session_id="protected-test")
    preview = executor.prepare_entry()
    assert preview["ready"]
    assert legacy.read_bytes() == previous_bytes
    assert executor.state_path != legacy
    assert executor.lock_path == rig.executor.lock_path
    restored = DemoExecutor(rig.executor.connector, rig.directory, session_id="protected-test")
    assert restored.protection == RiskProtection(5, 2)
    rig.api.order_send.assert_not_called()


def test_unresolved_other_session_blocks_all_new_entries(rig):
    rig.executor.state_path.write_text(json.dumps({"status": "open_unverified"}))
    executor = DemoExecutor(rig.executor.connector, rig.directory, protection=RiskProtection(5, 2), session_id="another-test")
    preview = executor.prepare_entry()
    assert not preview["ready"]
    assert any("Another session" in reason for reason in preview["blockers"])
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize("name", ["../outside", "a/b", "", "x" * 49])
def test_session_name_cannot_escape_log_root(rig, name):
    with pytest.raises(ValueError, match="Session"):
        DemoExecutor(rig.executor.connector, rig.directory, session_id=name)


@pytest.mark.parametrize("reason", [4, 5])
def test_broker_triggered_protected_exit_is_verified_without_a_second_send(rig, reason):
    rig.executor.protection = RiskProtection(5, 2)
    preview = rig.executor.prepare_entry()
    opened = rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    exit_price = opened["request"]["sl" if reason == 4 else "tp"]
    profit = round((exit_price - opened["executed_price"]) * 1000, 2)
    rig.ledger["positions"] = []
    rig.ledger["orders"][112] = (Record(ticket=112, symbol="EURUSDm", type=1, state=4, position_id=333),)
    exit_deal = Record(ticket=223, order=112, position_id=333, symbol="EURUSDm", type=1, entry=1, reason=reason,
                       volume=.01, price=exit_price, profit=profit, commission=0., fee=0., swap=0., time_msc=1234)
    original = rig.ledger["deals"][111][0]
    rig.api.history_deals_get.side_effect = lambda *args, **kwargs: (original, exit_deal)
    closed = rig.executor.verify(closing=True)
    assert closed["status"] == "closed_verified"
    assert closed["automatic_exit"]["reasons"] == ["stop_loss" if reason == 4 else "take_profit"]
    assert closed["realized_net_pnl"] == pytest.approx(profit - .01)
    assert rig.api.order_send.call_count == 1  # verifier never sends the exit


def test_protected_position_absence_without_deals_is_not_closure_proof(rig):
    rig.executor.protection = RiskProtection(5, 2)
    preview = rig.executor.prepare_entry()
    rig.executor.open_test(preview["preview_id"], PLACE_CONFIRMATION)
    rig.ledger["positions"] = []
    rig.api.history_deals_get.side_effect = lambda *args, **kwargs: ()
    with pytest.raises(DemoVerificationError, match="Exit is missing"):
        rig.executor.verify(closing=True)
    assert rig.api.order_send.call_count == 1


def test_cli_protected_preview_in_named_session_is_read_only(rig, monkeypatch, capsys):
    from scripts import test_demo_trade
    monkeypatch.setattr(test_demo_trade, "MT5Connector", lambda *args, **kwargs: MT5Connector(api=rig.api))
    args = ["--log-dir", str(rig.directory), "--session", "five-percent", "--risk-percent", "5", "--reward-risk", "2"]
    assert test_demo_trade.main(args) == 0
    assert '"stop_loss"' in capsys.readouterr().out
    assert (rig.directory / "sessions" / "five-percent" / "demo_order_preview.json").is_file()
    rig.api.order_send.assert_not_called()


def strategy_executor(rig,monkeypatch,side='buy'):
    from engine import strategy_orders
    from engine.strategy_orders import StrategyIntent,StrategyPolicy
    now=datetime(2026,11,13,8,0,30,tzinfo=timezone.utc)
    monkeypatch.setattr(strategy_orders,'utc_now',lambda:now)
    intent=StrategyIntent('EURUSDm',side,1.09812 if side=='buy' else 1.102,.001,
                          '2026-11-13T08:00:00+00:00','a'*64,'pullback')
    return DemoExecutor(rig.executor.connector,rig.directory,session_id='strategy-one-percent',
                        strategy_policy=StrategyPolicy(),strategy_intent=intent)


def dynamic_fake_send(rig):
    original=rig.api.order_send.side_effect
    def send(request):
        result=original(request)
        volume=request['volume']
        result.volume=volume
        for deal in rig.ledger['deals'][result.order]: deal.volume=volume
        for position in rig.ledger['positions']:
            position.volume=volume
            position.type=request['type']
        return result
    rig.api.order_send.side_effect=send


def test_strategy_preview_sizes_above_legacy_cap_without_sending(rig,monkeypatch):
    executor=strategy_executor(rig,monkeypatch)
    preview=executor.prepare_entry()
    assert preview['ready'] and preview['volume_lots']==.02
    assert preview['request']['sl']==1.09812
    assert preview['protection']['risk_percent']==1
    assert preview['protection']['planned_loss_with_reserves']<=5
    assert preview['estimated_spread_cost']==.24
    rig.api.order_send.assert_not_called()
    assert rig.api.order_calc_margin.call_args.args[2]==.02


@pytest.mark.parametrize('side',['buy','sell'])
def test_strategy_fill_and_larger_position_close_still_require_confirmations(rig,monkeypatch,side):
    executor=strategy_executor(rig,monkeypatch,side)
    dynamic_fake_send(rig)
    # Keep the fake SELL entry at the quote; legacy fake always fills BUY ask.
    if side=='sell':
        original=rig.api.order_send.side_effect
        def sell_send(request):
            result=original(request)
            if 'position' not in request:
                result.price=1.1
                rig.ledger['deals'][result.order][0].price=1.1
                rig.ledger['positions'][0].price_open=1.1
            return result
        rig.api.order_send.side_effect=sell_send
    preview=executor.prepare_entry()
    with pytest.raises(DemoExecutionDisabledError): executor.open_test(preview['preview_id'],'yes')
    rig.api.order_send.assert_not_called()
    opened=executor.open_test(preview['preview_id'],PLACE_CONFIRMATION)
    assert opened['executed_volume']==.02
    assert not opened['protection_at_fill']['budget_exceeded_by_fill']
    close=executor.prepare_close()
    with pytest.raises(DemoExecutionDisabledError): executor.close_test(close['preview_id'],'yes')
    assert rig.api.order_send.call_count==1
    closed=executor.close_test(close['preview_id'],CLOSE_CONFIRMATION)
    assert closed['status']=='closed_verified'
    assert rig.api.order_send.call_count==2
    assert rig.api.order_send.call_args.args[0]['position']==444
    assert rig.api.order_send.call_args.args[0]['volume']==.02


def test_strategy_request_remains_guarded_against_quote_movement(rig,monkeypatch):
    executor=strategy_executor(rig,monkeypatch)
    preview=executor.prepare_entry()
    def check(request):
        rig.tick.ask+=.0001
        rig.tick.bid+=.0001
        return Record(retcode=0,comment='Done')
    rig.api.order_check.side_effect=check
    with pytest.raises(DemoExecutionDisabledError,match='Quote moved'):
        executor.open_test(preview['preview_id'],PLACE_CONFIRMATION)
    rig.api.order_send.assert_not_called()


def test_strategy_preview_restores_intent_and_policy(rig,monkeypatch):
    executor=strategy_executor(rig,monkeypatch)
    executor.prepare_entry()
    restored=DemoExecutor(rig.executor.connector,rig.directory,session_id='strategy-one-percent')
    assert restored.strategy_intent==executor.strategy_intent
    assert restored.strategy_policy==executor.strategy_policy
    assert restored.protection is None


@pytest.mark.parametrize('volume',[.01,.005])
def test_desktop_close_requires_actual_full_exit_not_merely_absent_position(rig,volume):
    rig.executor.protection=RiskProtection(5,2)
    preview=rig.executor.prepare_entry()
    opened=rig.executor.open_test(preview['preview_id'],PLACE_CONFIRMATION)
    rig.ledger['positions']=[]
    rig.ledger['orders'][112]=(Record(ticket=112,symbol='EURUSDm',type=1,state=4,position_id=333),)
    original=rig.ledger['deals'][111][0]
    exit_deal=Record(ticket=223,order=112,position_id=333,symbol='EURUSDm',type=1,entry=1,reason=0,
                     volume=volume,price=1.1,profit=-.13,commission=0.,fee=0.,swap=0.,time_msc=1234)
    rig.api.history_deals_get.side_effect=lambda *args,**kwargs:(original,exit_deal)
    if volume==.01:
        closed=rig.executor.reconcile_external_close()
        assert closed['status']=='closed_verified'
        assert closed['external_exit']['reasons']==['desktop_terminal']
        assert 'automatic_exit' not in closed and 'close_result' not in closed
        assert closed['protection']['risk_percent']==5  # Historical policy is preserved.
    else:
        with pytest.raises(DemoVerificationError,match='volume'):
            rig.executor.reconcile_external_close()
        assert json.loads(rig.executor.state_path.read_text())['status']=='open_verified'
    assert rig.api.order_send.call_count==1


def test_no_strategy_signal_does_not_check_or_send_orders(rig,monkeypatch,capsys):
    from scripts import test_demo_trade
    monkeypatch.setattr(test_demo_trade,'MT5Connector',lambda *args,**kwargs:MT5Connector(api=rig.api))
    monkeypatch.setattr(test_demo_trade,'latest_strategy_intent',lambda *args:(None,{'status':'WAIT','orders_sent':0,'reason':'No signal'}))
    assert test_demo_trade.main(['--strategy','--session','waiting','--log-dir',str(rig.directory)])==2
    assert 'WAIT' in capsys.readouterr().out
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('reason',[4,5])
def test_strategy_sell_protective_exit_reconciles_buy_exit_without_sending(rig,monkeypatch,reason):
    executor=strategy_executor(rig,monkeypatch,'sell')
    dynamic_fake_send(rig)
    preview=executor.prepare_entry()
    opened=executor.open_test(preview['preview_id'],PLACE_CONFIRMATION)
    price=opened['request']['sl' if reason==4 else 'tp']
    volume=opened['executed_volume']
    rig.ledger['positions']=[]
    rig.ledger['orders'][112]=(Record(ticket=112,symbol='EURUSDm',type=0,state=4,position_id=333),)
    original=rig.ledger['deals'][111][0]
    exit_deal=Record(ticket=223,order=112,position_id=333,symbol='EURUSDm',type=0,entry=1,reason=reason,
                     volume=volume,price=price,profit=(opened['executed_price']-price)*volume*100000,
                     commission=0.,fee=0.,swap=0.,time_msc=1234)
    rig.api.history_deals_get.side_effect=lambda *args,**kwargs:(original,exit_deal)
    closed=executor.verify(closing=True)
    assert closed['status']=='closed_verified'
    assert closed['automatic_exit']['reasons']==['stop_loss' if reason==4 else 'take_profit']
    assert rig.api.order_send.call_count==1


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_monitor_uses_real_guarded_preview_path_without_submitting(rig, monkeypatch, side):
    from dataclasses import asdict
    from engine import market_monitor, mt5_connector, demo_executor
    from engine.market_monitor import MarketMonitor

    strategy = strategy_executor(rig, monkeypatch, side)
    fixed = datetime(2026, 11, 13, 8, 0, 30, tzinfo=timezone.utc)
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(mt5_connector, 'datetime', FrozenDatetime)
    monkeypatch.setattr(demo_executor, 'datetime', FrozenDatetime)
    rig.tick.time_msc = int(fixed.timestamp() * 1000)
    rig.api.TIMEFRAME_H1 = 16385
    hour = int(fixed.replace(minute=0, second=0).timestamp())
    rig.api.copy_rates_from_pos = Mock(return_value=[{'time': hour - 3600}, {'time': hour}])
    monkeypatch.setattr(market_monitor, 'latest_strategy_intent', lambda *args: (
        strategy.strategy_intent, dict(status='signal', orders_sent=0,
                                       decision_time_utc=strategy.strategy_intent.decision_time_utc)))
    policy = rig.directory / 'policy.json'
    policy.write_text(json.dumps(asdict(strategy.strategy_policy)), encoding='utf-8')
    monitor = MarketMonitor(rig.executor.connector, rig.directory, policy,
                            now=lambda: fixed, emit=lambda _: None)
    result = monitor.run(once=True)
    assert result['status'] == 'stopped'
    candidate = result['latest_decision']
    assert candidate['status'] == 'READY_FOR_CONFIRMATION'
    assert candidate['volume_lots'] == .02
    assert candidate['protection']['planned_loss_with_reserves'] <= 5
    assert 'awaiting explicit confirmation' in result['preview_status']
    rig.api.order_check.assert_called_once()
    rig.api.order_send.assert_not_called()
    assert not list(rig.directory.glob('sessions/*/demo_test_state.json'))


def test_monitor_complete_history_and_no_signal_never_check_or_send(rig):
    from dataclasses import asdict
    import pandas as pd
    from engine.market_monitor import MarketMonitor
    from engine.strategy_orders import StrategyPolicy
    now = datetime.now(timezone.utc)
    hours = pd.date_range(end=pd.Timestamp(now).floor('h'), periods=3001, freq='h')
    frame = pd.DataFrame(dict(time=hours.as_unit('s').astype('int64'), open=1.1, high=1.101,
                              low=1.099, close=1.1, spread=8, tick_volume=100, real_volume=0))
    rig.symbol.chart_mode = 0
    rig.api.TIMEFRAME_H1 = 16385
    rig.api.copy_rates_from_pos = Mock(side_effect=lambda symbol, timeframe, start, count:
                                      frame.tail(count).to_records(index=False))
    policy = rig.directory / 'policy.json'
    policy.write_text(json.dumps(asdict(StrategyPolicy())), encoding='utf-8')
    result = MarketMonitor(rig.executor.connector, rig.directory, policy, emit=lambda _: None).run(once=True)
    assert result['status'] == 'stopped'
    assert result['latest_decision']['status'] == 'WAIT'
    assert result['latest_decision']['bars'] == 3000
    assert [call.args[-1] for call in rig.api.copy_rates_from_pos.call_args_list] == [3, 3001]
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


def authorized_rig(rig, monkeypatch, side='buy', seconds=3600):
    from dataclasses import asdict
    from engine import demo_authorization, demo_executor, mt5_connector, strategy_orders
    from engine.demo_authorization import DemoEntryAuthorization
    strategy = strategy_executor(rig, monkeypatch, side)
    clock = Record(now=datetime(2026, 11, 13, 8, 0, 30, tzinfo=timezone.utc), ticks=0)
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz else clock.now.replace(tzinfo=None)
    monkeypatch.setattr(demo_executor, 'datetime', FrozenDatetime)
    monkeypatch.setattr(mt5_connector, 'datetime', FrozenDatetime)
    monkeypatch.setattr(demo_authorization, 'utc_now', lambda: clock.now)
    monkeypatch.setattr(strategy_orders, 'utc_now', lambda: clock.now)
    rig.tick.time_msc = int(clock.now.timestamp() * 1000)
    policy = rig.directory / 'authorized-policy.json'
    policy.write_text(json.dumps(asdict(strategy.strategy_policy)), encoding='utf-8')
    authorization = DemoEntryAuthorization.create(rig.executor.connector, rig.directory, policy,
                    expires_at=clock.now + timedelta(seconds=seconds),
                    user_instruction='Authorize one unattended DEMO entry only when the registered strategy has a valid setup.')
    return Record(executor=strategy, authorization=authorization, policy=policy, clock=clock, run_id='c' * 32)


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_one_use_authority_opens_one_actual_verified_protected_demo_position(rig, monkeypatch, side):
    ctx = authorized_rig(rig, monkeypatch, side)
    dynamic_fake_send(rig)
    preview = ctx.executor.prepare_entry()
    result = ctx.executor.open_authorized_session(preview['preview_id'], ctx.authorization.authorization_id,
                                                  monitor_run_id=ctx.run_id)
    assert result['status'] == 'open_verified'
    assert result['requested_volume'] == .02
    assert result['position_ticket'] == 444
    assert result['session_authorization']['authorization_id'] == ctx.authorization.authorization_id
    assert rig.api.order_send.call_args.args[0]['sl'] == preview['request']['sl']
    assert rig.api.order_send.call_args.args[0]['tp'] == preview['request']['tp']
    usage = json.loads(ctx.authorization.usage_path.read_text())
    assert usage['status'] == 'entry_verified' and usage['deal_ticket'] == 222
    with pytest.raises(ValueError, match='reserved/consumed'):
        ctx.executor.open_authorized_session(preview['preview_id'], ctx.authorization.authorization_id,
                                             monitor_run_id=ctx.run_id)
    assert rig.api.order_send.call_count == 1


@pytest.mark.parametrize('failure', ['none', 'exception', 'rejection', 'partial', 'missing_position', 'missing_sl'])
def test_failed_or_uncertain_authorized_send_is_never_retried(rig, monkeypatch, failure):
    ctx = authorized_rig(rig, monkeypatch)
    dynamic_fake_send(rig)
    original = rig.api.order_send.side_effect
    def send(request):
        if failure == 'none': return None
        if failure == 'exception': raise TimeoutError('IPC timeout')
        result = original(request)
        if failure in ('rejection', 'partial'): result.retcode = 10030 if failure == 'rejection' else 10010
        if failure == 'missing_position': rig.ledger['positions'] = []
        if failure == 'missing_sl': rig.ledger['positions'][0].sl = 0
        return result
    rig.api.order_send.side_effect = send
    preview = ctx.executor.prepare_entry()
    with pytest.raises(DemoVerificationError):
        ctx.executor.open_authorized_session(preview['preview_id'], ctx.authorization.authorization_id,
                                             monitor_run_id=ctx.run_id)
    assert ctx.authorization.usage_path.exists()
    with pytest.raises(ValueError, match='reserved/consumed'):
        ctx.executor.open_authorized_session(preview['preview_id'], ctx.authorization.authorization_id,
                                             monitor_run_id='d' * 32)
    assert rig.api.order_send.call_count == 1


@pytest.mark.parametrize('failure', ['real', 'contest', 'account_unknown', 'account_switch', 'expired', 'revoked', 'policy', 'risk_ceiling'])
def test_authority_scope_failure_prevents_any_send(rig, monkeypatch, failure):
    from engine.demo_authorization import DemoEntryAuthorization
    ctx = authorized_rig(rig, monkeypatch)
    preview = ctx.executor.prepare_entry()
    if failure == 'real': rig.account.trade_mode = 2
    if failure == 'contest': rig.account.trade_mode = 1
    if failure == 'account_unknown': rig.api.account_info.return_value = None
    if failure == 'account_switch': rig.account.login += 1
    if failure == 'expired': ctx.clock.now += timedelta(hours=2)
    if failure == 'revoked': ctx.authorization.revoked_path.write_text('{}')
    if failure == 'policy':
        policy = json.loads(ctx.policy.read_text())
        policy['risk_percent'] = .5
        ctx.policy.write_text(json.dumps(policy))
    if failure == 'risk_ceiling':
        approval = json.loads(ctx.authorization.path.read_text())
        approval['max_risk_usd'] = 1
        ctx.authorization.path.write_text(json.dumps(approval))
    with pytest.raises((ValueError, DemoAccountRequiredError, DemoExecutionDisabledError)):
        ctx.executor.open_authorized_session(preview['preview_id'], ctx.authorization.authorization_id,
                                             monitor_run_id=ctx.run_id)
    rig.api.order_send.assert_not_called()
    if failure == 'risk_ceiling':
        assert DemoEntryAuthorization(rig.directory, ctx.authorization.authorization_id).usage_path.exists()


@pytest.mark.parametrize('change', ['expiry', 'stop', 'revocation', 'policy', 'real', 'tamper', 'broker_check'])
def test_authority_is_rechecked_after_broker_check(rig, monkeypatch, change):
    from engine.execution_audit import atomic_json
    ctx = authorized_rig(rig, monkeypatch, seconds=60)
    preview = ctx.executor.prepare_entry()
    def check(request):
        if change == 'expiry': ctx.clock.now += timedelta(seconds=70)
        if change == 'stop': atomic_json(rig.directory / 'monitor/stop.json', {'run_id': ctx.run_id})
        if change == 'revocation': ctx.authorization.revoked_path.write_text('{}')
        if change == 'policy':
            policy = json.loads(ctx.policy.read_text())
            policy['risk_percent'] = .5
            ctx.policy.write_text(json.dumps(policy))
        if change == 'real': rig.account.trade_mode = 2
        if change == 'tamper': ctx.authorization.path.write_text(ctx.authorization.path.read_text() + '\n')
        return Record(retcode=10017 if change == 'broker_check' else 0, comment='test')
    rig.api.order_check.side_effect = check
    with pytest.raises((ValueError, DemoAccountRequiredError, DemoExecutionDisabledError)):
        ctx.executor.open_authorized_session(preview['preview_id'], ctx.authorization.authorization_id,
                                             monitor_run_id=ctx.run_id)
    rig.api.order_send.assert_not_called()
    assert ctx.authorization.usage_path.exists()
    with pytest.raises(ValueError): ctx.authorization.assert_unused()


def test_reserved_authority_survives_restart_without_retry(rig, monkeypatch):
    from engine.demo_authorization import DemoEntryAuthorization
    ctx = authorized_rig(rig, monkeypatch)
    preview = ctx.executor.prepare_entry()
    ctx.authorization.reserve(rig.executor.connector, preview_id=preview['preview_id'],
                               session_id=ctx.executor.session_id, run_id=ctx.run_id)
    restored = DemoEntryAuthorization(rig.directory, ctx.authorization.authorization_id)
    with pytest.raises(ValueError): restored.assert_unused()
    with pytest.raises(ValueError):
        ctx.executor.open_authorized_session(preview['preview_id'], restored.authorization_id,
                                             monitor_run_id='d' * 32)
    rig.api.order_send.assert_not_called()


def test_legacy_entry_cannot_use_unattended_authority(rig, monkeypatch):
    ctx = authorized_rig(rig, monkeypatch)
    preview = rig.executor.prepare_entry()
    with pytest.raises(DemoExecutionDisabledError, match='strategy-sized'):
        rig.executor.open_authorized_session(preview['preview_id'], ctx.authorization.authorization_id,
                                             monitor_run_id=ctx.run_id)
    rig.api.order_send.assert_not_called()


def session_monitor(rig, monkeypatch, *, signal=True, seconds=3600):
    from engine import market_monitor
    from engine.demo_session_monitor import DemoSessionMonitor
    from engine.market_monitor import MonitorConfig
    ctx = authorized_rig(rig, monkeypatch, seconds=seconds)
    rig.api.TIMEFRAME_H1 = 16385
    def rates(*args):
        hour = int(ctx.clock.now.replace(minute=0, second=0).timestamp())
        return [{'time': hour - 3600}, {'time': hour}]
    rig.api.copy_rates_from_pos = Mock(side_effect=rates)
    def decision(*args):
        return (ctx.executor.strategy_intent if signal else None,
                dict(status='signal' if signal else 'WAIT', orders_sent=0,
                     decision_time_utc=ctx.executor.strategy_intent.decision_time_utc,
                     reason='test signal' if signal else 'No setup'))
    monkeypatch.setattr(market_monitor, 'latest_strategy_intent', decision)
    ctx.after_sleep = lambda: None
    def sleep(seconds):
        ctx.clock.ticks += seconds
        ctx.clock.now += timedelta(seconds=seconds)
        rig.tick.time_msc = int(ctx.clock.now.timestamp() * 1000)
        ctx.after_sleep()
    ctx.monitor = DemoSessionMonitor(rig.executor.connector, rig.directory, ctx.policy,
                    authorization_id=ctx.authorization.authorization_id, config=MonitorConfig(.01, 10),
                    report_path=rig.directory / 'monitor_report.md',
                    now=lambda: ctx.clock.now, monotonic=lambda: ctx.clock.ticks, sleep=sleep, emit=lambda _: None)
    return ctx


def test_unattended_monitor_waits_without_setup_and_keeps_authority_unused(rig, monkeypatch):
    ctx = session_monitor(rig, monkeypatch, signal=False)
    report = ctx.monitor.run()
    assert report['status'] == 'stopped' and report['mode'] == 'demo_once'
    assert report['poll_count'] == 4
    assert report['latest_decision']['status'] == 'WAIT'
    assert not ctx.authorization.usage_path.exists()
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


def test_unattended_monitor_fills_once_and_stops_after_verified_position(rig, monkeypatch):
    ctx = session_monitor(rig, monkeypatch)
    dynamic_fake_send(rig)
    report = ctx.monitor.run()
    assert report['status'] == 'stopped' and report['finish_requested']
    assert report['poll_count'] == 1
    assert report['latest_decision']['status'] == 'EXECUTED_VERIFIED'
    assert report['orders_sent'] == report['entry_attempts'] == 1
    assert report['authorization']['consumed']
    assert report['execution_result']['position_ticket'] == 444
    human_report = (rig.directory / 'monitor_report.md').read_text(encoding='utf-8')
    assert 'Order / deal / position: 111 / 222 / 444' in human_report
    assert 'maximum entry attempts: 1' in human_report
    assert 'Review the preview file before running' not in human_report
    rig.api.order_send.assert_called_once()
    assert rig.api.order_check.call_count == 2  # Preview then fresh execution check.
    assert len(rig.ledger['positions']) == 1  # No unapproved closing or second order.
    restarted = ctx.monitor.run()
    assert restarted['status'] == 'failed'
    assert rig.api.order_send.call_count == 1


def test_unattended_monitor_stops_on_uncertain_send_and_does_not_retry(rig, monkeypatch):
    ctx = session_monitor(rig, monkeypatch)
    rig.api.order_send.side_effect = None
    rig.api.order_send.return_value = None
    report = ctx.monitor.run()
    assert report['status'] == 'failed'
    assert report['orders_sent'] == report['entry_attempts'] == 1
    assert report['latest_decision']['status'] == 'EXECUTION_STOPPED'
    assert report['authorization']['consumed']
    rig.api.order_send.assert_called_once()


def test_stop_waiting_session_revokes_authority_without_closing(rig, monkeypatch):
    from engine.market_monitor import request_stop
    ctx = session_monitor(rig, monkeypatch, signal=False)
    ctx.after_sleep = lambda: request_stop(rig.directory)
    report = ctx.monitor.run()
    assert report['status'] == 'stopped' and report['stop_reason'] == 'User requested stop.'
    assert ctx.authorization.revoked_path.exists()
    assert not ctx.authorization.usage_path.exists()
    rig.api.order_send.assert_not_called()
    assert ctx.monitor.run()['status'] == 'failed'


def test_authorized_deadline_does_not_extend_on_monitor_restart(rig, monkeypatch):
    ctx = session_monitor(rig, monkeypatch, signal=False, seconds=20)
    report = ctx.monitor.run()
    assert report['status'] == 'stopped'
    assert report['deadline_utc'] == ctx.authorization.expires.isoformat()
    assert report['poll_count'] == 2
    assert ctx.clock.ticks == 20
    rig.api.order_send.assert_not_called()


def test_expiry_during_last_native_verification_prevents_send(rig, monkeypatch):
    ctx = authorized_rig(rig, monkeypatch, seconds=60)
    preview = ctx.executor.prepare_entry()
    original = rig.executor.connector.verify_demo_account
    after_reservation_calls = 0
    def verify():
        nonlocal after_reservation_calls
        result = original()
        if ctx.executor.state_path.exists():
            after_reservation_calls += 1
            if after_reservation_calls == 2:
                ctx.clock.now += timedelta(seconds=70)
        return result
    monkeypatch.setattr(rig.executor.connector, 'verify_demo_account', verify)
    with pytest.raises(ValueError, match='expired'):
        ctx.executor.open_authorized_session(preview['preview_id'], ctx.authorization.authorization_id,
                                             monitor_run_id=ctx.run_id)
    assert after_reservation_calls == 2
    rig.api.order_send.assert_not_called()
    assert ctx.authorization.usage_path.exists()


@pytest.mark.parametrize('change', ['version', 'attempts', 'scope', 'expiry', 'naive', 'risk', 'instruction'])
def test_malformed_session_authorization_is_rejected(rig, monkeypatch, change):
    from engine.demo_authorization import DemoEntryAuthorization
    ctx = authorized_rig(rig, monkeypatch)
    data = json.loads(ctx.authorization.path.read_text())
    if change == 'version': data['version'] = True
    if change == 'attempts': data['max_entry_attempts'] = 2
    if change == 'scope': data['scope'] = 'real_account'
    if change == 'expiry': data['expires_at_utc'] = (ctx.clock.now + timedelta(days=2)).isoformat()
    if change == 'naive': data['approved_at_utc'] = ctx.clock.now.replace(tzinfo=None).isoformat()
    if change == 'risk': data['max_risk_usd'] = -1
    if change == 'instruction': data['user_instruction'] = ''
    ctx.authorization.path.write_text(json.dumps(data))
    with pytest.raises(ValueError): DemoEntryAuthorization(rig.directory, ctx.authorization.authorization_id)
    rig.api.order_send.assert_not_called()
