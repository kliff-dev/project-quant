"""Offline BTC CFD replay with explicit quote-side, lot and cost assumptions.

This module has no terminal access. Signals contain frozen structural stops from
completed candles; a signal can first enter at the next H1 bar's open. Broker history is
Bid OHLC, so Ask OHLC is an approximation using that bar's recorded spread.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

import math
import pandas as pd


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    point: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    currency_profit: str = "USD"

    def __post_init__(self):
        for key in ("point", "contract_size", "volume_min", "volume_max", "volume_step"):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid contract {key}.")
        if self.volume_min > self.volume_max or self.currency_profit != "USD":
            raise ValueError("Replay requires valid lot limits and a USD-linear contract.")


@dataclass(frozen=True)
class ReplaySettings:
    initial_balance: float = 500.0
    risk_fraction: float = .01
    risk_reserve_fraction: float = .05
    slippage_price: float = 2.0
    spread_multiplier: float = 1.0
    commission_bps_per_side: float = 0.0
    financing_annual_fraction: float = 0.0
    financing_usd_per_lot_day: float = 0.0
    max_friction_stop_fraction: float = .10
    max_lots: float = .10

    def __post_init__(self):
        if not math.isfinite(self.initial_balance) or self.initial_balance <= 0 or not 0 < self.risk_fraction <= .02:
            raise ValueError("Invalid initial balance or research risk fraction.")
        for key in ("risk_reserve_fraction", "slippage_price", "spread_multiplier",
                    "commission_bps_per_side", "financing_annual_fraction", "financing_usd_per_lot_day",
                    "max_friction_stop_fraction", "max_lots"):
            value = getattr(self, key)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid replay setting {key}.")
        if self.risk_reserve_fraction >= 1:
            raise ValueError('Risk reserve must be less than the whole budget.')


def prepare_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate without filling, aggregating or silently deleting candles."""
    result = frame.copy()
    if not isinstance(result.index, pd.DatetimeIndex) or result.index.tz is None:
        raise ValueError("Bars require a timezone-aware UTC DatetimeIndex.")
    result.index = pd.to_datetime(result.index, utc=True)
    if result.index.hasnans or result.index.has_duplicates or not result.index.is_monotonic_increasing:
        raise ValueError("Duplicate or unordered candle timestamps.")
    if any(stamp != stamp.floor('h') for stamp in result.index):
        raise ValueError("Candle opens must align with UTC H1 boundaries.")
    columns = ("open", "high", "low", "close", "spread")
    if any(key not in result for key in columns):
        raise ValueError("OHLC and historical spread points are required.")
    for key in columns:
        result[key] = pd.to_numeric(result[key], errors="raise")
        if not result[key].map(math.isfinite).all():
            raise ValueError("Non-finite candle data.")
    if ((result[list(columns[:4])] <= 0).any().any() or (result.spread < 0).any()
            or (result.high < result[["open", "close", "low"]].max(axis=1)).any()
            or (result.low > result[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("Invalid candle prices or spread.")
    return result


def rounded_lots(raw: float, contract: ContractSpec, maximum: float) -> float:
    """Round down; never increase a below-minimum risk allocation."""
    step = Decimal(str(contract.volume_step))
    capped = min(raw, contract.volume_max, maximum)
    lots = float((Decimal(str(max(0., capped))) / step).to_integral_value(rounding=ROUND_FLOOR) * step)
    return lots if lots + 1e-12 >= contract.volume_min else 0.


def _stamp(value) -> str:
    return pd.Timestamp(value).isoformat()


def _tick_price(price: float, tick: float, *, upward: bool) -> float:
    quantum = Decimal(str(tick))
    mode = ROUND_CEILING if upward else ROUND_FLOOR
    return float((Decimal(str(price)) / quantum).to_integral_value(rounding=mode) * quantum)


def replay(frame: pd.DataFrame, signals: pd.DataFrame, contract: ContractSpec,
           settings: ReplaySettings = ReplaySettings(), *, setup_id: str = "combined",
           start=None, end=None) -> dict[str, Any]:
    """One independently funded account per call; at most one position at once.

    ``end`` is exclusive. Open positions are marked, never fabricated as closed.
    In combined mode only the scanner's selected, nonconflicting signal is used.
    Individual setup runs retain its signals even when other setups disagreed.
    """
    bars = prepare_bars(frame)
    start = pd.Timestamp(start) if start is not None else bars.index[0]
    end = pd.Timestamp(end) if end is not None else bars.index[-1] + pd.Timedelta(hours=1)
    start, end = pd.to_datetime(start, utc=True), pd.to_datetime(end, utc=True)
    bars = bars.loc[(bars.index >= start) & (bars.index < end)]
    if bars.empty:
        raise ValueError("No candles in replay interval.")
    by_time: dict[pd.Timestamp, list[dict]] = {}
    for item in signals.to_dict("records"):
        decision = pd.to_datetime(item["decision_time_utc"], utc=True)
        if not start <= decision < end:
            continue
        if setup_id != "combined" and item["setup_id"] != setup_id:
            continue
        if setup_id == "combined" and (not item.get("eligible_for_selection", False)
                or item.get("selected_setup") != item["setup_id"]):
            continue
        by_time.setdefault(decision, []).append(item)
    balance = settings.initial_balance
    trades, decisions, curve = [], [], []
    position = None
    fee_rate = settings.commission_bps_per_side / 10000.
    finance = lambda pos, t: ((abs(pos["entry_price"] * pos["units"])
        * settings.financing_annual_fraction / 365.25
        + settings.financing_usd_per_lot_day * pos["lots"])
        * max(0., (t - pos["entry_time"]).total_seconds()) / 86400)

    for time, bar in bars.iterrows():
        spread = float(bar.spread) * contract.point * settings.spread_multiplier
        candidates = by_time.pop(time, [])
        # An exit later in this candle cannot enable an entry at its earlier open.
        if candidates:
            if len(candidates) > 1:
                raise ValueError("Scanner supplied multiple selected signals for one account/candle.")
            signal = candidates[0]
            record = {"decision_time_utc": _stamp(time), "setup_id": signal["setup_id"],
                      "side": signal["side"]}
            reason = None
            if position is not None:
                reason = "position_already_open"
            source_open = pd.to_datetime(signal["signal_open_utc"], utc=True)
            if source_open + pd.Timedelta(hours=1) != time:
                reason = "no_contiguous_next_open"
            buy = signal["side"] == "buy"
            if signal["side"] not in ("buy", "sell"):
                raise ValueError("Unknown signal side.")
            quote_entry = float(bar.open) + (spread if buy else 0.)
            entry = quote_entry + (settings.slippage_price if buy else -settings.slippage_price)
            frozen_stop = float(signal["stop_price"])
            # Native planner keeps structural invalidation and only expands a
            # stop to preserve its ATR floor after the next quote arrives.
            atr = float(signal.get('atr14', 0.))
            if not math.isfinite(atr) or atr < 0:
                raise ValueError('Invalid signal ATR.')
            crossed = float(bar.open) <= frozen_stop if buy else float(bar.open) + spread >= frozen_stop
            if crossed:
                reason = reason or 'price_already_crossed_frozen_stop'
            stop = min(frozen_stop, quote_entry - 1.5 * atr) if buy else max(frozen_stop, quote_entry + 1.5 * atr)
            stop = _tick_price(stop, contract.point, upward=not buy)
            reward = float(signal.get("reward_r", 2.))
            if not math.isfinite(reward) or reward <= 0:
                raise ValueError("Invalid signal reward multiple.")
            distance = (entry - stop) if buy else (stop - entry)
            quote_distance = (quote_entry - stop) if buy else (stop - quote_entry)
            if not math.isfinite(distance) or distance <= 0 or entry <= 0 or stop <= 0:
                reason = reason or "frozen_stop_invalid_at_entry"
            if quote_distance > 0 and spread + 2 * settings.slippage_price > quote_distance * settings.max_friction_stop_fraction:
                reason = reason or "spread_and_slippage_exceed_cost_budget"
            if reason is None:
                # Reserve is unspent budget headroom, not an invented cash fee.
                risk_budget = balance * settings.risk_fraction
                per_lot = contract.contract_size * (distance + settings.slippage_price
                    + fee_rate * (entry + stop + settings.slippage_price))
                lots = rounded_lots(risk_budget * (1 - settings.risk_reserve_fraction) / per_lot,
                                    contract, settings.max_lots)
                if lots == 0:
                    reason = "minimum_lot_exceeds_risk_budget"
                else:
                    units = lots * contract.contract_size
                    entry_fee = entry * units * fee_rate
                    balance -= entry_fee
                    position = dict(setup_id=signal["setup_id"], side=signal["side"],
                        entry_time=time, entry_price=entry, stop_price=stop,
                        target_price=_tick_price(quote_entry + (1 if buy else -1) * quote_distance * reward,
                                                 contract.point, upward=buy),
                        lots=lots, units=units, entry_fee=entry_fee,
                        planned_risk_usd=per_lot * lots, risk_budget_usd=risk_budget,
                        entry_spread_price=spread, requested_entry_price=quote_entry,
                        source_frozen_stop_price=frozen_stop, source_atr14=atr,
                        source_signal_open_utc=_stamp(source_open))
                    record.update(lots=lots, entry_price=entry, stop_price=stop,
                                  target_price=position["target_price"], planned_risk_usd=per_lot * lots)
            record.update(status="rejected" if reason else "entered", reason=reason)
            decisions.append(record)

        if position is not None:
            buy = position["side"] == "buy"
            quote_open = float(bar.open) + (0. if buy else spread)
            quote_low = float(bar.low) + (0. if buy else spread)
            quote_high = float(bar.high) + (0. if buy else spread)
            stop_hit = quote_low <= position["stop_price"] if buy else quote_high >= position["stop_price"]
            target_hit = quote_high >= position["target_price"] if buy else quote_low <= position["target_price"]
            if stop_hit or target_hit:
                if stop_hit:
                    fill = (min(quote_open, position["stop_price"]) - settings.slippage_price if buy
                            else max(quote_open, position["stop_price"]) + settings.slippage_price)
                    exit_reason = "stop_loss"
                else:
                    fill = position["target_price"] + (-1 if buy else 1) * settings.slippage_price
                    exit_reason = "take_profit"
                gross = (fill - position["entry_price"]) * (1 if buy else -1) * position["units"]
                exit_fee = fill * position["units"] * fee_rate
                financing = finance(position, time + pd.Timedelta(hours=1))
                net = gross - position["entry_fee"] - exit_fee - financing
                balance += gross - exit_fee - financing
                trades.append({**{k: v for k, v in position.items() if k != "entry_time"},
                    "entry_time_utc": _stamp(position["entry_time"]), "exit_bar_open_utc": _stamp(time),
                    "exit_time_resolution": "H1 bar; intrabar execution time unknown",
                    "exit_price": fill, "exit_reason": exit_reason, "both_levels_hit": bool(stop_hit and target_hit),
                    "gross_pnl_usd": gross, "commission_usd": position["entry_fee"] + exit_fee,
                    "financing_usd": financing, "net_pnl_usd": net,
                    "realized_r": net / position["planned_risk_usd"]})
                position = None
        unrealized = 0.
        if position is not None:
            quote = float(bar.close) + (spread if position["side"] == "sell" else 0.)
            liquidation_price = quote + (settings.slippage_price if position["side"] == "sell" else -settings.slippage_price)
            unrealized = ((liquidation_price - position["entry_price"])
                * (1 if position["side"] == "buy" else -1) * position["units"]
                - abs(liquidation_price * position["units"]) * fee_rate
                - finance(position, time + pd.Timedelta(hours=1)))
        curve.append(dict(time_utc=_stamp(time + pd.Timedelta(hours=1)), balance=balance,
                          equity=balance + unrealized, open_position=position is not None))

    for time, items in by_time.items():
        decisions.extend(dict(decision_time_utc=_stamp(time), setup_id=item["setup_id"],
                              side=item["side"], status="rejected", reason="missing_entry_candle") for item in items)
    equities = [settings.initial_balance] + [row["equity"] for row in curve]
    balances = [settings.initial_balance] + [row["balance"] for row in curve]
    def drawdown(values):
        peak, maximum = values[0], 0.
        for value in values:
            peak = max(peak, value)
            maximum = max(maximum, (peak - value) / peak if peak else 0.)
        return maximum
    net = sum(item["net_pnl_usd"] for item in trades)
    from collections import Counter
    summary = dict(closed_trades=len(trades), win_rate=sum(item["net_pnl_usd"] > 0 for item in trades) / len(trades) if trades else None,
        expectancy_r=sum(item["realized_r"] for item in trades) / len(trades) if trades else None,
        average_net_usd=net / len(trades) if trades else None, closed_net_pnl_usd=net,
        end_balance=balance, end_equity=curve[-1]["equity"],
        equity_change_usd=curve[-1]["equity"] - settings.initial_balance,
        max_drawdown_close_mark_fraction=drawdown(equities), max_drawdown_balance_fraction=drawdown(balances),
        open_positions=int(position is not None), both_levels_hit=sum(item["both_levels_hit"] for item in trades),
        technical_candidates=len(decisions),
        rejections=dict(Counter(row["reason"] for row in decisions if row["status"] == "rejected")))
    opened = None if position is None else {**{k: v for k,v in position.items() if k != "entry_time"},
                                           "entry_time_utc": _stamp(position["entry_time"])}
    return dict(setup_id=setup_id, period=dict(start_utc=_stamp(start), end_exclusive_utc=_stamp(end)),
                contract=asdict(contract), settings=asdict(settings), summary=summary,
                trades=trades, decisions=decisions, curve=curve, open_position=opened)
