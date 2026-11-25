"""Manually activated, bounded Gold DEMO pilot; offline status is the default."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from engine.demo_authorization import _exclusive_json
from engine.demo_executor import DemoExecutor
from engine.demo_series import DemoSeriesAuthorization, DemoSeriesMonitor
from engine.execution_audit import atomic_json
from engine.execution_state import unresolved_execution_files
from engine.gold_connector import GoldConnector, GOLD_SYMBOL, PINNED_IDENTITY, PINNED_BROKER
from engine.gold_orders import TARGET_MODE, verify_kernel
from engine.market_monitor import MonitorConfig, monitor_lock, read_status, request_stop
from engine.strategy_orders import StrategyPolicy, latest_strategy_intent

PLAN = Path('config/gold_demo_plan.json')
POLICY = Path('config/gold_demo_policy.json')


def utc_now():
    return datetime.now(timezone.utc)


def load_plan(root: Path) -> dict:
    plan = json.loads((root / PLAN).read_text(encoding='utf-8'))
    if (plan.get('target_mode') != TARGET_MODE or plan.get('symbol') != GOLD_SYMBOL or
        plan.get('demo_only') is not True or plan.get('scheduled') is not False or
        type(plan.get('risk_percent')) not in (int, float) or plan['risk_percent'] != 1 or
        type(plan.get('maximum_reward_r')) is not int or plan['maximum_reward_r'] != 4 or
        plan.get('account_identity') != dict(login=PINNED_IDENTITY[0], server=PINNED_IDENTITY[1], broker=PINNED_BROKER, currency='USD')):
        raise ValueError('Gold plan changed from the approved instrument/account/risk/target scope.')
    return plan


def verify_sources(root: Path, plan: dict):
    sources = plan.get('source_sha256')
    if not isinstance(sources, dict) or POLICY.as_posix() not in sources:
        raise ValueError('Gold source fingerprints are not finalized.')
    for relative, expected in sources.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Gold approved source changed: {relative}')
    verify_kernel()


def unresolved(root: Path) -> list[Path]:
    return unresolved_execution_files(root)


def describe(root: Path) -> dict:
    plan = load_plan(root)
    status = read_status(root / 'logs')
    observed = 'not_running'
    combined = status.get('mode') == 'dual_demo_series'
    if status.get('status') == 'running' and (combined or status.get('policy', {}).get('target_mode') == TARGET_MODE):
        observed = 'running' if status.get('status_observation') == 'recent_heartbeat' else 'status_unverified'
    return dict(status=observed,
                scheduled=False, demo_only=True, symbol=GOLD_SYMBOL, instrument='broker Gold CFD; USD per troy ounce',
                setup='M15 trend / M5 EMA20 reclaim', risk_percent=status.get('policy',{}).get('risk_percent',1) if combined else 1, maximum_reward_r=4,
                maximum_open_positions_account_wide=2 if combined else 1, maximum_positions_per_symbol=1, maximum_session_hours=6,
                combined_controller=combined, instrument_phase=(status.get('instruments', {}).get(GOLD_SYMBOL) or {}).get('phase'),
                session=('User-activated now; no fixed hour/weekday/clock-blackout filter.' if status.get('entry_timing')=='user_activation_v1' else
                         'Weekdays 07:00-17:00 Europe/London; routine US clock blackouts apply to entries.'),
                economic_filter=('No clock blackout or actual economic calendar; no surprise or seasonal model.' if status.get('entry_timing')=='user_activation_v1' else
                                 'Clock proxy only; no actual economic calendar, surprise or seasonal model.'),
                unresolved_execution_files=[str(p) for p in unresolved(root)],
                message=('Combined controller: one per instrument, 1% baseline, conditional maximum 2%, 2% combined. Use scripts/activate_demo.py --status/--stop.' if combined and status.get('policy',{}).get('risk_mode')=='minimum_lot_rescue_v1' else
                         'Combined controller: one position per instrument, 1% each, 2% combined. Use scripts/start_dual_demo.py --status/--stop.' if combined
                         else 'Manual --start required. Individual launchers share one monitor lock and one account position limit.'),
                monitor=status, sources_finalized=bool(plan.get('source_sha256')))


class GoldDemoMonitor(DemoSeriesMonitor):
    mode_description = 'Gold M5/M15 sequential DEMO research; one account position; no retries'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.policy.target_mode != TARGET_MODE:
            raise ValueError('Gold monitor requires its exact policy.')
        self.progress_path = self.folder / 'gold_state.json'

    def _closed_candle(self, now):
        api = self.connector.terminal_api
        rates = api.copy_rates_from_pos(GOLD_SYMBOL, api.TIMEFRAME_M5, 0, 3)
        if rates is None or len(rates) == 0:
            raise ValueError('Gold M5 candles unavailable; no reconnect/retry.')
        times = [int(row['time']) for row in rates]
        if len(set(times)) != len(times) or times != sorted(times) or any(t % 300 or t > now.timestamp() + 5 for t in times):
            raise ValueError('Gold M5 timestamps are invalid, duplicate, unordered or future.')
        closes = [t + 300 for t in times if t + 300 <= now.timestamp()]
        if not closes:
            raise ValueError('No completed Gold M5 candle.')
        return datetime.fromtimestamp(max(closes), timezone.utc).isoformat()


def start(root: Path, *, now=utc_now, connector_factory=GoldConnector,
          authorization_factory=DemoSeriesAuthorization, monitor_factory=GoldDemoMonitor):
    root = root.resolve()
    current = pd.Timestamp(now())
    if current.tz is None:
        raise ValueError('Aware PC clock required.')
    local = current.tz_convert('Europe/London')
    if local.day_of_week >= 5 or not 7 <= local.hour < 17:
        raise ValueError('Gold manual start requires a weekday between 07:00 and 17:00 London; no waiting process started.')
    plan = load_plan(root)
    verify_sources(root, plan)
    policy = StrategyPolicy.load(root / POLICY)
    if policy.target_mode != TARGET_MODE or policy.risk_percent != 1 or policy.slippage_reserve_points_per_side != 50:
        raise ValueError('Gold policy differs from the reviewed 1% version.')
    logs = root / 'logs'
    reservation_path = logs / f'gold_launch_{local.date().isoformat()}.json'
    with monitor_lock(logs / 'gold_launch.lock'):
        if reservation_path.exists():
            raise ValueError('This Gold daily launch is already reserved; inspect its outcome. No retry/reset.')
        if unresolved(root):
            raise ValueError('An earlier execution is unresolved. Use --reconcile read-only; never delete the state.')
        authorization = None
        with connector_factory() as connector:
            try:
                with monitor_lock(logs / 'monitor/monitor.lock'):
                    connector.verify_pinned_account()
                    snapshot = connector.inspect(GOLD_SYMBOL)
                    positions, pending = connector.terminal_api.positions_get(), connector.terminal_api.orders_get()
                    if positions is None or pending is None or positions or pending:
                        raise ValueError('Verified flat account inventory required; Gold cannot stack with EUR/USD.')
                    refreshed = pd.Timestamp(now())
                    if refreshed.tz is None or refreshed.tz_convert('Europe/London').date() != local.date():
                        raise ValueError('Gold startup crossed a date or lost its clock verification.')
                    deadline = min(refreshed + pd.Timedelta(hours=6), local.normalize() + pd.Timedelta(hours=17))
                    if refreshed >= deadline or refreshed < current or (deadline-refreshed).total_seconds() < 3.6:
                        raise ValueError('Gold window ended or PC clock moved backward during preflight.')
                    verify_sources(root, plan)
                    reservation = dict(status='reserved', started_at_utc=refreshed.isoformat(), expires_at_utc=deadline.isoformat(), symbol=GOLD_SYMBOL)
                    _exclusive_json(reservation_path, reservation)
                    authorization = authorization_factory.create(connector, logs, root/POLICY,
                        expires_at=deadline.to_pydatetime(),
                        user_instruction='Manually activated Gold DEMO research: valid M5/M15 setup only; 1% planned risk; adaptive targets up to 4R; one account position; no retries.')
                    if authorization.policy != policy or authorization.data['symbol'] != GOLD_SYMBOL:
                        raise ValueError('Gold runtime authorization differs from the pinned policy/symbol.')
                    reservation.update(status='authorized', authorization_id=authorization.authorization_id)
                    atomic_json(reservation_path, reservation)
                    monitor = monitor_factory(connector, logs, root/POLICY, authorization_id=authorization.authorization_id,
                        config=MonitorConfig((deadline-refreshed).total_seconds()/3600, 10),
                        report_path=root/'reports/latest_gold_monitor_status.md')
                result = monitor.run()
                reservation.update(status='finished', monitor_status=result.get('status'))
                atomic_json(reservation_path, reservation)
                return result
            except BaseException as exc:
                if reservation_path.exists():
                    saved = json.loads(reservation_path.read_text(encoding='utf-8'))
                    atomic_json(reservation_path, dict(saved, status='failed_no_retry', error=str(exc)))
                raise
            finally:
                if authorization is not None:
                    atomic_json(authorization.revoked_path, dict(reason='Gold launcher ended; no reuse or retry.', requested_at_utc=utc_now().isoformat()))


def reconcile(root: Path, *, connector_factory=GoldConnector):
    load_plan(root)
    logs = root / 'logs'
    with monitor_lock(logs/'monitor/monitor.lock'):
        paths = unresolved(root)
        if not paths:
            return dict(status='nothing_to_reconcile', orders_sent=0)
        with connector_factory() as connector:
            connector.verify_pinned_account()
            results = []
            for path in paths:
                state = json.loads(path.read_text(encoding='utf-8'))
                if (state.get('status') != 'open_verified' or not state.get('protection') or state.get('close_result') or
                    state.get('symbol') not in ('EURUSDm', GOLD_SYMBOL) or
                    state.get('account_identity') != dict(login=PINNED_IDENTITY[0], server=PINNED_IDENTITY[1])):
                    raise ValueError('Uncertain/unprotected or unrelated attempt requires investigation; no state reset or retry.')
                executor = DemoExecutor(connector, logs, session_id=None if path.parent == logs else path.parent.name)
                with executor._lock():
                    closed = executor.verify(closing=True)
                if not closed.get('automatic_exit', {}).get('verified_from_actual_history') or not closed.get('close_verified_deals'):
                    raise ValueError('Actual protective exit order/deal proof is missing.')
                results.append(dict(symbol=state['symbol'], position_ticket=closed['position_ticket'], status=closed['status']))
            return dict(status='reconciled', records=results, orders_sent=0)


def stop(root: Path):
    status = read_status(root/'logs')
    if status.get('mode') == 'dual_demo_series':
        return dict(status='combined_controller',message='Use scripts/start_dual_demo.py --stop to stop both instruments; no position is closed.')
    if status.get('policy', {}).get('target_mode') != TARGET_MODE:
        return dict(status='no_gold_monitor_running')
    return request_stop(root/'logs')


def preview(root: Path):
    plan = load_plan(root)
    verify_sources(root, plan)
    with GoldConnector() as connector:
        policy = StrategyPolicy.load(root/POLICY)
        intent, decision = latest_strategy_intent(connector, root/'logs', policy=policy)
        positions, pending = connector.terminal_api.positions_get(), connector.terminal_api.orders_get()
        connector.verify_pinned_account()
        decision.update(account_open_position_count=None if positions is None else len(positions),
                        account_pending_order_count=None if pending is None else len(pending),
                        unresolved_execution_files=[str(p) for p in unresolved(root)])
        if intent is not None:
            session = 'gold-' + datetime.fromisoformat(intent.decision_time_utc).strftime('%Y%m%dT%H%M%SZ')
            decision['preview'] = DemoExecutor(connector, root/'logs', session_id=session,
                strategy_policy=policy, strategy_intent=intent).prepare_entry()
        atomic_json(root/'logs/latest_gold_decision.json', decision)
        quote=decision.get('quote', {})
        lines=['# Latest Gold market snapshot', '', '**Preview only; no order sent.**', '',
               f"Checked UTC: {decision.get('checked_at_utc')}",
               f"Instrument: XAUUSDm broker Gold CFD; M5 entries / M15 context.",
               f"Bid / ask: {quote.get('bid')} / {quote.get('ask')} USD/oz; quote UTC {quote.get('timestamp_utc')}; age {quote.get('age_seconds')} seconds.",
               f"Decision: {decision.get('status')} — {decision.get('reason')}",
               f"Completed decision candle UTC: {decision.get('decision_time_utc')}",
               f"Warnings: {decision.get('warnings', [])}", '',
               f"Account open positions / pending orders: {decision['account_open_position_count']} / {decision['account_pending_order_count']}.",
               f"Unresolved saved executions: {len(decision['unresolved_execution_files'])}; broker exit proof is required before arming.", '',
               '1% maximum planned risk; adaptive target ceiling 4R. No valid affordable setup means no entry.',
               'Routine US clock blackouts are not an economic-event calendar or a macro forecast.',
               'See [Gold strategy and results](market_analysis/gold_intraday_2026-11-15/findings.md).']
        (root/'reports').mkdir(exist_ok=True)
        (root/'reports/latest_gold_market_snapshot.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        return decision
