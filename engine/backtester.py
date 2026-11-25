"""Single-instrument, long-only daily event simulation with next-open fills."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

import numpy as np
import pandas as pd

from analytics.metrics import drawdown_series
from analytics.performance import calculate_metrics
from engine.config import BacktestConfig
from engine.data_loader import validate_ohlcv
from engine.execution import ExecutionModel, Fill
from engine.risk_manager import RiskManager
from strategies.base_strategy import BaseStrategy, Signal

TRADE_COLUMNS = ["entry_timestamp", "exit_timestamp", "quantity", "entry_price", "exit_price", "entry_commission", "exit_commission", "net_pnl", "return_pct", "holding_bars"]
ORDER_COLUMNS = ["signal_timestamp", "execution_timestamp", "side", "status", "quantity", "reason"]


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    fills: pd.DataFrame
    trades: pd.DataFrame
    orders: pd.DataFrame
    open_position: dict[str, Any] | None
    metrics: dict[str, float | int | str | None]
    strategy_parameters: dict[str, str | int | float]
    config: BacktestConfig


class Backtester:
    def __init__(self, strategy: BaseStrategy, config: BacktestConfig | None = None) -> None:
        self.strategy = strategy
        self.config = config or BacktestConfig()

    def run(self, data: pd.DataFrame) -> BacktestResult:
        """Execute yesterday's close-time event at today's open, then mark close.

        Full order quantities fill at one adjusted open plus adverse costs. There
        are no partial fills, shorts, pyramiding, cash interest, or leverage.
        Unfilled/ignored decisions are recorded. End positions are not liquidated.
        """
        data = validate_ohlcv(data)
        signals = self.strategy.generate_signals(data.copy())
        if not isinstance(signals, pd.Series) or not signals.index.equals(data.index):
            raise ValueError("Strategy signals must be a Series aligned exactly to the data index.")
        if signals.isna().any() or not signals.isin([int(s) for s in Signal]).all():
            raise ValueError("Strategy signals must contain only -1, 0, or 1, with no missing values.")
        config = self.config
        model = ExecutionModel(config.costs)
        risk = RiskManager(config.allocation_fraction)
        cash = float(config.initial_capital)
        entry: Fill | None = None
        entry_bar = 0
        fills: list[Fill] = []
        trades: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []

        for i, (timestamp, bar) in enumerate(data.iterrows()):
            pending = int(signals.iloc[i - 1]) if i > 0 else int(Signal.HOLD)
            if pending != Signal.HOLD:
                side = "buy" if pending == Signal.BUY else "sell"
                order = dict(signal_timestamp=data.index[i - 1], execution_timestamp=timestamp, side=side, status="ignored", quantity=0, reason="")
                if pending == Signal.BUY and entry is None:
                    quantity = risk.size_position(cash, model.price("buy", float(bar["open"])), config.costs)
                    if quantity == 0:
                        order.update(status="rejected", reason="Insufficient cash for one share including costs")
                    else:
                        entry = model.fill("buy", float(bar["open"]), quantity, timestamp, data.index[i - 1])
                        cash -= entry.quantity * entry.price + entry.commission
                        entry_bar = i
                        fills.append(entry)
                        order.update(status="executed", quantity=quantity)
                elif pending == Signal.EXIT and entry is not None:
                    exit_fill = model.fill("sell", float(bar["open"]), entry.quantity, timestamp, data.index[i - 1])
                    cash += exit_fill.quantity * exit_fill.price - exit_fill.commission
                    if cash < 0:
                        raise ValueError("Exit commissions exceed the remaining account value; simulation is insolvent.")
                    fills.append(exit_fill)
                    profit = entry.quantity * (exit_fill.price - entry.price) - entry.commission - exit_fill.commission
                    trades.append(dict(
                        entry_timestamp=entry.timestamp, exit_timestamp=timestamp,
                        quantity=entry.quantity, entry_price=entry.price, exit_price=exit_fill.price,
                        entry_commission=entry.commission, exit_commission=exit_fill.commission,
                        net_pnl=profit, return_pct=profit / (entry.quantity * entry.price + entry.commission) * 100,
                        holding_bars=i - entry_bar,
                    ))
                    order.update(status="executed", quantity=entry.quantity)
                    entry = None
                else:
                    order["reason"] = "Already long" if pending == Signal.BUY else "Already flat"
                orders.append(order)
            quantity = entry.quantity if entry is not None else 0
            position_value = quantity * float(bar["close"])
            equity_value = cash + position_value
            if cash < 0 or not np.isfinite(equity_value) or equity_value <= 0:
                raise ValueError("Account became insolvent or non-finite under the configured assumptions.")
            equity_rows.append(dict(date=timestamp, cash=cash, position_quantity=quantity, position_value=position_value, equity=equity_value, signal=int(signals.iloc[i])))

        if int(signals.iloc[-1]) != Signal.HOLD:
            orders.append(dict(signal_timestamp=data.index[-1], execution_timestamp=pd.NaT,
                               side="buy" if signals.iloc[-1] == Signal.BUY else "sell",
                               status="unfilled", quantity=0, reason="No next bar available"))
        equity = pd.DataFrame(equity_rows).set_index("date")
        equity["drawdown"] = drawdown_series(equity["equity"], config.initial_capital)
        fills_frame = pd.DataFrame([asdict(fill) for fill in fills], columns=[field.name for field in fields(Fill)])
        trades_frame = pd.DataFrame(trades, columns=TRADE_COLUMNS)
        orders_frame = pd.DataFrame(orders, columns=ORDER_COLUMNS)
        open_position = None
        if entry is not None:
            final_close = float(data["close"].iloc[-1])
            open_position = dict(entry_timestamp=entry.timestamp, quantity=entry.quantity, entry_price=entry.price,
                                 entry_commission=entry.commission, last_close=final_close,
                                 market_value=entry.quantity * final_close,
                                 unrealized_net_pnl=entry.quantity * (final_close - entry.price) - entry.commission)
        metrics = calculate_metrics(equity["equity"], trades_frame, config.initial_capital, fills=fills_frame,
                                    periods_per_year=config.periods_per_year, annual_risk_free_rate=config.annual_risk_free_rate)
        metrics["open_positions"] = int(entry is not None)
        return BacktestResult(equity, fills_frame, trades_frame, orders_frame, open_position, metrics,
                              self.strategy.parameters(), config)
