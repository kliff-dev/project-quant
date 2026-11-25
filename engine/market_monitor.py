"""Bounded preview-only H1 monitor, with hooks for separately authorized modes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator
import uuid

from engine.demo_executor import DemoExecutionDisabledError, DemoExecutor
from engine.execution_audit import atomic_json, atomic_text
from engine.strategy_orders import StrategyPolicy, latest_strategy_intent, risk_budget


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MonitorConfig:
    hours: float = 8
    poll_seconds: float = 15

    def __post_init__(self) -> None:
        for name, minimum, maximum in (('hours', .001, 24), ('poll_seconds', 1, 60)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(f'{name} must be finite and between {minimum} and {maximum}.')


@contextmanager
def monitor_lock(path: Path) -> Iterator[None]:
    """OS-held lock: a crash releases it. Do not unlink a live lock's pathname."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another market monitor holds this log directory. Use --status or --stop.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_status(directory: Path, *, now: datetime | None = None) -> dict:
    path = directory / 'monitor/status.json'
    if not path.exists():
        return {'status': 'not_started', 'mode': 'monitor_only', 'orders_sent': 0}
    report = json.loads(path.read_text(encoding='utf-8'))
    if report['status'] == 'running':
        current = now or utc_now()
        age = (current - datetime.fromisoformat(report['heartbeat_utc'])).total_seconds()
        # Native terminal reads have a timeout; an old file is not proof of life.
        stale = not -5 <= age <= max(90, report['config']['poll_seconds'] * 3)
        ended = current >= datetime.fromisoformat(report['deadline_utc'])
        report['status_observation'] = 'stale_or_overdue' if stale or ended else 'recent_heartbeat'
        report['heartbeat_age_seconds'] = age
    return report


def request_stop(directory: Path) -> dict:
    status = read_status(directory)
    if status['status'] != 'running':
        return {'status': 'not_running', 'monitor_status': status['status']}
    request = {'run_id': status['run_id'], 'requested_at_utc': utc_now().isoformat()}
    atomic_json(directory / 'monitor/stop.json', request)
    if status.get('mode') in ('demo_once', 'demo_series'):
        identifier = status['authorization']['authorization_id']
        if uuid.UUID(identifier).hex != identifier:
            raise ValueError('Invalid authorization identity in monitor status.')
        atomic_json(directory / 'authorizations' / f'{identifier}.revoked.json', request)
    return {'status': 'stop_requested', **request,
            'message': 'Request only; check --status for the final stopped heartbeat. No positions are closed.'}


