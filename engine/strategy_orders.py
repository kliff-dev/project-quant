"""Stop-first EUR/USD sizing and causal strategy intents; never submits orders."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from analytics.setup_research import signal_features

MODE = 'strategy_stop_first'
MAX_STRATEGY_LOTS = .1


@dataclass(frozen=True)
class StrategyPolicy:
    risk_percent: float = 1.0
    reward_to_risk: float = 2.0
    max_lots: float = .1
    slippage_reserve_points_per_side: int = 2
    additional_cost_reserve_fraction: float = .05
    maximum_cost_fraction_of_stop: float = .1
    target_mode: str = 'fixed_2r'
    entry_timing: str = 'clock_session'
    risk_mode: str = 'fixed'
    entry_setups: str = 'reclaim_only'

    def __post_init__(self) -> None:
        if self.entry_setups not in ('reclaim_only', 'reclaim_sweep_v1','multi_setup_v1'):
            raise ValueError('Unknown entry setup profile.')
        if self.entry_setups != 'reclaim_only' and (self.entry_timing != 'user_activation_v1' or
                self.target_mode not in ('adaptive_pullback_v2', 'gold_intraday_v1')):
            raise ValueError('Expanded setups require the explicit activation instrument policies.')
        if self.risk_mode not in ('fixed', 'minimum_lot_rescue_v1'):
            raise ValueError('Unknown risk sizing mode.')
        conditional = self.risk_mode == 'minimum_lot_rescue_v1'
        if conditional and (self.entry_timing != 'user_activation_v1' or
                            self.target_mode not in ('gold_intraday_v1', 'adaptive_pullback_v2') or
                            type(self.risk_percent) not in (int,float) or self.risk_percent != 2):
            raise ValueError('Conditional risk requires the explicit activation strategy and exact 2% ceiling.')
        if self.entry_timing not in ('clock_session', 'user_activation_v1'):
            raise ValueError('Unknown entry timing profile.')
        if self.entry_timing == 'user_activation_v1' and self.target_mode not in ('adaptive_pullback_v2', 'gold_intraday_v1'):
            raise ValueError('Activation timing requires the explicit Gold or extended EUR strategy.')
        if self.target_mode not in ('fixed_2r', 'adaptive_pullback_v1', 'adaptive_pullback_v2', 'gold_intraday_v1'):
            raise ValueError('Unknown strategy target mode.')
        for key, maximum in (('risk_percent', 2 if conditional else 1), ('max_lots', MAX_STRATEGY_LOTS),
                             ('additional_cost_reserve_fraction', .25), ('maximum_cost_fraction_of_stop', .1)):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f'{key} must be positive, finite and at most {maximum}.')
        if type(self.reward_to_risk) not in (int, float) or self.reward_to_risk != 2:
            raise ValueError('Current strategy policy requires 2:1 reward/risk.')
        points = self.slippage_reserve_points_per_side
        if type(points) is not int or not 1 <= points <= 100:
            raise ValueError('Slippage reserve must be 1-100 whole points per side.')

    @classmethod
    def load(cls, path: Path) -> StrategyPolicy:
        return cls(**json.loads(path.read_text(encoding='utf-8')))


@dataclass(frozen=True)
class StrategyIntent:
    symbol: str
    side: str
    stop_loss: float
    atr: float
    decision_time_utc: str
    context_sha256: str
    trigger: str
    adaptive_context: dict | None = None

    def __post_init__(self) -> None:
        if self.side not in ('buy', 'sell') or self.trigger not in ('pullback', 'breakout', 'both', 'sweep','range'):
            raise ValueError('Valid strategy side and trigger required.')
        if self.trigger in ('sweep','range'):
            from engine.setup_profiles import trigger
            if trigger(self.adaptive_context) != self.trigger:
                raise ValueError('Sweep/range trigger requires the matching fixed setup context.')
        if not self.symbol or len(self.context_sha256) != 64 or any(c not in '0123456789abcdef' for c in self.context_sha256):
            raise ValueError('Symbol and SHA-256 context identity required.')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in (self.stop_loss, self.atr)):
            raise ValueError('Positive finite stop and ATR required.')
        decision = datetime.fromisoformat(self.decision_time_utc)
        gold = isinstance(self.adaptive_context, dict) and self.adaptive_context.get('target_mode') == 'gold_intraday_v1'
        if decision.utcoffset() is None or (decision.minute % 5 if gold else decision.minute) or decision.second or decision.microsecond:
            raise ValueError('Strategy decision must be an aware H1 close, or an explicitly versioned Gold M5 close.')


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_intent(intent: StrategyIntent) -> None:
    if isinstance(intent.adaptive_context, dict) and intent.adaptive_context.get('target_mode') == 'gold_intraday_v1':
        from engine.gold_orders import validate_gold_intent
        validate_gold_intent(intent)
        return
    decision = datetime.fromisoformat(intent.decision_time_utc)
    now = utc_now()
    if not 0 <= (now - decision).total_seconds() <= 300:
        raise ValueError('Strategy signal is not within five minutes of its H1 close; wait for a fresh signal.')
    from engine.entry_timing import ACTIVATION, context_profile
    profile = context_profile(intent.adaptive_context)
    local = pd.Timestamp(now).tz_convert('Europe/London')
    if profile != ACTIVATION and (local.day_of_week >= 5 or not 8 <= local.hour < 14):
        raise ValueError('Outside the strategy entry session, 08:00-14:00 Europe/London.')


def _positive(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f'Invalid {label}.')
    try:
        result = Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError) as exc:
        raise ValueError(f'Invalid {label}.') from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f'Positive finite {label} required.')
    return result


def risk_budget(funds: dict, policy: StrategyPolicy) -> tuple[Decimal, Decimal, Decimal]:
    if funds.get('currency') != 'USD':
        raise ValueError('Strategy sizing currently requires a USD account.')
    basis = min(_positive(funds['balance'], 'balance'), _positive(funds['equity'], 'equity'))
    raw = basis * Decimal(str(policy.risk_percent)) / 100
    budget = raw.quantize(Decimal('.01'), rounding=ROUND_FLOOR)
    if budget <= 0:
        raise ValueError('Risk budget rounds to zero.')
    return basis, raw, budget


def evaluate_strategy_levels(api: Any, symbol: str, volume: float, entry: float,
                             stop: float, target: float, plan: dict, *, at_fill: bool = False) -> dict:
    direction = 1 if plan['side'] == 'buy' else -1
    order_type = api.ORDER_TYPE_BUY if direction == 1 else api.ORDER_TYPE_SELL
    slip = plan['slippage_reserve_price_per_side']
    slip_price = Decimal(str(slip))
    worst_entry = entry if at_fill else float(Decimal(str(entry)) + direction * slip_price)
    worst_stop = float(Decimal(str(stop)) - direction * slip_price)
    worst_target = float(Decimal(str(target)) - direction * slip_price)
    fee_reserve = plan['additional_cost_reserve_usd']
    values = [api.order_calc_profit(order_type, symbol, volume, a, b) for a,b in
              ((entry,stop),(entry,target),(worst_entry,worst_stop),(worst_entry,worst_target))]
    if any(v is None or isinstance(v, bool) or not math.isfinite(v) for v in values) or values[0] >= 0 or values[1] <= 0 or values[2] >= 0:
        raise ValueError('Broker cannot verify adverse-stop / favorable-target P&L.')
    reserved_loss, reserved_gain = -values[2] + fee_reserve, values[3] - fee_reserve
    return dict(estimated_loss_at_sl=-values[0], estimated_profit_at_tp=values[1],
                planned_loss_with_reserves=reserved_loss, planned_profit_with_reserves=reserved_gain,
                reward_to_risk=reserved_gain/reserved_loss,
                gross_reward_to_risk=values[1]/-values[0])


def _build_strategy_protection(api: Any, info: Any, quote: dict, funds: dict,
                               intent: StrategyIntent, policy: StrategyPolicy,
                               *, target_reward_r: int = 2) -> dict:
    """Native broker sizing; non-2R targets require the explicit adaptive mode."""
    if type(target_reward_r) is not int or target_reward_r not in (1, 2, 3, 4):
        raise ValueError('Only whole 1R, 2R, 3R or 4R targets are supported.')
    if target_reward_r == 4 and policy.target_mode not in ('adaptive_pullback_v2', 'gold_intraday_v1'):
        raise ValueError('4R targets require the extended adaptive policy.')
    if target_reward_r != 2 and policy.target_mode not in ('adaptive_pullback_v1', 'adaptive_pullback_v2', 'gold_intraday_v1'):
        raise ValueError('Variable targets require the adaptive policy.')
    validate_intent(intent)
    from engine.entry_timing import validate_policy
    validate_policy(policy, intent)
    gold = policy.target_mode == 'gold_intraday_v1'
    expected_base = 'XAU' if gold else 'EUR'
    if (info.name != intent.symbol or info.currency_base != expected_base or info.currency_profit != 'USD' or
        gold and (info.name != 'XAUUSDm' or float(info.trade_contract_size) != 100)):
        raise ValueError('Strategy requires its exact verified EUR/USD or Gold CFD contract.')
    if gold and (intent.adaptive_context or {}).get('target_mode') != policy.target_mode:
        raise ValueError('Gold strategy requires its own versioned M5 context.')
    if not int(info.order_mode) & 16 or not int(info.order_mode) & 32:
        raise ValueError('Both SL and TP must be supported.')
    tick, point = _positive(info.trade_tick_size, 'tick'), _positive(info.point, 'point')
    bid, ask = _positive(quote['bid'], 'bid'), _positive(quote['ask'], 'ask')
    if ask < bid:
        raise ValueError('Crossed quotes.')
    direction = 1 if intent.side == 'buy' else -1
    entry = ask if direction == 1 else bid
    stop = Decimal(str(intent.stop_loss))
    if abs(stop/tick - (stop/tick).to_integral_value()) > Decimal('0.000001'):
        raise ValueError('Frozen strategy stop is not tick aligned.')
    distance = direction * (entry-stop)
    if distance < Decimal(str(intent.atr)):
        raise ValueError('Fresh quote leaves less than the strategy minimum ATR stop distance; review a new signal.')
    stops_level = info.trade_stops_level
    if type(stops_level) is not int or stops_level < 0:
        raise ValueError('Invalid broker stop-distance rule.')
    trigger_quote = bid if direction == 1 else ask
    if direction*(trigger_quote-stop) <= 0 or direction*(trigger_quote-stop) < stops_level*point:
        raise ValueError('Strategy stop violates the broker trigger-side distance; it will not be widened.')
    basis, ceiling_raw, ceiling_budget = risk_budget(funds, policy)
    conditional = policy.risk_mode == 'minimum_lot_rescue_v1'
    raw = basis*Decimal('.01') if conditional else ceiling_raw
    budget = raw.quantize(Decimal('.01'), rounding=ROUND_FLOOR)
    slip = policy.slippage_reserve_points_per_side * point
    reserve = (budget * Decimal(str(policy.additional_cost_reserve_fraction))).quantize(Decimal('.01'), rounding=ROUND_CEILING)
    minimum, maximum, step = [_positive(getattr(info,k), k) for k in ('volume_min','volume_max','volume_step')]
    cap = min(maximum,Decimal(str(policy.max_lots)))
    if minimum > cap or minimum % step:
        raise ValueError('Broker minimum/step cannot fit the strategy volume cap.')
    order_type = api.ORDER_TYPE_BUY if direction == 1 else api.ORDER_TYPE_SELL
    worst_entry, worst_stop = entry+direction*slip, stop-direction*slip
    if min(worst_entry,worst_stop) <= 0:
        raise ValueError('Invalid reserved execution prices.')
    def pnl(volume: Decimal, close: Decimal) -> Decimal:
        value = api.order_calc_profit(order_type,info.name,float(volume),float(worst_entry),float(close))
        if value is None or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError('Broker P&L calculation unavailable.')
        return Decimal(str(value))
    def loss(size: Decimal) -> Decimal:
        value = pnl(size,worst_stop)
        if value >= 0:
            raise ValueError('Broker did not return a loss for the reserved stop.')
        return -value+reserve
    minimum_at_baseline, rescue = loss(minimum), False
    if minimum_at_baseline > budget and conditional:
        from engine.conditional_risk import favorable_for_intent, verified_features
        if not favorable_for_intent(intent,verified_features(intent, info), gold=gold):
            raise ValueError('Minimum lot exceeds 1%; elevated risk requires strong trend and normal volatility.')
        raw, budget, rescue = ceiling_raw, ceiling_budget, True
        reserve = (budget*Decimal(str(policy.additional_cost_reserve_fraction))).quantize(Decimal('.01'), rounding=ROUND_CEILING)
    if loss(minimum) > budget:
        raise ValueError(f'Minimum lot exceeds the {2 if rescue else 1}% budget with reserves; skip this setup.')
    # The exception admits only minimum volume. It never sizes toward the 2% cap.
    low, high = 0, 0 if rescue else int(((cap-minimum)/step).to_integral_value(rounding=ROUND_FLOOR))
    while low < high:
        middle=(low+high+1)//2
        if loss(minimum+middle*step) <= budget: low=middle
        else: high=middle-1
    volume = minimum+low*step
    planned_loss=loss(volume)
    # Solve the target in favorable ticks from worst entry, reserving exit slip.
    required=Decimal(target_reward_r)*planned_loss
    def target_at(ticks: int) -> Decimal:
        unaligned=worst_entry+direction*(ticks*tick+slip)
        rounding=ROUND_CEILING if direction == 1 else ROUND_FLOOR
        return (unaligned/tick).to_integral_value(rounding=rounding)*tick
    high=max(1,int((2*abs(worst_entry-worst_stop)/tick).to_integral_value(rounding=ROUND_CEILING)))
    while pnl(volume,target_at(high)-direction*slip)-reserve < required:
        high*=2
        if high > 100_000_000 or target_at(high) <= 0:
            raise ValueError('A valid finite target could not be calculated.')
    low=1
    while low<high:
        middle=(low+high)//2
        if pnl(volume,target_at(middle)-direction*slip)-reserve >= required: high=middle
        else: low=middle+1
    target=target_at(low)
    if target <= 0 or direction*(target-trigger_quote) < stops_level*point:
        raise ValueError('Target violates broker distance or positive-price requirement.')
    cost_fraction=float((ask-bid+2*slip)/distance)
    if cost_fraction > policy.maximum_cost_fraction_of_stop:
        raise ValueError('Spread plus slippage allowance exceeds 10% of the stop distance; skip this setup.')
    plan=dict(mode=MODE,side=intent.side,volume_lots=float(volume),max_lots=policy.max_lots,
              risk_percent=(2 if rescue else 1) if conditional else policy.risk_percent,reward_to_risk=target_reward_r,
              risk_basis='min(balance, equity)',risk_basis_amount=float(basis),currency='USD',
              raw_percent_budget=float(raw),risk_budget=float(budget),reference_entry=float(entry),
              stop_loss=float(stop),take_profit=float(target),tick_size=float(tick),
              stop_distance_pips=float(distance/Decimal('.0001')),
              target_distance_pips=float(direction*(target-entry)/Decimal('.0001')),
              slippage_reserve_price_per_side=float(slip),additional_cost_reserve_usd=float(reserve),
              cost_fraction_of_stop=cost_fraction,policy=asdict(policy),strategy_intent=asdict(intent),
              closure_authorization='Approval includes broker SL/TP closure. Manual closing requires separate confirmation.',
              time_exit='16:00 London is a strategy proposal; no automatic time-exit service is running.',
              limitations='Estimated reserves do not guarantee a loss cap through gaps, slippage, fees or swaps. Strategy profitability is untested.')
    plan.update(evaluate_strategy_levels(api,info.name,float(volume),float(entry),float(stop),float(target),plan))
    if plan['planned_loss_with_reserves'] > float(budget)+1e-9 or plan['reward_to_risk']+1e-9 < target_reward_r:
        raise ValueError('Final broker P&L does not meet the reserved budget/ratio.')
    plan['planned_reward_to_risk']=plan['reward_to_risk']
    # Keep the immutable requested ratio separate from the tick-rounded result.
    plan['reward_to_risk']=target_reward_r
    if conditional:
        from engine.conditional_risk import SPEC_SHA256
        plan.update(actual_planned_risk_percent=100*plan['planned_loss_with_reserves']/float(basis),
            conditional_risk_evidence=dict(risk_mode=policy.risk_mode,risk_spec_sha256=SPEC_SHA256,
                minimum_lot_rescue=rescue,broker_minimum_lots=float(minimum),
                minimum_lot_loss_at_baseline=float(minimum_at_baseline),
                reason='minimum_lot_only_with_strong_context' if rescue else 'baseline_1_percent_sizing'))
    return plan


def build_strategy_protection(api: Any, info: Any, quote: dict, funds: dict,
                              intent: StrategyIntent, policy: StrategyPolicy) -> dict:
    """Keep legacy 2R behavior unless the frozen adaptive policy is explicit."""
    if policy.target_mode == 'gold_intraday_v1':
        from engine.gold_orders import build_gold_protection
        return build_gold_protection(api, info, quote, funds, intent, policy)
    if policy.target_mode in ('adaptive_pullback_v1', 'adaptive_pullback_v2'):
        from engine.adaptive_orders import build_adaptive_protection
        return build_adaptive_protection(api, info, quote, funds, intent, policy)
    return _build_strategy_protection(api, info, quote, funds, intent, policy)


def latest_strategy_intent(connector: Any, directory: Path, *,
                           policy: StrategyPolicy | None = None) -> tuple[StrategyIntent | None, dict]:
    """One read-only history inspection. WAIT never creates an executable preview."""
    if policy is not None and policy.target_mode == 'gold_intraday_v1':
        from engine.gold_orders import latest_gold_intent
        return latest_gold_intent(connector, directory, policy=policy)
    if policy is not None and policy.target_mode in ('adaptive_pullback_v1', 'adaptive_pullback_v2'):
        from engine.adaptive_orders import latest_adaptive_intent
        return latest_adaptive_intent(connector, directory, policy=policy)
    snapshot=connector.inspect()
    api=connector.terminal_api
    symbol=snapshot['quote']['symbol']
    info=api.symbol_info(symbol)
    if info is None or getattr(info,'chart_mode',None)!=0 or info.currency_base!='EUR' or info.currency_profit!='USD':
        raise ValueError('Strategy inputs require a verified bid-chart EUR/USD symbol.')
    rates=api.copy_rates_from_pos(symbol,api.TIMEFRAME_H1,0,3001)
    if rates is None or len(rates)<2400:
        raise ValueError('Insufficient broker H1 history for the 600-H4-bar strategy warmup.')
    data=pd.DataFrame(rates)
    data.index=pd.DatetimeIndex(pd.to_datetime(data.pop('time'),unit='s',utc=True))
    now=pd.Timestamp(utc_now())
    data=data.loc[data.index+pd.Timedelta(hours=1)<=now].sort_index()
    raw=data.to_csv(float_format='%.10g',date_format='%Y-%m-%dT%H:%M:%SZ',lineterminator='\n').encode()
    fingerprint=hashlib.sha256(raw).hexdigest()
    folder=directory/'strategy_inputs'
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/f'{symbol}_H1_{fingerprint}.csv'
    if path.exists() and path.read_bytes()!=raw:
        raise ValueError('Strategy input snapshot checksum collision or corruption.')
    if not path.exists(): path.write_bytes(raw)
    features=signal_features(data)
    last=features.iloc[-1]
    report=dict(status='WAIT',account=snapshot['account'],symbol=symbol,
                decision_time_utc=last.decision_time.isoformat(),bars=len(data),
                context_sha256=fingerprint,input_file=str(path),orders_sent=0)
    long_signal=bool(last.pullback_long or last.breakout_long)
    short_signal=bool(last.pullback_short or last.breakout_short)
    if not last.session:
        report['reason']='Outside the 08:00-14:00 London strategy entry window.'
        return None,report
    if not long_signal and not short_signal:
        report['reason']='No completed-bar H4/H1 continuation signal.'
        return None,report
    if long_signal and short_signal or not last.recent_is_consecutive:
        report['reason']='Conflicting signal or nonconsecutive structural bars.'
        return None,report
    side='buy' if long_signal else 'sell'
    pullback=last.pullback_long if long_signal else last.pullback_short
    breakout=last.breakout_long if long_signal else last.breakout_short
    entry=snapshot['quote']['ask' if long_signal else 'bid']
    spread=snapshot['quote']['ask']-snapshot['quote']['bid']
    raw_stop=min(last.low5-.1*last.atr14,entry-last.atr14) if long_signal else max(last.high5+spread+.1*last.atr14,entry+last.atr14)
    tick=Decimal(str(info.trade_tick_size))
    stop=float((Decimal(str(raw_stop))/tick).to_integral_value(rounding=ROUND_FLOOR if long_signal else ROUND_CEILING)*tick)
    intent=StrategyIntent(symbol,side,stop,float(last.atr14),last.decision_time.isoformat(),fingerprint,
                          'both' if pullback and breakout else 'pullback' if pullback else 'breakout')
    try: validate_intent(intent)
    except ValueError as exc:
        report['reason']=str(exc)
        return None,report
    report.update(status='signal',strategy_intent=asdict(intent))
    return intent,report
