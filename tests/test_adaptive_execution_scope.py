"""Offline integration guards for opting the DEMO executor into adaptive exits."""

from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json

import pytest

from test_demo_executor import rig, authorized_rig
from test_market_monitor import watcher
from engine import demo_executor, market_monitor
from engine.demo_authorization import DemoEntryAuthorization
from engine.demo_executor import DemoExecutionDisabledError
from engine.strategy_orders import StrategyPolicy


def adaptive_authority(rig, monkeypatch, target_mode='adaptive_pullback_v1'):
    ctx = authorized_rig(rig, monkeypatch)
    facts = ctx.executor.prepare_entry()
    policy = StrategyPolicy(target_mode=target_mode)
    ctx.policy.write_text(json.dumps(asdict(policy)), encoding='utf-8')
    authority = DemoEntryAuthorization.create(rig.executor.connector, rig.directory, ctx.policy,
        expires_at=ctx.clock.now + timedelta(hours=1), user_instruction='Monday adaptive DEMO pilot.')
    facts['protection'].update(policy=asdict(policy), selected_reward_r=1, exit_policy='adaptive_1_2_3',
        reward_to_risk=1, planned_reward_to_risk=1.01, adaptive_target_evidence={'branch': 1})
    facts['protection']['strategy_intent']['adaptive_context'] = {'source': 'synthetic gate fixture'}
    if target_mode == 'adaptive_pullback_v2':
        facts['protection'].update(exit_policy='adaptive_1_2_3_4', research_spec_sha256='c' * 64)
        facts['protection']['strategy_intent']['adaptive_context'].update(
            target_mode=target_mode, research_spec_sha256='c' * 64)
    return ctx, authority, facts


@pytest.mark.parametrize('selected', [1, 2, 3])
def test_authority_accepts_only_explicit_adaptive_selected_ratio(rig, monkeypatch, selected):
    _, authority, facts = adaptive_authority(rig, monkeypatch)
    facts['protection'].update(selected_reward_r=selected, reward_to_risk=selected,
                               planned_reward_to_risk=selected + .001)
    authority.validate(rig.executor.connector, facts)
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('selected', [1, 2, 3, 4])
def test_extended_authority_accepts_up_to_four_r_only_for_v2(rig, monkeypatch, selected):
    _, authority, facts = adaptive_authority(rig, monkeypatch, 'adaptive_pullback_v2')
    facts['protection'].update(selected_reward_r=selected, reward_to_risk=selected,
                               planned_reward_to_risk=selected + .001)
    authority.validate(rig.executor.connector, facts)
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('field,value', [('selected_reward_r', 5), ('selected_reward_r', True),
    ('selected_reward_r', 4.0), ('exit_policy', 'adaptive_1_2_3'), ('planned_reward_to_risk', 3.99),
    ('reward_to_risk', 3), ('research_spec_sha256', 'd' * 64)])
def test_extended_authority_rejects_invalid_or_changed_scope(rig, monkeypatch, field, value):
    _, authority, facts = adaptive_authority(rig, monkeypatch, 'adaptive_pullback_v2')
    facts['protection'].update(selected_reward_r=4, reward_to_risk=4, planned_reward_to_risk=4.001)
    facts['protection'][field] = value
    with pytest.raises(ValueError):
        authority.validate(rig.executor.connector, facts)
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('selected', [None, True, False, '1', 1.0, float('nan'), 0, 4])
def test_authority_rejects_invalid_adaptive_branch(rig, monkeypatch, selected):
    _, authority, facts = adaptive_authority(rig, monkeypatch)
    facts['protection']['selected_reward_r'] = selected
    with pytest.raises(ValueError, match='Adaptive candidate'):
        authority.validate(rig.executor.connector, facts)
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('field,value', [('reward_to_risk', 2), ('exit_policy', 'fixed_2r'),
    ('adaptive_target_evidence', {}), ('planned_reward_to_risk', .99),
    ('planned_reward_to_risk', float('nan')), ('planned_reward_to_risk', True)])
def test_adaptive_authority_rejects_incomplete_or_under_target_plan(rig, monkeypatch, field, value):
    _, authority, facts = adaptive_authority(rig, monkeypatch)
    facts['protection'][field] = value
    with pytest.raises(ValueError):
        authority.validate(rig.executor.connector, facts)
    rig.api.order_send.assert_not_called()


def test_fixed_authority_still_rejects_one_r(rig, monkeypatch):
    ctx = authorized_rig(rig, monkeypatch)
    facts = ctx.executor.prepare_entry()
    facts['protection']['planned_reward_to_risk'] = 1
    with pytest.raises(ValueError, match='risk/volume/ratio'):
        ctx.authorization.validate(rig.executor.connector, facts)


@pytest.mark.parametrize('target_mode', ['adaptive_pullback_v1', 'adaptive_pullback_v2'])
@pytest.mark.parametrize('field,value', [('selected_reward_r', 2), ('exit_policy', 'fixed_2r'),
    ('adaptive_target_evidence', {'branch': 2})])
def test_checked_preview_cannot_change_adaptive_branch(rig, monkeypatch, target_mode, field, value):
    ctx, _, approved = adaptive_authority(rig, monkeypatch, target_mode)
    fresh = deepcopy(approved)
    fresh['protection'][field] = value
    with pytest.raises(DemoExecutionDisabledError, match='adaptive target condition changed'):
        ctx.executor._match(approved, fresh)
    rig.api.order_send.assert_not_called()


def test_entry_signal_is_rechecked_after_last_blocking_verification(rig, monkeypatch):
    ctx = authorized_rig(rig, monkeypatch)
    facts = ctx.executor.prepare_entry()
    # Simulate time passing during the final audit/native-account call, after
    # the ordinary preflight has already passed. Durable reservation stays.
    original = rig.executor.connector.verify_demo_account
    def delayed_verify():
        result = original()
        ctx.clock.now += timedelta(seconds=301)
        return result
    monkeypatch.setattr(rig.executor.connector, 'verify_demo_account', delayed_verify)
    with pytest.raises(ValueError, match='five minutes|five-minute|300|fresh|expired|stale'):
        ctx.executor._send_once({'status': 'open_send_started', 'account_identity': facts['account_identity']},
                                facts, closing=False)
    rig.api.order_send.assert_not_called()
    assert ctx.executor.state_path.exists()


@pytest.mark.parametrize('target_mode', ['adaptive_pullback_v1', 'adaptive_pullback_v2'])
def test_adaptive_monitor_passes_its_explicit_policy_to_signal_adapter(watcher, monkeypatch, target_mode):
    watcher.policy.write_text(json.dumps(asdict(StrategyPolicy(target_mode=target_mode))))
    original = watcher.evaluate.side_effect
    monkeypatch.setattr(market_monitor, 'latest_strategy_intent',
                        lambda *args, **kwargs: original(*args))
    calls = []
    def evaluate(connector, directory, *, policy):
        calls.append(policy)
        return original(connector, directory)
    monkeypatch.setattr(market_monitor, 'latest_strategy_intent', evaluate)
    result = watcher.make().run(once=True)
    assert result['status'] == 'stopped'
    assert len(calls) == 1 and calls[0].target_mode == target_mode
    watcher.api.order_send.assert_not_called()
