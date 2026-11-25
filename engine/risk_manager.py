"""Cash-constrained, whole-share sizing with no leverage or stop-loss model."""

import math

from engine.config import TransactionCosts, finite_number


class RiskManager:
    def __init__(self, allocation_fraction: float) -> None:
        finite_number("allocation_fraction", allocation_fraction)
        if not 0 < allocation_fraction <= 1:
            raise ValueError("Allocation must be in (0, 1].")
        self.allocation_fraction = allocation_fraction

    def size_position(self, cash: float, buy_price: float, costs: TransactionCosts) -> int:
        """Spend at most cash * allocation, including both commission components.

        q = floor((budget - fixed_fee) / (fill_price * (1 + commission_rate))).
        This allocates capital; it does not constrain a trade's maximum loss.
        """
        finite_number("cash", cash)
        finite_number("buy_price", buy_price)
        if buy_price <= 0:
            raise ValueError("Buy price must be positive.")
        budget = cash * self.allocation_fraction
        if budget <= costs.fixed_commission:
            return 0
        unit_cost = buy_price * (1 + costs.commission_bps / 10_000)
        quantity = max(0, math.floor((budget - costs.fixed_commission) / unit_cost))
        # Guard floating-point rounding exactly at the available-cash boundary.
        while quantity > 0 and quantity * buy_price + (costs.fixed_commission + quantity * buy_price * costs.commission_bps / 10_000) > budget:
            quantity -= 1
        return quantity
