import math

import numpy as np
import pandas as pd
import pytest

from analytics.metrics import consecutive_streaks, drawdown_series, sharpe_ratio
from analytics.performance import calculate_metrics


def test_metrics_against_hand_calculated_example():
    equity = pd.Series([100, 120, 90, 108], dtype=float)
    trades = pd.DataFrame({"net_pnl": [20, -10, -5, 3, 0]})
    metrics = calculate_metrics(equity, trades, 100)
    assert metrics["initial_capital"] == 100
    assert metrics["final_equity"] == 108
    assert metrics["net_profit"] == 8
    assert metrics["return_pct"] == pytest.approx(8)
    assert metrics["number_of_trades"] == 5
    assert metrics["winning_trades"] == 2
    assert metrics["losing_trades"] == 2
    assert metrics["win_rate_pct"] == 40
    assert metrics["average_win"] == 11.5
    assert metrics["average_loss"] == -7.5
    assert metrics["profit_factor"] == pytest.approx(23 / 15)
    assert metrics["expectancy_per_trade"] == pytest.approx(1.6)
    assert metrics["max_drawdown_pct"] == 25
    assert metrics["average_drawdown_pct"] == pytest.approx(8.75)
    assert metrics["largest_winning_trade"] == 20
    assert metrics["largest_losing_trade"] == -10
    assert metrics["consecutive_wins"] == 1
    assert metrics["consecutive_losses"] == 2
    expected_returns = np.array([0, 0.2, -0.25, 0.2])
    assert metrics["sharpe_ratio"] == pytest.approx(math.sqrt(252) * expected_returns.mean() / expected_returns.std(ddof=1))


def test_initial_capital_is_in_drawdown_peak():
    pd.testing.assert_series_equal(drawdown_series(pd.Series([90.0, 95.0]), 100), pd.Series([-0.1, -0.05], name="drawdown"))


def test_flat_and_no_loss_cases_are_explicit():
    flat = pd.Series([100.0] * 3)
    no_trades = calculate_metrics(flat, pd.DataFrame(), 100)
    assert no_trades["profit_factor"] is None
    assert no_trades["profit_factor_status"] == "undefined"
    assert no_trades["sharpe_ratio"] is None
    wins = calculate_metrics(flat, pd.DataFrame({"net_pnl": [2, 3]}), 100)
    assert wins["profit_factor"] is None
    assert wins["profit_factor_status"] == "infinite"


def test_breakeven_resets_streak():
    assert consecutive_streaks(pd.Series([1, 2, 0, 1, -1, -2, -3])) == (2, 3)


def test_risk_free_rate_is_geometrically_converted():
    equity = pd.Series([100.0, 101.0, 100.0])
    returns = np.array([0, .01, 100 / 101 - 1])
    rate = 1.05 ** (1 / 252) - 1
    assert sharpe_ratio(equity, 100, annual_risk_free_rate=.05) == pytest.approx(np.sqrt(252) * (returns - rate).mean() / returns.std(ddof=1))
