"""Versioned minimum-volume exception; no order submission or terminal login."""
from __future__ import annotations
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
from pathlib import Path

MODE = 'minimum_lot_rescue_v1'
SPEC_FILE = 'config/conditional_risk_spec.json'
SPEC_SHA256 = 'd06e5ff4a63f6a4059fc9dc8a519242cc7350d32d2baa32c9f215e06c360f13b'
ROOT = Path(__file__).resolve().parents[1]


def verify_profile():
    if hashlib.sha256((ROOT/SPEC_FILE).read_bytes()).hexdigest() != SPEC_SHA256:
        raise ValueError('Conditional risk specification changed; review before execution.')


def verified_features(intent, info):
    verify_profile()
    if intent.symbol == 'XAUUSDm':
        from engine.gold_orders import verified_context
        return verified_context(intent, info)
    if intent.symbol == 'EURUSDm':
        from engine.adaptive_orders import _verified_context
        return _verified_context(intent, info, target_mode='adaptive_pullback_v2')
    raise ValueError('Conditional risk requires an exact approved Gold or EUR/USD symbol.')


def favorable(features, *, gold):
    keys = (('m15_ema50', 'm15_ema200', 'm15_ema50_slope5', 'm15_atr14') if gold else
            ('h4_ema50', 'h4_ema200', 'h4_ema50_slope5', 'h4_atr14'))
    from engine.strategy_orders import _positive
    ema50, ema200 = [_positive(features[k], k) for k in keys[:2]]
    slope = Decimal(str(features[keys[2]]))
    atr = _positive(features[keys[3]], keys[3])
    ratio = _positive(features['volatility_ratio'], 'volatility ratio')
    if not slope.is_finite():
        raise ValueError('Finite context slope required.')
    return (abs(ema50-ema200) >= atr and abs(slope) >= Decimal('.25')*atr and
            Decimal('.75') <= ratio <= Decimal('1.5'))


def favorable_for_intent(intent,features,*,gold):
    """New non-trend families cannot borrow the trend-only sizing exception."""
    if (intent.adaptive_context or {}).get('entry_setups')=='multi_setup_v1':
        key='trend_long' if intent.side=='buy' else 'trend_short'
        if features.get(key) is not True:
            return False
    return favorable(features,gold=gold)


def validate_plan(plan, policy=None, *, info=None, replay=False):
    """Validate final selected plan in the planner, authority and portfolio proof."""
    from engine.strategy_orders import StrategyPolicy, StrategyIntent, _positive
    policy = policy or StrategyPolicy(**plan['policy'])
    if policy.risk_mode != MODE:
        return
    verify_profile()
    evidence = plan.get('conditional_risk_evidence') or {}
    rescue = evidence.get('minimum_lot_rescue')
    if (type(rescue) is not bool or evidence.get('risk_mode') != MODE or
        evidence.get('risk_spec_sha256') != SPEC_SHA256):
        raise ValueError('Conditional risk decision/source evidence is missing or changed.')
    basis = _positive(plan['risk_basis_amount'], 'risk basis')
    baseline = (basis*Decimal('.01')).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
    ceiling = (basis*Decimal('.02')).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
    budget = _positive(plan['risk_budget'], 'risk budget')
    risk = _positive(plan['planned_loss_with_reserves'], 'planned loss')
    expected_budget = ceiling if rescue else baseline
    if (budget != expected_budget or risk > budget or
        plan.get('risk_percent') != (2 if rescue else 1) or
        abs(Decimal(str(plan.get('actual_planned_risk_percent', -1)))-risk/basis*100) > Decimal('0.000000001')):
        raise ValueError('Conditional plan exceeds or misstates its baseline/ceiling risk.')
    if not rescue:
        return
    minimum = _positive(evidence.get('broker_minimum_lots'), 'broker minimum lots')
    baseline_fee = (baseline*Decimal(str(policy.additional_cost_reserve_fraction))).quantize(Decimal('.01'), rounding=ROUND_CEILING)
    ceiling_fee = (ceiling*Decimal(str(policy.additional_cost_reserve_fraction))).quantize(Decimal('.01'), rounding=ROUND_CEILING)
    baseline_loss = risk-ceiling_fee+baseline_fee
    if (Decimal(str(plan['volume_lots'])) != minimum or risk <= baseline or
        Decimal(str(plan['additional_cost_reserve_usd'])) != ceiling_fee or baseline_loss <= baseline or
        abs(Decimal(str(evidence.get('minimum_lot_loss_at_baseline', -1)))-baseline_loss) > Decimal('0.000000001') or
        type(plan.get('selected_reward_r')) is not int or plan['selected_reward_r'] < 2):
        raise ValueError('Elevated risk requires the minimum lot, baseline overflow and room for at least 2R.')
    if info is not None and Decimal(str(info.volume_min)) != minimum:
        raise ValueError('Broker minimum changed from the conditional sizing decision.')
    if replay:
        if info is None:
            raise ValueError('Native symbol properties required to replay elevated risk.')
        intent = StrategyIntent(**plan['strategy_intent'])
        features = verified_features(intent, info)
        if not favorable_for_intent(intent,features, gold=intent.symbol=='XAUUSDm'):
            raise ValueError('Elevated risk requires verified strong trend and normal volatility.')
