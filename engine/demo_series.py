"""DEMO research: fresh signals, frozen position limits, verified exits, no retries."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from engine.demo_authorization import DemoEntryAuthorization, _exclusive_json, _identifier, utc_now
from engine.demo_executor import DemoExecutor, DemoVerificationError, MAGIC
from engine.execution_audit import atomic_json
from engine.market_monitor import MarketMonitor, monitor_lock


class DemoSeriesAuthorization(DemoEntryAuthorization):
    SCOPE = 'sequential_demo_strategy_entries'
    ENTRY_LIMIT = None
    DYNAMIC_RISK = True
    CLOSURE_SCOPE = 'Broker SL/TP exits; fresh later signals under the frozen position limit; no retries or automatic manual/time exits.'

    @property
    def maximum_positions(self):
        from engine.portfolio_guard import MULTI_SCOPE
        return 2 if self.data.get('portfolio_scope') == MULTI_SCOPE else 1

    def __init__(self, directory: Path, authorization_id: str, *, session_id: str | None = None,
                 decision_time_utc: str | None = None) -> None:
        super().__init__(directory, authorization_id)
        self.entries_folder = self.folder / f'{authorization_id}.entries'
        self.session_id, self.decision_time_utc = session_id, decision_time_utc
        if (session_id is None) != (decision_time_utc is None):
            raise ValueError('A series entry requires both session and H1 decision time.')
        if session_id is not None:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', session_id):
                raise ValueError('Invalid execution session identity.')
            decision = datetime.fromisoformat(decision_time_utc)
            minute = decision.minute % 5 if self.policy.target_mode == 'gold_intraday_v1' else decision.minute
            if decision.utcoffset() is None or any((minute, decision.second, decision.microsecond)):
                raise ValueError('Series entry must bind an aware strategy candle close.')
            self.usage_path = self.entries_folder / (decision.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')

    def records(self) -> list[dict]:
        result = []
        for path in sorted(self.entries_folder.glob('*.json')):
            item = json.loads(path.read_text(encoding='utf-8'))
            decision = datetime.fromisoformat(item['decision_time_utc'])
            expected = decision.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json'
            if (item.get('authorization_id') != self.authorization_id or path.name != expected or
                decision.utcoffset() is None or any((decision.minute % 5 if self.policy.target_mode == 'gold_intraday_v1' else decision.minute, decision.second, decision.microsecond)) or
                not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', item.get('session_id', '')) or
                item.get('status') not in ('reserved', 'entry_verified', 'closed_verified', 'consumed_stopped_or_unverified')):
                raise ValueError('Invalid sequential entry checkpoint; no new orders permitted.')
            if item['status'] == 'closed_verified':
                if type(item.get('exit_time_msc')) is not int or item['exit_time_msc'] < decision.timestamp()*1000:
                    raise ValueError('Verified close is missing its actual exit time.')
            result.append(item)
        return result

    def validate(self, connector: Any, facts: dict | None = None) -> None:
        super().validate(connector, facts)
        active = []
        for record in self.records():
            own = self.session_id == record['session_id'] and self.decision_time_utc == record['decision_time_utc']
            if self.session_id is not None:
                if own and record['status'] == 'reserved':
                    continue
                if record['status'] == 'entry_verified' and self.maximum_positions > 1:
                    if not own: active.append(record)
                    continue
                if record['status'] != 'closed_verified':
                    raise ValueError('Another entry is active or an outcome is uncertain; no new send.')
            elif record['status'] == 'entry_verified':
                active.append(record)
            elif record['status'] != 'closed_verified':
                raise ValueError('Sequential session has a reserved/failed/uncertain attempt; inspect it without retrying.')
        if len(active) > self.maximum_positions or self.session_id is not None and len(active)>=self.maximum_positions:
            raise ValueError('Multiple active entry checkpoints; session stopped.')

    def assert_unused(self) -> None:
        if self.session_id is None:
            raise ValueError('Bind a specific new candle before reserving a sequential entry.')
        super().assert_unused()
        records = self.records()
        if any(record['status'] not in ('closed_verified','entry_verified') for record in records) or sum(record['status']=='entry_verified' for record in records)>=self.maximum_positions:
            raise ValueError('Previous entry must be verified closed before another entry.')
        if records:
            latest_exit = max(max(record.get('exit_time_msc',0),datetime.fromisoformat(record['decision_time_utc']).timestamp()*1000) for record in records)
            if datetime.fromisoformat(self.decision_time_utc).timestamp() * 1000 <= latest_exit:
                raise ValueError('A new decision must follow the actual prior exit; never reuse an old signal.')

    def reserve(self, connector: Any, *, preview_id: str, session_id: str, run_id: str) -> None:
        if session_id != self.session_id:
            raise ValueError('Sequential reservation belongs to another execution session.')
        _identifier(run_id)
        with monitor_lock(self.entries_folder / 'ledger.lock'):
            self.assert_unused()
            self.validate(connector)
            _exclusive_json(self.usage_path, dict(authorization_id=self.authorization_id, status='reserved',
                            reserved_at_utc=utc_now().isoformat(), preview_id=preview_id, session_id=session_id,
                            monitor_run_id=run_id, decision_time_utc=self.decision_time_utc))

    def summary(self) -> dict:
        records = self.records()
        verified = [record for record in records if record['status'] in ('entry_verified', 'closed_verified')]
        closed = [record for record in records if record['status'] == 'closed_verified']
        return dict(authorization_id=self.authorization_id, expires_at_utc=self.expires.isoformat(),
                    symbol=self.data['symbol'], max_entry_attempts=None, max_risk_usd=None,
                    risk_percent=self.policy.risk_percent, max_open_positions=self.maximum_positions, entry_attempts=len(records),
                    verified_entries=len(verified), closed_trades=len(closed),
                    halted=any(record['status'] not in ('entry_verified', 'closed_verified') for record in records),
                    revoked=self.revoked_path.exists())


TRADE_COLUMNS = ['authorization_id', 'session_id', 'decision_time_utc', 'symbol', 'side', 'volume_lots',
                 'entry_time_utc', 'exit_time_utc', 'holding_seconds', 'entry_price', 'exit_price',
                 'stop_loss', 'take_profit', 'planned_risk_usd', 'risk_percent', 'entry_slippage_points',
                 'entry_commission', 'exit_commission', 'entry_fee', 'exit_fee', 'entry_swap', 'exit_swap',
                 'realized_net_pnl', 'realized_r', 'exit_reason', 'order_ticket', 'deal_ticket', 'position_ticket',
                 'context_sha256', 'state_file', 'selected_reward_r', 'target_mode', 'target_branch_reason',
                 'target_spec_sha256', 'setup_id', 'entry_setups']


class DemoSeriesMonitor(MarketMonitor):
    mode = 'demo_series'
    mode_description = 'valid-setup DEMO entries under frozen position limits; no execution retries'

    def __init__(self, connector: Any, directory: Path, policy_path: Path, *, authorization_id: str, **kwargs: Any) -> None:
        self.authorization = DemoSeriesAuthorization(directory, authorization_id)
        if 'deadline_cap' in kwargs:
            raise ValueError('Sequential session expiry is bound to its authorization.')
        super().__init__(connector, directory, policy_path, deadline_cap=self.authorization.expires, **kwargs)
        if self.policy != self.authorization.policy or self.policy_path.resolve() != self.authorization.policy_path.resolve():
            raise ValueError('Series policy differs from the authorized policy.')
        self.trade_data_path = self.folder / 'series' / authorization_id / 'completed_trades.csv'
        self._exported_closed_count: int | None = None

    def _health(self) -> dict:
        self.authorization.validate(self.connector)
        snapshot = super()._health()
        if self.symbol != self.authorization.data['symbol']:
            raise ValueError('Series symbol differs from authorization.')
        summary = self.authorization.summary()
        self.status.update(authorization=summary, execution_enabled=True, entry_attempts=summary['entry_attempts'],
                           orders_sent=summary['verified_entries'], closed_trades=summary['closed_trades'],
                           trade_data_file=str(self.trade_data_path),
                           realized_net_pnl=sum(record.get('realized_net_pnl', 0) for record in self.authorization.records()
                                                if record['status'] == 'closed_verified'))
        return snapshot

    def _executor(self, session: str) -> DemoExecutor:
        return DemoExecutor(self.connector, self.directory, session_id=session, strategy_policy=self.policy)

    def _save_decision(self, decision: dict) -> None:
        self.progress['latest_decision'] = decision
        atomic_json(self.progress_path, self.progress)
        atomic_json(self.directory / 'latest_strategy_decision.json', decision)
        self.status['latest_decision'] = decision

    def _export_trades(self) -> None:
        rows = []
        for record in self.authorization.records():
            if record['status'] != 'closed_verified':
                continue
            state_path = self.directory / 'sessions' / record['session_id'] / 'demo_test_state.json'
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if state['status'] != 'closed_verified' or not state.get('automatic_exit', {}).get('verified_from_actual_history'):
                raise ValueError('Closed checkpoint lacks verified protective-exit evidence.')
            entry_ms = min(int(deal['time_msc']) for deal in state['entry_verified_deals'])
            exit_ms = record['exit_time_msc']
            if not 0 < entry_ms <= exit_ms:
                raise ValueError('Actual deal timestamps cannot establish a valid holding time.')
            plan = state['protection']
            rows.append(dict(authorization_id=self.authorization.authorization_id, session_id=record['session_id'],
                decision_time_utc=record['decision_time_utc'], symbol=state['symbol'], side=plan['side'],
                volume_lots=state['executed_volume'], entry_time_utc=datetime.fromtimestamp(entry_ms/1000, timezone.utc).isoformat(),
                exit_time_utc=datetime.fromtimestamp(exit_ms/1000, timezone.utc).isoformat(), holding_seconds=(exit_ms-entry_ms)/1000,
                entry_price=state['executed_price'], exit_price=state['close_executed_price'], stop_loss=plan['stop_loss'],
                take_profit=plan['take_profit'], planned_risk_usd=plan['planned_loss_with_reserves'], risk_percent=plan['risk_percent'],
                entry_slippage_points=state['entry_slippage_points'], entry_commission=state['entry_commission'],
                exit_commission=state['close_commission'], entry_fee=state['entry_fee'], exit_fee=state['close_fee'],
                entry_swap=state['entry_swap'], exit_swap=state['close_swap'], realized_net_pnl=state['realized_net_pnl'],
                realized_r=state['realized_net_pnl']/plan['planned_loss_with_reserves'],
                exit_reason=','.join(state['automatic_exit']['reasons']), order_ticket=state['open_result']['order'],
                deal_ticket=state['open_result']['deal'], position_ticket=state['position_ticket'],
                context_sha256=plan['strategy_intent']['context_sha256'], state_file=str(state_path),
                selected_reward_r=plan.get('selected_reward_r', plan['reward_to_risk']),
                target_mode=plan['policy'].get('target_mode', 'fixed_2r'),
                target_branch_reason=plan.get('branch_reason', 'fixed_2r'),
                target_spec_sha256=plan.get('research_spec_sha256', ''),
                setup_id=(plan['strategy_intent'].get('adaptive_context') or {}).get('setup_id','legacy_'+plan['strategy_intent']['trigger']),
                entry_setups=plan['policy'].get('entry_setups','reclaim_only')))
        self.trade_data_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.trade_data_path.with_suffix('.csv.tmp')
        with temporary.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=TRADE_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.trade_data_path)
        self._exported_closed_count = len(rows)

    def _poll_position(self) -> None:
        records = self.authorization.records()
        closed_count = sum(record['status'] == 'closed_verified' for record in records)
        if self._exported_closed_count != closed_count:
            self._export_trades()
        active = [record for record in records if record['status'] == 'entry_verified']
        if not active:
            return
        for record in active:
            self._poll_record(record)

    def _poll_record(self, record):
        executor = self._executor(record['session_id'])
        state = json.loads(executor.state_path.read_text(encoding='utf-8'))
        identity = dict(login=self.identity[0], server=self.identity[1])
        if (state['account_identity'] != identity or state['status'] not in ('open_verified', 'closed_verified') or
            state.get('session_authorization', {}).get('authorization_id') != self.authorization.authorization_id or
            state['symbol'] != self.symbol):
            raise DemoVerificationError('Active series position/state identity cannot be verified.')
        api = self.connector.terminal_api
        positions, orders = api.positions_get(), api.orders_get()
        if positions is None or orders is None:
            raise DemoVerificationError('Position/pending-order inventory is unavailable.')
        if orders:
            raise DemoVerificationError('Pending orders appeared while the series position was active.')
        from engine.portfolio_guard import get_guard
        portfolio=get_guard(self.connector)
        if portfolio is not None:
            portfolio.inventory(self.connector,positions,orders,symbol=self.symbol,
                skip_path=executor.state_path,owned_position_id=state['position_id'])
            positions=tuple(p for p in positions if p.symbol==self.symbol)
        matches = [position for position in positions if int(position.ticket) == state['position_ticket']
                   and int(position.identifier) == state['position_id']]
        if matches or portfolio is None and positions:
            if len(matches) != 1 or portfolio is None and len(positions) != 1:
                raise DemoVerificationError('Unexpected/multiple positions appeared; no additional entry.')
            position = matches[0]
            if (position.symbol != state['symbol'] or int(position.magic) != MAGIC or int(position.type) != state['position_type'] or
                not math.isclose(float(position.volume), state['executed_volume'], rel_tol=0, abs_tol=1e-9) or
                any(not math.isclose(float(getattr(position, key, 0)), state['request'][key], rel_tol=0,
                                    abs_tol=state['protection']['tick_size']*1e-5) for key in ('sl', 'tp'))):
                raise DemoVerificationError('Owned position volume, direction or protection changed; session stopped.')
            self.status['active_position_ticket'] = state['position_ticket']
            return
        # Absence alone never releases the next-entry gate. Verify actual exit.
        closed = executor.verify(closing=True)
        exit_times = [int(deal['time_msc']) for deal in closed['close_verified_deals']]
        if not exit_times or min(exit_times) <= 0:
            raise DemoVerificationError('Actual exit timestamps are missing.')
        bound = DemoSeriesAuthorization(self.directory, self.authorization.authorization_id,
                                       session_id=record['session_id'], decision_time_utc=record['decision_time_utc'])
        bound.finish('closed_verified', exit_time_msc=max(exit_times), realized_net_pnl=closed['realized_net_pnl'])
        self._export_trades()
        self.status.pop('active_position_ticket', None)
        self.status['execution_result'] = dict(status='closed_verified', order_ticket=closed['open_result']['order'],
            deal_ticket=closed['open_result']['deal'], position_ticket=closed['position_ticket'],
            executed_volume=closed['executed_volume'], executed_price=closed['executed_price'],
            stop_loss=closed['request']['sl'], take_profit=closed['request']['tp'],
            exit_price=closed['close_executed_price'], realized_net_pnl=closed['realized_net_pnl'], state_file=str(executor.state_path))
        self._health()
        self._event('series_exit_verified', session_id=record['session_id'], realized_net_pnl=closed['realized_net_pnl'])

    def _handle_decision(self, decision: dict) -> None:
        if decision['status'] != 'READY_FOR_CONFIRMATION':
            return
        records = self.authorization.records()
        if records and datetime.fromisoformat(decision['decision_time_utc']).timestamp()*1000 <= max(record.get('exit_time_msc', 0) for record in records):
            decision.update(status='BLOCKED', ready=False, reason='Wait for a fresh signal after the prior actual exit.')
            decision.pop('manual_open_command', None)
            self._save_decision(decision)
            return
        self._health()
        if self.status['execution_blockers']:
            decision.update(status='BLOCKED', ready=False, reason='; '.join(self.status['execution_blockers']))
            decision.pop('manual_open_command', None)
            self._save_decision(decision)
            return
        executor = self._executor(decision['session_id'])
        decision.update(status='AUTO_ENTRY_PREFLIGHT', ready=False, reason='Sequential DEMO entry; fresh native checks required.')
        decision.pop('manual_open_command', None)
        self._save_decision(decision)
        try:
            result = executor.open_authorized_session(decision['preview_id'], self.authorization.authorization_id,
                                                      monitor_run_id=self.run_id)
        except BaseException as exc:
            calls: int | None = 0
            if executor.state_path.exists():
                saved = json.loads(executor.state_path.read_text(encoding='utf-8'))
                calls = 1 if 'open_result' in saved or 'send_error' in saved else None
            self.status.update(orders_sent=None if calls is None else self.status['orders_sent']+calls,
                               authorization=self.authorization.summary(), entry_attempts=len(self.authorization.records()),
                               execution_result=dict(status='stopped_or_unverified', error=f'{type(exc).__name__}: {exc}',
                                                     state_file=str(executor.state_path)))
            decision.update(status='EXECUTION_STOPPED', reason=f'{type(exc).__name__}: {exc}. No retry or further entries.')
            self._save_decision(decision)
            raise
        proof = dict(status=result['status'], order_ticket=result['open_result']['order'], deal_ticket=result['open_result']['deal'],
                     position_ticket=result['position_ticket'], executed_volume=result['executed_volume'],
                     executed_price=result['executed_price'], stop_loss=result['request']['sl'], take_profit=result['request']['tp'],
                     entry_slippage_points=result['entry_slippage_points'], commission=result['entry_commission'],
                     fee=result['entry_fee'], swap=result['entry_swap'], protection_at_fill=result.get('protection_at_fill'),
                     state_file=str(executor.state_path))
        protection = result['protection']
        proof.update(selected_reward_r=protection.get('selected_reward_r', protection['reward_to_risk']),
                     target_mode=protection['policy'].get('target_mode', 'fixed_2r'),
                     target_branch_reason=protection.get('branch_reason', 'fixed_2r'))
        self.status.update(execution_result=proof, authorization=self.authorization.summary(),
                           orders_sent=self.status['orders_sent']+1, entry_attempts=len(self.authorization.records()),
                           active_position_ticket=result['position_ticket'], open_position_count=sum(r['status']=='entry_verified' for r in self.authorization.records()))
        decision.update(status='EXECUTED_VERIFIED', orders_sent=1, execution=proof,
                        reason='Entry verified. A later fresh setup still requires a free position slot and account risk budget.')
        self._save_decision(decision)
        self._event('series_entry_verified', execution=proof)
        if self.authorization.maximum_positions>1 and (result.get('protection_at_fill') or {}).get('budget_exceeded_by_fill') is True:
            raise DemoVerificationError('Actual fill exceeded its per-trade budget. Protected position retained; stop further entries.')
