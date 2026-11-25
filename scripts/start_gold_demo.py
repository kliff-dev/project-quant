"""Offline Gold plan by default; explicit native preview/start/reconciliation."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from engine import gold_demo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    for name in ('plan', 'status', 'preview', 'start', 'stop', 'reconcile'):
        mode.add_argument('--'+name, action='store_true')
    args = parser.parse_args(argv)
    try:
        action = gold_demo.start if args.start else gold_demo.preview if args.preview else gold_demo.stop if args.stop else gold_demo.reconcile if args.reconcile else gold_demo.describe
        result = action(ROOT)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 1 if result.get('status') == 'failed' else 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'Gold DEMO stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
