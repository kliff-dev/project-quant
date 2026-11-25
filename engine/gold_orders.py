"""Causal Gold intents and broker-native protection; no order or login calls."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from analytics.gold_strategy import SPEC_SHA256, session_status, validate_m5, window_features, select_target
from engine import strategy_orders as orders
from engine.gold_connector import GOLD_SYMBOL, validate_gold_contract
from engine.entry_timing import ACTIVATION, context_profile, metadata, validate_policy

ROOT = Path(__file__).resolve().parents[1]
TARGET_MODE = 'gold_intraday_v1'
KERNEL_SHA256 = 'aaf0b0743c476c818ceec5ebfac37aa9e9652443e2c57d8062aa769888830d2b'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def verify_kernel() -> None:
    if (digest(ROOT / 'config/gold_strategy_spec.json') != SPEC_SHA256 or
        digest(ROOT / 'analytics/gold_strategy.py') != KERNEL_SHA256 or
        digest(ROOT / 'analytics/candidate_signals.py') != '4d7042ec4d3e5ba2898a9a350635bdb23d899a617323852e8042bda6d7f803c1'):
        raise ValueError('Frozen Gold specification/kernel changed; review before execution.')


def validate_gold_intent(intent: orders.StrategyIntent) -> None:
    context = intent.adaptive_context or {}
    from engine.setup_profiles import trigger
    if intent.symbol != GOLD_SYMBOL or context.get('target_mode') != TARGET_MODE or intent.trigger != trigger(context):
        raise ValueError('Exact versioned Gold pullback intent required.')
    decision = pd.Timestamp(intent.decision_time_utc)
    now = pd.Timestamp(orders.utc_now())
    if decision.tz is None or decision.minute % 5 or decision.second or decision.microsecond:
        raise ValueError('Aware Gold M5 candle close required.')
    if not 0 <= (now - decision).total_seconds() <= 60:
        raise ValueError('Gold signal expired: must be within 60 seconds of its M5 close.')
    reason = '' if context_profile(context) == ACTIVATION else session_status(now)
    if reason:
        raise ValueError(f'Gold entry paused: {reason}.')


def frozen_stop(features: dict, quote: dict, tick: float, side: str) -> float:
    bid, ask, tick = [orders._positive(x, label) for x, label in
                      [(quote['bid'], 'bid'), (quote['ask'], 'ask'), (tick, 'tick')]]
    atr = orders._positive(features['atr14'], 'ATR')
    if ask < bid:
        raise ValueError('Crossed Gold quote.')
    if side == 'buy':
        raw = min(Decimal(str(features['low3'])) - Decimal('.2') * atr, ask - Decimal('1.2') * atr)
        rounding = ROUND_FLOOR
    else:
        raw = max(Decimal(str(features['high3'])) + ask - bid + Decimal('.2') * atr, bid + Decimal('1.2') * atr)
        rounding = ROUND_CEILING
    return float((raw / tick).to_integral_value(rounding=rounding) * tick)


def latest_gold_intent(connector, directory: Path, *, policy: orders.StrategyPolicy):
    if policy.target_mode != TARGET_MODE or policy.slippage_reserve_points_per_side != 50:
        raise ValueError('Gold requires its separately approved policy and slippage reserve.')
    verify_kernel()
    snapshot = connector.inspect(GOLD_SYMBOL)
    info = connector.terminal_api.symbol_info(GOLD_SYMBOL)
    contract = validate_gold_contract(info)
    if contract['chart_mode'] != 0 or contract['point'] != .001 or contract['trade_tick_size'] != .001:
        raise ValueError('Gold execution requires the frozen bid-chart .001 point/tick contract.')
    api = connector.terminal_api
    rates = api.copy_rates_from_pos(GOLD_SYMBOL, api.TIMEFRAME_M5, 0, 1201)
    if rates is None or len(rates) == 0:
        raise ValueError('Gold M5 history unavailable.')
    frame = pd.DataFrame(rates)
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop('time'), unit='s', utc=True))
    now = pd.Timestamp(orders.utc_now())
    frame = validate_m5(frame.loc[frame.index + pd.Timedelta(minutes=5) <= now]).iloc[-1200:]
    raw = frame.to_csv(float_format='%.17g', date_format='%Y-%m-%dT%H:%M:%SZ', lineterminator='\n').encode()
    sha = hashlib.sha256(raw).hexdigest()
    folder = Path(directory) / 'gold_strategy_inputs'
    folder.mkdir(parents=True, exist_ok=True)
    path = (folder / f'XAUUSDm_M5_{sha}.csv').resolve()
    if path.exists() and path.read_bytes() != raw:
        raise ValueError('Gold snapshot checksum collision/corruption.')
    if not path.exists():
        path.write_bytes(raw)
    decision = frame.index[-1] + pd.Timedelta(minutes=5)
    report = dict(status='WAIT', decision_time_utc=decision.isoformat(), symbol=GOLD_SYMBOL,
                  checked_at_utc=now.isoformat(), account=snapshot['account'], quote=snapshot['quote'],
                  warnings=snapshot['warnings'], instrument_type='broker Gold CFD',
                  target_mode=TARGET_MODE, input_file=str(path), context_sha256=sha,
                  bars=len(frame), orders_sent=0, research_spec_sha256=SPEC_SHA256,
                  economic_filter='Routine US clock blackouts only; no actual economic-event calendar or macro forecast.')
    timing = metadata(policy.entry_timing)
    report.update(timing)
    if timing:
        report['economic_filter'] = 'User-activated timing: no fixed clock blackout or actual economic-event calendar.'
        from engine import setup_profiles
        report.update(setup_profiles.metadata(policy.entry_setups))
        features, comparison = setup_profiles.features(frame, GOLD_SYMBOL, policy.entry_setups)
        report.update(comparison)
    else:
        features = window_features(frame)
    if not features.get('side'):
        return None, dict(report, reason=features.get('reason') or report.get('selection_reason') or 'No Gold setup.')
    quote = {key: float(snapshot['quote'][key]) for key in ('bid', 'ask')}
    side = features['side']
    stop = frozen_stop(features, quote, info.trade_tick_size, side)
    context = dict(schema_version=1, target_mode=TARGET_MODE, timeframe='M5', research_spec_sha256=SPEC_SHA256,
                   kernel_sha256=KERNEL_SHA256, input_file=str(path), input_sha256=sha,
                   decision_time_utc=decision.isoformat(), side=side, features=features,
                   frozen_quote=quote, frozen_stop_loss=stop)
    context.update(timing)
    from engine import setup_profiles
    setup_metadata = setup_profiles.metadata(policy.entry_setups)
    if setup_metadata:
        context.update(setup_metadata, setup_id=features['setup_id'])
        report.update(setup_metadata, setup_id=features['setup_id'])
    context['context_payload_sha256'] = hashlib.sha256(canonical(context)).hexdigest()
    intent = orders.StrategyIntent(GOLD_SYMBOL, side, stop, features['atr14'], decision.isoformat(), sha, setup_profiles.trigger(context), context)
    try:
        validate_gold_intent(intent)
    except ValueError as exc:
        return None, dict(report, reason=str(exc))
    return intent, dict(report, status='signal', reason='Completed M5 setup with verified M15 context.', strategy_intent=asdict(intent))


def verified_context(intent: orders.StrategyIntent, info) -> dict:
    verify_kernel()
    contract = validate_gold_contract(info)
    if contract['chart_mode'] != 0 or contract['point'] != .001 or contract['trade_tick_size'] != .001:
        raise ValueError('Gold native contract differs from the frozen research contract.')
    context = intent.adaptive_context or {}
    payload = {k: v for k, v in context.items() if k != 'context_payload_sha256'}
    if (type(context.get('schema_version')) is not int or context['schema_version'] != 1 or
        context.get('target_mode') != TARGET_MODE or context.get('timeframe') != 'M5' or
        context.get('research_spec_sha256') != SPEC_SHA256 or context.get('kernel_sha256') != KERNEL_SHA256 or
        context.get('input_sha256') != intent.context_sha256 or context.get('side') != intent.side or
        context.get('decision_time_utc') != intent.decision_time_utc or
        hashlib.sha256(canonical(payload)).hexdigest() != context.get('context_payload_sha256')):
        raise ValueError('Gold intent/source metadata changed.')
    path = Path(context.get('input_file', ''))
    if not path.is_file() or digest(path) != intent.context_sha256:
        raise ValueError('Gold completed-M5 snapshot is missing or changed.')
    frame = pd.read_csv(path, index_col=0, float_precision='round_trip')
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
    frame = validate_m5(frame)
    if len(frame) != 1200 or (frame.index[-1] + pd.Timedelta(minutes=5)).isoformat() != intent.decision_time_utc:
        raise ValueError('Gold snapshot does not match the bounded decision window.')
    from engine import setup_profiles
    setup_profile = setup_profiles.context_profile(context)
    if intent.trigger != setup_profiles.trigger(context):
        raise ValueError('Gold setup trigger differs from frozen context.')
    if context_profile(context) == ACTIVATION:
        features, _ = setup_profiles.features(frame, GOLD_SYMBOL, setup_profile)
        if setup_profile != setup_profiles.DEFAULT and features.get('setup_id') != context['setup_id']:
            raise ValueError('Gold selected setup does not reproduce from frozen evidence.')
    else:
        features = window_features(frame)
    if (features.get('side') != intent.side or canonical(features) != canonical(context.get('features', {})) or
        features.get('atr14') != intent.atr or
        frozen_stop(features, context['frozen_quote'], info.trade_tick_size, intent.side) != intent.stop_loss or
        context.get('frozen_stop_loss') != intent.stop_loss):
        raise ValueError('Gold signal/stop does not reproduce from its frozen input.')
    return features


def build_gold_protection(api, info, quote: dict, funds: dict, intent: orders.StrategyIntent, policy: orders.StrategyPolicy) -> dict:
    if policy.target_mode != TARGET_MODE or policy.slippage_reserve_points_per_side != 50:
        raise ValueError('Gold policy mismatch.')
    validate_gold_intent(intent)
    validate_policy(policy, intent)
    features = verified_context(intent, info)
    reference = Decimal(str(quote['ask' if intent.side == 'buy' else 'bid']))
    direction = 1 if intent.side == 'buy' else -1
    if direction * (reference - Decimal(str(intent.stop_loss))) < Decimal('1.2') * Decimal(str(intent.atr)):
        raise ValueError('Fresh Gold quote leaves less than the frozen 1.2 ATR stop floor.')
    spread = Decimal(str(quote['ask'])) - Decimal(str(quote['bid']))
    if not 0 < spread <= 1:
        raise ValueError('Gold spread is zero, crossed or exceeds $1/oz.')
    plans = {r: orders._build_strategy_protection(api, info, quote, funds, intent, policy, target_reward_r=r) for r in (1, 2, 3, 4)}
    for r in (2, 3, 4):
        if any(plans[r][key] != plans[1][key] for key in ('stop_loss', 'volume_lots', 'planned_loss_with_reserves', 'risk_budget')):
            raise ValueError('Gold targets changed the common stop/size/risk.')
    selected, evidence, reason = select_target(features, plans, float(spread))
    if selected is None:
        raise ValueError(f'Gold target unavailable: {reason}')
    if policy.entry_setups=='multi_setup_v1':
        aligned=features.get('trend_long' if intent.side=='buy' else 'trend_short') is True
        evidence=dict(evidence,directional_context_aligned=aligned,raw_selector_reward_r=selected)
        if not aligned and selected>2:
            selected=2
            evidence.update(selected_reward_r=selected,strong_context=False,
                branch_reason='2r_room_nontrend_context_cap')
    evidence = dict(evidence, research_spec_sha256=SPEC_SHA256, kernel_sha256=KERNEL_SHA256,
                    target_mode=TARGET_MODE, context_payload_sha256=intent.adaptive_context['context_payload_sha256'])
    evidence.update(metadata(policy.entry_timing))
    plan = dict(plans[selected], **{k: v for k, v in evidence.items() if k not in plans[selected]})
    plan.update(selected_reward_r=selected, reward_to_risk=selected, exit_policy='gold_adaptive_1_2_3_4',
                adaptive_target_evidence=evidence, research_spec_sha256=SPEC_SHA256, target_mode=TARGET_MODE,
                stop_distance_price=float(direction * (reference - Decimal(str(intent.stop_loss)))),
                target_distance_price=float(direction * (Decimal(str(plan['take_profit'])) - reference)),
                stop_distance_pips=None, target_distance_pips=None, instrument_type='broker Gold CFD',
                economic_filter='Routine US clock blackouts only; no actual event-calendar or macro model.',
                time_exit='Fixed broker SL/TP only; no guaranteed intraday exit.',
                limitations='Unproven Gold DEMO research; gaps/slippage/costs can exceed planned risk.')
    if policy.entry_timing == ACTIVATION:
        plan['economic_filter'] = 'User-activated timing: no fixed clock blackout or actual economic-event calendar.'
    from engine.conditional_risk import validate_plan
    validate_plan(plan, policy, info=info)
    return plan
