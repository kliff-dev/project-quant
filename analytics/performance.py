"""Summary statistics; trade metrics use completed round trips after all costs."""

import numpy as np
import pandas as pd

from analytics.metrics import consecutive_streaks, drawdown_series, sharpe_ratio
from engine.config import finite_number


def calculate_metrics(equity: pd.Series, trades: pd.DataFrame, initial_capital: float, *, fills: pd.DataFrame | None = None, periods_per_year: int = 252, annual_risk_free_rate: float = 0.0) -> dict[str, float | int | str | None]:
    """Compute money values, percentages (0..100), counts, and annualized Sharpe.

    Average drawdown includes peak bars (zero drawdown). Average losses and largest
    losses are signed negative P&L. Breakevens count in trades and expectancy,
    but neither wins nor losses. Undefined/infinite profit factor is represented
    by None plus a status, so exported JSON never contains invalid Infinity/NaN.
    """
    finite_number("initial_capital", initial_capital)
    if initial_capital <= 0:
        raise ValueError("Initial capital must be positive.")
    if equity.empty or not np.isfinite(equity.to_numpy(dtype=float)).all() or (equity <= 0).any():
        raise ValueError("Equity must contain finite, positive observations.")
    profits = trades["net_pnl"].astype(float) if not trades.empty else pd.Series(dtype=float)
    if not np.isfinite(profits.to_numpy()).all():
        raise ValueError("Trade P&L must be finite.")
    wins, losses = profits[profits > 0], profits[profits < 0]
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    factor_status = "finite" if gross_loss > 0 else ("infinite" if gross_profit > 0 else "undefined")
    drawdown = drawdown_series(equity, initial_capital)
    max_wins, max_losses = consecutive_streaks(profits)
    final_equity = float(equity.iloc[-1])
    total_commission = total_spread = total_slippage = 0.0
    if fills is not None and not fills.empty:
        total_commission = float(fills["commission"].sum())
        total_spread = float(fills["spread_cost"].sum())
        total_slippage = float(fills["slippage_cost"].sum())
    return {
        "initial_capital": float(initial_capital),
        "final_equity": final_equity,
        "net_profit": final_equity - initial_capital,
        "return_pct": (final_equity / initial_capital - 1) * 100,
        "number_of_trades": len(profits),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": int((profits == 0).sum()),
        "win_rate_pct": len(wins) / len(profits) * 100 if len(profits) else 0.0,
        "average_win": float(wins.mean()) if len(wins) else 0.0,
        "average_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": profit_factor,
        "profit_factor_status": factor_status,
        "expectancy_per_trade": float(profits.mean()) if len(profits) else 0.0,
        "max_drawdown_pct": float(-drawdown.min() * 100),
        "average_drawdown_pct": float(-drawdown.mean() * 100),
        "sharpe_ratio": sharpe_ratio(equity, initial_capital, periods_per_year, annual_risk_free_rate),
        "largest_winning_trade": float(wins.max()) if len(wins) else 0.0,
        "largest_losing_trade": float(losses.min()) if len(losses) else 0.0,
        "consecutive_wins": max_wins,
        "consecutive_losses": max_losses,
        "total_commission": total_commission,
        "total_spread_cost": total_spread,
        "total_slippage_cost": total_slippage,
        "total_transaction_costs": total_commission + total_spread + total_slippage,
    }
