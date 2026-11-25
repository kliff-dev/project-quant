"""Small, independently testable performance calculations."""

import numpy as np
import pandas as pd


def drawdown_series(equity: pd.Series, initial_capital: float) -> pd.Series:
    """Non-positive fractions below running peak, including the starting capital."""
    peaks = equity.cummax().clip(lower=initial_capital)
    return (equity / peaks - 1).rename("drawdown")


def consecutive_streaks(profits: pd.Series) -> tuple[int, int]:
    """Maximum consecutive wins/losses; breakeven trades reset both streaks."""
    wins = losses = maximum_wins = maximum_losses = 0
    for profit in profits:
        wins = wins + 1 if profit > 0 else 0
        losses = losses + 1 if profit < 0 else 0
        maximum_wins, maximum_losses = max(maximum_wins, wins), max(maximum_losses, losses)
    return maximum_wins, maximum_losses


def sharpe_ratio(equity: pd.Series, initial_capital: float, periods_per_year: int = 252, annual_risk_free_rate: float = 0.0) -> float | None:
    """Annualized mean/sample-standard-deviation of per-bar excess returns.

    Includes the first bar's return from initial capital, warmup, and flat days.
    Annual risk-free rate is geometrically converted to a per-bar rate. Returns
    None for fewer than two observations or zero variance. Annualization assumes
    equally spaced trading sessions and independent returns; it is a convention,
    not a guarantee. Cash earns no interest in the equity simulation.
    """
    returns = equity.pct_change(fill_method=None)
    returns.iloc[0] = equity.iloc[0] / initial_capital - 1
    daily_risk_free = (1 + annual_risk_free_rate) ** (1 / periods_per_year) - 1
    excess = returns - daily_risk_free
    deviation = excess.std(ddof=1)
    if len(excess) < 2 or not np.isfinite(deviation) or deviation <= 1e-15:
        return None
    return float(np.sqrt(periods_per_year) * excess.mean() / deviation)
