"""Broker-calculated SL/TP for a capped-size EUR/USD DEMO buy test."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

SYMBOL_ORDER_SL, SYMBOL_ORDER_TP = 16, 32


@dataclass(frozen=True)
class RiskProtection:
    """Planned gross price risk, excluding commission, gaps, and fill slippage."""

    risk_percent: float = 1.0
    reward_to_risk: float = 2.0

    def __post_init__(self) -> None:
        for name, value, maximum in (("risk_percent", self.risk_percent, 5), ("reward_to_risk", self.reward_to_risk, 10)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f"{name} must be finite, greater than zero, and at most {maximum}.")


def build_buy_protection(api: Any, info: Any, quote: dict[str, Any], volume: float, funds: dict[str, Any], policy: RiskProtection) -> dict[str, Any]:
    """Solve tick-aligned SL/TP using order_calc_profit, without sending orders.

    The test retains its fixed minimum volume (at most 0.01 lots). Thus the risk
    budget determines stop distance; it does not select a strategy stop or raise
    volume. Budget uses min(balance, equity), rounded down to whole USD cents.
    A finite integer search finds the furthest adverse tick within that budget,
    then the nearest profitable tick reaching the requested gross reward ratio.
    """
    if getattr(info, "currency_base", None) != "EUR" or getattr(info, "currency_profit", None) != "USD" or funds.get("currency") != "USD":
        raise ValueError("Protected test currently supports EUR/USD on a USD account only.")
    if not 0 < volume <= .01:
        raise ValueError("Protected test volume must be positive and at most 0.01 lots.")
    mode = int(getattr(info, "order_mode", 0))
    if not mode & SYMBOL_ORDER_SL or not mode & SYMBOL_ORDER_TP:
        raise ValueError("Broker symbol must support both stop-loss and take-profit.")
    tick = Decimal(str(getattr(info, "trade_tick_size", 0)))
    point = Decimal(str(getattr(info, "point", 0)))
    if not tick.is_finite() or tick <= 0 or not point.is_finite() or point <= 0:
        raise ValueError("Broker tick size and point must be finite and positive.")
    ask, bid = Decimal(str(quote["ask"])), Decimal(str(quote["bid"]))
    if not ask.is_finite() or not bid.is_finite() or bid <= 0 or ask < bid:
        raise ValueError("Valid, non-crossed bid/ask required for protection.")
    entry_ticks = int((ask / tick).to_integral_value(rounding=ROUND_HALF_UP))
    entry = tick * entry_ticks
    if abs(entry - ask) > tick * Decimal("0.000001"):
        raise ValueError("Quoted entry is not aligned to the broker tick grid.")
    amounts = [Decimal(str(funds[key])) for key in ("balance", "equity")]
    if not all(value.is_finite() and value > 0 for value in amounts):
        raise ValueError("Positive finite balance/equity required.")
    basis = min(amounts)
    raw_budget = basis * Decimal(str(policy.risk_percent)) / 100
    budget = raw_budget.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    if budget <= 0 or entry_ticks <= 1:
        raise ValueError("Risk budget is too small for a positive-price stop.")

    def pnl_at(price_ticks: int) -> Decimal:
        price = float(tick * price_ticks)
        value = api.order_calc_profit(api.ORDER_TYPE_BUY, info.name, volume, float(entry), price)
        if value is None or not math.isfinite(value):
            raise ValueError("Broker could not calculate protected-order P&L.")
        return Decimal(str(value))

    def loss_at(distance: int) -> Decimal:
        loss = -pnl_at(entry_ticks - distance)
        if loss < 0:
            raise ValueError("Broker returned a profit for an adverse BUY stop.")
        return loss

    maximum = entry_ticks - 1
    if loss_at(maximum) <= budget:
        raise ValueError("Budget cannot be represented by a positive SL at this capped size. Choose a smaller risk budget.")
    low, high = 0, maximum
    while low < high:
        middle = (low + high + 1) // 2
        if loss_at(middle) <= budget:
            low = middle
        else:
            high = middle - 1
    stop_ticks = low
    if stop_ticks < 1:
        raise ValueError("Risk budget cannot fund even one adverse price tick.")
    loss = loss_at(stop_ticks)
    if loss <= 0:
        raise ValueError("Broker P&L precision cannot resolve this stop's risk.")
    target = loss * Decimal(str(policy.reward_to_risk))
    high = max(1, math.ceil(stop_ticks * policy.reward_to_risk))
    while pnl_at(entry_ticks + high) < target:
        high *= 2
        if high > 100_000_000:
            raise ValueError("Could not bracket a finite profit target.")
    low = 1
    while low < high:
        middle = (low + high) // 2
        if pnl_at(entry_ticks + middle) >= target:
            high = middle
        else:
            low = middle + 1
    target_ticks = low
    stop, take_profit = entry - stop_ticks * tick, entry + target_ticks * tick
    stop_level = getattr(info, "trade_stops_level", None)
    if isinstance(stop_level, bool) or not isinstance(stop_level, int) or stop_level < 0:
        raise ValueError("Broker stop-distance rule is unavailable or invalid.")
    minimum_distance = point * stop_level
    # BUY position SL/TP trigger against bid, not the entry ask.
    if not stop < bid or bid - stop < minimum_distance or not take_profit > bid or take_profit - bid < minimum_distance:
        raise ValueError("Computed SL/TP violates broker bid-based minimum distances. The stop will not be widened beyond the budget.")
    gain = pnl_at(entry_ticks + target_ticks)
    return {
        "risk_percent": float(policy.risk_percent), "reward_to_risk": float(policy.reward_to_risk),
        "risk_basis": "min(balance, equity)", "risk_basis_amount": float(basis), "currency": "USD",
        "raw_percent_budget": float(raw_budget), "risk_budget": float(budget),
        "reference_entry": float(entry), "stop_loss": float(stop), "take_profit": float(take_profit),
        "stop_distance_pips": float((entry - stop) / Decimal("0.0001")),
        "target_distance_pips": float((take_profit - entry) / Decimal("0.0001")),
        "estimated_loss_at_sl": float(loss), "estimated_profit_at_tp": float(gain),
        "planned_reward_to_risk": float(gain / loss), "tick_size": float(tick),
        "broker_stops_level_points": stop_level, "broker_freeze_level_points": getattr(info, "trade_freeze_level", None),
        "closure_authorization": "Approval of this protected order also authorizes broker-triggered SL/TP closure; manual closing still requires separate confirmation.",
        "limitations": "Gross price P&L estimate. Commission/fees, gaps, fill slippage, and currency rounding can change actual risk and reward. Levels are budget-derived, not strategy signals.",
    }


def evaluate_buy_levels(api: Any, symbol: str, volume: float, entry_price: float, stop: float, target: float) -> dict[str, float]:
    """Recalculate gross risk/reward at a fresh or actual fill price."""
    loss_value = api.order_calc_profit(api.ORDER_TYPE_BUY, symbol, volume, entry_price, stop)
    gain_value = api.order_calc_profit(api.ORDER_TYPE_BUY, symbol, volume, entry_price, target)
    if loss_value is None or gain_value is None or not all(math.isfinite(v) for v in (loss_value, gain_value)) or loss_value >= 0 or gain_value <= 0:
        raise ValueError("Broker could not verify positive SL risk and TP reward.")
    return {"estimated_loss_at_sl": -float(loss_value), "estimated_profit_at_tp": float(gain_value),
            "reward_to_risk": float(gain_value / -loss_value)}
