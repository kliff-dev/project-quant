"""Validated, immutable assumptions shared by the CLI, engine, and dashboard."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Real


def finite_number(name: str, value: float, *, minimum: float = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}.")


@dataclass(frozen=True)
class TransactionCosts:
    """Bps are 1/10,000. Spread is full bid-ask spread; half is paid per side."""

    commission_bps: float = 5.0
    fixed_commission: float = 0.0
    spread_bps: float = 2.0
    slippage_bps: float = 2.0

    def __post_init__(self) -> None:
        for name in ("commission_bps", "fixed_commission", "spread_bps", "slippage_bps"):
            finite_number(name, getattr(self, name))
        if self.commission_bps >= 10_000 or self.spread_bps / 2 + self.slippage_bps >= 10_000:
            raise ValueError("Percentage costs must be less than 100% per side.")


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 100_000.0
    allocation_fraction: float = 0.95
    costs: TransactionCosts = field(default_factory=TransactionCosts)
    periods_per_year: int = 252
    annual_risk_free_rate: float = 0.0

    def __post_init__(self) -> None:
        finite_number("initial_capital", self.initial_capital)
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be greater than zero.")
        finite_number("allocation_fraction", self.allocation_fraction)
        if not 0 < self.allocation_fraction <= 1:
            raise ValueError("allocation_fraction must be in (0, 1].")
        if isinstance(self.periods_per_year, bool) or not isinstance(self.periods_per_year, int) or self.periods_per_year < 1:
            raise ValueError("periods_per_year must be a positive integer.")
        finite_number("annual_risk_free_rate", self.annual_risk_free_rate, minimum=-0.999999)
        if not isinstance(self.costs, TransactionCosts):
            raise ValueError("costs must be a TransactionCosts configuration.")
