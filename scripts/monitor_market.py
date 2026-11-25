"""Run a bounded local monitor; optional one-entry DEMO mode needs prior authority."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.market_monitor import MarketMonitor, MonitorConfig, read_status, request_stop
from engine.demo_session_monitor import DemoSessionMonitor
from engine.demo_series import DemoSeriesMonitor
from engine.mt5_connector import DEFAULT_TERMINAL, MT5Connector, MT5ConnectionError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--once', action='store_true', help='Inspect once and exit, reserving this candle against repeats.')
    action.add_argument('--status', action='store_true', help='Read the local heartbeat; no MT5 connection.')
    action.add_argument('--stop', action='store_true', help='Request a graceful stop; does not close positions.')
    parser.add_argument('--hours', type=float, default=8, help='Bounded session duration, .001 to 24 hours (default 8).')
    parser.add_argument('--poll-seconds', type=float, default=15, help='Heartbeat/H1 probe interval, 1 to 60 seconds (default 15).')
    parser.add_argument('--terminal', type=Path, default=DEFAULT_TERMINAL)
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'logs')
    parser.add_argument('--report-file', type=Path, default=ROOT / 'reports/latest_monitor_status.md')
    parser.add_argument('--run-id', help=argparse.SUPPRESS)  # Launcher handshake, not an execution authorization.
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo-once', metavar='AUTHORIZATION_ID',
                        help='Attempt one protected DEMO strategy entry under an existing bounded user authorization.')
    modes.add_argument('--demo-series', metavar='AUTHORIZATION_ID',
                       help='Repeated sequential DEMO entries on new setups, after verified exits; no execution retries.')
    args = parser.parse_args(argv)
    try:
        if args.status or args.stop:
            report = request_stop(args.log_dir) if args.stop else read_status(args.log_dir)
        else:
            config = MonitorConfig(args.hours, args.poll_seconds)
            with MT5Connector(args.terminal) as connector:
                factory = DemoSeriesMonitor if args.demo_series else DemoSessionMonitor if args.demo_once else MarketMonitor
                identifier = args.demo_series or args.demo_once
                options = {'authorization_id': identifier} if identifier else {}
                report = factory(connector, args.log_dir, ROOT / 'config/trading_policy.json',
                                 config=config, report_path=args.report_file, run_id=args.run_id, **options).run(once=args.once)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 1 if report['status'] == 'failed' else 0
    except (MT5ConnectionError, ValueError, OSError, RuntimeError) as exc:
        print(f'Market monitor stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
