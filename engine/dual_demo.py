"""One manually activated, date-bounded controller for Gold and EUR/USD DEMO.

Reuses the existing native planners/executor and individual series ledgers. No
entry before its window, frozen per-instrument limits, verified exits, no retries.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from engine.demo_authorization import _exclusive_json
from engine.demo_executor import DemoExecutor
from engine.demo_series import DemoSeriesAuthorization, DemoSeriesMonitor
from engine.execution_audit import atomic_json, atomic_text
from engine.gold_connector import GoldConnector
from engine.gold_demo import GoldDemoMonitor, load_plan as gold_plan, verify_sources as verify_gold, unresolved
from engine.market_monitor import MonitorConfig, monitor_lock, read_status
from engine.monday_session import load_plan as eur_plan, _verify_sources as verify_eur
from engine.mt5_connector import MT5Connector
from engine.portfolio_guard import TwoInstrumentGuard, SCOPE, MULTI_SCOPE, LIMITS

PLAN = Path('config/dual_demo_2026-11-16.json')
RESERVATION = Path('logs/dual_demo_2026-11-16.launch.json')
MODE = 'dual_demo_series'
WINDOWS = {
    'XAUUSDm': (datetime(2026, 11, 16, 7, tzinfo=timezone.utc), datetime(2026, 11, 16, 13, tzinfo=timezone.utc)),
    'EURUSDm': (datetime(2026, 11, 16, 8, tzinfo=timezone.utc), datetime(2026, 11, 16, 14, tzinfo=timezone.utc)),
}
POLICIES = {'XAUUSDm': Path('config/gold_demo_policy.json'), 'EURUSDm': Path('config/monday_adaptive_policy.json')}
ORDER = ('EURUSDm', 'XAUUSDm')
DEADLINE = WINDOWS['EURUSDm'][1]
INSTRUCTION = 'Can you activate the trade on both gold and eurusd?'
POSITION_UPDATE = 'you can have 2 position open but in gold and eurusd 1 for each. With the same risk management'
MULTI_POSITION_UPDATE = 'Lets do this. the maximum is 2 active trades for eur and 2 active trade for gold? is that fine?'
MULTI_RISK_UPDATE = 'what we are going to do is have a total of 8 percent risk limit if we have valid setups'


def utc_now():
    return datetime.now(timezone.utc)


def refresh_journal(root):
    """Generate from saved broker-verified execution states; no second MT5 IPC."""
    from analytics.trade_journal import build_journal,write_journal
    with monitor_lock(root/'logs/trade_journal/update.lock'):
        write_journal(root,build_journal(root/'logs'))


def verify_sources(root):
    plan = json.loads((root / PLAN).read_text(encoding='utf-8'))
    expected = dict(version=1, demo_only=True, session_date='2026-11-16', risk_percent=1,
                    max_open_positions=2, maximum_positions_per_symbol=1, maximum_combined_risk_percent=2,
                    maximum_reward_r=4, user_instruction=INSTRUCTION,
                    position_update_user_instruction=POSITION_UPDATE,
                    windows={s: [a.isoformat(), b.isoformat()] for s, (a, b) in WINDOWS.items()})
    if any(plan.get(k) != v or type(plan.get(k)) is not type(v) for k, v in expected.items()):
        raise ValueError('Combined DEMO plan differs from the approved date, instruments or risk scope.')
    required = {'engine/dual_demo.py', 'engine/portfolio_guard.py', 'scripts/start_dual_demo.py',
                'config/gold_demo_plan.json', 'config/monday_demo_2026-11-16.json'}
    hashes = plan.get('source_sha256', {})
    if not required.issubset(hashes):
        raise ValueError('Combined source fingerprints are not finalized.')
    for relative, expected_sha in hashes.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
            raise ValueError(f'Combined approved source changed: {relative}')
    verify_gold(root, gold_plan(root))
    verify_eur(root, eur_plan(root))
    return plan


class InstrumentView:
    """Two exact-symbol views of one native IPC connection; no extra login."""
    def __init__(self, connector, symbol):
        self.base, self.symbol = connector, symbol

    def inspect(self, symbol=None):
        if symbol not in (None, self.symbol):
            raise ValueError('Combined instrument view cannot substitute another symbol.')
        self.base.verify_pinned_account()
        result = (GoldConnector.inspect(self.base, self.symbol) if self.symbol == 'XAUUSDm'
                  else MT5Connector.inspect(self.base, self.symbol))
        self.base.verify_pinned_account()
        return result

    def verify_demo_account(self):
        return self.base.verify_pinned_account()

    @property
    def account_identity(self):
        return self.base.account_identity

    @property
    def terminal_api(self):
        return self.base.terminal_api

    @property
    def terminal_path(self):
        return self.base.terminal_path


class EntryGate:
    def _handle_decision(self, decision):
        if decision.get('status') == 'READY_FOR_CONFIRMATION':
            reason = self.coordinator.entry_blocker(self.symbol, decision['decision_time_utc'])
            if reason:
                decision.update(status='BLOCKED', ready=False, reason=reason)
                self._save_decision(decision)
                return
        super()._handle_decision(decision)


class CoordinatedGold(EntryGate, GoldDemoMonitor):
    pass


class CoordinatedEUR(EntryGate, DemoSeriesMonitor):
    pass


class DualController:
    def __init__(self, root, connector, *, now=utc_now, sleep=time.sleep, monotonic=time.monotonic,
                 authorization_factory=DemoSeriesAuthorization, monitor_factories=None,journal_refresh=refresh_journal,
                 windows=None, policies=None, source_verifier=None, activation_id=None, instruction=INSTRUCTION,
                 maximum_risk_percent=1, risk_mode='fixed', observer=None, portfolio_scope=SCOPE):
        self.root, self.connector = Path(root).resolve(), connector
        self.logs = self.root / 'logs'
        self.windows = WINDOWS if windows is None else windows
        self.policies = POLICIES if policies is None else policies
        self.deadline = max(end for _, end in self.windows.values())
        self.verify_sources = source_verifier or verify_sources
        self.activation_id, self.instruction = activation_id, instruction
        if ((maximum_risk_percent,risk_mode) not in ((1,'fixed'),(2,'minimum_lot_rescue_v1')) or
            maximum_risk_percent==2 and not activation_id):
            raise ValueError('Combined risk override requires an explicit conditional activation.')
        self.maximum_risk_percent, self.risk_mode = maximum_risk_percent, risk_mode
        if portfolio_scope not in LIMITS or portfolio_scope==MULTI_SCOPE and (not activation_id or maximum_risk_percent!=2 or risk_mode!='minimum_lot_rescue_v1'):
            raise ValueError('Expanded positions require explicit bounded DEMO activation and the existing per-trade risk policy.')
        self.portfolio_scope=portfolio_scope
        self.per_symbol_limit,self.total_position_limit,self.combined_risk_limit=LIMITS[portfolio_scope]
        self.observer = observer
        self.folder = self.logs / ('activations/' + activation_id if activation_id else 'dual_demo_2026-11-16')
        self.folder.mkdir(parents=True, exist_ok=True)
        self.now, self.sleep, self.monotonic = now, sleep, monotonic
        self.authorization_factory = authorization_factory
        self.journal_refresh,self.journal_marker=journal_refresh,None
        self.factories = monitor_factories or {'XAUUSDm': CoordinatedGold, 'EURUSDm': CoordinatedEUR}
        self.children, self.authorizations = {}, {}
        self.portfolio_guard = (TwoInstrumentGuard(self.logs) if portfolio_scope==SCOPE else TwoInstrumentGuard(self.logs,scope=portfolio_scope))
        self.views = {s: InstrumentView(connector, s) for s in ORDER}
        for view in self.views.values():view.portfolio_guard=self.portfolio_guard
        self.run_id = uuid.uuid4().hex
        current = self.now()
        self.status = dict(status='running', phase='monitoring_setups' if activation_id else 'waiting_for_session', mode=MODE,
            policy={'target_mode':'gold_and_eurusd', 'risk_percent':maximum_risk_percent,
                    'baseline_risk_percent':1, 'risk_mode':risk_mode},
            activation_id=activation_id, entry_timing='user_activation_v1' if activation_id else 'clock_session',
            control_script='scripts/activate_demo.py' if activation_id else 'scripts/start_dual_demo.py',
            run_id=self.run_id, pid=os.getpid(), started_at_utc=current.isoformat(),
            deadline_utc=self.deadline.isoformat(), heartbeat_utc=current.isoformat(),
            config={'poll_seconds':10}, poll_count=0, orders_sent=0,
            maximum_open_positions_account_wide=self.total_position_limit, maximum_positions_per_symbol=self.per_symbol_limit,
            maximum_combined_risk_percent=self.combined_risk_limit, portfolio_scope=portfolio_scope, authorizations={}, instruments={},
            stop_reason=None)
        self.last_clock, self.started_tick = current, self.monotonic()
        self.maximum_seconds = (self.deadline-current).total_seconds()

    def inventory(self):
        account = self.connector.verify_pinned_account()
        api = self.connector.terminal_api
        positions, orders = api.positions_get(), api.orders_get()
        if positions is None or orders is None:
            raise ValueError('Cannot verify account-wide positions and pending orders.')
        names=[getattr(p,'symbol',None) for p in positions]
        if self.portfolio_scope==MULTI_SCOPE:
            native=api.account_info()
            if native is None or type(getattr(native,'margin_mode',None)) is not int or native.margin_mode!=2:
                raise ValueError('Expanded position scope requires a verified hedging DEMO account.')
        if orders or len(positions)>self.total_position_limit or any(names.count(s)>self.per_symbol_limit for s in ORDER) or any(s not in ORDER for s in names):
            raise ValueError('Unexpected pending/multiple positions; all new entries stopped.')
        self.status.update(account=account, open_position_count=len(positions), pending_order_count=len(orders))
        return positions

    def owned_records(self):
        rows = [(symbol, row) for symbol, auth in self.authorizations.items() for row in auth.records()]
        if any(row['status'] not in ('entry_verified', 'closed_verified') for _, row in rows):
            raise ValueError('Reserved/failed/uncertain entry; stop both instruments without retry.')
        if any(sum(s==symbol and row['status']=='entry_verified' for s,row in rows)>self.per_symbol_limit for symbol in ORDER):
            raise ValueError('Duplicate active strategy records for an instrument; no additional send.')
        return rows

    def check_owned_states(self):
        active_paths = {(self.logs/'sessions'/row['session_id']/'demo_test_state.json').resolve()
                        for _, row in self.owned_records() if row['status']=='entry_verified'}
        if any(path.resolve() not in active_paths for path in unresolved(self.root)):
            raise ValueError('An unrelated/unverified execution appeared; no new entries.')

    def entry_blocker(self, symbol, decision_time):
        self.verify_sources(self.root)
        stop_path = self.logs / 'monitor/stop.json'
        if stop_path.exists() and json.loads(stop_path.read_text(encoding='utf-8')).get('run_id') == self.run_id:
            return 'User requested stopping both instruments.'
        current = self.now()
        begins, ends = self.windows[symbol]
        if not begins <= current < ends:
            return 'Outside this instrument\'s approved entry window.'
        decision = datetime.fromisoformat(decision_time)
        freshness = 60 if symbol == 'XAUUSDm' else 300
        earliest = begins.timestamp() - freshness if self.activation_id else begins.timestamp()
        if (decision.utcoffset() is None or not earliest <= decision.timestamp() < ends.timestamp()
                or self.activation_id and not 0 <= (current-decision).total_seconds() <= freshness):
            return 'Signal precedes this session or is outside its window.'
        rows = self.owned_records()
        if sum(s==symbol and row['status']=='entry_verified' for s,row in rows)>=self.per_symbol_limit:
            return 'This instrument already owns its permitted position.'
        if self.portfolio_scope==MULTI_SCOPE:
            latest_entry=max((datetime.fromisoformat(row['decision_time_utc']).timestamp()*1000 for s,row in rows if s==symbol),default=0)
            if decision.timestamp()*1000<=latest_entry:
                return 'Require a fresh later candle after the prior entry; never duplicate one setup.'
        latest_exit = max((row.get('exit_time_msc', 0) for s,row in rows if s==symbol), default=0)
        if decision.timestamp()*1000 <= latest_exit:
            return 'Require a fresh candle after this instrument\'s prior actual exit.'
        self.check_owned_states()
        positions=self.inventory()
        if sum(p.symbol==symbol for p in positions)>=self.per_symbol_limit:
            return 'This instrument already has its permitted position.'
        self.portfolio_guard.inventory(self.views[symbol],positions,(),symbol=symbol,opening=True)
        return None

    def arm(self, symbol):
        current = self.now()
        begins, ends = self.windows[symbol]
        if not begins <= current < ends or symbol in self.children:
            raise ValueError('Instrument cannot be armed before its window or more than once.')
        if sum(p.symbol==symbol for p in self.inventory())>=self.per_symbol_limit:
            return
        self.check_owned_states()
        self.verify_sources(self.root)
        auth = self.authorization_factory.create(self.views[symbol], self.logs, self.root/self.policies[symbol],
            expires_at=ends, user_instruction=f'{self.instruction} Maximum {self.per_symbol_limit} positions per market and {self.total_position_limit} across Gold/EURUSD; {symbol}; authority {begins.isoformat()} to {ends.isoformat()}; timing {self.status["entry_timing"]}; DEMO only, baseline 1%, maximum {self.maximum_risk_percent}% planned risk per trade, mode {self.risk_mode}, {self.combined_risk_limit}% combined ceiling, up to 4R, no retries.')
        # Retain authority immediately so any following failure revokes it.
        self.authorizations[symbol] = auth
        self.portfolio_guard.register(symbol,auth)
        self.status['authorizations'] = {s:a.summary() for s,a in self.authorizations.items()}
        self.publish()  # Expose the new ID to --stop before any possible entry.
        if (auth.data['symbol'] != symbol or auth.policy.risk_percent != self.maximum_risk_percent or
            auth.policy.risk_mode != self.risk_mode or auth.policy.max_lots != .1):
            raise ValueError('Combined authorization differs from its instrument or risk policy.')
        profile=self.status['policy'].get('entry_setups')
        if profile is not None and profile!=auth.policy.entry_setups:
            raise ValueError('Instrument authorities have different setup profiles.')
        self.status['policy']['entry_setups']=auth.policy.entry_setups
        self.status['setups_per_instrument']=5 if auth.policy.entry_setups=='multi_setup_v1' else 2 if auth.policy.entry_setups=='reclaim_sweep_v1' else 1
        child = self.factories[symbol](self.views[symbol], self.logs, self.root/self.policies[symbol],
            authorization_id=auth.authorization_id, config=MonitorConfig((ends-current).total_seconds()/3600, 10),
            now=self.now, sleep=self.sleep, monotonic=self.monotonic, emit=lambda value:None)
        child.coordinator = self
        child.progress_path = ((self.logs/'activations/user_activation_v1') if self.activation_id else self.folder) / f'{symbol}_state.json'
        child.status = dict(status='running', mode='demo_series', run_id=child.run_id, pid=os.getpid(),
            started_at_utc=current.isoformat(), deadline_utc=ends.isoformat(), heartbeat_utc=current.isoformat(),
            config={'poll_seconds':10}, policy=asdict(child.policy), poll_count=0, orders_sent=0)
        child._health()
        child._load_progress()
        child._event('started', deadline_utc=ends.isoformat(), coordinator_run_id=self.run_id)
        self.children[symbol] = child

    def reconcile_absent_position(self, symbol, row):
        """Read-only exit proof still works after entry authority expires."""
        child = self.children[symbol]
        executor = child._executor(row['session_id'])
        state = json.loads(executor.state_path.read_text(encoding='utf-8'))
        if (state.get('session_authorization', {}).get('authorization_id') != self.authorizations[symbol].authorization_id
                or state.get('symbol') != symbol):
            raise ValueError('Expired position does not belong to the combined session.')
        with executor._lock():
            closed = executor.verify(closing=True)
        times = [int(deal['time_msc']) for deal in closed['close_verified_deals']]
        if not times or min(times) <= 0 or not closed.get('automatic_exit', {}).get('verified_from_actual_history'):
            raise ValueError('Protective exit proof is missing; no new entries.')
        bound = DemoSeriesAuthorization(self.logs, self.authorizations[symbol].authorization_id,
            session_id=row['session_id'], decision_time_utc=row['decision_time_utc'])
        bound.finish('closed_verified', exit_time_msc=max(times), realized_net_pnl=closed['realized_net_pnl'])
        child._export_trades()
        child.status.pop('active_position_ticket', None)

    def tick(self):
        current = self.now()
        if current.utcoffset() is None or current < self.last_clock:
            raise ValueError('PC clock is naive or moved backwards; both instruments stopped.')
        self.last_clock = current
        self.verify_sources(self.root)
        positions = self.inventory()
        self.check_owned_states()
        rows = self.owned_records()
        active = [(s, r) for s, r in rows if r['status']=='entry_verified']
        if self.portfolio_scope==MULTI_SCOPE:
            bindings=[]
            for symbol,row in active:
                state=json.loads((self.logs/'sessions'/row['session_id']/'demo_test_state.json').read_text(encoding='utf-8'))
                if any(type(state.get(k)) is not int or state[k]<=0 for k in ('position_ticket','position_id')):
                    raise ValueError('Owned position lacks verified ticket/identifier binding.')
                bindings.append((symbol,row,state.get('position_ticket'),state.get('position_id')))
            if any(len({row[i] for row in bindings})!=len(bindings) for i in (2,3)):
                raise ValueError('Multiple records claim the same position identity.')
            if any(sum(symbol==p.symbol and ticket==int(p.ticket) and identifier==int(p.identifier) for symbol,_,ticket,identifier in bindings)!=1 for p in positions):
                raise ValueError('Unowned account position appeared; do not add or close positions.')
            for symbol,row,ticket,identifier in bindings:
                if not any(p.symbol==symbol and int(p.ticket)==ticket and int(p.identifier)==identifier for p in positions):
                    self.reconcile_absent_position(symbol,row)
        elif any(not any(s==p.symbol for s,_ in active) for p in positions):
            raise ValueError('Unowned account position appeared; do not add or close positions.')
        # Reconcile all absent positions before validating either entry authority.
        # Both instruments can exit between polls; absence alone never releases a slot.
        for symbol,row in active:
            if self.portfolio_scope!=MULTI_SCOPE and not any(p.symbol==symbol for p in positions):
                self.reconcile_absent_position(symbol,row)
        active=[(s,r) for s,r in self.owned_records() if r['status']=='entry_verified']
        for symbol in dict.fromkeys(s for s,_ in active):
            if current < self.windows[symbol][1]:
                self.children[symbol]._health()
                self.children[symbol]._poll_position()
            else:
                # Continue verifying the owned broker protection without expired
                # entry authority. This method only reads while a position exists.
                self.children[symbol]._poll_position()
            positions = self.inventory()
        for symbol in ORDER:
            begins, ends = self.windows[symbol]
            if current >= ends:
                if symbol in self.authorizations:
                    self.revoke(symbol, 'Instrument entry window expired.')
                self.status['instruments'][symbol] = dict(phase='entry_window_expired',
                    begins_at_utc=begins.isoformat(), expires_at_utc=ends.isoformat())
                continue
            if current < begins:
                self.status['instruments'][symbol] = dict(phase='waiting_for_session',
                    begins_at_utc=begins.isoformat(), expires_at_utc=ends.isoformat())
                continue
            occupied=sum(p.symbol==symbol for p in positions)>=self.per_symbol_limit
            if not occupied and symbol not in self.children:
                self.arm(symbol)
            child = self.children.get(symbol)
            if child is None:
                self.status['instruments'][symbol] = dict(phase='waiting_for_flat_account',
                    begins_at_utc=begins.isoformat(), expires_at_utc=ends.isoformat())
                continue
            child._health()
            candle = child._closed_candle(self.now())
            if not occupied:
                child._evaluate(candle)
            if self.observer is not None:
                try:
                    observer_call=getattr(self.observer,'observe_decision',None)
                    if observer_call is not None:
                        observation=observer_call(symbol,candle,position_open=any(p.symbol==symbol for p in self.inventory()),
                            execution_decision=child.status.get('latest_decision',{}))
                    else:
                        observation=self.observer.observe(symbol,candle,position_open=any(p.symbol==symbol for p in self.inventory()))
                    if observation.get('status')!='observed':
                        raise ValueError('Earlier observation was interrupted; no repeat on the same candle.')
                    self.status['strategy_comparison']=self.observer.summary()
                    self.status.pop('strategy_comparison_warning',None)
                except (ValueError,RuntimeError,OSError,KeyError) as exc:
                    # Research cannot grant, consume or modify entry authority.
                    self.status['strategy_comparison_warning']=f'{type(exc).__name__}: {exc}'
            child.status.update(heartbeat_utc=self.now().isoformat(),poll_count=child.status['poll_count']+1)
            self.status['instruments'][symbol] = dict(phase='position_open' if any(p.symbol==symbol for p in self.inventory()) else 'monitoring_setups',
                open_positions=sum(p.symbol==symbol for p in self.inventory()), maximum_positions=self.per_symbol_limit,
                begins_at_utc=begins.isoformat(), expires_at_utc=ends.isoformat(), latest_decision=child.status.get('latest_decision', {}),
                authorization_id=self.authorizations[symbol].authorization_id)
            atomic_json(self.folder/f'{symbol}_status.json', child.status)
            positions = self.inventory()
        self.status.update(phase='position_open' if positions else
            ('monitoring_setups' if any(self.windows[s][0] <= current < self.windows[s][1] for s in ORDER) else 'waiting_for_session'),
            orders_sent=sum(auth.summary()['verified_entries'] for auth in self.authorizations.values()),
            authorizations={s:auth.summary() for s,auth in self.authorizations.items()},
            heartbeat_utc=self.now().isoformat(),poll_count=self.status['poll_count']+1)
        self.publish()

    def publish(self):
        atomic_json(self.logs/'monitor/status.json',self.status)
        atomic_json(self.folder/'status.json',self.status)
        lines=['# Gold and EUR/USD DEMO monitor','',
            f"Process: **{self.status['status']}**; phase: **{self.status['phase']}**; PID {self.status['pid']}.",
            f"Heartbeat UTC: {self.status['heartbeat_utc']}; deadline UTC: {self.status['deadline_utc']}.",'',
            f'DEMO only; 1% baseline, {self.maximum_risk_percent}% maximum planned risk per trade; {self.combined_risk_limit}% combined planned ceiling; targets up to 4R.',
            ('Elevated risk admits minimum volume only, with verified strong trend, normal volatility and at least 2R target room; never size toward 2%.' if self.risk_mode!='fixed' else 'Fixed 1% risk policy.'),
            f'At most {self.per_symbol_limit} Gold and {self.per_symbol_limit} EUR/USD positions ({self.total_position_limit} total); fresh-candle entries only; no other instruments.',
            ('Entries enabled by explicit activation; no fixed clock-entry schedule; setup weekend and broker-session guards apply.' if self.activation_id else
             'Gold entries: Monday 16 November, 15:00-21:00 Manila. EUR/USD: 16:00-22:00 Manila.'),
            f"Timing profile: {self.status['entry_timing']}; activation: {self.activation_id or 'dated session'}.",
            'At shared decision times EUR/USD is checked first, then Gold within the combined risk ceiling.',
            'No valid affordable setup means no trade. No retries, automatic manual closes or guaranteed same-day exit.','',
            f"Open positions: {self.status.get('open_position_count', 'unverified')}; order sends recorded: {self.status.get('orders_sent',0)}.",
            f"Stop/error: {self.status.get('stop_reason') or 'none'}.",'']
        if self.observer is not None:
            if self.status.get('strategy_comparison',{}).get('mode')=='multi_setup_v1':
                lines += ['Five executable setups per instrument: trend reclaim, trend sweep, channel breakout, compression breakout and range re-entry.',
                    'Instrument-specific regime gates; fresh closed bars; frozen priority on overlap; opposite directions skip.',
                    'Weekday/month/session context is recorded; no historical weekday advantage is presumed.',
                    'See reports/latest_setup_comparison.md and docs/FIVE_SETUP_GUIDE.md for detailed rules and history.']
            elif self.status.get('strategy_comparison',{}).get('mode')=='reclaim_sweep_v1':
                lines += ['Two entry setups: A trend/EMA20 reclaim; B trend-aligned prior-20-bar sweep/rejection.',
                    'A priority on overlap; opposite directions skip both; one shared risk/position budget.',
                    'See reports/latest_setup_comparison.md for separate technical and execution records.']
            else:
                lines += ['Entry execution retains the existing immediate reclaim setup.',
                    'A separate one-bar confirmation experiment is recorded for research only; it cannot place orders.']
            lines += [f"Observation warning: {self.status.get('strategy_comparison_warning','none')}",'']
        for symbol, item in self.status['instruments'].items():
            decision=item.get('latest_decision',{})
            lines += [f"## {symbol}",'',f"State: **{item['phase']}**.",
                f"Decision: {decision.get('status','not evaluated')} - {decision.get('reason','Waiting for session/account availability.')}",'']
        lines += [f"Status and stop: `{self.status['control_script']} --status` / `--stop`.",
                  'An old heartbeat does not prove the process is alive. Expiry stops new entries; no automatic restart.',
                  'Activation timing is unproven research; no actual economic-calendar filter.']
        for name in ('latest_monitor_status.md','latest_gold_monitor_status.md','latest_dual_monitor_status.md'):
            path=self.root/'reports'/name
            atomic_text(path,'\n'.join(lines)+'\n')
        paths=[self.logs/'demo_test_state.json',*sorted((self.logs/'sessions').glob('*/demo_test_state.json'))]
        marker=(self.status['status'],tuple((str(path),path.stat().st_mtime_ns) for path in paths if path.is_file()))
        if marker!=self.journal_marker:
            self.journal_refresh(self.root)
            self.journal_marker=marker

    def revoke(self, symbol, reason):
        auth=self.authorizations[symbol]
        if not auth.revoked_path.exists():
            atomic_json(auth.revoked_path,dict(reason=reason,requested_at_utc=self.now().isoformat()))

    def run(self, *, once=False):
        try:
            while self.now()<self.deadline and self.monotonic()-self.started_tick < self.maximum_seconds:
                stop_path=self.logs/'monitor/stop.json'
                if stop_path.exists() and json.loads(stop_path.read_text(encoding='utf-8')).get('run_id')==self.run_id:
                    self.status['stop_reason']='User requested stop; no positions closed.'
                    break
                self.tick()
                if once:
                    self.status['stop_reason']='Single test inspection completed.'
                    break
                self.sleep(min(10,max(0,(self.deadline-self.now()).total_seconds())))
            self.status.update(status='stopped',phase='stopped',stop_reason=self.status['stop_reason'] or 'Session deadline reached; no positions closed.')
        except KeyboardInterrupt:
            self.status.update(status='stopped',phase='stopped',stop_reason='Interrupted; no positions closed.')
        except Exception as exc:
            self.status.update(status='failed',phase='stopped',stop_reason=f'{type(exc).__name__}: {exc}; no retry.')
        finally:
            for symbol in self.authorizations:
                self.revoke(symbol,'Combined controller ended; no reuse/retry.')
            calls=[child.status.get('orders_sent',0) for child in self.children.values()]
            self.status.update(heartbeat_utc=self.now().isoformat(),
                orders_sent=None if any(value is None for value in calls) else sum(calls),
                authorizations={s:a.summary() for s,a in self.authorizations.items()})
            self.publish()
        return self.status


def start(root, *, now=utc_now, connector_factory=GoldConnector, controller_factory=DualController):
    root=Path(root).resolve()
    current=now()
    if current.utcoffset() is None or current.astimezone(timezone.utc).date()!=WINDOWS['XAUUSDm'][0].date() or current>=WINDOWS['XAUUSDm'][1]:
        raise ValueError('Combined activation is only for Monday 16 November before 21:00 Manila; it expires today.')
    verify_sources(root)
    logs=root/'logs'
    reservations=[root/RESERVATION,logs/'gold_launch_2026-11-16.json',logs/'monday_2026-11-16.launch.json']
    with monitor_lock(logs/'dual_demo_2026-11-16.launch.lock'):
        with monitor_lock(logs/'monitor/monitor.lock'):
            if any(p.exists() for p in reservations):
                raise ValueError('A Gold/EUR/combined launch is already consumed; do not reset or retry.')
            if unresolved(root):
                raise ValueError('Earlier execution remains unresolved; actual deal proof is required.')
            with connector_factory() as connector:
                connector.verify_pinned_account()
                positions,pending=connector.terminal_api.positions_get(),connector.terminal_api.orders_get()
                if positions is None or pending is None or positions or pending:
                    raise ValueError('Combined activation requires a verified flat account, with no pending orders.')
                snapshots={s:InstrumentView(connector,s).inspect() for s in ORDER}
                for snapshot in snapshots.values():
                    if (snapshot['permissions']['terminal_trade_allowed'] is not True or
                        snapshot['permissions']['external_python_trading_disabled'] is not False or
                        snapshot['account']['account_trade_allowed'] is not True or snapshot['account']['account_expert_allowed'] is not True):
                        raise ValueError('Native terminal/account Python trading permissions are off or unknown.')
                if now()>=WINDOWS['XAUUSDm'][1]:
                    raise ValueError('Window ended during native preflight.')
                verify_sources(root)
                controller=controller_factory(root,connector,now=now)
                reservation=dict(status='reserved_by_dual_controller',run_id=controller.run_id,
                    requested_at_utc=now().isoformat(),user_instruction=INSTRUCTION,deadline_utc=DEADLINE.isoformat(),retry_allowed=False)
                for path in reservations:
                    _exclusive_json(path,reservation)
                try:
                    result=controller.run()
                except BaseException as exc:
                    atomic_json(root/RESERVATION,dict(reservation,status='failed_no_retry',error=str(exc)))
                    raise
                atomic_json(root/RESERVATION,dict(reservation,status='finished',monitor_status=result['status']))
                return result


def status(root):
    root=Path(root)
    result=read_status(root/'logs')
    if result.get('mode')!=MODE:
        return dict(status='not_running',scheduled=False,message='No combined controller is recorded.')
    if result.get('status')=='running' and result.get('status_observation')!='recent_heartbeat':
        result=dict(result,status='status_unverified')
    return result


def stop(root):
    root=Path(root)
    result=status(root)
    if result.get('mode')!=MODE or result['status'] not in ('running','status_unverified'):
        return dict(status='not_running',orders_sent=0)
    for summary in result.get('authorizations',{}).values():
        identifier=summary['authorization_id']
        if uuid.UUID(identifier).hex!=identifier:
            raise ValueError('Invalid combined authorization identifier.')
        atomic_json(root/'logs/authorizations'/f'{identifier}.revoked.json',dict(reason='User requested combined stop.',requested_at_utc=utc_now().isoformat()))
    atomic_json(root/'logs/monitor/stop.json',dict(run_id=result['run_id'],requested_at_utc=utc_now().isoformat()))
    return dict(status='stop_requested',positions_closed=0)
