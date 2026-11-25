"""Explicit, bounded, one-use authority for an unattended DEMO strategy entry."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
import uuid

from engine.execution_audit import append_event, atomic_json
from engine.strategy_orders import MODE, StrategyPolicy, risk_budget


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or uuid.UUID(value).hex != value:
        raise ValueError('Authorization/run ID must be a lowercase UUID hex string.')
    return value


def _exclusive_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())


class DemoEntryAuthorization:
    """A local audit record of user approval, not a password or broker credential.

    Native DEMO verification is mandatory independently of this file. The one-use
    reservation is never deleted/reset, including if preflight or a send fails.
    """

    SCOPE = 'one_demo_strategy_entry'
    ENTRY_LIMIT = 1
    DYNAMIC_RISK = False
    CLOSURE_SCOPE = 'Attached broker SL/TP permitted; no automatic manual/time exit or second entry.'

    def __init__(self, directory: Path, authorization_id: str) -> None:
        self.directory = Path(directory).resolve()
        self.authorization_id = _identifier(authorization_id)
        self.folder = self.directory / 'authorizations'
        self.path = self.folder / f'{authorization_id}.json'
        self.usage_path = self.folder / f'{authorization_id}.usage.json'
        self.revoked_path = self.folder / f'{authorization_id}.revoked.json'
        raw = self.path.read_bytes()
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        self.data = json.loads(raw)
        data = self.data
        if (type(data.get('version')) is not int or data['version'] != 1 or
            data.get('scope') != self.SCOPE or data.get('authorization_id') != authorization_id or
            'max_entry_attempts' not in data or
            (data.get('max_entry_attempts') is not None if self.ENTRY_LIMIT is None else
             type(data.get('max_entry_attempts')) is not int or data['max_entry_attempts'] != self.ENTRY_LIMIT)):
            raise ValueError('Invalid one-entry DEMO authorization scope.')
        self.policy = StrategyPolicy(**data['policy'])
        self.expires = datetime.fromisoformat(data['expires_at_utc'])
        approved = datetime.fromisoformat(data['approved_at_utc'])
        if self.expires.utcoffset() is None or approved.utcoffset() is None or not 0 < (self.expires - approved).total_seconds() <= 86400:
            raise ValueError('Authorization must have an aware expiry within 24 hours of approval.')
        identity, ceiling = data['account_identity'], data['max_risk_usd']
        invalid_ceiling = (ceiling is not None if self.DYNAMIC_RISK else
                           isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not math.isfinite(ceiling) or ceiling <= 0)
        if (type(identity.get('login')) is not int or identity['login'] <= 0 or not identity.get('server') or
            not data.get('broker') or not data.get('symbol') or not data.get('user_instruction') or
            invalid_ceiling):
            raise ValueError('Authorization requires a complete account, instruction and positive risk ceiling.')
        self.policy_path = Path(data['policy_file'])
        if not self.policy_path.is_absolute():
            raise ValueError('Absolute policy path required.')

    @classmethod
    def create(cls, connector: Any, directory: Path, policy_path: Path, *,
               expires_at: datetime, user_instruction: str) -> DemoEntryAuthorization:
        """Call only after explicit user approval for one unattended demo entry."""
        now = utc_now()
        if expires_at.utcoffset() is None or not 0 < (expires_at - now).total_seconds() <= 86400:
            raise ValueError('Approval must expire within the next 24 hours.')
        if not isinstance(user_instruction, str) or not user_instruction.strip():
            raise ValueError('The explicit user instruction must be retained.')
        policy = StrategyPolicy.load(policy_path)
        snapshot = connector.inspect()
        login, server = connector.account_identity
        api = connector.terminal_api
        account, positions, orders = api.account_info(), api.positions_get(), api.orders_get()
        connector.verify_demo_account()
        if account is None or (account.login, account.server) != (login, server) or connector.account_identity != (login, server):
            raise ValueError('Account changed while authorizing the session.')
        from engine.portfolio_guard import get_guard, SCOPE
        guard = get_guard(connector)
        if guard is not None:
            guard.inventory(connector, positions, orders, symbol=snapshot['quote']['symbol'], opening=True)
        elif positions is None or orders is None or positions or orders:
            raise ValueError('Verified empty positions and pending orders are required before arming.')
        directory = Path(directory).resolve()
        for path in [directory / 'demo_test_state.json', *sorted((directory / 'sessions').glob('*/demo_test_state.json'))]:
            if guard is None and path.is_file() and json.loads(path.read_text(encoding='utf-8')).get('status') != 'closed_verified':
                raise ValueError('An unresolved execution attempt prevents a new session authorization.')
        _, _, budget = risk_budget(dict(balance=account.balance, equity=account.equity, currency=account.currency), policy)
        identifier = uuid.uuid4().hex
        data = dict(version=1, scope=cls.SCOPE, authorization_id=identifier,
                    approved_at_utc=now.isoformat(), expires_at_utc=expires_at.isoformat(),
                    account_identity=dict(login=login, server=server), broker=snapshot['account']['broker'],
                    symbol=snapshot['quote']['symbol'], policy=asdict(policy), policy_file=str(policy_path.resolve()),
                    max_risk_usd=None if cls.DYNAMIC_RISK else float(budget), initial_risk_budget_usd=float(budget),
                    max_entry_attempts=cls.ENTRY_LIMIT, user_instruction=user_instruction,
                    closure_scope=cls.CLOSURE_SCOPE)
        if guard is not None:
            if guard.directory != directory:
                raise ValueError('Portfolio authorization log root differs from its guard.')
            data['portfolio_scope'] = guard.scope
            data['maximum_positions_per_symbol'] = guard.per_symbol
            data['maximum_open_positions_account_wide'] = guard.total_limit
            data['maximum_combined_risk_percent'] = guard.combined_percent
        path = directory / 'authorizations' / f'{identifier}.json'
        _exclusive_json(path, data)
        result = cls(directory, identifier)
        result.validate(connector)
        append_event(directory, 'demo_session_authorized', 'one_entry_approved' if cls.ENTRY_LIMIT == 1 else 'sequential_entries_approved', account=snapshot['account'],
                     quote=snapshot['quote'], message=f'Authorization={identifier}; expires={expires_at.isoformat()}; max risk USD={budget}; no order submitted.')
        return result

    def assert_unused(self) -> None:
        if self.usage_path.exists():
            raise ValueError('One-entry authorization is already reserved/consumed. No automatic retry or reset.')

    def validate(self, connector: Any, facts: dict | None = None) -> None:
        """Repeat immediately before the sole native send, including cancellation."""
        now = utc_now()
        if not datetime.fromisoformat(self.data['approved_at_utc']) <= now < self.expires:
            raise ValueError('One-entry authorization is expired or ahead of the PC clock.')
        if self.revoked_path.exists():
            raise ValueError('One-entry authorization was revoked.')
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.fingerprint:
            raise ValueError('Authorization file changed during execution.')
        if StrategyPolicy.load(self.policy_path) != self.policy:
            raise ValueError('The authorized trading policy changed.')
        summary = connector.verify_demo_account()
        identity = self.data['account_identity']
        if (connector.account_identity != (identity['login'], identity['server']) or
            summary['broker'] != self.data['broker'] or summary['currency'] != 'USD'):
            raise ValueError('Authorization belongs to a different demo account/broker/currency.')
        from engine.portfolio_guard import get_guard, SCOPE
        guard = get_guard(connector)
        if guard is not None or self.data.get('portfolio_scope') is not None:
            if guard is None or self.data.get('portfolio_scope') != guard.scope or guard.directory != self.directory:
                raise ValueError('Portfolio scope does not match the connector/authorization.')
            from engine.portfolio_guard import MULTI_SCOPE
            if guard.scope == MULTI_SCOPE and any(type(self.data.get(key)) is not int or self.data[key]!=value for key,value in
                    [('maximum_positions_per_symbol',guard.per_symbol),('maximum_open_positions_account_wide',guard.total_limit),
                     ('maximum_combined_risk_percent',guard.combined_percent)]):
                raise ValueError('Two-position authority has invalid frozen position/risk limits.')
            session = getattr(self, 'session_id', None)
            skip = self.directory/'sessions'/session/'demo_test_state.json' if session else None
            guard.inventory(connector, connector.terminal_api.positions_get(), connector.terminal_api.orders_get(),
                symbol=self.data['symbol'], opening=facts is not None, skip_path=skip)
            if facts is not None:
                guard.budget(connector, self.data['symbol'], facts['protection'])
        if self.usage_path.exists():
            usage = json.loads(self.usage_path.read_text(encoding='utf-8'))
            stop = self.directory / 'monitor/stop.json'
            if stop.exists() and json.loads(stop.read_text(encoding='utf-8')).get('run_id') == usage['monitor_run_id']:
                raise ValueError('Stop requested before submission; no order will be sent.')
        if facts is not None:
            plan = facts.get('protection') or {}
            if (facts['account_identity'] != identity or facts['quote']['symbol'] != self.data['symbol'] or
                plan.get('mode') != MODE or plan.get('policy') != asdict(self.policy)):
                raise ValueError('Candidate does not match the authorized account/symbol/strategy policy.')
            if self.policy.risk_mode == 'minimum_lot_rescue_v1':
                from engine.conditional_risk import validate_plan
                validate_plan(plan, self.policy, info=connector.terminal_api.symbol_info(self.data['symbol']), replay=True)
            required_ratio = 2
            if self.policy.target_mode in ('adaptive_pullback_v1', 'adaptive_pullback_v2', 'gold_intraday_v1'):
                gold = self.policy.target_mode == 'gold_intraday_v1'
                extended = self.policy.target_mode in ('adaptive_pullback_v2', 'gold_intraday_v1')
                allowed = (1, 2, 3, 4) if extended else (1, 2, 3)
                exit_policy = 'gold_adaptive_1_2_3_4' if gold else 'adaptive_1_2_3_4' if extended else 'adaptive_1_2_3'
                selected = plan.get('selected_reward_r')
                context = (plan.get('strategy_intent') or {}).get('adaptive_context')
                if (type(selected) is not int or selected not in allowed or
                    plan.get('exit_policy') != exit_policy or
                    plan.get('reward_to_risk') != selected or not isinstance(context, dict) or not context or
                    not isinstance(plan.get('adaptive_target_evidence'), dict) or not plan['adaptive_target_evidence']):
                    raise ValueError('Adaptive candidate lacks the authorized target branch and source evidence.')
                if extended and (context.get('target_mode') != self.policy.target_mode or
                                 context.get('research_spec_sha256') != plan.get('research_spec_sha256') or
                                 not isinstance(plan.get('research_spec_sha256'), str) or
                                 len(plan['research_spec_sha256']) != 64):
                    raise ValueError('Extended target candidate has mismatched version/spec evidence.')
                required_ratio = selected
            ratio = plan.get('planned_reward_to_risk')
            if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio):
                raise ValueError('Candidate requires a finite verified reward/risk ratio.')
            cap = self.data['max_risk_usd']
            if self.DYNAMIC_RISK:
                account = connector.terminal_api.account_info()
                connector.verify_demo_account()
                if account is None or (account.login, account.server) != (identity['login'], identity['server']):
                    raise ValueError('Cannot verify fresh funds for sequential entry risk.')
                _, _, current_budget = risk_budget(dict(balance=account.balance, equity=account.equity,
                                                        currency=account.currency), self.policy)
                if self.policy.risk_mode == 'minimum_lot_rescue_v1' and plan['risk_percent'] == 1:
                    current_budget = (current_budget/2).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
                cap = float(current_budget)
            if (not facts['ready'] or (cap is not None and
                (plan['risk_budget'] > cap + 1e-9 or plan['planned_loss_with_reserves'] > cap + 1e-9)) or
                ratio + 1e-9 < required_ratio or facts['volume_lots'] > self.policy.max_lots or
                facts['request'].get('sl') != plan['stop_loss'] or facts['request'].get('tp') != plan['take_profit']):
                raise ValueError('Candidate exceeds the authorized risk/volume/ratio or protection scope.')
        # Native account verification itself can take time. Do not carry an
        # earlier time check through a blocking terminal call past the deadline.
        if utc_now() >= self.expires or self.revoked_path.exists():
            raise ValueError('Authorization expired or was revoked during final verification.')

    def reserve(self, connector: Any, *, preview_id: str, session_id: str, run_id: str) -> None:
        self.validate(connector)
        _identifier(run_id)
        try:
            _exclusive_json(self.usage_path, dict(authorization_id=self.authorization_id, status='reserved',
                            reserved_at_utc=utc_now().isoformat(), preview_id=preview_id,
                            session_id=session_id, monitor_run_id=run_id))
        except FileExistsError as exc:
            raise ValueError('One-entry authorization already consumed; no retry.') from exc

    def finish(self, status: str, **details: Any) -> None:
        usage = json.loads(self.usage_path.read_text(encoding='utf-8'))
        usage.update(status=status, updated_at_utc=utc_now().isoformat(), **details)
        atomic_json(self.usage_path, usage)

    def summary(self) -> dict:
        return dict(authorization_id=self.authorization_id, expires_at_utc=self.expires.isoformat(),
                    symbol=self.data['symbol'], max_entry_attempts=1, max_risk_usd=self.data['max_risk_usd'],
                    consumed=self.usage_path.exists(), revoked=self.revoked_path.exists())


def load_entry_authorization(directory: Path, authorization_id: str, *, session_id: str,
                             decision_time_utc: str) -> DemoEntryAuthorization:
    """Dispatch by saved authority scope; never reinterpret a one-entry approval."""
    identifier = _identifier(authorization_id)
    data = json.loads((Path(directory) / 'authorizations' / f'{identifier}.json').read_text(encoding='utf-8'))
    if data.get('scope') == 'sequential_demo_strategy_entries':
        from engine.demo_series import DemoSeriesAuthorization
        return DemoSeriesAuthorization(directory, identifier, session_id=session_id, decision_time_utc=decision_time_utc)
    return DemoEntryAuthorization(directory, identifier)
