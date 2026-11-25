"""Explicit start-now DEMO authority. No scheduler, reconnect loop, or retry."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from engine import dual_demo as dual
from engine.demo_authorization import _exclusive_json
from engine.entry_timing import ACTIVATION, metadata
from engine.execution_audit import atomic_json
from engine.gold_connector import GoldConnector
from engine.confirmation_observer import ConfirmationObserver
from engine.setup_observer import SetupObserver
from engine.gold_demo import unresolved
from engine.market_monitor import monitor_lock
from engine.strategy_orders import StrategyPolicy, latest_strategy_intent

PLAN = Path('config/activation_demo_plan.json')
POLICIES = {'XAUUSDm': Path('config/activation_gold_policy.json'),
            'EURUSDm': Path('config/activation_eurusd_policy.json')}
INSTRUCTION = 'We dont have to make schedules when to trade as long as I tell you you can trade at this moment if there is a valid setup. Can you do that?'
HOURS = 6
MAX_EXPLICIT_HOURS = 24
RISK_INSTRUCTION = 'you do not have to maximize that 2 percent even 1.3-2.0 percent would be good. just make sure we are able to enter valid setups but the ceiling would be 2 percent'
SETUP_INSTRUCTION = 'Then can you make 5 valid setups for eurusd and another 5 for gold base on the research that we did.'


def requested_deadline(current, until=None):
    """Six hours by default; a separately explicit aware deadline may override it."""
    if current.utcoffset() is None:
        raise ValueError('Timezone-aware PC clock required.')
    if until is None:
        return current + timedelta(hours=HOURS)
    try:
        deadline = datetime.fromisoformat(until) if isinstance(until, str) else until
        aware = isinstance(deadline, datetime) and deadline.utcoffset() is not None
    except ValueError as exc:
        raise ValueError('Explicit end time must be an ISO timestamp with a timezone.') from exc
    if not aware or not 3.6 <= (deadline-current).total_seconds() <= MAX_EXPLICIT_HOURS*3600:
        raise ValueError('Explicit end time must be aware, future, and within 24 hours (at least 3.6 seconds away).')
    return deadline.astimezone(timezone.utc)


def verify_sources(root):
    root = Path(root).resolve()
    plan = json.loads((root / PLAN).read_text(encoding='utf-8'))
    expected = dict(version=1, demo_only=True, scheduled=False, entry_timing=ACTIVATION,
        maximum_session_hours=HOURS, maximum_explicit_session_hours=MAX_EXPLICIT_HOURS,
        risk_percent=2, baseline_risk_percent=1, risk_mode='minimum_lot_rescue_v1',
        risk_update_user_instruction=RISK_INSTRUCTION, max_open_positions=4,
        maximum_positions_per_symbol=2, maximum_combined_risk_percent=8, portfolio_scope=dual.MULTI_SCOPE,
        maximum_reward_r=4, entry_setups='multi_setup_v1', user_instruction=INSTRUCTION,
        setup_update_user_instruction=SETUP_INSTRUCTION,
        position_update_user_instruction=dual.MULTI_POSITION_UPDATE, combined_risk_user_instruction=dual.MULTI_RISK_UPDATE)
    if any(plan.get(k) != v or type(plan.get(k)) is not type(v) for k,v in expected.items()):
        raise ValueError('Activation plan differs from the explicit DEMO scope.')
    required = {p.as_posix() for p in POLICIES.values()} | {
        'engine/activation_demo.py', 'engine/dual_demo.py', 'engine/entry_timing.py', 'engine/execution_state.py',
        'engine/portfolio_guard.py','engine/demo_authorization.py','engine/demo_series.py','engine/demo_executor.py','engine/market_monitor.py',
        'analytics/activation_signals.py', 'config/activation_timing_spec.json', 'scripts/activate_demo.py',
        'engine/conditional_risk.py', 'config/conditional_risk_spec.json',
        'engine/setup_profiles.py','engine/setup_observer.py','analytics/two_setups.py','config/two_setup_spec.json',
        'analytics/five_setups.py','config/five_setup_spec.json',
        'engine/confirmation_observer.py','analytics/entry_confirmation.py','config/entry_confirmation_spec.json'}
    hashes = plan.get('source_sha256', {})
    if not required.issubset(hashes):
        raise ValueError('Activation source fingerprints are not finalized.')
    for relative, expected_sha in hashes.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
            raise ValueError(f'Activation approved source changed: {relative}')
    metadata(ACTIVATION)
    from engine.setup_profiles import metadata as setup_metadata
    setup_metadata('multi_setup_v1')
    from engine.gold_orders import verify_kernel
    from engine.adaptive_orders import verify_frozen_kernel
    verify_kernel()
    verify_frozen_kernel('adaptive_pullback_v2')
    for symbol, relative in POLICIES.items():
        policy = StrategyPolicy.load(root / relative)
        if (policy.entry_timing != ACTIVATION or policy.entry_setups != 'multi_setup_v1' or policy.risk_percent != 2 or policy.risk_mode != 'minimum_lot_rescue_v1' or policy.max_lots != .1 or
            policy.target_mode != ('gold_intraday_v1' if symbol == 'XAUUSDm' else 'adaptive_pullback_v2')):
            raise ValueError('Activation policy differs from the instrument/risk scope.')
    return plan


def start(root, activation_id, *, until=None, now=dual.utc_now, connector_factory=GoldConnector,
          controller_factory=dual.DualController):
    root = Path(root).resolve()
    if not isinstance(activation_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', activation_id):
        raise ValueError('Explicit activation ID required: 1-48 letters, digits, hyphens or underscores.')
    current = now()
    deadline = requested_deadline(current, until)
    instruction = f'{INSTRUCTION} Risk update: {RISK_INSTRUCTION}. Position update: {dual.MULTI_POSITION_UPDATE}. Combined risk update: {dual.MULTI_RISK_UPDATE}. Baseline 1%; conditional minimum-lot-only exception up to 2%; combined ceiling 8%; maximum two positions per market, four total.'
    instruction += f' Setup update: {SETUP_INSTRUCTION} Five-setup DEMO experiment per instrument: trend reclaim, trend sweep, channel breakout, compression breakout and range re-entry; independent versioned rules with shared unchanged risk and position limits.'
    if until is not None:
        instruction += f' Explicit user activation deadline: {until}; normalized UTC {deadline.isoformat()}.'
    verify_sources(root)
    logs = root / 'logs'
    reservation_path = logs / 'activations' / f'{activation_id}.launch.json'
    with monitor_lock(logs / 'monitor/monitor.lock'):
        if reservation_path.exists():
            raise ValueError('This activation was already consumed; no reset or automatic retry.')
        if unresolved(root):
            raise ValueError('Earlier execution remains unresolved; actual exit proof is required.')
        with connector_factory() as connector:
            connector.verify_pinned_account()
            positions, pending = connector.terminal_api.positions_get(), connector.terminal_api.orders_get()
            if positions is None or pending is None or positions or pending:
                raise ValueError('Activation requires a verified flat account with no pending orders.')
            for symbol in dual.ORDER:
                snapshot = dual.InstrumentView(connector, symbol).inspect()
                if (snapshot['permissions']['terminal_trade_allowed'] is not True or
                    snapshot['permissions']['external_python_trading_disabled'] is not False or
                    snapshot['account']['account_trade_allowed'] is not True or
                    snapshot['account']['account_expert_allowed'] is not True):
                    raise ValueError('Native terminal/account Python trading permissions are off or unknown.')
            if not current <= now() < deadline:
                raise ValueError('Clock changed or activation expired during native preflight.')
            verify_sources(root)
            controller = controller_factory(root, connector, now=now,
                windows={s: (current, deadline) for s in dual.ORDER}, policies=POLICIES,
                source_verifier=verify_sources, activation_id=activation_id, instruction=instruction,
                maximum_risk_percent=2, risk_mode='minimum_lot_rescue_v1', observer=SetupObserver(root,connector,profile='multi_setup_v1'),portfolio_scope=dual.MULTI_SCOPE)
            reservation = dict(status='reserved', activation_id=activation_id, run_id=controller.run_id,
                requested_at_utc=current.isoformat(), deadline_utc=deadline.isoformat(),
                user_instruction=instruction, explicit_until=until.isoformat() if isinstance(until,datetime) else until,
                retry_allowed=False, entry_timing=ACTIVATION)
            _exclusive_json(reservation_path, reservation)
            try:
                result = controller.run()
            except BaseException as exc:
                atomic_json(reservation_path, dict(reservation, status='failed_no_retry', error=str(exc)))
                raise
            atomic_json(reservation_path, dict(reservation, status='finished', monitor_status=result['status']))
            return result


def preview(root, *, connector_factory=GoldConnector):
    """Read-only native setup inspection: no authority, order_check, or order_send."""
    root = Path(root).resolve()
    verify_sources(root)
    result = dict(mode='read_only_preview', entry_timing=ACTIVATION, orders_sent=0, instruments={})
    with connector_factory() as connector:
        result['account'] = connector.verify_pinned_account()
        for symbol in dual.ORDER:
            view = dual.InstrumentView(connector, symbol)
            _, decision = latest_strategy_intent(view, root/'logs', policy=StrategyPolicy.load(root/POLICIES[symbol]))
            result['instruments'][symbol] = decision
        positions, pending = connector.terminal_api.positions_get(), connector.terminal_api.orders_get()
        if positions is None or pending is None:
            raise ValueError('Preview cannot verify account positions or pending orders.')
        result['positions'] = len(positions)
        result['pending_orders'] = len(pending)
    atomic_json(root/'reports/latest_activation_preview.json', result)
    return result


status = dual.status
stop = dual.stop
