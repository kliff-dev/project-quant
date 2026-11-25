"""Generate local trade documentation. Offline by default; never sends orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.trade_journal import build_journal, capture_broker_snapshot, load_sources, write_journal
from engine.execution_audit import atomic_json
from engine.market_monitor import monitor_lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-mt5', action='store_true', help='Optional native read-only refresh; may launch the terminal with its existing DEMO login.')
    args = parser.parse_args(argv)
    directory = ROOT / 'logs'
    snapshot_path = directory / 'trade_journal/broker_snapshot_latest.json'
    try:
        with monitor_lock(directory / 'trade_journal/update.lock'):
            snapshot = json.loads(snapshot_path.read_text(encoding='utf-8')) if snapshot_path.exists() else None
            if args.refresh_mt5:
                from engine.mt5_connector import MT5Connector
                sources, _ = load_sources(directory)
                with MT5Connector() as connector:
                    snapshot = capture_broker_snapshot(connector, sources)
                # Preserve the raw read before generating derived documents.
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                atomic_json(snapshot_path.with_name(f'broker_snapshot_{stamp}.json'), snapshot)
                atomic_json(snapshot_path, snapshot)
            journal = build_journal(directory, snapshot=snapshot)
            write_journal(ROOT, journal)
        print(json.dumps(dict(report=str(ROOT / 'reports/trade_journal.md'),
            strategy=journal['strategy_summary'], execution_tests=journal['execution_test_summary'],
            update_mode='native_read_only' if args.refresh_mt5 else 'offline_saved_evidence', orders_sent=0), indent=2))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f'Trade journal not refreshed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
