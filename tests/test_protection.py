from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pytest

from engine.protection import RiskProtection, build_buy_protection


@pytest.fixture
def inputs():
    def pnl(side, symbol, volume, entry, exit_price):
        amount = (Decimal(str(exit_price)) - Decimal(str(entry))) * Decimal(str(volume)) * 100_000
        return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return Record(
        api=Record(ORDER_TYPE_BUY=0, order_calc_profit=Mock(side_effect=pnl)),
        info=Record(name="EURUSDm", currency_base="EUR", currency_profit="USD", order_mode=127,
                    trade_tick_size=.00001, point=.00001, trade_stops_level=0, trade_freeze_level=0),
        quote={"bid": 1.10590, "ask": 1.10598},
        funds={"balance": 499.90, "equity": 499.90, "currency": "USD"},
    )


def build(inputs, policy=None):
    return build_buy_protection(inputs.api, inputs.info, inputs.quote, .01, inputs.funds, policy or RiskProtection(5, 2))


def test_five_percent_and_two_to_one_match_hand_calculated_example(inputs):
    plan = build(inputs)
    assert plan["raw_percent_budget"] == pytest.approx(24.995)
    assert plan["risk_budget"] == 24.99
    assert plan["stop_loss"] == 1.08099
    assert plan["take_profit"] == 1.15596
    assert plan["stop_distance_pips"] == 249.9
    assert plan["target_distance_pips"] == 499.8
    assert plan["estimated_loss_at_sl"] == 24.99
    assert plan["estimated_profit_at_tp"] == 49.98
    assert plan["planned_reward_to_risk"] == 2
    assert plan["estimated_loss_at_sl"] <= 499.9 * .05


def test_uses_lower_equity_without_increasing_size(inputs):
    inputs.funds["equity"] = 400
    plan = build(inputs)
    assert plan["risk_budget"] == 20
    assert plan["estimated_loss_at_sl"] == 20
    assert plan["estimated_profit_at_tp"] == 40


def test_rejects_stop_inside_spread_without_widening_budget(inputs):
    with pytest.raises(ValueError, match="minimum distances"):
        build(inputs, RiskProtection(.01, 2))


def test_respects_broker_bid_based_stop_distance(inputs):
    inputs.info.trade_stops_level = 3000
    with pytest.raises(ValueError, match="minimum distances"):
        build(inputs)


@pytest.mark.parametrize("kind", ["missing_sl", "missing_tp", "tick", "funds", "pnl", "nonfinite"])
def test_invalid_broker_inputs_fail(inputs, kind):
    if kind == "missing_sl":
        inputs.info.order_mode &= ~16
    elif kind == "missing_tp":
        inputs.info.order_mode &= ~32
    elif kind == "tick":
        inputs.info.trade_tick_size = 0
    elif kind == "funds":
        inputs.funds["balance"] = float("nan")
    elif kind == "pnl":
        inputs.api.order_calc_profit.side_effect = lambda *args: None
    else:
        inputs.api.order_calc_profit.side_effect = lambda *args: float("inf")
    with pytest.raises(ValueError):
        build(inputs)


@pytest.mark.parametrize("percent,ratio", [(0, 2), (5.1, 2), (True, 2), (5, 0), (float("nan"), 2)])
def test_policy_limits(percent, ratio):
    with pytest.raises(ValueError):
        RiskProtection(percent, ratio)