class MarketMonitor:
    """One account/symbol/policy per run, one evaluation per closed candle.

    Durable candle reservations survive restarts. A crash may skip a candidate,
    but cannot cause the monitor to retry it. This base class never sends an
    order. The separate demo-session subclass requires one-use user authority.
    """

    mode = 'monitor_only'
    mode_description = 'monitor only; no automatic entries or exits'

    def __init__(self, connector: Any, directory: Path, policy_path: Path, *,
                 config: MonitorConfig | None = None, report_path: Path | None = None,
                 now: Callable[[], datetime] = utc_now,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 emit: Callable[[str], None] = print, run_id: str | None = None,
                 deadline_cap: datetime | None = None) -> None:
        self.connector = connector
        self.directory, self.policy_path = Path(directory), Path(policy_path)
        self.folder = self.directory / 'monitor'
        self.progress_path = self.folder / 'state.json'
        self.config = config or MonitorConfig()
        self.report_path = report_path
        if deadline_cap is not None and deadline_cap.utcoffset() is None:
            raise ValueError('Monitor deadline cap must be timezone-aware.')
        self.deadline_cap = deadline_cap
        self.now, self.monotonic, self.sleep, self.emit = now, monotonic, sleep, emit
        self.policy = StrategyPolicy.load(self.policy_path)
        if run_id is not None and uuid.UUID(run_id).hex != run_id:
            raise ValueError('Run ID must be a 32-character lowercase UUID hex string.')
        self.run_id = run_id or uuid.uuid4().hex
        self.identity: tuple | None = None
        self.symbol: str | None = None
        self.progress: dict = {}
        self.status: dict = {}

    def _event(self, event: str, **fields: Any) -> None:
        with (self.folder / 'events.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'timestamp_utc': self.now().isoformat(), 'run_id': self.run_id,
                                     'event': event, 'orders_sent': self.status.get('orders_sent', 0), **fields}, allow_nan=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    def _publish(self) -> None:
        atomic_json(self.folder / 'status.json', self.status)
        if self.report_path is not None:
            decision = self.status.get('latest_decision') or {}
            account = self.status.get('account') or {}
            lines = [
                '# Local market monitor', '',
                f"- Process status: **{self.status['status']}**; PID {self.status['pid']}",
                f'- Mode: **{self.mode_description}**',
                f"- Heartbeat (UTC): {self.status['heartbeat_utc']}",
                f"- Session deadline (UTC): {self.status['deadline_utc']}",
                f"- Account: {account.get('broker', 'unverified')} / {account.get('server', 'unverified')} / {account.get('account_type', 'unverified')}",
                f"- Symbol: {self.status.get('symbol', 'unverified')}; " + ('M5 entries with M15 context' if self.policy.target_mode == 'gold_intraday_v1' else 'H1 entries with H4 context'),
                f'- Target policy: {self.policy.target_mode}',
                f"- Latest decision: **{decision.get('status', 'not checked')}** — {decision.get('reason', '')}",
                f"- Decision candle close (UTC): {decision.get('decision_time_utc', 'none')}",
                f"- Preview state: {self.status.get('preview_status', 'none')}",
                f"- Current risk ceiling: {self.status.get('risk_budget', 'unverified')} USD ({self.policy.risk_percent:g}% maximum; mode {self.policy.risk_mode})",
                f"- Order-send calls recorded: **{self.status.get('orders_sent', 0)}** (null means uncertain)",
                f"- Entry attempts consumed: {self.status.get('entry_attempts', 0)}",
                f"- Stop/error reason: {self.status.get('stop_reason', 'none')}", '',
                'A saved preview is a point-in-time check. The executor rechecks the account,',
                'quote, sizing and signal freshness before submission.',
                'An old heartbeat is not proof that the program is running; use `--status`.',
            ]
            authorization = self.status.get('authorization')
            if authorization:
                if authorization.get('max_entry_attempts') is None:
                    lines += ['', f"Authorization: {authorization['authorization_id']}; sequential entries until the deadline;",
                              f"maximum planned risk: {authorization['risk_percent']}% per trade; at most {authorization.get('max_open_positions',1)} positions for this instrument.",
                              f"Verified entries / closes: {authorization['verified_entries']} / {authorization['closed_trades']}; no execution retries."]
                else:
                    lines += ['', f"Authorization: {authorization['authorization_id']}; maximum entry attempts: 1;",
                              f"maximum planned risk: {authorization['max_risk_usd']} USD; consumed: {authorization['consumed']}."]
            if self.status.get('trade_data_file'):
                lines += ['', f"Completed-trade dataset: {self.status['trade_data_file']}",
                          f"Verified realized net P&L: {self.status.get('realized_net_pnl', 0)} USD."]
            execution = self.status.get('execution_result')
            if execution:
                lines += ['', f"Execution status: **{execution['status']}**.",
                          f"Order / deal / position: {execution.get('order_ticket', 'unverified')} / {execution.get('deal_ticket', 'unverified')} / {execution.get('position_ticket', 'unverified')}",
                          f"Executed lots / price: {execution.get('executed_volume', 'unverified')} / {execution.get('executed_price', 'unverified')}",
                          f"SL / TP: {execution.get('stop_loss', 'unverified')} / {execution.get('take_profit', 'unverified')}",
                          f"Execution error: {execution.get('error', 'none')}"]
            if decision.get('manual_open_command'):
                lines += ['', 'Review the preview file before running this command:', '', '```powershell',
                          decision['manual_open_command'], '```', '',
                          'The command still prompts for `PLACE DEMO TEST ORDER`. Signals expire five minutes after H1 close.']
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(self.report_path, '\n'.join(lines) + '\n')

    def _health(self) -> dict:
        if StrategyPolicy.load(self.policy_path) != self.policy:
            raise ValueError('Trading policy changed during monitoring. Stop and review before restarting.')
        snapshot = self.connector.inspect(self.symbol)
        identity = (*self.connector.account_identity, snapshot['account']['broker'], snapshot['account']['currency'])
        if self.identity is not None and self.identity != identity:
            raise ValueError('Account identity changed during monitoring; session stopped.')
        if self.symbol is not None and snapshot['quote']['symbol'] != self.symbol:
            raise ValueError('Broker symbol changed during monitoring.')
        self.identity, self.symbol = identity, snapshot['quote']['symbol']
        api = self.connector.terminal_api
        funds = api.account_info()
        self.connector.verify_demo_account()
        if funds is None or (funds.login, funds.server) != identity[:2] or self.connector.account_identity != identity[:2]:
            raise ValueError('Cannot verify current account funds and identity.')
        _, _, budget = risk_budget({'balance': funds.balance, 'equity': funds.equity,
                                     'currency': funds.currency}, self.policy)
        positions, orders = api.positions_get(), api.orders_get()
        if positions is None or orders is None:
            raise ValueError('Cannot verify open positions and pending orders.')
        blockers = []
        permissions = snapshot['permissions']
        if permissions['terminal_trade_allowed'] is not True or permissions['external_python_trading_disabled'] is not False:
            blockers.append('Terminal/external Python trading permission is off or unverified.')
        if snapshot['account']['account_trade_allowed'] is not True or snapshot['account']['account_expert_allowed'] is not True:
            blockers.append('Account/expert trading permission is off or unverified.')
        if not -5 <= snapshot['quote']['age_seconds'] <= 120:
            blockers.append('Quote is stale or ahead of the PC clock.')
        if self.policy.target_mode == 'gold_intraday_v1':
            if snapshot['quote']['spread_price'] > 1:
                blockers.append('Gold spread exceeds $1 per ounce.')
        elif snapshot['quote']['spread_pips'] > 2:
            blockers.append('Spread exceeds two pips.')
        from engine.portfolio_guard import get_guard
        portfolio=get_guard(self.connector)
        if portfolio is not None:
            # Each instrument keeps its own position blocker. The portfolio
            # controller verifies all native positions and their saved proof.
            positions = tuple(p for p in positions if p.symbol == self.symbol)
        position_limit = getattr(portfolio,'per_symbol',1) if portfolio is not None else 1
        if len(positions)>=position_limit or orders:
            blockers.append('Existing positions or pending orders block a new isolated demo entry.')
        self.status.update(account=snapshot['account'], symbol=self.symbol, quote=snapshot['quote'],
                           permissions=permissions, risk_budget=float(budget),
                           open_position_count=len(positions), pending_order_count=len(orders),
                           execution_blockers=blockers)
        return snapshot

    def _closed_candle(self, now: datetime) -> str:
        api = self.connector.terminal_api
        rates = api.copy_rates_from_pos(self.symbol, api.TIMEFRAME_H1, 0, 3)
        if rates is None or len(rates) == 0:
            raise ValueError('Latest H1 candles unavailable; monitoring stopped without reconnecting.')
        times = [int(row['time']) for row in rates]
        if len(set(times)) != len(times) or times != sorted(times) or any(t % 3600 for t in times):
            raise ValueError('Invalid, duplicate or unordered H1 timestamps.')
        if any(t > now.timestamp() + 5 for t in times):
            raise ValueError('H1 candle is ahead of the PC clock.')
        closes = [t + 3600 for t in times if t + 3600 <= now.timestamp()]
        if not closes:
            raise ValueError('No completed H1 candle available.')
        return datetime.fromtimestamp(max(closes), timezone.utc).isoformat()

    def _load_progress(self) -> None:
        path = self.progress_path
        fingerprint = hashlib.sha256(json.dumps(self.identity).encode()).hexdigest()
        self.progress = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'version': 1, 'account_fingerprint': fingerprint, 'symbol': self.symbol,
            'last_decision_time_utc': None, 'latest_decision': {}, 'evaluations': 0}
        if self.progress.get('version') != 1 or self.progress.get('account_fingerprint') != fingerprint or self.progress.get('symbol') != self.symbol:
            raise ValueError('Monitor checkpoint belongs to another account/symbol or is invalid; review local state.')
        if type(self.progress.get('evaluations')) is not int or self.progress['evaluations'] < 0:
            raise ValueError('Invalid monitor evaluation counter.')
        previous = self.progress['last_decision_time_utc']
        if previous is not None and datetime.fromisoformat(previous).utcoffset() is None:
            raise ValueError('Invalid monitor checkpoint timestamp.')
        self.status['latest_decision'] = self.progress['latest_decision']

    def _evaluate(self, candle: str) -> None:
        previous = self.progress['last_decision_time_utc']
        if previous is not None:
            if candle == previous:
                return
            if datetime.fromisoformat(candle) < datetime.fromisoformat(previous):
                raise ValueError('Broker history or PC clock moved behind the monitor checkpoint.')
        # Publish the reservation BEFORE any expensive calculation or broker check.
        decision = {'status': 'SKIPPED', 'reason': 'Evaluation reserved; an interrupted evaluation is never retried.',
                    'decision_time_utc': candle, 'orders_sent': 0}
        self.progress.update(last_decision_time_utc=candle, latest_decision=decision,
                             evaluations=self.progress['evaluations'] + 1)
        atomic_json(self.progress_path, self.progress)
        if self.policy.target_mode in ('adaptive_pullback_v1', 'adaptive_pullback_v2', 'gold_intraday_v1'):
            intent, decision = latest_strategy_intent(self.connector, self.directory, policy=self.policy)
        else:
            intent, decision = latest_strategy_intent(self.connector, self.directory)
        if decision['decision_time_utc'] != candle:
            raise ValueError('History changed across the H1 probe and strategy calculation; discard candidate.')
        self._health()  # Bind the history result to the same account/policy before preview.
        decision['ready'] = False
        if intent is not None:
            if self.status['execution_blockers']:
                decision.update(status='BLOCKED', reason='; '.join(self.status['execution_blockers']))
            else:
                stamp = datetime.fromisoformat(candle).strftime('%Y%m%dT%H%M%SZ')
                session = f'mon-{stamp}-{intent.context_sha256[:8]}'
                executor = DemoExecutor(self.connector, self.directory, session_id=session,
                                        strategy_policy=self.policy, strategy_intent=intent)
                try:
                    preview = executor.prepare_entry()  # order_check only; never open_test.
                except (ValueError, DemoExecutionDisabledError) as exc:
                    decision.update(status='BLOCKED', reason=str(exc))
                else:
                    self._health()
                    if preview['account_identity'] != dict(login=self.identity[0], server=self.identity[1]):
                        raise ValueError('Preview belongs to a different account; discard it.')
                    expires = min(datetime.fromisoformat(candle) + timedelta(seconds=60 if self.policy.target_mode == 'gold_intraday_v1' else 300),
                                  datetime.fromisoformat(preview['created_at_utc']) + timedelta(seconds=300))
                    decision.update(status='READY_FOR_CONFIRMATION' if preview['ready'] else 'BLOCKED',
                                    reason='; '.join(preview['blockers']) or 'Review the saved preview; explicit confirmation is required.',
                                    ready=preview['ready'], preview_id=preview['preview_id'],
                                    preview_file=str(executor.preview_path), session_id=session,
                                    expires_at_utc=expires.isoformat(),
                                    volume_lots=preview['volume_lots'], protection=preview['protection'])
                    if preview['ready'] and self.policy.target_mode == 'fixed_2r':
                        decision['manual_open_command'] = (
                            f'.\\.venv\\Scripts\\python.exe -B scripts/test_demo_trade.py --strategy --open '
                            f'--session {session} --preview-id {preview["preview_id"]} '
                            f'--log-dir "{self.directory.resolve()}" --terminal "{self.connector.terminal_path}"')
        self.progress['latest_decision'] = decision
        atomic_json(self.progress_path, self.progress)
        atomic_json(self.directory / 'latest_strategy_decision.json', decision)
        self.status['latest_decision'] = decision
        self._event('candle_evaluated', decision=decision)
        self.emit(f"{candle} {decision['status']}: {decision.get('reason', '')}")
        self._handle_decision(decision)

    def _handle_decision(self, decision: dict) -> None:
        """Preview-only mode does nothing further. Authorized subclass may act once."""

    def _poll_position(self) -> None:
        """Default mode does not manage positions; a sequential mode can inspect."""

    def _preview_status(self, now: datetime) -> str:
        decision = self.status.get('latest_decision') or {}
        if decision.get('status') != 'READY_FOR_CONFIRMATION':
            return 'none'
        if now >= datetime.fromisoformat(decision['expires_at_utc']):
            return 'expired; no automatic refresh or submission'
        if self.status.get('execution_blockers'):
            return 'currently blocked; execution requires fresh checks'
        return 'awaiting explicit confirmation; no automatic submission'

    def run(self, *, once: bool = False) -> dict:
        with monitor_lock(self.folder / 'monitor.lock'):
            start, start_tick = self.now(), self.monotonic()
            deadline = start + timedelta(hours=self.config.hours)
            if self.deadline_cap is not None:
                deadline = min(deadline, self.deadline_cap)
            self.status = dict(status='running', mode=self.mode, run_id=self.run_id, pid=os.getpid(),
                               started_at_utc=start.isoformat(), deadline_utc=deadline.isoformat(),
                               heartbeat_utc=start.isoformat(), config=asdict(self.config),
                               policy=asdict(self.policy), poll_count=0, orders_sent=0)
            try:
                self._health()
                self._load_progress()
                self._event('started', deadline_utc=deadline.isoformat(), policy=asdict(self.policy))
                self.emit(f'{self.mode_description}: PID {os.getpid()}, deadline {deadline.isoformat()}.')
                while True:
                    now = self.now()
                    if now < datetime.fromisoformat(self.status['heartbeat_utc']) - timedelta(seconds=5):
                        raise ValueError('PC clock moved backwards; monitoring stopped.')
                    if now >= deadline or self.monotonic() - start_tick >= self.config.hours * 3600:
                        self.status['stop_reason'] = 'Session deadline reached.'
                        break
                    stop_path = self.folder / 'stop.json'
                    if stop_path.exists() and json.loads(stop_path.read_text(encoding='utf-8')).get('run_id') == self.run_id:
                        self.status['stop_reason'] = 'User requested stop.'
                        break
                    self._health()
                    self._poll_position()
                    self._evaluate(self._closed_candle(now))
                    self.status.update(heartbeat_utc=self.now().isoformat(), poll_count=self.status['poll_count'] + 1,
                                       total_candles_evaluated=self.progress['evaluations'],
                                       preview_status=self._preview_status(self.now()))
                    self._publish()
                    if self.status.get('finish_requested'):
                        break
                    if once:
                        self.status['stop_reason'] = 'Single inspection completed.'
                        break
                    remaining = min((deadline - self.now()).total_seconds(),
                                    self.config.hours * 3600 - (self.monotonic() - start_tick))
                    if remaining > 0:
                        self.sleep(min(self.config.poll_seconds, remaining))
                self.status['status'] = 'stopped'
            except KeyboardInterrupt:
                self.status.update(status='stopped', stop_reason='Interrupted by user; no positions closed.')
            except Exception as exc:
                self.status.update(status='failed', stop_reason=f'{type(exc).__name__}: {exc}')
            self.status.update(heartbeat_utc=self.now().isoformat(), preview_status=self._preview_status(self.now()))
            self._event(self.status['status'], reason=self.status['stop_reason'])
            self._publish()
            self.emit(f"Monitor {self.status['status']}: {self.status['stop_reason']}")
            return self.status
