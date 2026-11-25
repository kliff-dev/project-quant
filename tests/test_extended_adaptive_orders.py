"""Offline native 4R extension checks; v1 remains a separately pinned policy."""

from copy import deepcopy
from dataclasses import replace
import pytest

from analytics.account_research import ReplaySettings
from analytics.extended_targets import policy_plan
from engine import adaptive_orders as module
from engine import strategy_orders as orders
from test_adaptive_orders import (add_obstacle, build, fixture_signal, live_fixture,
                                  native, synthetic_intent)


@pytest.fixture
def extended(native):
    native.policy = replace(native.policy, target_mode=module.EXTENDED_TARGET_MODE)
    return native


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('branch', [1, 2, 3, 4])
def test_native_extension_selects_highest_qualified_target_without_resizing(extended, monkeypatch, side, branch):
    signal = fixture_signal(side)
    intent = synthetic_intent(signal, side)
    add_obstacle(extended, signal, intent, branch)
    monkeypatch.setattr(module, '_verified_context', lambda *_, **__: signal)
    result = build(extended, intent)
    base = orders._build_strategy_protection(extended.api, extended.info, extended.quote,
            extended.funds, intent, extended.policy, target_reward_r=1)
    assert type(result['selected_reward_r']) is int and result['selected_reward_r'] == branch
    assert result['exit_policy'] == 'adaptive_1_2_3_4'
    assert result['reward_to_risk'] == branch
    assert result['planned_reward_to_risk'] >= branch
    assert result['target_room_flags'] == {str(r): r <= branch for r in (1, 2, 3, 4)}
    for key in ('volume_lots', 'stop_loss', 'risk_budget', 'risk_basis_amount',
                'additional_cost_reserve_usd', 'planned_loss_with_reserves'):
        assert result[key] == base[key]
    evidence = result['adaptive_target_evidence']
    assert evidence['target_mode'] == module.EXTENDED_TARGET_MODE
    assert evidence['exit_policy'] == 'adaptive_1_2_3_4'
    assert evidence['research_spec_sha256'] == module.EXTENDED_SPEC_SHA256
    assert evidence['base_research_spec_sha256'] == module.SPEC_SHA256
    assert evidence['base_target_mode'] == module.TARGET_MODE
    assert result['risk_percent'] == 1 and result['risk_budget'] == 5
    expected, reason = policy_plan(signal, 1.1, 500., ReplaySettings(), policy='adaptive_1_2_3_4')
    assert not reason
    for key in ('selected_reward_r', 'take_profit', 'stop_loss', 'risk_budget',
                'target_room_flags', 'base_selected_reward_r', 'branch_reason'):
        assert result[key] == expected[key]
    assert result['volume_lots'] == expected['lots']
    assert result['planned_loss_with_reserves'] == expected['planned_risk']
    extended.api.order_send.assert_not_called()
    extended.api.order_check.assert_not_called()


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_fraction_short_of_native_4r_boundary_falls_back_to_3r(extended, monkeypatch, side):
    signal = fixture_signal(side)
    intent = synthetic_intent(signal, side)
    add_obstacle(extended, signal, intent, 4, extra=-.000001)
    monkeypatch.setattr(module, '_verified_context', lambda *_, **__: signal)
    result = build(extended, intent)
    assert result['selected_reward_r'] == 3
    assert result['target_room_flags']['3'] is True
    assert result['target_room_flags']['4'] is False


@pytest.mark.parametrize('field,value', [('h4_ema50_slope5', .000249), ('h4_ema50', 1.1009),
                                       ('volatility_ratio', .749999), ('volatility_ratio', 1.500001)])
def test_4r_room_alone_does_not_qualify_without_strong_normal_context(extended, monkeypatch, field, value):
    signal = fixture_signal()
    signal[field] = value
    intent = synthetic_intent(signal, 'buy')
    add_obstacle(extended, signal, intent, 4)
    monkeypatch.setattr(module, '_verified_context', lambda *_, **__: signal)
    result = build(extended, intent)
    assert result['target_room_flags']['4'] is True
    assert result['selected_reward_r'] == 2


@pytest.mark.parametrize('ratio', [.75, 1.5])
def test_4r_context_thresholds_are_inclusive(extended, monkeypatch, ratio):
    signal = fixture_signal()
    signal.update(h4_ema50=1.101, h4_ema200=1.1, h4_ema50_slope5=.00025,
                  volatility_ratio=ratio)
    intent = synthetic_intent(signal, 'buy')
    add_obstacle(extended, signal, intent, 4)
    monkeypatch.setattr(module, '_verified_context', lambda *_, **__: signal)
    assert build(extended, intent)['selected_reward_r'] == 4


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_extension_uses_nearest_historical_obstacle_not_more_distant_room(extended, monkeypatch, side):
    signal = fixture_signal(side)
    intent = synthetic_intent(signal, side)
    add_obstacle(extended, signal, intent, 2)
    key = 'h4_swing_highs' if side == 'buy' else 'h4_swing_lows'
    nearest = deepcopy(signal[key])
    add_obstacle(extended, signal, intent, 4)
    signal[key].extend(nearest)
    monkeypatch.setattr(module, '_verified_context', lambda *_, **__: signal)
    assert build(extended, intent)['selected_reward_r'] == 2


