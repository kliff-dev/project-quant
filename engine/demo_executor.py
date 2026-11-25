"""Single authorized MT5 DEMO execution test; never automatically retries orders."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from engine.execution_audit import append_event, atomic_json
from engine.demo_authorization import DemoEntryAuthorization, load_entry_authorization
from engine.mt5_connector import MT5Connector
from engine.protection import RiskProtection, build_buy_protection, evaluate_buy_levels
from engine.strategy_orders import MODE, MAX_STRATEGY_LOTS, StrategyIntent, StrategyPolicy, build_strategy_protection, evaluate_strategy_levels, validate_intent

PLACE_CONFIRMATION = "PLACE DEMO TEST ORDER"
CLOSE_CONFIRMATION = "CLOSE DEMO TEST POSITION"
MAX_TEST_LOTS = .01
MAGIC = 82611013
# MQL5 symbol flags are bit masks, distinct from ORDER_FILLING_* enum values.
# These flags are documented by MetaQuotes but absent from the Python package.
SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC, SYMBOL_ORDER_MARKET = 1, 2, 1


class DemoExecutionDisabledError(PermissionError):
    """Required confirmation or execution conditions are absent."""


class DemoVerificationError(RuntimeError):
    """An attempted order's actual outcome is not fully verified."""


def place_demo_test_order(confirmation: str) -> None:
    """Legacy entry point fails closed; a bound, reviewed preview is required."""
    if confirmation != PLACE_CONFIRMATION:
        raise DemoExecutionDisabledError("Explicit demo-order confirmation has not been supplied.")
    raise DemoExecutionDisabledError("Use DemoExecutor.open_test with a saved reviewed preview; this context-free entry point is disabled.")


def _fields(value: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(value, name, None) for name in names}


