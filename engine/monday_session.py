"""Manually launched, date-bounded DEMO pilot; importing/status never contacts MT5.

The approved plan is an audit record, not a scheduled task. A persistent launch
reservation deliberately survives failures so restarting cannot retry execution.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from engine.demo_authorization import _exclusive_json
from engine.demo_executor import DemoExecutor
from engine.demo_series import DemoSeriesAuthorization, DemoSeriesMonitor
from engine.execution_audit import atomic_json
from engine.market_monitor import MonitorConfig, monitor_lock, read_status, request_stop
from engine.mt5_connector import MT5Connector
from engine.strategy_orders import StrategyPolicy


PLAN_FILE = Path('config/monday_demo_2026-11-16.json')
POLICY_FILE = Path('config/monday_adaptive_policy.json')
OPEN = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 16, 14, tzinfo=timezone.utc)
IDENTITY = (464192812, 'Exness-MT5Trial17', 'Exness Technologies Ltd', 'USD')
SYMBOL = 'EURUSDm'
TARGET_MODE = 'adaptive_pullback_v2'
INSTRUCTION = 'Lets use this strategy on monday.'
TARGET_UPDATE = 'Up to 4R initially (recommended)'
RESERVATION = Path('logs/monday_2026-11-16.launch.json')


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('A timezone-aware PC clock is required.')
    return now.astimezone(timezone.utc)


def load_plan(root: Path) -> dict:
    data = json.loads((root / PLAN_FILE).read_text(encoding='utf-8'))
    expected = dict(version=1, session_date='2026-11-16', starts_at_utc=OPEN.isoformat(),
                    expires_at_utc=CLOSE.isoformat(), symbol=SYMBOL, target_mode=TARGET_MODE,
                    policy_file=POLICY_FILE.as_posix(), risk_percent=1.0, max_lots=.1,
                    max_open_positions=1, demo_only=True, scheduled=False,
                    user_instruction=INSTRUCTION, instruction_received_date='2026-11-14',
                    maximum_reward_r=4, target_update_user_choice=TARGET_UPDATE,
                    target_update_received_date='2026-11-15')
    if any(data.get(key) != value or type(data.get(key)) is not type(value)
           for key, value in expected.items()):
        raise ValueError('The Monday plan differs from the approved date, scope or safety rules.')
    if data.get('account_identity') != dict(login=IDENTITY[0], server=IDENTITY[1],
                                             broker=IDENTITY[2], currency=IDENTITY[3]):
        raise ValueError('The Monday plan account identity differs from the approved DEMO account.')
    return data


def _verify_sources(root: Path, plan: dict) -> None:
    hashes = plan.get('source_sha256')
    if not isinstance(hashes, dict) or not hashes or POLICY_FILE.as_posix() not in hashes:
        raise ValueError('Monday source/policy fingerprints have not been finalized; start is disabled.')
    for relative, digest in hashes.items():
        path = (root / relative).resolve()
        if (not path.is_relative_to(root.resolve()) or not isinstance(digest, str) or
                len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid Monday source fingerprint.')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f'Approved Monday source changed: {relative}. Review before starting.')


def describe_plan(root: Path, *, now: datetime | None = None) -> dict:
    """Only local files are read. A plan is never proof that a process is active."""
    plan = load_plan(root)
    current = _aware(now or utc_now())
    reservation = root / RESERVATION
    launch = json.loads(reservation.read_text(encoding='utf-8')) if reservation.exists() else None
    try:
        _unresolved(root)
        blocker = None
    except ValueError as exc:
        blocker = str(exc)
    monitor=read_status(root / 'logs', now=current)
    combined=monitor.get('mode')=='dual_demo_series'
    live_combined=combined and monitor.get('status')=='running' and monitor.get('status_observation')=='recent_heartbeat'
    return dict(status='running_in_combined_controller' if live_combined else 'not_started' if launch is None else launch.get('status', 'reserved'),
                scheduled=False, background_process_started=live_combined,
                message=('Combined controller active now; user-activated timing, no fixed hour filter.' if monitor.get('entry_timing')=='user_activation_v1' else
                         'Combined controller active; EUR/USD retains its 16:00-22:00 Manila entry window.') if live_combined else 'Manual start only; this plan does not schedule or activate future trading.',
                checked_at_utc=current.isoformat(),
                start_allowed_now=OPEN <= current < CLOSE and launch is None,
                local_session='Monday 16 November 2026, 16:00-22:00 Asia/Manila',
                entry_window_london='08:00-14:00 Europe/London',
                account=dict(broker=IDENTITY[2], server=IDENTITY[1], currency=IDENTITY[3],
                             account_id_masked='****2812', account_type_required='DEMO'),
                symbol=SYMBOL, risk_percent=monitor.get('policy',{}).get('risk_percent',plan['risk_percent']) if live_combined else plan['risk_percent'], max_lots=plan['max_lots'],
                max_open_positions=1, target_mode=TARGET_MODE,
                maximum_reward_r=4,
                target_rules='Predefined 1R/2R/3R/4R selection at entry; 4R requires room, strong trend and normal volatility; no valid setup means no trade.',
                earlier_execution_blocker=blocker,
                reconciliation_command=r'.\.venv\Scripts\python.exe -B scripts\start_monday_demo.py --reconcile',
                stops='Stop request/session deadline stops new entries; existing positions retain broker SL/TP.',
                launch=launch, monitor=monitor)


def _unresolved(root: Path) -> None:
    logs = root / 'logs'
    for path in [logs / 'demo_test_state.json', *sorted((logs / 'sessions').glob('*/demo_test_state.json'))]:
        if path.is_file() and json.loads(path.read_text(encoding='utf-8')).get('status') != 'closed_verified':
            raise ValueError(r'An earlier execution is unresolved. Run .\.venv\Scripts\python.exe -B scripts\start_monday_demo.py --reconcile to verify a prior protected exit read-only. An actual open/uncertain position remains blocked; no new authorization.')


def _verify_account(connector: Any, *, require_flat: bool = True) -> dict:
    connector.verify_demo_account()
    api = connector.terminal_api
    account = api.account_info()
    if (account is None or type(getattr(account, 'trade_mode', None)) is not int or
            account.trade_mode != 0 or type(getattr(api, 'ACCOUNT_TRADE_MODE_DEMO', None)) is not int or
            api.ACCOUNT_TRADE_MODE_DEMO != 0):
        raise ValueError('Fresh native account_info() must verify DEMO; REAL, CONTEST and unknown are rejected.')
    identity = (getattr(account, 'login', None), getattr(account, 'server', None),
                getattr(account, 'company', None), getattr(account, 'currency', None))
    if type(identity[0]) is not int or identity != IDENTITY or connector.account_identity != IDENTITY[:2]:
        raise ValueError('Connected account is not the exact approved Monday DEMO account.')
    snapshot = connector.inspect(SYMBOL)
    if (snapshot.get('quote', {}).get('symbol') != SYMBOL or
            any(snapshot.get('account', {}).get(key) != value for key, value in
                (('broker', IDENTITY[2]), ('server', IDENTITY[1]), ('currency', IDENTITY[3])))):
        raise ValueError('Cannot verify the approved broker and exact EUR/USD symbol.')
    # Inspection may block or the user may switch accounts while it is running.
    connector.verify_demo_account()
    after = api.account_info()
    if (after is None or type(getattr(after, 'trade_mode', None)) is not int or after.trade_mode != 0 or
            (getattr(after, 'login', None), getattr(after, 'server', None),
             getattr(after, 'company', None), getattr(after, 'currency', None)) != IDENTITY or
            connector.account_identity != IDENTITY[:2]):
        raise ValueError('Account changed during Monday verification.')
    positions, orders = api.positions_get(), api.orders_get()
    if positions is None or orders is None or require_flat and (positions or orders):
        raise ValueError('Monday requires verified empty positions and pending orders across this account.')
    return snapshot


def start_session(root: Path, *, now: Callable[[], datetime] = utc_now,
                  connector_factory: Callable[[], Any] = MT5Connector,
                  authorization_factory: Any = DemoSeriesAuthorization,
                  monitor_factory: Any = DemoSeriesMonitor,
                  policy_loader: Callable[[Path], Any] | None = None) -> dict:
    """Explicit --start only: no waiting, scheduling, hidden process or retries.

    Native terminal access occurs only after date, policy, local-state and source
    checks. Injectable factories allow every guard to be exercised offline.
    """
    root = Path(root).resolve()
    current = _aware(now())
    if not OPEN <= current < CLOSE:
        raise ValueError('Manual start is allowed only Monday 16 November 2026, 16:00-22:00 Asia/Manila. No waiting process was started.')
    plan = load_plan(root)
    _verify_sources(root, plan)
    policy_path = root / POLICY_FILE
    policy = (policy_loader or StrategyPolicy.load)(policy_path)
    if (getattr(policy, 'target_mode', None) != TARGET_MODE or
            type(policy.risk_percent) not in (float, int) or policy.risk_percent != 1 or
            type(policy.max_lots) not in (float, int) or policy.max_lots != .1):
        raise ValueError('Monday requires the separately pinned adaptive policy, 1% risk and 0.1-lot ceiling.')
    logs = root / 'logs'
    reservation_path = root / RESERVATION
    with monitor_lock(logs / 'monday_2026-11-16.launch.lock'):
        if reservation_path.exists():
            raise ValueError('Monday launch is already reserved/consumed. No automatic retry or reset.')
        _unresolved(root)
        # Do not hold the monitor kernel lock when monitor.run() obtains it.
        authorization = None
        reservation = None
        with ExitStack() as connection:
            try:
                with monitor_lock(logs / 'monitor/monitor.lock'):
                    connector = connection.enter_context(connector_factory())
                    _verify_account(connector)
                    current = _aware(now())
                    remaining = (CLOSE - current).total_seconds() / 3600
                    if not OPEN <= current < CLOSE or remaining < .001:
                        raise ValueError('Monday window ended during preflight; no session authorization created.')
                    _unresolved(root)
                    _verify_sources(root, plan)
                    reservation = dict(version=1, status='reserved', reserved_at_utc=current.isoformat(),
                        deadline_utc=CLOSE.isoformat(), instruction_received_date=plan['instruction_received_date'],
                        user_instruction=plan['user_instruction'], automatic_retry_allowed=False)
                    _exclusive_json(reservation_path, reservation)
                    authorization = authorization_factory.create(connector, logs, policy_path, expires_at=CLOSE,
                        user_instruction=f"{INSTRUCTION} (instruction received 2026-11-14; target update received 2026-11-15: {TARGET_UPDATE}; bounded DEMO pilot manually activated 2026-11-16; one position, no retries.)")
                    bound = authorization.data
                    if (bound.get('account_identity') != dict(login=IDENTITY[0], server=IDENTITY[1]) or
                            bound.get('broker') != IDENTITY[2] or bound.get('symbol') != SYMBOL or
                            authorization.policy != policy):
                        raise ValueError('New authorization does not match the pinned Monday account, symbol or policy.')
                    reservation.update(status='authorized', authorization_id=authorization.authorization_id)
                    atomic_json(reservation_path, reservation)
                    monitor = monitor_factory(connector, logs, policy_path,
                        authorization_id=authorization.authorization_id,
                        config=MonitorConfig(hours=remaining, poll_seconds=15),
                        report_path=root / 'reports/latest_monitor_status.md')
                # run() now obtains its own kernel lock; IPC remains connected.
                result = monitor.run()
                reservation.update(status='finished', finished_at_utc=_aware(now()).isoformat(),
                                   monitor_status=result.get('status'))
                atomic_json(reservation_path, reservation)
                return result
            except BaseException as exc:
                if reservation is not None:
                    reservation.update(status='failed_no_retry', error=str(exc))
                    atomic_json(reservation_path, reservation)
                raise
            finally:
                if authorization is not None:
                    atomic_json(authorization.revoked_path,
                                dict(reason='Monday launcher ended; no retry or reuse.',
                                     requested_at_utc=_aware(now()).isoformat()))


def stop_session(root: Path) -> dict:
    """Revoke/request stopping the existing monitor; never close a position."""
    if read_status(Path(root)/'logs').get('mode')=='dual_demo_series':
        from engine.dual_demo import stop
        return stop(Path(root))
    return request_stop(Path(root) / 'logs')


def reconcile_prior_execution(root: Path, *, connector_factory: Callable[[], Any] = MT5Connector,
                              executor_factory: Any = DemoExecutor) -> dict:
    """Read actual SL/TP exit history; never send, close, reset or retry an order.

    Unlike --start, this explicit diagnostic is usable before Monday. Only a
    previously verified protected entry may advance to verified closed, and the
    existing executor must establish filled exit orders and actual broker deals.
    """
    root = Path(root).resolve()
    load_plan(root)
    logs = root / 'logs'
    with monitor_lock(logs / 'monday_2026-11-16.launch.lock'):
        with monitor_lock(logs / 'monitor/monitor.lock'):
            candidates = []
            for path in [logs / 'demo_test_state.json', *sorted((logs / 'sessions').glob('*/demo_test_state.json'))]:
                if not path.is_file():
                    continue
                state = json.loads(path.read_text(encoding='utf-8'))
                if state.get('status') == 'closed_verified':
                    continue
                if (state.get('status') != 'open_verified' or not state.get('protection') or
                        state.get('close_result') or state.get('symbol') != SYMBOL or
                        state.get('account_identity') != dict(login=IDENTITY[0], server=IDENTITY[1]) or
                        state.get('account', {}).get('broker') != IDENTITY[2]):
                    raise ValueError('Read-only reconciliation only accepts a verified protected entry on the pinned DEMO account; uncertain/unprotected attempts require manual investigation, never a retry.')
                candidates.append((path, None if path.parent == logs else path.parent.name))
            if not candidates:
                return dict(status='nothing_to_reconcile', read_only=True, orders_sent=0,
                            message='No unresolved local execution state; no MT5 connection was made.')
            results = []
            with connector_factory() as connector:
                _verify_account(connector, require_flat=False)
                for path, session_id in candidates:
                    _verify_account(connector, require_flat=False)
                    executor = executor_factory(connector, logs, session_id=session_id)
                    if executor.state_path.resolve() != path.resolve():
                        raise ValueError('Reconciliation state path does not match the original execution record.')
                    with executor._lock():
                        verified = executor.verify(closing=True)
                    if (verified.get('status') != 'closed_verified' or
                            not verified.get('automatic_exit', {}).get('verified_from_actual_history') or
                            not verified.get('close_verified_deals')):
                        raise ValueError('Actual filled SL/TP exit/deal evidence is missing; Monday remains blocked.')
                    results.append(dict(state_file=str(path), position_ticket=verified['position_ticket'],
                                        status='closed_verified', realized_net_pnl=verified['realized_net_pnl']))
            _unresolved(root)
            return dict(status='reconciled', read_only=True, orders_sent=0, records=results,
                        message='Prior protected exits verified from actual broker history. No order was sent; Monday still requires manual --start.')
