"""Deterministic simulated fills. This module cannot place real orders."""

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from engine.config import TransactionCosts, finite_number

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Fill:
    timestamp: pd.Timestamp
    signal_timestamp: pd.Timestamp
    side: Side
    quantity: int
    reference_price: float
    price: float
    commission: float
    spread_cost: float
    slippage_cost: float


class ExecutionModel:
    def __init__(self, costs: TransactionCosts) -> None:
        self.costs = costs

    def price(self, side: Side, reference_price: float) -> float:
        """Apply half-spread and slippage adversely on each side of the trade."""
        if side not in ("buy", "sell"):
            raise ValueError("Side must be buy or sell.")
        finite_number("reference_price", reference_price)
        if reference_price <= 0:
            raise ValueError("reference_price must be positive.")
        direction = 1 if side == "buy" else -1
        return reference_price * (1 + direction * (self.costs.spread_bps / 2 + self.costs.slippage_bps) / 10_000)

    def commission(self, quantity: int, price: float) -> float:
        return self.costs.fixed_commission + quantity * price * self.costs.commission_bps / 10_000

    def fill(self, side: Side, reference_price: float, quantity: int, timestamp: pd.Timestamp, signal_timestamp: pd.Timestamp) -> Fill:
        """A fill must occur after the close-time signal, never on its own bar."""
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("Quantity must be a positive integer.")
        timestamp, signal_timestamp = pd.Timestamp(timestamp), pd.Timestamp(signal_timestamp)
        if pd.isna(timestamp) or pd.isna(signal_timestamp) or timestamp <= signal_timestamp:
            raise ValueError("Execution timestamp must follow the signal timestamp.")
        price = self.price(side, reference_price)
        return Fill(
            timestamp, signal_timestamp, side, quantity, reference_price, price,
            self.commission(quantity, price),
            quantity * reference_price * self.costs.spread_bps / 20_000,
            quantity * reference_price * self.costs.slippage_bps / 10_000,
        )
