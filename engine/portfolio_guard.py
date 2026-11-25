"""Explicit, frozen Gold/EURUSD DEMO position and combined-risk scopes.

Default connectors retain the account-wide one-position rule. The combined
controller supplies this guard, and its authorizations record the same scope.
"""
from __future__ import annotations
from decimal import Decimal, ROUND_FLOOR
import json
import math
from pathlib import Path
import uuid

SCOPE = 'gold_eurusd_one_each_v1'
MULTI_SCOPE = 'gold_eurusd_two_each_8pct_v1'
LIMITS = {SCOPE: (1, 2, 2), MULTI_SCOPE: (2, 4, 8)}
SYMBOLS = {'XAUUSDm':'gold_intraday_v1', 'EURUSDm':'adaptive_pullback_v2'}
MAGIC = 82611013


def get_guard(connector):
    value = getattr(connector, 'portfolio_guard', None)
    return value if isinstance(value, TwoInstrumentGuard) else None


class TwoInstrumentGuard:
    def __init__(self, directory, *, scope=SCOPE):
        if scope not in LIMITS:
            raise ValueError('Unreviewed portfolio scope.')
        self.directory = Path(directory).resolve()
        self.scope = scope
        self.per_symbol, self.total_limit, self.combined_percent = LIMITS[scope]
        self.authorizations = {}

    def register(self, symbol, authorization):
        if symbol not in SYMBOLS or symbol in self.authorizations:
            raise ValueError('Portfolio authority must be unique per approved instrument.')
        if authorization.data.get('portfolio_scope') != self.scope:
            raise ValueError('Portfolio authority differs from the explicit position/risk scope.')
        self.authorizations[symbol] = authorization.authorization_id

    def paths(self):
        return [self.directory/'demo_test_state.json', *sorted((self.directory/'sessions').glob('*/demo_test_state.json'))]

    def state_for(self, connector, position):
        matching = []
        for path in self.paths():
            if not path.is_file():continue
            state=json.loads(path.read_text(encoding='utf-8'))
            if state.get('position_id') == int(position.identifier) and state.get('position_ticket') == int(position.ticket):
                matching.append((path,state))
        if len(matching)!=1:
            raise ValueError('Portfolio position lacks one unique verified local execution record.')
        path,state=matching[0]
        symbol=position.symbol
        identity=dict(zip(('login','server'),connector.account_identity))
        plan=state.get('protection') or {}
        policy=plan.get('policy') or {}
        conditional = policy.get('risk_mode') == 'minimum_lot_rescue_v1'
        if (not conditional and policy.get('risk_mode','fixed') != 'fixed' or
            policy.get('risk_percent') != (2 if conditional else 1)):
            raise ValueError('Portfolio position has an unapproved risk sizing mode or ceiling.')
        if any(type(plan.get(k)) not in (int,float) or not math.isfinite(plan[k]) or plan[k]<=0
               for k in ('planned_loss_with_reserves','risk_budget','tick_size','risk_basis_amount')):
            raise ValueError('Portfolio position has invalid frozen risk or tick values.')
        original_cap=(Decimal(str(plan['risk_basis_amount']))*Decimal('.02' if conditional else '.01')).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
        if Decimal(str(plan['risk_budget']))>original_cap:
            raise ValueError('Portfolio position frozen budget exceeds its original approved risk basis.')
        authorization=(state.get('session_authorization') or {}).get('authorization_id')
        expected=self.authorizations.get(symbol)
        if (state.get('status')!='open_verified' or state.get('symbol')!=symbol or state.get('account_identity')!=identity or
            not expected or authorization!=expected or uuid.UUID(expected).hex!=expected or
            policy.get('target_mode')!=SYMBOLS[symbol] or policy.get('max_lots')!=.1 or
            not state.get('entry_verified_deals') or plan.get('planned_loss_with_reserves',math.inf)>plan.get('risk_budget',-1)):
            raise ValueError('Portfolio position identity, entry proof, authority or risk policy is invalid.')
        auth=json.loads((self.directory/'authorizations'/f'{expected}.json').read_text(encoding='utf-8'))
        if auth.get('portfolio_scope')!=self.scope or auth.get('account_identity')!=identity or auth.get('symbol')!=symbol:
            raise ValueError('Portfolio position authorization does not match its scope/account/symbol.')
        if self.scope==MULTI_SCOPE and any(type(auth.get(k)) is not int or auth[k]!=v for k,v in
            [('maximum_positions_per_symbol',self.per_symbol),('maximum_open_positions_account_wide',self.total_limit),
             ('maximum_combined_risk_percent',self.combined_percent)]):
            raise ValueError('Existing position authority differs from the expanded frozen limits.')
        if conditional:
            if auth.get('policy') != policy:
                raise ValueError('Conditional portfolio policy differs from its exact authorization.')
            from engine.conditional_risk import validate_plan
            validate_plan(plan, info=connector.terminal_api.symbol_info(symbol), replay=True)
        if (int(position.magic)!=MAGIC or int(position.type)!=state.get('position_type') or
            not math.isclose(float(position.volume),state.get('executed_volume',-1),rel_tol=0,abs_tol=1e-9) or
            any(not math.isclose(float(getattr(position,k,0)),state['request'][k],rel_tol=0,
                abs_tol=plan['tick_size']*1e-5) for k in ('sl','tp'))):
            raise ValueError('Portfolio position volume, direction or broker SL/TP changed.')
        return path,state

    def inventory(self, connector, positions, orders, *, symbol=None, opening=False,
                  skip_path=None, owned_position_id=None):
        account=connector.verify_demo_account()
        if (connector.account_identity != (464192812,'Exness-MT5Trial17') or
            account.get('broker')!='Exness Technologies Ltd' or account.get('currency')!='USD'):
            raise ValueError('Portfolio execution requires the exact pinned DEMO account.')
        if positions is None or orders is None or orders:
            raise ValueError('Portfolio inventory unknown or pending orders exist.')
        if self.scope == MULTI_SCOPE:
            native=connector.terminal_api.account_info()
            if native is None or type(getattr(native,'margin_mode',None)) is not int or native.margin_mode != 2:
                raise ValueError('Two positions per market require a verified hedging DEMO account.')
        names=[getattr(p,'symbol',None) for p in positions]
        if len(positions)>self.total_limit or any(names.count(s)>self.per_symbol for s in SYMBOLS) or any(s not in SYMBOLS for s in names):
            raise ValueError('Account or per-instrument position limit exceeded.')
        if opening and (len(positions)>=self.total_limit or names.count(symbol)>=self.per_symbol):
            raise ValueError('This instrument already has a position or account has reached its permitted limit.' if self.scope==SCOPE else
                             'This instrument or account already has its permitted positions.')
        keys=[(int(p.ticket),int(p.identifier)) for p in positions]
        if any(len({key[i] for key in keys})!=len(keys) for i in (0,1)):
            raise ValueError('Duplicate native position identities; inventory cannot be verified.')
        verified=set()
        for position in positions:
            if owned_position_id is not None and int(position.identifier)==owned_position_id:
                continue
            path,_=self.state_for(connector,position)
            verified.add(path.resolve())
        for path in self.paths():
            if not path.is_file() or skip_path is not None and path.resolve()==Path(skip_path).resolve():continue
            state=json.loads(path.read_text(encoding='utf-8'))
            if state.get('status')!='closed_verified' and path.resolve() not in verified:
                raise ValueError('Unresolved/uncertain portfolio execution requires actual exit proof; no new send.')

    def budget(self, connector, symbol, plan, *, include_candidate=True):
        api=connector.terminal_api
        positions,orders=api.positions_get(),api.orders_get()
        # The caller validates inventory with its own send-reservation excluded.
        if positions is None or orders is None or orders:
            raise ValueError('Cannot verify portfolio risk inventory.')
        account=api.account_info()
        connector.verify_demo_account()
        if account is None or (account.login,account.server)!=connector.account_identity:
            raise ValueError('Portfolio risk account cannot be verified.')
        basis=min(Decimal(str(account.balance)),Decimal(str(account.equity)))
        if not basis.is_finite() or basis<=0:
            raise ValueError('Portfolio risk basis is invalid.')
        cap=(basis*Decimal(str(self.combined_percent))/100).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
        candidate=plan.get('planned_loss_with_reserves')
        if include_candidate and (type(candidate) not in (int,float) or not math.isfinite(candidate) or candidate<=0):
            raise ValueError('Candidate planned risk is invalid.')
        risk=Decimal(str(candidate)) if include_candidate else Decimal(0)
        for position in positions:
            _,state=self.state_for(connector,position)
            frozen=state['protection']['planned_loss_with_reserves']
            filled=(state.get('protection_at_fill') or {}).get('planned_loss_with_reserves',frozen)
            if isinstance(filled,bool) or not isinstance(filled,(int,float)) or not math.isfinite(filled) or filled<=0:
                raise ValueError('Existing position actual fill risk is unverified.')
            risk+=Decimal(str(max(frozen,filled)))
        if not basis.is_finite() or basis<=0 or risk>cap:
            raise ValueError(f'Combined planned risk exceeds {self.combined_percent}% of current lower balance/equity.')
        return float(risk),float(cap)