class DemoExecutor:
    """One test per state directory, including crashes and uncertain outcomes.

    A durable send reservation and exclusive lock prevent accidental duplicates
    across processes. Existing state is never reset automatically. Preview and
    verification are read-only. Manual entries/exits require separate phrases;
    a session-authorized entry instead consumes an explicit one-use approval.
    """

    def __init__(self, connector: MT5Connector, directory: str | Path, *, max_spread_pips: float = 2.0,
                 protection: RiskProtection | None = None, session_id: str | None = None,
                 strategy_policy: StrategyPolicy | None = None, strategy_intent: StrategyIntent | None = None) -> None:
        if not math.isfinite(max_spread_pips) or not 0 < max_spread_pips <= 2:
            raise ValueError("Maximum spread must be in (0, 2] pips for this test.")
        self.connector, self.api = connector, connector.terminal_api
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        if session_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", session_id):
            raise ValueError("Session ID must contain 1–48 letters, digits, underscores, or hyphens.")
        self.session_id = session_id
        self.work_dir = self.directory / "sessions" / session_id if session_id is not None else self.directory
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work_dir / "demo_test_state.json"
        self.preview_path = self.work_dir / "demo_order_preview.json"
        self.close_preview_path = self.work_dir / "demo_close_preview.json"
        # All sessions under this log root share the same exclusive execution lock.
        self.lock_path = self.directory / "demo_execution.lock"
        self.max_spread_pips = max_spread_pips
        self.protection = protection
        self.strategy_policy, self.strategy_intent = strategy_policy, strategy_intent
        if self.protection is None:
            saved_path = self.state_path if self.state_path.exists() else self.preview_path
            if saved_path.is_file():
                saved = self._read(saved_path).get("protection")
                if saved and saved.get('mode') == MODE:
                    self.strategy_policy = self.strategy_policy or StrategyPolicy(**saved['policy'])
                    self.strategy_intent = self.strategy_intent or StrategyIntent(**saved['strategy_intent'])
                elif saved and self.strategy_policy is None:
                    self.protection = RiskProtection(saved["risk_percent"], saved["reward_to_risk"])
        if (self.strategy_policy is None) != (self.strategy_intent is None) or self.protection and self.strategy_policy:
            raise ValueError('Strategy mode requires a policy and intent, separate from legacy protection.')

    @contextmanager
    def _lock(self) -> Iterator[None]:
        try:
            handle = self.lock_path.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise DemoExecutionDisabledError("Execution lock exists. Inspect the previous process/outcome; do not retry an order.") from exc
        try:
            with handle:
                handle.write(datetime.now(timezone.utc).isoformat())
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _identity(self) -> dict[str, Any]:
        login, server = self.connector.account_identity
        return {"login": login, "server": server}

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise DemoExecutionDisabledError(f"Required local state is missing: {path.name}.")
        return json.loads(path.read_text(encoding="utf-8"))

    def _inventory(self) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        positions, orders = self.api.positions_get(), self.api.orders_get()
        if positions is None or orders is None:
            raise DemoExecutionDisabledError("Existing positions/orders could not be verified.")
        return tuple(positions), tuple(orders)

    def _other_unresolved_attempts(self) -> bool:
        paths = [self.directory / "demo_test_state.json", *sorted((self.directory / "sessions").glob("*/demo_test_state.json"))]
        for path in paths:
            if path != self.state_path and path.is_file():
                if self._read(path).get("status") != "closed_verified":
                    return True
        return False

    def _filling(self, info: Any) -> tuple[int, str]:
        mask, execution = int(info.filling_mode), int(info.trade_exemode)
        if execution in (self.api.SYMBOL_TRADE_EXECUTION_REQUEST, self.api.SYMBOL_TRADE_EXECUTION_INSTANT) or mask & SYMBOL_FILLING_FOK:
            return self.api.ORDER_FILLING_FOK, "FOK (full volume or no fill)"
        if mask & SYMBOL_FILLING_IOC:
            return self.api.ORDER_FILLING_IOC, "IOC (partial fill possible; remainder cancelled)"
        raise DemoExecutionDisabledError("No supported FOK/IOC market-order policy. No substitution/retry.")

    def _facts(self, *, state: dict[str, Any] | None = None, owned_lock: bool = False) -> dict[str, Any]:
        """Re-read DEMO, positions, permissions, quotes, volume rules, and costs."""
        closing = state is not None
        snapshot = self.connector.inspect(state["symbol"] if state else None)
        identity = self._identity()
        account = self.api.account_info()
        self.connector.verify_demo_account()
        if account is None or account.login != identity["login"] or account.server != identity["server"] or self._identity() != identity:
            raise DemoExecutionDisabledError("Account changed during preflight.")
        if state and identity != state["account_identity"]:
            raise DemoExecutionDisabledError("This test belongs to another account/server.")
        quote = snapshot["quote"]
        info = self.api.symbol_info(quote["symbol"])
        if info is None:
            raise DemoExecutionDisabledError("Symbol properties unavailable.")
        positions, orders = self._inventory()
        from engine.portfolio_guard import get_guard
        portfolio = get_guard(self.connector)
        if portfolio is not None:
            portfolio.inventory(self.connector, positions, orders, symbol=quote['symbol'], opening=not closing,
                skip_path=self.state_path, owned_position_id=state.get('position_id') if state else None)
        blockers: list[str] = []
        if snapshot["permissions"]["terminal_trade_allowed"] is not True:
            blockers.append("MT5 Algo Trading permission is off or unknown (terminal trade_allowed is not True).")
        if snapshot["permissions"]["external_python_trading_disabled"] is not False:
            blockers.append("External Python trading is disabled or unverified.")
        if snapshot["account"]["account_trade_allowed"] is not True or snapshot["account"]["account_expert_allowed"] is not True:
            blockers.append("Account trading/expert permissions are not both enabled.")
        if not -5 <= quote["age_seconds"] <= 120:
            blockers.append("Quote timestamp is outside -5 to 120 seconds of the PC clock. Check synchronization.")
        gold = self.strategy_policy is not None and self.strategy_policy.target_mode == 'gold_intraday_v1'
        if gold and quote['spread_price'] > 1:
            blockers.append('Gold spread exceeds $1 per ounce; relative spread/stop filter also applies.')
        if not gold and quote["spread_pips"] > self.max_spread_pips:
            blockers.append(f"Spread exceeds the {self.max_spread_pips:g}-pip ceiling.")
        if orders:
            blockers.append("Pending orders exist; this isolated test requires none.")
        if not int(getattr(info, "order_mode", 0)) & SYMBOL_ORDER_MARKET:
            blockers.append("Symbol does not advertise market-order support.")
        if closing:
            matches = [p for p in positions if int(p.ticket) == state["position_ticket"] and int(p.identifier) == state["position_id"]]
            if (portfolio is None and len(positions) != 1) or len(matches) != 1:
                raise DemoExecutionDisabledError("Exactly the verified test position must be open; inspect other/missing positions.")
            position = matches[0]
            if position.symbol != state["symbol"] or int(position.magic) != MAGIC or int(position.type) != state["position_type"] or not math.isclose(float(position.volume), state["executed_volume"], rel_tol=0, abs_tol=1e-9):
                raise DemoExecutionDisabledError("The verified test position changed; inspect before closing.")
            volume = float(position.volume)
            side = "sell" if position.type == self.api.POSITION_TYPE_BUY else "buy"
            if info.trade_mode == self.api.SYMBOL_TRADE_MODE_DISABLED:
                blockers.append("Symbol trading is disabled, including closing.")
        else:
            if positions and portfolio is None:
                blockers.append("Open positions exist; no additional/netting exposure is permitted.")
            if self.state_path.exists():
                blockers.append("A test attempt is already recorded; a second entry is disabled.")
            if portfolio is None and self._other_unresolved_attempts():
                blockers.append("Another session has an unresolved execution attempt; inspect it before any new entry.")
            if self.lock_path.exists() and not owned_lock:
                blockers.append("An execution process/lock already exists.")
            minimum, maximum, step = [Decimal(str(getattr(info, key))) for key in ("volume_min", "volume_max", "volume_step")]
            if not all(v.is_finite() and v > 0 for v in (minimum, maximum, step)) or maximum < minimum:
                raise DemoExecutionDisabledError("Invalid broker volume rules.")
            entry_cap = Decimal(str(self.strategy_policy.max_lots)) if self.strategy_policy else Decimal('0.01')
            if minimum > entry_cap:
                raise DemoExecutionDisabledError(f"Broker minimum {minimum} lots exceeds the {entry_cap}-lot limit.")
            if minimum % step != 0:
                raise DemoExecutionDisabledError("Broker minimum is not aligned with its volume step.")
            volume, side = float(minimum), self.strategy_intent.side if self.strategy_intent else 'buy'
            allowed_direction = self.api.SYMBOL_TRADE_MODE_LONGONLY if side == 'buy' else getattr(self.api,'SYMBOL_TRADE_MODE_SHORTONLY',2)
            if info.trade_mode not in (self.api.SYMBOL_TRADE_MODE_FULL, allowed_direction):
                blockers.append(f"Symbol does not permit opening a {side.upper()} position.")
        strategy_mode = bool(self.strategy_policy) if not closing else (state.get('protection') or {}).get('mode') == MODE
        cap = MAX_STRATEGY_LOTS if strategy_mode else MAX_TEST_LOTS
        if not 0 < volume <= cap:
            raise DemoExecutionDisabledError(f"Volume exceeds the {cap}-lot limit.")
        filling, filling_name = self._filling(info)
        order_type = self.api.ORDER_TYPE_BUY if side == "buy" else self.api.ORDER_TYPE_SELL
        reference = float(quote["ask"] if side == "buy" else quote["bid"])
        funds = _fields(account, ("balance", "equity", "margin_free", "leverage"))
        funds["currency"] = snapshot["account"]["currency"]
        if any(not isinstance(funds[k], (int, float)) or not math.isfinite(funds[k]) for k in ("balance", "equity", "margin_free")):
            raise DemoExecutionDisabledError("Account funds are unavailable/invalid.")
        protection_plan = state.get('protection') if closing else None
        if not closing and self.strategy_policy:
            protection_plan = build_strategy_protection(self.api,info,quote,funds,self.strategy_intent,self.strategy_policy)
            volume = protection_plan['volume_lots']
            if portfolio is not None:
                portfolio.budget(self.connector, quote['symbol'], protection_plan)
        elif not closing and self.protection is not None:
            protection_plan = build_buy_protection(self.api,info,quote,volume,funds,self.protection)
        if not 0 < volume <= cap:
            raise DemoExecutionDisabledError('Sized volume exceeds the mode-specific limit.')
        margin = self.api.order_calc_margin(order_type, quote["symbol"], volume, reference)
        spread_pnl = self.api.order_calc_profit(order_type, quote["symbol"], volume, reference, quote["bid"] if side == "buy" else quote["ask"])
        if margin is None or not math.isfinite(margin) or margin < 0:
            blockers.append("Broker margin estimate is unavailable.")
            margin = None
        if not closing and margin is not None and (funds["equity"] <= 0 or margin > funds["margin_free"]):
            blockers.append("Insufficient equity/free margin for the minimum order.")
        if spread_pnl is None or not math.isfinite(spread_pnl):
            spread_pnl = None
        request = {"action": self.api.TRADE_ACTION_DEAL, "symbol": quote["symbol"], "volume": volume,
                   "type": order_type, "deviation": 10, "magic": MAGIC,
                   "comment": "PQ demo test close" if closing else "PQ demo test open",
                   "type_time": self.api.ORDER_TIME_GTC, "type_filling": filling}
        if info.trade_exemode != self.api.SYMBOL_TRADE_EXECUTION_MARKET:
            request["price"] = reference
        if closing:
            request["position"] = state["position_ticket"]
        if not closing and protection_plan:
            request.update(sl=protection_plan["stop_loss"], tp=protection_plan["take_profit"])
        return {"account": snapshot["account"], "account_identity": identity, "funds": funds, "quote": quote,
                "symbol_properties": _fields(info, ("volume_min", "volume_max", "volume_step", "filling_mode", "trade_exemode", "trade_mode", "trade_contract_size", "point", "trade_tick_size", "trade_stops_level", "trade_freeze_level")),
                "side": side, "volume_lots": volume, "reference_price": reference,
                "filling_policy": filling_name, "estimated_margin": margin,
                "estimated_spread_cost": -spread_pnl if spread_pnl is not None else None,
                "commission_estimate": "Unknown before execution; verify actual deal commission/fee/swap.",
                "market_session": "Exact broker schedule unavailable through Python; fresh quotes and order_check cannot guarantee acceptance.",
                "execution_assumption": ("One confirmed DEMO strategy candidate with stop-first sizing. Profitability is untested; no continuous trading or automatic time exit."
                                         if strategy_mode else "One protected DEMO BUY test; direction is arbitrary, not a strategy signal. Broker SL/TP may close it automatically; manual closing requires separate confirmation."
                                         if protection_plan else "One market-order connectivity test. BUY direction is arbitrary. No stop-loss/take-profit; separate confirmation is required to close."),
                "slippage_limit": "Deviation=10 points is not a guaranteed limit under Market Execution.",
                "session_id": self.session_id, "protection": protection_plan,
                "approval_scope": ("Frozen strategy stop/context and volume; fresh TP may refresh within the same policy and reviewed risk budget."
                                   if strategy_mode else "Fresh market price and SL/TP may refresh together; size, risk policy, and reviewed maximum risk budget cannot increase."),
                "max_spread_pips": self.max_spread_pips, "request": request, "blockers": blockers, "ready": not blockers}

    def prepare_entry(self) -> dict[str, Any]:
        """Save a draft. Protected drafts also run order_check; no draft sends."""
        with self._lock():
            facts = self._facts(owned_lock=True)
            if facts["protection"] and not facts["blockers"]:
                checked = self.api.order_check(facts["request"])
                facts["broker_check"] = _fields(checked, ("retcode", "comment", "margin", "margin_free")) if checked is not None else {"last_error": self.api.last_error()}
                if checked is None or int(checked.retcode) != 0:
                    facts["blockers"].append(f"Broker rejected protected draft: {facts['broker_check']}")
                    facts["ready"] = False
            preview = {"preview_id": uuid.uuid4().hex, "created_at_utc": datetime.now(timezone.utc).isoformat(),
                       "confirmation": PLACE_CONFIRMATION, "expires_after_seconds": 300, **facts}
            atomic_json(self.preview_path, preview)
            append_event(self.directory, "demo_order_preview", "ready" if preview["ready"] else "blocked",
                         account=facts["account"], quote=facts["quote"], volume=facts["volume_lots"],
                         price=facts["reference_price"], message="; ".join(facts["blockers"]) or "Draft only; no order submitted.")
            return preview

    def prepare_close(self) -> dict[str, Any]:
        with self._lock():
            state = self._read(self.state_path)
            if state["status"] != "open_verified":
                raise DemoExecutionDisabledError("Closing requires an open position verified by this executor.")
            facts = self._facts(state=state, owned_lock=True)
            preview = {"preview_id": uuid.uuid4().hex, "created_at_utc": datetime.now(timezone.utc).isoformat(),
                       "confirmation": CLOSE_CONFIRMATION, "expires_after_seconds": 300,
                       "position_ticket": state["position_ticket"], **facts}
            atomic_json(self.close_preview_path, preview)
            return preview

    def _approved(self, path: Path, preview_id: str, confirmation: str, expected: str) -> dict[str, Any]:
        if confirmation != expected:
            raise DemoExecutionDisabledError(f"Exact typed confirmation required: {expected}")
        return self._checked_preview(path, preview_id)

    def _checked_preview(self, path: Path, preview_id: str) -> dict[str, Any]:
        preview = self._read(path)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(preview["created_at_utc"])).total_seconds()
        if preview["preview_id"] != preview_id or not 0 <= age <= 300:
            raise DemoExecutionDisabledError("Preview identity/age is invalid. Generate and review a fresh preview.")
        if not preview["ready"]:
            raise DemoExecutionDisabledError("The reviewed preview was blocked. Resolve blockers and create a fresh preview.")
        return preview

    def _match(self, approved: dict[str, Any], fresh: dict[str, Any]) -> None:
        if fresh["blockers"]:
            raise DemoExecutionDisabledError("; ".join(fresh["blockers"]))
        for key in ("account_identity", "side", "volume_lots", "filling_policy", "max_spread_pips"):
            if approved[key] != fresh[key]:
                raise DemoExecutionDisabledError(f"Approved condition changed: {key}. Review another preview.")
        if approved["quote"]["symbol"] != fresh["quote"]["symbol"]:
            raise DemoExecutionDisabledError("Approved broker symbol changed.")
        planned, current = approved.get("protection"), fresh.get("protection")
        if bool(planned) != bool(current):
            raise DemoExecutionDisabledError("Reviewed SL/TP protection policy changed.")
        if planned:
            if planned.get('mode') != current.get('mode'):
                raise DemoExecutionDisabledError('Reviewed sizing mode changed.')
            if planned.get('mode') == MODE:
                for key in ('policy','strategy_intent','stop_loss'):
                    if planned[key] != current[key]:
                        raise DemoExecutionDisabledError(f'Reviewed strategy condition changed: {key}.')
                if planned['policy'].get('target_mode') in ('adaptive_pullback_v1', 'adaptive_pullback_v2', 'gold_intraday_v1'):
                    for key in ('selected_reward_r', 'exit_policy', 'adaptive_target_evidence'):
                        if key not in planned or planned[key] != current.get(key):
                            raise DemoExecutionDisabledError(f'Reviewed adaptive target condition changed: {key}.')
                if current['planned_loss_with_reserves'] > planned['risk_budget']+1e-9:
                    raise DemoExecutionDisabledError('Fresh reserved risk exceeds the reviewed risk budget.')
            if any(planned[key] != current[key] for key in ("risk_percent", "reward_to_risk")):
                raise DemoExecutionDisabledError("Reviewed risk/target policy changed.")
            if current["risk_budget"] > planned["risk_budget"] + 1e-9 or current["estimated_loss_at_sl"] > planned["risk_budget"] + 1e-9:
                raise DemoExecutionDisabledError("Fresh protection exceeds the reviewed risk budget.")

    def _check_and_guard(self, facts: dict[str, Any], *, state: dict[str, Any] | None = None) -> dict[str, Any]:
        check = self.api.order_check(facts["request"])
        if check is None or int(check.retcode) != 0:
            detail = _fields(check, ("retcode", "comment")) if check is not None else {"last_error": self.api.last_error()}
            append_event(self.directory, "order_check", "rejected", account=facts["account"], quote=facts["quote"], message=json.dumps(detail))
            raise DemoExecutionDisabledError(f"order_check failed: {detail}. No send or automatic retry.")
        # Re-read every guard after order_check, keeping exactly the checked
        # request. Non-market execution uses its checked price/deviation.
        guarded = self._facts(state=state, owned_lock=True)
        self._match(facts, guarded)
        if state is None and facts.get("protection"):
            # The request already checked by the broker keeps its absolute levels.
            # A later quote must not make those levels exceed the approved budget.
            plan = facts["protection"]
            strategy = plan.get('mode') == MODE
            reference = guarded['quote']['ask' if facts['side']=='buy' else 'bid']
            actual = (evaluate_strategy_levels(self.api,facts['quote']['symbol'],facts['volume_lots'],reference,facts['request']['sl'],facts['request']['tp'],plan)
                      if strategy else evaluate_buy_levels(self.api,facts['quote']['symbol'],facts['volume_lots'],reference,facts['request']['sl'],facts['request']['tp']))
            minimum = guarded["symbol_properties"]["trade_stops_level"] * guarded["symbol_properties"]["point"]
            trigger, sl, tp = guarded['quote']['bid' if facts['side']=='buy' else 'ask'], facts['request']['sl'], facts['request']['tp']
            direction=1 if facts['side']=='buy' else -1
            if not direction*(trigger-sl)>0 or not direction*(tp-trigger)>0 or direction*(trigger-sl)+1e-12<minimum or direction*(tp-trigger)+1e-12<minimum:
                raise DemoExecutionDisabledError("Checked SL/TP no longer satisfies the fresh broker stop distance. No order sent.")
            risk=actual['planned_loss_with_reserves'] if strategy else actual['estimated_loss_at_sl']
            if risk > plan["risk_budget"] + 1e-9 or actual["reward_to_risk"] + 1e-9 < plan["reward_to_risk"]:
                raise DemoExecutionDisabledError("Quote moved after order_check; checked SL/TP no longer meets the approved risk/ratio. No order sent.")
        return _fields(check, ("retcode", "comment", "margin", "margin_free"))

    def open_test(self, preview_id: str, confirmation: str) -> dict[str, Any]:
        """Send at most once after exact confirmation and a fresh passing preflight."""
        approved = self._approved(self.preview_path, preview_id, confirmation, PLACE_CONFIRMATION)
        with self._lock():
            facts = self._facts(owned_lock=True)
            self._match(approved, facts)
            check = self._check_and_guard(facts)
            state = {"status": "open_send_started", "account_identity": facts["account_identity"],
                     "account": facts["account"], "symbol": facts["quote"]["symbol"],
                     "requested_volume": facts["volume_lots"], "reference_price": facts["reference_price"],
                     "point": facts["symbol_properties"]["point"], "quote": facts["quote"],
                     "request": facts["request"], "order_check": check,
                     "session_id": self.session_id, "protection": facts.get("protection"),
                     "started_at_utc": datetime.now(timezone.utc).isoformat()}
            return self._send_once(state, facts, closing=False)

    def open_authorized_session(self, preview_id: str, authorization_id: str, *, monitor_run_id: str) -> dict[str, Any]:
        """One unattended strategy entry under a prior, bounded user approval.

        This does not forge a typed per-order confirmation. It uses a separate
        one-use authorization and otherwise retains the same native safeguards.
        """
        if self.strategy_policy is None or self.strategy_intent is None:
            raise DemoExecutionDisabledError('Session authority requires strategy-sized, protected entry.')
        authorization = load_entry_authorization(self.directory, authorization_id, session_id=self.session_id,
                                                  decision_time_utc=self.strategy_intent.decision_time_utc)
        approved = self._checked_preview(self.preview_path, preview_id)
        with self._lock():
            authorization.assert_unused()
            authorization.reserve(self.connector, preview_id=preview_id, session_id=self.session_id, run_id=monitor_run_id)
            try:
                facts = self._facts(owned_lock=True)
                self._match(approved, facts)
                authorization.validate(self.connector, facts)
                check = self._check_and_guard(facts)
                authorization.validate(self.connector, facts)
                state = dict(status='open_send_started', account_identity=facts['account_identity'],
                             account=facts['account'], symbol=facts['quote']['symbol'],
                             requested_volume=facts['volume_lots'], reference_price=facts['reference_price'],
                             point=facts['symbol_properties']['point'], quote=facts['quote'],
                             request=facts['request'], order_check=check, session_id=self.session_id,
                             protection=facts['protection'], started_at_utc=datetime.now(timezone.utc).isoformat(),
                             session_authorization=authorization.summary())
                report = self._send_once(state, facts, closing=False, authorization=authorization)
            except BaseException as exc:
                authorization.finish('consumed_stopped_or_unverified', error=f'{type(exc).__name__}: {exc}')
                raise
            authorization.finish('entry_verified', order_ticket=report['open_result']['order'],
                                 deal_ticket=report['open_result']['deal'], position_ticket=report['position_ticket'])
            return report

    def close_test(self, preview_id: str, confirmation: str) -> dict[str, Any]:
        """Close only the unchanged verified test position, with separate approval."""
        approved = self._approved(self.close_preview_path, preview_id, confirmation, CLOSE_CONFIRMATION)
        with self._lock():
            state = self._read(self.state_path)
            if state["status"] != "open_verified":
                raise DemoExecutionDisabledError("Test is not verified open; inspect it without sending.")
            if approved["position_ticket"] != state["position_ticket"]:
                raise DemoExecutionDisabledError("Approved closing ticket does not match the test position.")
            facts = self._facts(state=state, owned_lock=True)
            self._match(approved, facts)
            check = self._check_and_guard(facts, state=state)
            state.update(status="close_send_started", close_request=facts["request"], close_order_check=check,
                         close_reference_price=facts["reference_price"], close_quote=facts["quote"])
            return self._send_once(state, facts, closing=True)

    def _send_once(self, state: dict[str, Any], facts: dict[str, Any], *, closing: bool,
                   authorization: DemoEntryAuthorization | None = None) -> dict[str, Any]:
        atomic_json(self.state_path, state)
        append_event(self.directory, "close_send_attempt" if closing else "open_send_attempt", state["status"],
                     account=facts["account"], quote=facts["quote"], volume=facts["volume_lots"], price=facts["reference_price"],
                     message="Durable send reservation. Never retry automatically, including timeout/None.")
        # Directly adjacent to the only native send call: no cached demo verdict.
        self.connector.verify_demo_account()
        if self._identity() != state["account_identity"]:
            raise DemoExecutionDisabledError("Account switched immediately before send; reservation retained, no send.")
        if authorization is not None:
            authorization.validate(self.connector, facts)
        if not closing and self.strategy_policy is not None:
            # Account verification/auditing can block. Do not carry an earlier
            # signal-freshness verdict through those calls into a late entry.
            validate_intent(self.strategy_intent)
        try:
            result = self.api.order_send(facts["request"])
        except Exception as exc:
            state.update(status="close_unverified" if closing else "open_unverified",
                         send_error=f"{type(exc).__name__}: {exc}")
            atomic_json(self.state_path, state)
            append_event(self.directory, "send_exception", state["status"], account=facts["account"], quote=facts["quote"], message=state["send_error"])
            raise DemoVerificationError("Send raised an exception; actual outcome is unknown. Reservation retained; do not retry.") from exc
        result_key = "close_result" if closing else "open_result"
        state[result_key] = _fields(result, ("retcode", "order", "deal", "volume", "price", "bid", "ask", "comment", "request_id", "retcode_external")) if result is not None else None
        state["status"] = "close_unverified" if closing else "open_unverified"
        atomic_json(self.state_path, state)
        append_event(self.directory, "close_send_result" if closing else "open_send_result", state["status"],
                     account=facts["account"], quote=facts["quote"],
                     order_ticket=getattr(result, "order", ""), deal_ticket=getattr(result, "deal", ""),
                     volume=getattr(result, "volume", ""), price=getattr(result, "price", ""), message=json.dumps(state[result_key]))
        if result is None or int(result.retcode) != self.api.TRADE_RETCODE_DONE:
            raise DemoVerificationError("Order is rejected, partial, pending, or unknown. Inspect the terminal/state; no automatic retry.")
        return self.verify(closing=closing)

    def verify(self, *, closing: bool = False) -> dict[str, Any]:
        """Read actual order/deals/positions. May be rerun; never sends an order."""
        state = self._read(self.state_path)
        if closing and state.get("protection") and not state.get("close_result") and state["status"] in ("open_verified", "closed_verified"):
            return self._verify_protected_exit(state)
        allowed_states = ("close_send_started", "close_unverified", "closed_verified") if closing else ("open_send_started", "open_unverified", "open_verified")
        if state["status"] not in allowed_states:
            raise DemoVerificationError("Verification phase does not match the recorded attempt. Never downgrade a closing attempt to an open state.")
        self.connector.verify_demo_account()
        if self._identity() != state["account_identity"]:
            raise DemoVerificationError("This test cannot be verified on a different account/server.")
        result = state.get("close_result" if closing else "open_result")
        if not result or not result.get("order") or not result.get("deal") or result.get("retcode") != self.api.TRADE_RETCODE_DONE:
            raise DemoVerificationError("No completed result with order/deal tickets. Inspect manually; do not retry.")
        orders = self.api.history_orders_get(ticket=int(result["order"]))
        deals = self.api.history_deals_get(ticket=int(result["order"]))
        lookup = {"method": "order_ticket"}
        if deals is None:
            # Some terminal/package combinations reject ticket queries with
            # Invalid params. A documented interval query is read-only; filter
            # its records by the exact order/deal/position below. Never resend.
            ticket_error = self.api.last_error()
            start = datetime.fromisoformat(state["started_at_utc"]) - timedelta(minutes=5)
            end = datetime.now(timezone.utc) + timedelta(minutes=1)
            deals = self.api.history_deals_get(start, end)
            lookup = {"method": "time_range_after_ticket_error", "ticket_error": ticket_error,
                      "from_utc": start.isoformat(), "to_utc": end.isoformat()}
        positions = self.api.positions_get()
        if orders is None or deals is None or positions is None:
            raise DemoVerificationError("History/positions unavailable; outcome remains unverified.")
        expected_type = state["close_request"]["type"] if closing else state["request"]["type"]
        entries = (self.api.DEAL_ENTRY_OUT, self.api.DEAL_ENTRY_OUT_BY) if closing else (self.api.DEAL_ENTRY_IN,)
        selected = [d for d in deals if int(d.order) == int(result["order"]) and d.symbol == state["symbol"] and int(d.magic) == MAGIC and int(d.type) == expected_type and int(d.entry) in entries]
        valid_orders = [o for o in orders if int(o.ticket) == int(result["order"]) and o.symbol == state["symbol"] and int(o.magic) == MAGIC and int(o.type) == expected_type and int(o.state) == self.api.ORDER_STATE_FILLED]
        if len(valid_orders) != 1 or not selected or not any(int(d.ticket) == int(result["deal"]) for d in selected):
            raise DemoVerificationError("Result tickets do not reconcile to a filled order and expected actual deals.")
        volume = sum(float(d.volume) for d in selected)
        if not math.isclose(volume, state["requested_volume"], rel_tol=0, abs_tol=1e-9):
            raise DemoVerificationError("Actual volume differs from the requested size; inspect before further action.")
        identifiers = {int(d.position_id) for d in selected}
        if len(identifiers) != 1:
            raise DemoVerificationError("Execution affected multiple position identities.")
        position_id = identifiers.pop()
        weighted_price = sum(float(d.price) * float(d.volume) for d in selected) / volume
        commission, fee, swap = [sum(float(getattr(d, key)) for d in selected) for key in ("commission", "fee", "swap")]
        if closing:
            if position_id != state["position_id"] or any(int(p.identifier) == position_id or int(p.ticket) == state["position_ticket"] for p in positions):
                raise DemoVerificationError("The original test position is not confirmed fully closed.")
            state.update(status="closed_verified", close_executed_price=weighted_price, close_commission=commission, close_fee=fee, close_swap=swap,
                         realized_net_pnl=sum(float(d.profit) for d in selected) + commission + fee + swap + state["entry_commission"] + state["entry_fee"] + state["entry_swap"])
        else:
            matches = [p for p in positions if int(p.identifier) == position_id and p.symbol == state["symbol"] and int(p.magic) == MAGIC]
            from engine.portfolio_guard import get_guard
            portfolio=get_guard(self.connector)
            if portfolio is not None:
                portfolio.inventory(self.connector,positions,(),symbol=state['symbol'],skip_path=self.state_path,owned_position_id=position_id)
            if len(matches) != 1 or (portfolio is None and len(positions) != 1) or int(matches[0].type) != expected_type or not math.isclose(float(matches[0].volume), volume, rel_tol=0, abs_tol=1e-9):
                raise DemoVerificationError("No unique open position matches the actual execution deals.")
            position = matches[0]
            if not math.isclose(float(position.price_open), weighted_price, rel_tol=0, abs_tol=float(state["point"])):
                raise DemoVerificationError("Position entry price does not reconcile to actual deals.")
            if state.get("protection"):
                plan = state["protection"]
                tolerance = plan["tick_size"] * 1e-5
                if not all(math.isclose(float(getattr(position, key, 0)), state["request"][key], rel_tol=0, abs_tol=tolerance) for key in ("sl", "tp")):
                    raise DemoVerificationError("The actual position does not have both approved SL/TP levels. Inspect immediately; no retry or unconfirmed modification.")
                strategy=plan.get('mode') == MODE
                state['protection_at_fill'] = (evaluate_strategy_levels(self.api,state['symbol'],volume,weighted_price,plan['stop_loss'],plan['take_profit'],plan,at_fill=True)
                                               if strategy else evaluate_buy_levels(self.api,state['symbol'],volume,weighted_price,plan['stop_loss'],plan['take_profit']))
                risk=state['protection_at_fill']['planned_loss_with_reserves' if strategy else 'estimated_loss_at_sl']
                state['protection_at_fill']['budget_exceeded_by_fill']=risk>plan['risk_budget']+1e-9
            state.update(status="open_verified", position_id=position_id, position_ticket=int(position.ticket),
                         position_type=int(position.type), executed_volume=volume, executed_price=weighted_price,
                         entry_commission=commission, entry_fee=fee, entry_swap=swap)
        prefix = "close_" if closing else "entry_"
        state[prefix + "deal_history_lookup"] = lookup
        reference = state["close_reference_price"] if closing else state["reference_price"]
        direction = 1 if expected_type == self.api.ORDER_TYPE_BUY else -1
        state[prefix + "slippage_price"] = direction * (weighted_price - reference)
        state[prefix + "slippage_points"] = state[prefix + "slippage_price"] / state["point"]
        state[prefix + "verified_deals"] = [_fields(d, ("ticket", "order", "position_id", "time_msc", "entry", "type", "volume", "price", "profit", "commission", "fee", "swap")) for d in selected]
        state["verified_at_utc"] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.state_path, state)
        append_event(self.directory, "close_verification" if closing else "open_verification", state["status"],
                     account=state["account"], quote=state.get("close_quote" if closing else "quote", {}),
                     order_ticket=result["order"], deal_ticket=result["deal"], position_ticket=state["position_ticket"],
                     volume=volume, price=weighted_price, commission=commission, fee=fee, swap=swap,
                     message="Actual order, deal(s), and position state verified; see demo_test_state.json.")
        return state

    def reconcile_external_close(self) -> dict[str, Any]:
        """Explicit read-only broker reconciliation; preserve all original evidence."""
        with self._lock():
            state=self._read(self.state_path)
            if state.get('status') not in ('open_verified','closed_verified') or state.get('close_result') or not state.get('protection'):
                raise DemoVerificationError('External reconciliation requires a verified protected entry with no attempted executor close.')
            return self._verify_protected_exit(state,allow_desktop=True)

    def _verify_protected_exit(self, state: dict[str, Any], *, allow_desktop: bool = False) -> dict[str, Any]:
        """Reconcile a broker-triggered SL/TP exit without submitting any order."""
        self.connector.verify_demo_account()
        if self._identity() != state["account_identity"]:
            raise DemoVerificationError("Protected exit belongs to another account/server.")
        positions = self.api.positions_get()
        if positions is None:
            raise DemoVerificationError("Position inventory unavailable; cannot verify protected exit.")
        if any(int(p.identifier) == state["position_id"] or int(p.ticket) == state["position_ticket"] for p in positions):
            raise DemoVerificationError("Protected position is still open; SL/TP exit has not occurred.")
        start = datetime.fromisoformat(state["started_at_utc"]) - timedelta(minutes=5)
        end = datetime.now(timezone.utc) + timedelta(minutes=1)
        deals = self.api.history_deals_get(start, end)
        if deals is None:
            raise DemoVerificationError("Deal history unavailable; absence of a position alone is insufficient proof.")
        expected = self.api.ORDER_TYPE_SELL if state['position_type']==self.api.POSITION_TYPE_BUY else self.api.ORDER_TYPE_BUY
        selected = [d for d in deals if int(d.position_id) == state["position_id"] and d.symbol == state["symbol"]
                    and int(d.entry) == self.api.DEAL_ENTRY_OUT and int(d.type) == expected]
        reasons = {self.api.DEAL_REASON_SL: "stop_loss", self.api.DEAL_REASON_TP: "take_profit"}
        if allow_desktop:
            reasons[getattr(self.api,'DEAL_REASON_CLIENT',0)]='desktop_terminal'
        if not selected or any(int(d.reason) not in reasons for d in selected):
            raise DemoVerificationError("Exit is missing or was not broker-reported as SL/TP; inspect actual deals.")
        volume = sum(float(d.volume) for d in selected)
        if not math.isclose(volume, state["executed_volume"], rel_tol=0, abs_tol=1e-9):
            raise DemoVerificationError("SL/TP exit volume does not fully reconcile to the test position.")
        filled_orders = []
        for ticket in sorted({int(d.order) for d in selected}):
            records = self.api.history_orders_get(ticket=ticket)
            matching = [] if records is None else [o for o in records if int(o.ticket) == ticket and o.symbol == state["symbol"]
                       and int(o.state) == self.api.ORDER_STATE_FILLED and int(o.type) == expected
                       and int(getattr(o, "position_id", 0)) == state["position_id"]]
            if len(matching) != 1:
                raise DemoVerificationError("SL/TP exit does not match a filled order for the exact position.")
            filled_orders.append(ticket)
        commission, fee, swap = [sum(float(getattr(d, key)) for d in selected) for key in ("commission", "fee", "swap")]
        exit_price = sum(float(d.price) * float(d.volume) for d in selected) / volume
        exit_key='external_exit' if any(reasons[int(d.reason)]=='desktop_terminal' for d in selected) else 'automatic_exit'
        state[exit_key]={"reasons": sorted({reasons[int(d.reason)] for d in selected}),
                         "filled_order_tickets": filled_orders, "verified_from_actual_history": True}
        state.update(status="closed_verified",
                     close_executed_price=exit_price, close_commission=commission, close_fee=fee, close_swap=swap,
                     realized_net_pnl=sum(float(d.profit) for d in selected) + commission + fee + swap
                        + state["entry_commission"] + state["entry_fee"] + state["entry_swap"],
                     close_verified_deals=[_fields(d, ("ticket", "order", "position_id", "time_msc", "entry", "type", "reason", "volume", "price", "profit", "commission", "fee", "swap")) for d in selected],
                     verified_at_utc=datetime.now(timezone.utc).isoformat())
        atomic_json(self.state_path, state)
        append_event(self.directory, "external_exit_verification" if exit_key=='external_exit' else "protected_exit_verification", "closed_verified", account=state["account"], quote=state["quote"],
                     position_ticket=state["position_ticket"], order_ticket=filled_orders[-1], deal_ticket=selected[-1].ticket,
                     volume=volume, price=exit_price, commission=commission, fee=fee, swap=swap,
                     message=f"{exit_key} verified from actual deals/filled orders; session={self.session_id}; no order sent by verification.")
        return state
