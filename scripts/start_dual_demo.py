"""Manually activate today's combined Gold/EURUSD DEMO controller; default status only."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from engine import dual_demo


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    action=parser.add_mutually_exclusive_group()
    for name in ('start','status','stop'):
        action.add_argument('--'+name,action='store_true')
    args=parser.parse_args(argv)
    try:
        result=(dual_demo.start(ROOT) if args.start else dual_demo.stop(ROOT) if args.stop else dual_demo.status(ROOT))
        print(json.dumps(result,indent=2,allow_nan=False))
        return int(result.get('status')=='failed')
    except (ValueError,RuntimeError,OSError) as exc:
        print(f'Combined DEMO stopped: {exc}',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
