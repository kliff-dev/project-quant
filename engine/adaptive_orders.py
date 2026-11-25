"""Read-only broker adapter for the frozen H4-trend/H1-reclaim DEMO pilot.

This module calculates intents and native-broker protection plans only. It has
no order-check/send calls, terminal initialization, or authorization authority.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from analytics.adaptive_targets import confirmed_h4_swings
from analytics.candidate_signals import candidate_window_features, wilder_atr
from analytics.setup_research import validate_bars
from engine import strategy_orders as orders
from engine.entry_timing import ACTIVATION, context_profile, metadata, validate_policy


ROOT = Path(__file__).resolve().parents[1]
TARGET_MODE = 'adaptive_pullback_v1'
EXTENDED_TARGET_MODE = 'adaptive_pullback_v2'
SPEC_FILE = 'reports/market_analysis/adaptive_target_spec_2026-11-14.json'
SPEC_SHA256 = 'c149c1412cbb1ca78ca9f7bf341c6d251d226cd86da42051d96238fb0dcb17f8'
EXTENDED_SPEC_FILE = 'reports/market_analysis/extended_target_spec_2026-11-15.json'
EXTENDED_SPEC_SHA256 = '0e3ea1516921f25a033999a9394dece75884ef394816bcbadc1151adad1db2b4'
KERNEL_SHA256 = {
    'analytics/adaptive_targets.py': '7594fadd33f8903d99c29cfe479ab4e325e6d7fa707a156f06fc039e86ff6b58',
    'analytics/candidate_signals.py': '4d7042ec4d3e5ba2898a9a350635bdb23d899a617323852e8042bda6d7f803c1',
    'analytics/setup_research.py': '995ea08a83e6a1666aaf09e02ec39625445798a48356ea186d7625987b036297',
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _mode_spec(target_mode: str) -> tuple[int, str, str]:
    if target_mode == TARGET_MODE:
        return 1, SPEC_SHA256, 'adaptive_1_2_3'
    if target_mode == EXTENDED_TARGET_MODE:
        return 2, EXTENDED_SPEC_SHA256, 'adaptive_1_2_3_4'
    raise ValueError('Explicit adaptive target mode required.')


def verify_frozen_kernel(target_mode: str = TARGET_MODE) -> None:
    _mode_spec(target_mode)
    pins = {SPEC_FILE: SPEC_SHA256, **KERNEL_SHA256}
    if target_mode == EXTENDED_TARGET_MODE:
        pins[EXTENDED_SPEC_FILE] = EXTENDED_SPEC_SHA256
    for relative, expected in pins.items():
        path = ROOT / relative
        if not path.is_file() or _sha(path.read_bytes()) != expected:
            raise ValueError(f'Frozen adaptive research input changed: {relative}')


def _features(window: pd.DataFrame) -> dict:
    item = candidate_window_features(window)
    if not item:
        return {}
    atr = wilder_atr(window)
    median = float(atr.iloc[-100:].median())
    if not math.isfinite(median) or median <= 0:
        raise ValueError('Positive finite completed-H1 ATR median required.')
    h4 = window.resample('4h', origin='epoch').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    long, short = item['trend_reclaim_long'], item['trend_reclaim_short']
    return dict(item, strategy_family='trend_reclaim', pullback_long=long,
                pullback_short=short, breakout_long=False, breakout_short=False,
                volatility_ratio=float(atr.iloc[-1]) / median,
                **confirmed_h4_swings(h4, pd.Timestamp(item['decision_time_utc'])))


def _prices(quote: dict) -> tuple[Decimal, Decimal]:
    bid = orders._positive(quote['bid'], 'bid')
    ask = orders._positive(quote['ask'], 'ask')
    if ask < bid:
        raise ValueError('Crossed quotes.')
    return bid, ask


def _frozen_stop(features: dict, quote: dict, tick: Any, side: str) -> float:
    bid, ask = _prices(quote)
    atr = orders._positive(features['atr14'], 'H1 ATR')
    tick = orders._positive(tick, 'tick')
    if side == 'buy':
        raw = min(Decimal(str(features['low5'])) - Decimal('.1') * atr, ask - atr)
        rounding = ROUND_FLOOR
    else:
        raw = max(Decimal(str(features['high5'])) + ask - bid + Decimal('.1') * atr, bid + atr)
        rounding = ROUND_CEILING
    return float((raw / tick).to_integral_value(rounding=rounding) * tick)


def _save_exact(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('Adaptive evidence file checksum collision or corruption.')
    else:
        path.write_bytes(raw)


def latest_adaptive_intent(connector: Any, directory: Path, *,
                            policy: orders.StrategyPolicy) -> tuple[orders.StrategyIntent | None, dict]:
    """Inspect one completed rolling H1 window; never execute or start a loop."""
    schema_version, spec_sha256, _ = _mode_spec(policy.target_mode)
    if policy.target_mode == TARGET_MODE:
        verify_frozen_kernel()
    else:
        verify_frozen_kernel(policy.target_mode)
    snapshot = connector.inspect()
    api = connector.terminal_api
    symbol = snapshot['quote']['symbol']
    info = api.symbol_info(symbol)
    if (info is None or getattr(info, 'chart_mode', None) != 0
            or info.currency_base != 'EUR' or info.currency_profit != 'USD'):
        raise ValueError('Adaptive inputs require a verified bid-chart EUR/USD symbol.')
    rates = api.copy_rates_from_pos(symbol, api.TIMEFRAME_H1, 0, 3001)
    if rates is None:
        raise ValueError('Adaptive H1 history unavailable.')
    data = pd.DataFrame(rates)
    if 'time' not in data:
        raise ValueError('Broker H1 timestamp field unavailable.')
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('time'), unit='s', utc=True))
    now = pd.Timestamp(orders.utc_now())
    data = validate_bars(data.loc[data.index + pd.Timedelta(hours=1) <= now]).iloc[-3000:]
    if len(data) < 2400:
        raise ValueError('Insufficient completed H1 history for adaptive H4 warmup.')
    # Round-trip decimal text keeps later evidence verification bit-for-bit.
    raw = data.to_csv(float_format='%.17g', date_format='%Y-%m-%dT%H:%M:%SZ',
                      lineterminator='\n').encode('utf-8')
    fingerprint = _sha(raw)
    folder = Path(directory) / 'strategy_inputs'
    folder.mkdir(parents=True, exist_ok=True)
    path = (folder / f'{symbol}_adaptive_H1_{fingerprint}.csv').resolve()
    _save_exact(path, raw)
    decision = data.index[-1] + pd.Timedelta(hours=1)
    report = dict(status='WAIT', account=snapshot['account'], symbol=symbol,
                  decision_time_utc=decision.isoformat(), bars=len(data),
                  context_sha256=fingerprint, input_file=str(path), target_mode=policy.target_mode,
                  research_spec_sha256=spec_sha256, orders_sent=0)
    timing = metadata(policy.entry_timing)
    report.update(timing)
    from engine import setup_profiles
    report.update(setup_profiles.metadata(policy.entry_setups))
    if not 0 <= (now - decision).total_seconds() <= 300:
        report['reason'] = 'No completed H1 decision within the last five minutes.'
        return None, report
    if timing and policy.entry_setups != setup_profiles.DEFAULT:
        features, comparison = setup_profiles.features(data, symbol, policy.entry_setups)
        report.update(comparison)
    elif timing:
        from analytics.activation_signals import eur_features
        features = eur_features(data)
    else:
        features = _features(data)
    if not features:
        report['reason'] = report.get('selection_reason') or 'Outside entry session, nonconsecutive H1 bars, or insufficient H4 warmup.'
        return None, report
    if policy.entry_setups==setup_profiles.MULTI:
        long,short=features.get('side')=='buy',features.get('side')=='sell'
    else:
        long, short = features['pullback_long'], features['pullback_short']
    if bool(long) == bool(short):
        report['reason'] = 'No unique completed-bar H4-trend/H1-reclaim setup.'
        return None, report
    side = 'buy' if long else 'sell'
    quote = {key: float(value) for key, value in snapshot['quote'].items() if key in ('bid', 'ask')}
    stop = _frozen_stop(features, quote, info.trade_tick_size, side)
    context = dict(schema_version=schema_version, target_mode=policy.target_mode, research_spec_sha256=spec_sha256,
                   kernel_sha256=dict(KERNEL_SHA256), input_file=str(path), input_sha256=fingerprint,
                   decision_time_utc=decision.isoformat(), side=side, features=features,
                   frozen_quote=quote, frozen_stop_loss=stop)
    context.update(timing)
    setup_metadata = setup_profiles.metadata(policy.entry_setups)
    if setup_metadata:
        context.update(setup_metadata, setup_id=features['setup_id'])
        report.update(setup_metadata, setup_id=features['setup_id'])
    if policy.target_mode == EXTENDED_TARGET_MODE:
        context['base_research_spec_sha256'] = SPEC_SHA256
        context['base_target_mode'] = TARGET_MODE
        report['base_research_spec_sha256'] = SPEC_SHA256
    context['context_payload_sha256'] = _sha(_canonical(context))
    evidence = folder / f'{symbol}_adaptive_context_{context["context_payload_sha256"]}.json'
    _save_exact(evidence, _canonical(context))
    intent = orders.StrategyIntent(symbol, side, stop, float(features['atr14']),
                                   decision.isoformat(), fingerprint, setup_profiles.trigger(context), context)
    try:
        orders.validate_intent(intent)
    except ValueError as exc:
        report['reason'] = str(exc)
        return None, report
    report.update(status='signal', strategy_intent=asdict(intent), context_file=str(evidence))
    return intent, report


def _verified_context(intent: orders.StrategyIntent, info: Any, *,
                      target_mode: str = TARGET_MODE) -> dict:
    schema_version, spec_sha256, _ = _mode_spec(target_mode)
    if target_mode == TARGET_MODE:
        verify_frozen_kernel()
    else:
        verify_frozen_kernel(target_mode)
    if (getattr(info, 'chart_mode', None) != 0 or info.name != intent.symbol
            or info.currency_base != 'EUR' or info.currency_profit != 'USD'):
        raise ValueError('Adaptive context requires the same verified bid-chart EUR/USD symbol.')
    context = intent.adaptive_context
    if not isinstance(context, dict):
        raise ValueError('Frozen adaptive context required; no legacy signal fallback.')
    payload = {key: value for key, value in context.items() if key != 'context_payload_sha256'}
    if context.get('context_payload_sha256') != _sha(_canonical(payload)):
        raise ValueError('Adaptive context checksum mismatch.')
    if (context.get('schema_version') != schema_version or context.get('target_mode') != target_mode
            or context.get('research_spec_sha256') != spec_sha256
            or context.get('kernel_sha256') != KERNEL_SHA256
            or context.get('input_sha256') != intent.context_sha256
            or context.get('decision_time_utc') != intent.decision_time_utc
            or context.get('side') != intent.side):
        raise ValueError('Adaptive context metadata does not match the frozen intent.')
    from engine import setup_profiles
    setup_profile = setup_profiles.context_profile(context)
    if intent.trigger != setup_profiles.trigger(context):
        raise ValueError('Adaptive setup trigger differs from frozen context.')
    if (target_mode == EXTENDED_TARGET_MODE
            and (type(context.get('schema_version')) is not int
                 or context.get('base_research_spec_sha256') != SPEC_SHA256
                 or context.get('base_target_mode') != TARGET_MODE)):
        raise ValueError('Extended adaptive context does not match the frozen base version.')
    path = Path(context.get('input_file', ''))
    if not path.is_file() or _sha(path.read_bytes()) != intent.context_sha256:
        raise ValueError('Adaptive completed-H1 evidence is missing or changed.')
    data = pd.read_csv(path, index_col=0, float_precision='round_trip')
    data.index = pd.DatetimeIndex(pd.to_datetime(data.index, utc=True))
    data = validate_bars(data)
    if not 2400 <= len(data) <= 3000:
        raise ValueError('Adaptive evidence requires the bounded completed-H1 window.')
    if (data.index[-1] + pd.Timedelta(hours=1)).isoformat() != intent.decision_time_utc:
        raise ValueError('Adaptive evidence does not end at the frozen decision.')
    if setup_profile != setup_profiles.DEFAULT:
        if context_profile(context) != ACTIVATION:
            raise ValueError('Two setups require explicit activation timing.')
        features, _ = setup_profiles.features(data, intent.symbol, setup_profile)
        if not features or features['setup_id'] != context['setup_id']:
            raise ValueError('Selected setup does not reproduce from frozen evidence.')
    elif context_profile(context) == ACTIVATION:
        from analytics.activation_signals import eur_features
        features = eur_features(data)
    else:
        features = _features(data)
    if not features or _canonical(features) != _canonical(context.get('features', {})):
        raise ValueError('Adaptive entry features do not reproduce from the frozen H1 evidence.')
    if setup_profile==setup_profiles.MULTI:
        long,short=features.get('side')=='buy',features.get('side')=='sell'
    else:
        long, short = features['pullback_long'], features['pullback_short']
    if bool(long) == bool(short) or bool(long) != (intent.side == 'buy'):
        raise ValueError('Adaptive context does not contain the frozen selected entry.')
    stop = _frozen_stop(features, context['frozen_quote'], info.trade_tick_size, intent.side)
    if (stop != intent.stop_loss or stop != context.get('frozen_stop_loss')
            or features['atr14'] != intent.atr):
        raise ValueError('Frozen adaptive structural stop or ATR changed.')
    return features


def build_adaptive_protection(api: Any, info: Any, quote: dict, funds: dict,
                              intent: orders.StrategyIntent, policy: orders.StrategyPolicy) -> dict:
    """Select a native target against prior-only H4 references; 4R is opt-in."""
    _, spec_sha256, exit_policy = _mode_spec(policy.target_mode)
    extended = policy.target_mode == EXTENDED_TARGET_MODE
    orders.validate_intent(intent)
    validate_policy(policy, intent)
    # Preserve the v1 two-argument adapter contract for its frozen tests/callers.
    signal = (_verified_context(intent, info, target_mode=policy.target_mode) if extended
              else _verified_context(intent, info))
    bid, ask = _prices(quote)
    # Native order_calc_profit remains authoritative for the contract, account
    # currency and tick-rounded target. No linear research-math fallback exists.
    rewards = (1, 2, 3, 4) if extended else (1, 2, 3)
    plans = {r: orders._build_strategy_protection(api, info, quote, funds, intent, policy,
                                                target_reward_r=r) for r in rewards}
    invariant = ('volume_lots', 'stop_loss', 'risk_budget', 'risk_basis_amount',
                 'additional_cost_reserve_usd', 'planned_loss_with_reserves')
    if any(any(plans[r][key] != plans[1][key] for key in invariant) for r in rewards[1:]):
        raise ValueError('Adaptive targets must not change frozen risk, lots or stop.')
    direction = 1 if intent.side == 'buy' else -1
    reference = Decimal(str(plans[1]['reference_entry']))
    candidates = []
    key = 'h4_swing_highs' if direction == 1 else 'h4_swing_lows'
    decision = pd.Timestamp(intent.decision_time_utc)
    for pivot in signal[key]:
        opened, confirmed = pd.Timestamp(pivot['pivot_open_utc']), pd.Timestamp(pivot['confirmed_at_utc'])
        if opened.tz is None or confirmed.tz is None or not opened < confirmed <= decision:
            raise ValueError('Unconfirmed or future adaptive obstacle.')
        price = orders._positive(pivot['price'], 'H4 reference price')
        # H4 history is bid-based; a SELL TP triggers on ask. Use the current
        # verified spread, not the last H1 candle's historical spread.
        exit_quote = price + (ask - bid if direction == -1 else 0)
        if direction * (exit_quote - reference) > 0:
            candidates.append((pivot, exit_quote))
    if not candidates:
        raise ValueError('No confirmed directional H4 obstacle; skip this setup.')
    pivot, obstacle = (min(candidates, key=lambda item: item[1]) if direction == 1
                       else max(candidates, key=lambda item: item[1]))
    buffer = Decimal('.1') * Decimal(str(intent.atr))
    boundary = obstacle - direction * buffer
    room = {str(r): direction * (boundary - Decimal(str(plan['take_profit']))) >= 0
            for r, plan in plans.items()}
    if not room['1']:
        raise ValueError('Insufficient room for the actual rounded 1R target; skip this setup.')
    values = [signal[k] for k in ('h4_ema50', 'h4_ema200', 'h4_ema50_slope5', 'h4_atr14', 'volatility_ratio')]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError('Finite frozen H4 trend and volatility features required.')
    ema50, ema200, slope, h4_atr, ratio = map(lambda v: Decimal(str(v)), values)
    if h4_atr <= 0 or ratio <= 0:
        raise ValueError('Positive H4 ATR and volatility ratio required.')
    strong = abs(ema50 - ema200) >= h4_atr and abs(slope) >= Decimal('.25') * h4_atr
    if policy.entry_setups=='multi_setup_v1':
        strong=strong and signal.get('trend_long' if intent.side=='buy' else 'trend_short') is True
    normal = Decimal('.75') <= ratio <= Decimal('1.5')
    selected = 3 if room['3'] and strong and normal else 2 if room['2'] else 1
    base_selected = selected
    if extended and room['4'] and strong and normal:
        selected = 4
    reason = ('3r_room_strong_trend_normal_volatility' if selected == 3 else
              '2r_room_without_all_3r_conditions' if selected == 2 else 'only_1r_room_provisional_branch')
    if selected == 4:
        reason = '4r_room_strong_trend_normal_volatility'
    evidence = dict(selected_reward_r=selected, market_branch_r=selected, branch_reason=reason,
                    target_room_flags=room, obstacle_bid_price=float(pivot['price']),
                    obstacle_exit_quote=float(obstacle),
                    obstacle_pivot_open_utc=pivot['pivot_open_utc'],
                    obstacle_confirmed_at_utc=pivot['confirmed_at_utc'],
                    obstacle_buffer_price=float(buffer), strong_context=bool(strong),
                    normal_volatility=bool(normal), volatility_ratio=float(ratio),
                    research_spec_sha256=spec_sha256, frozen_kernel_sha256=dict(KERNEL_SHA256),
                    context_payload_sha256=intent.adaptive_context['context_payload_sha256'])
    if extended:
        evidence.update(target_mode=policy.target_mode, base_target_mode=TARGET_MODE,
                        base_research_spec_sha256=SPEC_SHA256, exit_policy=exit_policy,
                        base_selected_reward_r=base_selected)
    result = dict(plans[selected], selected_reward_r=selected, exit_policy=exit_policy,
                market_branch_r=selected, branch_reason=reason, target_room_flags=room,
                obstacle_bid_price=float(pivot['price']), obstacle_exit_quote=float(obstacle),
                obstacle_pivot_open_utc=pivot['pivot_open_utc'],
                obstacle_confirmed_at_utc=pivot['confirmed_at_utc'], obstacle_buffer_price=float(buffer),
                strong_context=bool(strong), normal_volatility=bool(normal), volatility_ratio=float(ratio),
                research_spec_sha256=spec_sha256, frozen_kernel_sha256=dict(KERNEL_SHA256),
                adaptive_target_evidence=evidence,
                time_exit='Frozen broker SL/TP only; no trailing, partial or time exit.',
                limitations='Unproven DEMO pilot. Reserves do not cap gaps, slippage, fees or swaps; 1R/3R branches are unvalidated.')
    if extended:
        result.update(target_mode=policy.target_mode, base_target_mode=TARGET_MODE,
                      base_research_spec_sha256=SPEC_SHA256,
                      base_selected_reward_r=base_selected,
                      limitations='Unproven DEMO pilot. Reserves do not cap gaps, slippage, fees or swaps; 1R/3R/4R branches are unvalidated.')
    from engine.conditional_risk import validate_plan
    validate_plan(result, policy, info=info)
    return result
