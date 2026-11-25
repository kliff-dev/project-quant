"""Activate Gold/EURUSD DEMO for six hours or an explicit --until; default status only."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from engine import activation_demo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    for name in ('start', 'status', 'stop', 'preview'):
        actions.add_argument('--'+name, action='store_true')
    parser.add_argument('--activation-id', help='New explicit user activation ID; consumed IDs cannot be retried.')
    parser.add_argument('--until', help='Explicit ISO end time with timezone, e.g. 2026-11-16T22:00:00+08:00; max 24 hours.')
    args = parser.parse_args(argv)
    if args.start and not args.activation_id:
        parser.error('--start requires --activation-id for durable no-retry tracking.')
    if args.activation_id and not args.start:
        parser.error('--activation-id is only used with --start.')
    if args.until and not args.start:
        parser.error('--until is only used with an explicit --start.')
    try:
        result = (activation_demo.start(ROOT, args.activation_id, until=args.until) if args.start else
                  activation_demo.stop(ROOT) if args.stop else
                  activation_demo.preview(ROOT) if args.preview else activation_demo.status(ROOT))
        print(json.dumps(result, indent=2, allow_nan=False))
        return int(result.get('status') == 'failed')
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'Activation stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