def test_v1_cannot_native_solve_or_select_4r(native, extended, monkeypatch):
    # The shared native object is deliberately restored to the original policy.
    native.policy = orders.StrategyPolicy(target_mode=module.TARGET_MODE)
    signal = fixture_signal()
    intent = synthetic_intent(signal, 'buy')
    far = orders._build_strategy_protection(native.api, native.info, native.quote,
            native.funds, intent, orders.StrategyPolicy(target_mode=module.EXTENDED_TARGET_MODE),
            target_reward_r=4)
    signal['h4_swing_highs'] = [dict(price=far['take_profit'] + .001,
            pivot_open_utc='2026-11-12T00:00:00+00:00', confirmed_at_utc='2026-11-12T12:00:00+00:00')]
    monkeypatch.setattr(module, '_verified_context', lambda *_: signal)
    result = build(native, intent)
    assert result['selected_reward_r'] == 3 and result['exit_policy'] == 'adaptive_1_2_3'
    assert '4' not in result['target_room_flags']
    assert 'base_research_spec_sha256' not in result['adaptive_target_evidence']
    with pytest.raises(ValueError, match='extended adaptive policy'):
        orders._build_strategy_protection(native.api, native.info, native.quote,
                native.funds, intent, native.policy, target_reward_r=4)


@pytest.mark.parametrize('reward', [True, 4., 5, 0, '4'])
def test_native_solver_rejects_malformed_reward_or_reward_over_ceiling(extended, reward):
    intent = synthetic_intent(fixture_signal(), 'buy')
    with pytest.raises(ValueError, match='whole'):
        orders._build_strategy_protection(extended.api, extended.info, extended.quote,
                extended.funds, intent, extended.policy, target_reward_r=reward)


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_v2_closed_context_roundtrips_and_pins_both_versions(extended, tmp_path, side):
    connector, window = live_fixture(extended, side)
    intent, report = orders.latest_strategy_intent(connector, tmp_path, policy=extended.policy)
    assert intent and report['target_mode'] == module.EXTENDED_TARGET_MODE
    assert report['research_spec_sha256'] == module.EXTENDED_SPEC_SHA256
    assert intent.adaptive_context['schema_version'] == 2
    assert intent.adaptive_context['base_research_spec_sha256'] == module.SPEC_SHA256
    signal = module._verified_context(intent, extended.info, target_mode=module.EXTENDED_TARGET_MODE)
    assert signal == intent.adaptive_context['features']
    assert signal['input_window_rows'] == 3000
    extended.api.order_check.assert_not_called()
    extended.api.order_send.assert_not_called()


@pytest.mark.parametrize('origin,destination', [(module.TARGET_MODE, module.EXTENDED_TARGET_MODE),
                                              (module.EXTENDED_TARGET_MODE, module.TARGET_MODE)])
def test_context_cannot_cross_target_versions_even_with_same_entry(extended, tmp_path, origin, destination):
    connector, _ = live_fixture(extended)
    intent, _ = orders.latest_strategy_intent(connector, tmp_path,
                                            policy=replace(extended.policy, target_mode=origin))
    with pytest.raises(ValueError, match='metadata'):
        module._verified_context(intent, extended.info, target_mode=destination)


@pytest.mark.parametrize('key,value', [('research_spec_sha256', '0' * 64),
                                    ('base_research_spec_sha256', '0' * 64),
                                    ('base_target_mode', 'adaptive_pullback_v2'),
                                    ('schema_version', 1), ('schema_version', 2.),
                                    ('target_mode', module.TARGET_MODE)])
def test_v2_rehashed_version_metadata_tampering_is_rejected(extended, tmp_path, key, value):
    connector, _ = live_fixture(extended)
    intent, _ = orders.latest_strategy_intent(connector, tmp_path, policy=extended.policy)
    context = deepcopy(intent.adaptive_context)
    context[key] = value
    payload = {k: v for k, v in context.items() if k != 'context_payload_sha256'}
    context['context_payload_sha256'] = module._sha(module._canonical(payload))
    with pytest.raises(ValueError, match='metadata|base version'):
        module._verified_context(replace(intent, adaptive_context=context), extended.info,
                                 target_mode=module.EXTENDED_TARGET_MODE)


def test_extended_spec_tampering_is_checked_independently_of_base(extended, tmp_path, monkeypatch):
    for relative in [module.SPEC_FILE, *module.KERNEL_SHA256]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((module.ROOT / relative).read_bytes())
    bad = tmp_path / module.EXTENDED_SPEC_FILE
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    # A v1 context must remain independent of the new optional spec.
    module.verify_frozen_kernel()
    with pytest.raises(ValueError, match='extended_target_spec'):
        module.verify_frozen_kernel(module.EXTENDED_TARGET_MODE)


def test_default_fixed_policy_stays_2r_and_does_not_require_extended_spec(native, monkeypatch):
    intent = synthetic_intent(fixture_signal(), 'buy')
    monkeypatch.setattr(module, 'verify_frozen_kernel', lambda *_: pytest.fail('legacy routed to adaptive'))
    result = orders.build_strategy_protection(native.api, native.info, native.quote, native.funds,
                                            intent, orders.StrategyPolicy())
    assert result['reward_to_risk'] == 2
    assert 'adaptive_target_evidence' not in result
    assert orders.StrategyPolicy().target_mode == 'fixed_2r'

