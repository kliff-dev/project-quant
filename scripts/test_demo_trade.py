"""Explicit one-shot DEMO test: preview by default; exact phrases gate sends."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from engine.demo_executor import CLOSE_CONFIRMATION, PLACE_CONFIRMATION, DemoExecutor, DemoVerificationError
from engine.mt5_connector import DEFAULT_TERMINAL, MT5ConnectionError, MT5Connector
from engine.protection import RiskProtection
from engine.strategy_orders import StrategyPolicy, latest_strategy_intent, risk_budget
from engine.execution_audit import atomic_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One EUR/USD DEMO execution test. Default action previews only.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--preview", action="store_true", help="Save a draft; protected drafts also run order_check, never order_send (default).")
    actions.add_argument("--open", action="store_true", help="Submit once after exact typed confirmation of a ready preview.")
    actions.add_argument("--preview-close", action="store_true", help="Inspect the verified test position and prepare a close draft.")
    actions.add_argument("--close", action="store_true", help="Close only the verified test position after separate confirmation.")
    actions.add_argument("--verify", choices=["open", "close"], help="Read-only order/deal/position reconciliation; never retries sends.")
    actions.add_argument('--reconcile-external',action='store_true',help='Verify an already-completed desktop or broker protective exit; never sends.')
    parser.add_argument("--preview-id", help="ID of the exact preview you reviewed; required for --open/--close.")
    parser.add_argument("--terminal", type=Path, default=DEFAULT_TERMINAL)
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    parser.add_argument("--session", help="Named session retaining earlier test records; shares the root execution lock.")
    parser.add_argument('--strategy',action='store_true',help='Use registered H4/H1 signals, 1% stop-first sizing and a 2R target; requires --session.')
    parser.add_argument("--risk-percent", type=float, help="Attach SL/TP using this gross risk percentage (maximum 5); requires --session.")
    parser.add_argument("--reward-risk", type=float, help="Profit target / planned stop loss (default 2 with --risk-percent).")
    args = parser.parse_args(argv)
    if (args.open or args.close) and not args.preview_id:
        parser.error("--open and --close require the reviewed --preview-id.")
    if args.open and not args.strategy:
        parser.error('New entries require --strategy and a reviewed strategy-sized preview under the current policy.')
    if args.risk_percent is not None and not args.session:
        parser.error("Protected orders require --session to retain earlier execution evidence.")
    if args.reward_risk is not None and args.risk_percent is None:
        parser.error("--reward-risk requires --risk-percent.")
    if args.strategy and (not args.session or args.risk_percent is not None or args.reward_risk is not None):
        parser.error('--strategy requires --session and uses config/trading_policy.json rather than legacy risk flags.')
    try:
        protection = RiskProtection(args.risk_percent, args.reward_risk if args.reward_risk is not None else 2) if args.risk_percent is not None else None
        with MT5Connector(args.terminal) as connector:
            policy=StrategyPolicy.load(ROOT/'config/trading_policy.json') if args.strategy else None
            intent=None
            if args.strategy and not (args.open or args.close or args.preview_close or args.verify or args.reconcile_external):
                intent,decision=latest_strategy_intent(connector,args.log_dir)
                if intent is None:
                    decision['policy']=json.loads((ROOT/'config/trading_policy.json').read_text(encoding='utf-8'))
                    connector.verify_demo_account()
                    account=connector.terminal_api.account_info()
                    if account is None:
                        raise ValueError('Account funds unavailable for the strategy risk budget.')
                    basis,raw,budget=risk_budget({'balance':account.balance,'equity':account.equity,'currency':account.currency},policy)
                    decision.update(risk_basis_amount=float(basis),raw_percent_budget=float(raw),risk_budget=float(budget))
                    decision['ready']=False
                    atomic_json(args.log_dir/'latest_strategy_decision.json',decision)
                    print(json.dumps(decision,indent=2,allow_nan=False))
                    return 2
            executor = DemoExecutor(connector, args.log_dir, protection=protection, session_id=args.session,
                                    strategy_policy=policy,strategy_intent=intent)
            if args.open or args.close:
                if args.open and executor.strategy_policy is None:
                    raise ValueError('New CLI entries require a strategy-sized preview under the current 1% policy. Legacy sessions remain available for verification/closing.')
                expected = CLOSE_CONFIRMATION if args.close else PLACE_CONFIRMATION
                confirmation = input(f"Type {expected} exactly to confirm the reviewed preview: ")
                report = executor.close_test(args.preview_id, confirmation) if args.close else executor.open_test(args.preview_id, confirmation)
            elif args.reconcile_external:
                report=executor.reconcile_external_close()
            elif args.preview_close:
                report = executor.prepare_close()
            elif args.verify:
                report = executor.verify(closing=args.verify == "close")
            else:
                report = executor.prepare_entry()
        # Full identity is retained only in local state; console output is masked.
        console = {key: value for key, value in report.items() if key != "account_identity"}
        print(json.dumps(console, indent=2, allow_nan=False))
        return 0 if report.get("ready", True) else 2
    except (MT5ConnectionError, DemoVerificationError, ValueError, OSError, EOFError) as exc:
        print(f"Demo test stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
