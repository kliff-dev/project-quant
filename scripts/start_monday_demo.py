"""Inspect or manually start the bounded Monday 16 November 2026 DEMO pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.monday_session import describe_plan, reconcile_prior_execution, start_session, stop_session
from engine.mt5_connector import MT5ConnectionError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--plan', action='store_true', help='Read the local plan without connecting to MT5 (default).')
    action.add_argument('--status', action='store_true', help='Read the local plan and saved heartbeat without connecting to MT5.')
    action.add_argument('--start', action='store_true', help='Manually start Monday 16 November, 16:00-22:00 Asia/Manila only. No scheduling or waiting.')
    action.add_argument('--reconcile', action='store_true', help='Read actual broker history for prior protected SL/TP exits. Never send/close/retry an order; may connect to MT5.')
    action.add_argument('--stop', action='store_true', help='Request stopping/revoke the existing monitor; never close a position.')
    args = parser.parse_args(argv)
    try:
        result = (start_session(ROOT) if args.start else stop_session(ROOT) if args.stop else
                  reconcile_prior_execution(ROOT) if args.reconcile else describe_plan(ROOT))
        print(json.dumps(result, indent=2, allow_nan=False))
        return 1 if result.get('status') == 'failed' else 0
    except (MT5ConnectionError, ValueError, OSError, RuntimeError) as exc:
        print(f'Monday DEMO pilot stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
