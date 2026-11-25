import numpy as np
import pandas as pd
import pytest

from engine.backtester import Backtester
from engine.config import BacktestConfig, TransactionCosts
from engine.execution import ExecutionModel
from engine.risk_manager import RiskManager
from strategies.base_strategy import BaseStrategy
from strategies.trend_following import MovingAverageCrossover

ZERO_COSTS = TransactionCosts(0, 0, 0, 0)


class ScriptedStrategy(BaseStrategy):
    def __init__(self, signals):
        self.signals = signals

    def generate_signals(self, data):
        return pd.Series(self.signals, index=data.index)

    def parameters(self):
        return {"name": "TestOnlyScriptedStrategy"}


def test_signal_executes_at_next_open_never_same_close(bars):
    data = bars([100, 200, 10, 300], opens=[100, 20, 10, 30])
    result = Backtester(ScriptedStrategy([1, 0, -1, 0]), BacktestConfig(1000, 1, ZERO_COSTS)).run(data)
    assert list(result.fills["timestamp"]) == [data.index[1], data.index[3]]
    assert list(result.fills["reference_price"]) == [20, 30]
    assert list(result.fills["quantity"]) == [50, 50]
    assert result.metrics["final_equity"] == 1500
    assert result.trades.iloc[0]["net_pnl"] == 500
    assert result.equity.iloc[0]["equity"] == 1000


def test_costs_and_cash_match_hand_calculated_round_trip(bars):
    costs = TransactionCosts(commission_bps=100, fixed_commission=1, spread_bps=20, slippage_bps=5)
    data = bars([100, 100, 110], opens=[100, 100, 110])
    result = Backtester(ScriptedStrategy([1, -1, 0]), BacktestConfig(1000, 1, costs)).run(data)
    assert result.fills.iloc[0]["quantity"] == 9
    assert result.fills.iloc[0]["price"] == pytest.approx(100.15)
    assert result.fills.iloc[1]["price"] == pytest.approx(109.835)
    assert result.trades.iloc[0]["net_pnl"] == pytest.approx(66.26635)
    assert result.metrics["final_equity"] == pytest.approx(1066.26635)
    assert result.metrics["total_transaction_costs"] == pytest.approx(23.73365)
    assert (result.equity["cash"] >= 0).all()


def test_gap_price_controls_position_size(bars):
    data = bars([10, 200, 200], opens=[10, 200, 200])
    result = Backtester(ScriptedStrategy([1, 0, 0]), BacktestConfig(1000, 1, ZERO_COSTS)).run(data)
    assert result.fills.iloc[0]["quantity"] == 5
    assert result.equity.iloc[1]["cash"] == 0


def test_final_signal_is_recorded_but_not_filled(bars):
    result = Backtester(ScriptedStrategy([0, 0, 1]), BacktestConfig(1000, 1, ZERO_COSTS)).run(bars([10, 11, 12]))
    assert result.fills.empty
    assert result.orders.iloc[0]["status"] == "unfilled"
    assert result.metrics["final_equity"] == 1000


def test_open_position_is_marked_and_excluded_from_closed_trade_metrics(bars):
    result = Backtester(ScriptedStrategy([1, 0, 0]), BacktestConfig(1000, 1, ZERO_COSTS)).run(bars([10, 10, 12]))
    assert result.metrics["number_of_trades"] == 0
    assert result.metrics["open_positions"] == 1
    assert result.metrics["final_equity"] == 1200
    assert result.open_position["unrealized_net_pnl"] == 200
    assert len(result.fills) == 1


def test_ignored_and_rejected_orders_are_visible(bars):
    result = Backtester(ScriptedStrategy([-1, 1, 0]), BacktestConfig(5, 1, ZERO_COSTS)).run(bars([10, 10, 10]))
    assert list(result.orders["status"]) == ["ignored", "rejected"]
    assert result.fills.empty
    assert result.metrics["final_equity"] == 5


def test_no_pyramiding_or_shorting(bars):
    result = Backtester(ScriptedStrategy([1, 1, -1, -1, 0]), BacktestConfig(1000, 1, ZERO_COSTS)).run(bars([10] * 5))
    assert len(result.fills) == 2
    assert list(result.orders["status"]) == ["executed", "ignored", "executed", "ignored"]
    assert result.metrics["number_of_trades"] == 1
    assert result.metrics["breakeven_trades"] == 1


def test_trade_pnl_reconciles_with_equity_including_open_position():
    from engine.data_loader import demo_data
    result = Backtester(MovingAverageCrossover(10, 30)).run(demo_data())
    closed_pnl = result.trades["net_pnl"].sum()
    open_pnl = result.open_position["unrealized_net_pnl"] if result.open_position else 0
    assert result.metrics["net_profit"] == pytest.approx(closed_pnl + open_pnl)
    assert np.allclose(result.equity["equity"], result.equity["cash"] + result.equity["position_value"])
    assert (result.fills["timestamp"] > result.fills["signal_timestamp"]).all()


@pytest.mark.parametrize("signals", [[1, np.nan, 0], [2, 0, 0]])
def test_invalid_strategy_signals_fail(bars, signals):
    with pytest.raises(ValueError, match="signals"):
        Backtester(ScriptedStrategy(signals)).run(bars([10] * 3))


def test_execution_rejects_same_bar_fill():
    with pytest.raises(ValueError, match="follow"):
        ExecutionModel(ZERO_COSTS).fill("buy", 10, 1, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-01"))


def test_sizing_reserves_costs_and_fixed_fee():
    manager = RiskManager(1)
    assert manager.size_position(100, 10, ZERO_COSTS) == 10
    assert manager.size_position(100, 10, TransactionCosts(100, 1, 0, 0)) == 9
    assert manager.size_position(1, 10, TransactionCosts(0, 2, 0, 0)) == 0


@pytest.mark.parametrize("kwargs", [{"initial_capital": 0}, {"initial_capital": float("inf")}, {"allocation_fraction": 2}, {"periods_per_year": 0}, {"annual_risk_free_rate": -1}])
def test_invalid_account_assumptions(kwargs):
    with pytest.raises(ValueError):
        BacktestConfig(**kwargs)


@pytest.mark.parametrize("kwargs", [{"spread_bps": -1}, {"slippage_bps": 10000}, {"commission_bps": float("nan")}, {"fixed_commission": -1}])
def test_invalid_cost_assumptions(kwargs):
    with pytest.raises(ValueError):
        TransactionCosts(**kwargs)
