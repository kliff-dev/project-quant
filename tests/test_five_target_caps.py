"""Native target/risk guards under deliberately adversarial context features.

Only frozen-feature reconstruction is stubbed here. Real native profit math,
stop/target rounding, cost checks, conditional sizing and target selectors run.
The separate profile-integration tests prove actual-price reconstruction.
"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import shutil

import pytest

from engine import adaptive_orders, gold_orders, setup_profiles, strategy_orders as orders
from engine.conditional_risk import MODE, favorable, validate_plan
from engine.entry_timing import ACTIVATION, metadata
from test_adaptive_orders import native, fixture_signal, synthetic_intent
from test_gold_execution import gold, candidate, rig


def _native_case(native, gold, monkeypatch, symbol, side, setup_id, *, aligned, wide=False):
    """Ample verified-obstacle room; direction alignment is the tested variable."""
    d = 1 if side == 'buy' else -1
    if symbol == 'XAUUSDm':
        monkeypatch.setattr(orders, 'utc_now', lambda: gold.clock.now)
        intent = candidate(gold, monkeypatch, side)
        f = gold_orders.verified_context(intent, gold.rig.symbol)
        api, info = gold.rig.api, gold.rig.symbol
        quote = {'bid': gold.rig.tick.bid, 'ask': gold.rig.tick.ask}
        funds = {'balance': 500., 'equity': 500., 'currency': 'USD'}
        policy = gold.policy
        prefix, distance, obstacle_distance = 'm15', Decimal('7.5'), Decimal('100')
        opened, confirmed = '2026-11-13T08:00:00Z', '2026-11-13T08:45:00Z'
    else:
        monkeypatch.setattr(orders, 'utc_now', lambda: datetime(2026, 11, 13, 11, 0, 30, tzinfo=timezone.utc))
        f = fixture_signal(side)
        intent = synthetic_intent(f, side)
        monkeypatch.setattr(adaptive_orders, '_verified_context', lambda *args, **kwargs: f)
        api, info, quote, funds = native.api, native.info, native.quote, native.funds
        policy = replace(native.policy, target_mode='adaptive_pullback_v2')
        prefix, distance, obstacle_distance = 'h4', Decimal('.0075'), Decimal('.1')
        opened, confirmed = '2026-11-12T00:00:00Z', '2026-11-12T12:00:00Z'
    policy = replace(policy, entry_timing=ACTIVATION, entry_setups=setup_profiles.MULTI,
                     risk_percent=2, risk_mode=MODE)
    ctx = dict(intent.adaptive_context, **metadata(ACTIVATION),
               **setup_profiles.metadata(setup_profiles.MULTI), setup_id=setup_id)
    intent = replace(intent, trigger=setup_profiles.trigger(ctx), adaptive_context=ctx)
    reference = Decimal(str(quote['ask' if side == 'buy' else 'bid']))
    if wide:
        intent = replace(intent, stop_loss=float(reference-d*distance))
    # Absolute strength alone would qualify the old selector, even when the
    # profile explicitly says that context is not aligned with this entry.
    atr = Decimal(str(f[f'{prefix}_atr14']))
    f[f'{prefix}_ema50'] = float(Decimal(str(f[f'{prefix}_ema200']))+d*2*atr)
    f[f'{prefix}_ema50_slope5'] = float(d*Decimal('.5')*atr)
    f.update(side=side, trend_long=bool(aligned and side == 'buy'),
             trend_short=bool(aligned and side == 'sell'),
             market_regime='trend_up' if aligned and side == 'buy' else
                           'trend_down' if aligned else 'mixed',
             volatility_ratio=1., setup_id=setup_id, strategy_family=setup_id)
    obstacle = reference+d*obstacle_distance
    if side == 'sell':
        obstacle -= Decimal(str(quote['ask']))-Decimal(str(quote['bid']))
    f[f'{prefix}_swing_highs' if side == 'buy' else f'{prefix}_swing_lows'] = [dict(
        price=float(obstacle), pivot_open_utc=opened, confirmed_at_utc=confirmed)]
    assert favorable(f, gold=symbol == 'XAUUSDm')
    def build():
        return orders.build_strategy_protection(api, info, quote, funds, intent, policy)
    return intent, f, api, info, policy, build


@pytest.mark.parametrize('symbol', ['EURUSDm', 'XAUUSDm'])
@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('setup_id', ['range_reentry', 'compression_breakout'])
def test_native_nontrend_families_cannot_borrow_three_or_four_r_from_absolute_strength(
        native, gold, monkeypatch, symbol, side, setup_id):
    intent, f, api, info, policy, build = _native_case(
        native, gold, monkeypatch, symbol, side, setup_id, aligned=False)
    plan = build()
    assert plan['target_room_flags']['4'] is True
    assert plan['selected_reward_r'] == 2
    assert plan['strong_context'] is False
    assert plan['risk_percent'] == 1 and plan['risk_budget'] == 5.
    assert plan['planned_loss_with_reserves'] <= 5.
    assert plan['conditional_risk_evidence']['minimum_lot_rescue'] is False
    assert plan['stop_loss'] == intent.stop_loss
    assert plan['strategy_intent']['adaptive_context']['setup_id'] == setup_id
    if symbol == 'XAUUSDm':
        assert plan['adaptive_target_evidence']['raw_selector_reward_r'] == 4
        assert plan['adaptive_target_evidence']['directional_context_aligned'] is False
    api.order_check.assert_not_called()
    api.order_send.assert_not_called()


@pytest.mark.parametrize('symbol', ['EURUSDm', 'XAUUSDm'])
@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_native_aligned_compression_can_use_four_r_without_increasing_baseline_risk(
        native, gold, monkeypatch, symbol, side):
    intent, f, api, info, policy, build = _native_case(
        native, gold, monkeypatch, symbol, side, 'compression_breakout', aligned=True)
    plan = build()
    assert plan['selected_reward_r'] == 4 and plan['target_room_flags']['4'] is True
    assert plan['strong_context'] is True
    assert plan['risk_percent'] == 1 and plan['risk_budget'] == 5.
    assert plan['planned_loss_with_reserves'] <= 5.
    assert plan['stop_loss'] == intent.stop_loss
    api.order_check.assert_not_called()
    api.order_send.assert_not_called()


@pytest.mark.parametrize('symbol', ['EURUSDm', 'XAUUSDm'])
@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('setup_id', ['range_reentry', 'compression_breakout'])
def test_native_minimum_lot_rescue_rejects_nontrend_context_even_with_absolute_strength(
        native, gold, monkeypatch, symbol, side, setup_id):
    intent, f, api, info, policy, build = _native_case(
        native, gold, monkeypatch, symbol, side, setup_id, aligned=False, wide=True)
    with pytest.raises(ValueError, match='elevated risk requires strong trend and normal volatility'):
        build()
    api.order_check.assert_not_called()
    api.order_send.assert_not_called()
    # For the compression hypothesis, change only the tested alignment flag;
    # the same stop, obstacle, costs and contract now fit the qualified rescue.
    if setup_id == 'compression_breakout':
        f['trend_long' if side == 'buy' else 'trend_short'] = True
        plan = build()
        assert plan['volume_lots'] == info.volume_min
        assert plan['conditional_risk_evidence']['minimum_lot_rescue'] is True
        assert 1 < plan['actual_planned_risk_percent'] <= 2
        assert plan['risk_budget'] == 10.
        assert plan['stop_loss'] == intent.stop_loss
        validate_plan(plan, policy, info=info, replay=True)
        api.order_check.assert_not_called()
        api.order_send.assert_not_called()


@pytest.mark.parametrize('relative', ['analytics/five_setups.py', 'config/five_setup_spec.json'])
def test_multi_profile_source_or_spec_mutation_fails_closed(tmp_path, monkeypatch, relative):
    original = setup_profiles.ROOT
    for source in setup_profiles.MULTI_PINS:
        target = tmp_path/source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original/source, target)
    monkeypatch.setattr(setup_profiles, 'ROOT', tmp_path)
    assert setup_profiles.metadata(setup_profiles.MULTI)['entry_setups'] == setup_profiles.MULTI
    changed = tmp_path/relative
    changed.write_bytes(changed.read_bytes()+b'\n')
    with pytest.raises(ValueError, match='Setup evidence changed'):
        setup_profiles.metadata(setup_profiles.MULTI)
