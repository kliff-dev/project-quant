"""Wait for a valid strategy candidate, attempt one authorized DEMO entry, stop."""

from pathlib import Path
from typing import Any
import json

from engine.demo_authorization import DemoEntryAuthorization
from engine.demo_executor import DemoExecutor
from engine.execution_audit import atomic_json
from engine.market_monitor import MarketMonitor


class DemoSessionMonitor(MarketMonitor):
    mode = 'demo_once'
    mode_description = 'one authorized DEMO entry on a valid setup; no retries or automatic manual exits'

    def __init__(self, connector: Any, directory: Path, policy_path: Path, *, authorization_id: str, **kwargs: Any) -> None:
        self.authorization = DemoEntryAuthorization(directory, authorization_id)
        if 'deadline_cap' in kwargs:
            raise ValueError('DEMO session deadline is bound to its authorization.')
        super().__init__(connector, directory, policy_path, deadline_cap=self.authorization.expires, **kwargs)
        if self.policy != self.authorization.policy or self.policy_path.resolve() != self.authorization.policy_path.resolve():
            raise ValueError('Monitor policy does not match the authorized policy file.')

    def _health(self) -> dict:
        self.authorization.assert_unused()
        self.authorization.validate(self.connector)
        snapshot = super()._health()
        if self.symbol != self.authorization.data['symbol']:
            raise ValueError('Monitor symbol differs from authorized symbol.')
        self.status.update(authorization=self.authorization.summary(), entry_attempts=0,
                           execution_enabled=True)
        return snapshot

    def _save_execution_decision(self, decision: dict) -> None:
        self.progress['latest_decision'] = decision
        atomic_json(self.folder / 'state.json', self.progress)
        atomic_json(self.directory / 'latest_strategy_decision.json', decision)
        self.status['latest_decision'] = decision

    def _handle_decision(self, decision: dict) -> None:
        if decision['status'] != 'READY_FOR_CONFIRMATION':
            return
        self._health()
        if self.status['execution_blockers']:
            decision.update(status='BLOCKED', ready=False, reason='; '.join(self.status['execution_blockers']))
            decision.pop('manual_open_command', None)
            self._save_execution_decision(decision)
            return
        executor = DemoExecutor(self.connector, self.directory, session_id=decision['session_id'],
                                strategy_policy=self.policy)
        decision.update(status='AUTO_ENTRY_PREFLIGHT', ready=False,
                        reason='One user-authorized DEMO attempt; all native guards will be repeated.')
        decision.pop('manual_open_command', None)
        self._save_execution_decision(decision)
        self.status['heartbeat_utc'] = self.now().isoformat()
        self._publish()
        try:
            result = executor.open_authorized_session(decision['preview_id'], self.authorization.authorization_id,
                                                      monitor_run_id=self.run_id)
        except BaseException as exc:
            sends: int | None = 0
            if executor.state_path.exists():
                try:
                    saved = json.loads(executor.state_path.read_text(encoding='utf-8'))
                    sends = 1 if 'open_result' in saved or 'send_error' in saved else None
                except (OSError, ValueError):
                    sends = None
            self.status.update(orders_sent=sends, entry_attempts=int(self.authorization.usage_path.exists()),
                               authorization=self.authorization.summary(),
                               execution_result=dict(status='stopped_or_unverified', state_file=str(executor.state_path),
                                                     error=f'{type(exc).__name__}: {exc}'))
            decision.update(status='EXECUTION_STOPPED', orders_sent=sends,
                            reason=f'{type(exc).__name__}: {exc}. No automatic retry.')
            self._save_execution_decision(decision)
            self._event('demo_entry_stopped', reason=decision['reason'])
            raise
        proof = dict(status=result['status'], order_ticket=result['open_result']['order'],
                     deal_ticket=result['open_result']['deal'], position_ticket=result['position_ticket'],
                     executed_volume=result['executed_volume'], executed_price=result['executed_price'],
                     stop_loss=result['request']['sl'], take_profit=result['request']['tp'],
                     entry_slippage_points=result['entry_slippage_points'], commission=result['entry_commission'],
                     fee=result['entry_fee'], swap=result['entry_swap'],
                     protection_at_fill=result.get('protection_at_fill'), state_file=str(executor.state_path))
        self.status.update(orders_sent=1, entry_attempts=1, authorization=self.authorization.summary(),
                           execution_result=proof, finish_requested=True, open_position_count=1,
                           stop_reason='One DEMO entry verified. No further entries; attached broker SL/TP remain active.')
        decision.update(status='EXECUTED_VERIFIED', orders_sent=1, execution=proof,
                        reason='Actual order, deal and protected position verified; single-entry session finished.')
        self._save_execution_decision(decision)
        self._event('demo_entry_verified', execution=proof)
