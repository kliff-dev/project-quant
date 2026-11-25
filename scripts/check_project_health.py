"""Offline project checks. Never connects to MT5, activates trading or runs batch studies."""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FOLDERS = ('analytics', 'dashboard', 'desktop', 'engine', 'scripts', 'strategies')
# Reviewed entry points: argument parsing handles --help before any action.
# Older batch studies do not implement --help; passing it could overwrite their
# frozen research outputs. They receive syntax checks and are listed separately.
HELP_SCRIPTS = ('main.py', 'desktop/main.py', *(
    f'scripts/{name}.py' for name in (
        'activate_demo', 'app_btc_session', 'app_monitor_session', 'build_desktop',
        'check_mt5_connection', 'check_project_health', 'check_strategy_observer',
        'collect_gold_data', 'monitor_market', 'refresh_btc_data', 'refresh_setup_lab',
        'research_btc', 'research_gold_strategy', 'start_dual_demo', 'start_gold_demo',
        'start_monday_demo', 'start_research_dashboard', 'test_demo_trade',
        'update_trade_journal', 'verify_btc_desktop_controls', 'verify_desktop_controls')))


def source_inventory(root: Path) -> list[dict]:
    paths = [root / 'main.py']
    paths += sorted(path for folder in SOURCE_FOLDERS
                    for path in (root / folder).rglob('*.py')
                    if '__pycache__' not in path.parts)
    result = []
    for path in paths:
        row = dict(file=path.relative_to(root).as_posix(), syntax_ok=False)
        try:
            source = path.read_text(encoding='utf-8-sig')
            ast.parse(source, filename=str(path))
            compile(source, str(path), 'exec')  # Compile without executing imports or code.
            row['syntax_ok'] = True
            if row['file'].startswith('scripts/'):
                row['runtime_check'] = ('help_only' if row['file'] in HELP_SCRIPTS else
                                        'not_executed_batch_or_helper')
        except (OSError, UnicodeError, SyntaxError) as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
        result.append(row)
    return result


def check_source_pins(root: Path) -> list[dict]:
    rows = []
    for relative in ('config/activation_demo_plan.json', 'config/btc_demo_plan.json'):
        path = root / relative
        if not path.is_file():
            rows.append(dict(plan=relative, checked=False, reason='Optional execution plan missing'))
            continue
        try:
            plan = json.loads(path.read_text(encoding='utf-8-sig'))
            pins = plan['source_sha256']
            if not isinstance(pins, dict) or not pins:
                raise ValueError('Missing source checksums')
            mismatches = []
            for name, expected in pins.items():
                source = (root / name).resolve()
                if (not source.is_relative_to(root) or not source.is_file() or
                        hashlib.sha256(source.read_bytes()).hexdigest() != expected):
                    mismatches.append(name)
            rows.append(dict(plan=relative, checked=True, files=len(pins), mismatches=mismatches))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rows.append(dict(plan=relative, checked=True, error=f'{type(exc).__name__}: {exc}'))
    return rows


def check_cli_help(root: Path, python: Path, relative: str) -> dict:
    if relative not in HELP_SCRIPTS:
        raise ValueError('Only reviewed --help entry points may be run.')
    started = time.perf_counter()
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    try:
        completed = subprocess.run([str(python), '-B', str(root / relative), '--help'],
            cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', env=env, timeout=35,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        passed = completed.returncode == 0 and 'usage:' in completed.stdout.lower()
        return dict(file=relative, passed=passed, exit_code=completed.returncode,
                    seconds=round(time.perf_counter() - started, 3),
                    error=completed.stderr[-1500:] if not passed else '')
    except (OSError, subprocess.SubprocessError) as exc:
        return dict(file=relative, passed=False, error=f'{type(exc).__name__}: {exc}')


def saved_monitor_status(root: Path) -> dict:
    path = root / 'logs/monitor/status.json'
    try:
        saved = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(saved, dict):
            raise ValueError('Expected a status object')
        # This is saved evidence, not a native account or process check.
        return dict(evidence='saved_only_not_live_broker_verification',
                    status=saved.get('status'), heartbeat_utc=saved.get('heartbeat_utc'),
                    deadline_utc=saved.get('deadline_utc'), stop_reason=saved.get('stop_reason'))
    except (OSError, ValueError, TypeError) as exc:
        return dict(evidence='unavailable', warning=f'{type(exc).__name__}: {exc}')


def check(root: Path, *, smoke_cli=False, python: Path | None = None) -> dict:
    root = root.resolve()
    inventory = source_inventory(root)
    pins = check_source_pins(root)
    result = dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
        mode='offline_read_only', source_files=len(inventory), inventory=inventory,
        source_pins=pins, saved_monitor=saved_monitor_status(root), cli_checks=[])
    if smoke_cli:
        with ThreadPoolExecutor(max_workers=2) as pool:
            result['cli_checks'] = list(pool.map(
                lambda name: check_cli_help(root, python or Path(sys.executable), name), HELP_SCRIPTS))
    result['passed'] = (bool(inventory) and all(row['syntax_ok'] for row in inventory)
        and all(not row.get('error') and not row.get('mismatches') for row in pins)
        and all(row['passed'] for row in result['cli_checks']))
    result['limitations'] = [
        'Syntax and --help checks do not prove every runtime path works.',
        'Batch research scripts are not rerun and their existing outputs are not overwritten.',
        'No MT5 connection, data download, order check, order send or session activation is performed.',
        'Run the offline pytest suite and desktop self-test for behavioral and packaged-page checks.',
    ]
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--smoke-cli', action='store_true', help='Check reviewed --help commands only.')
    parser.add_argument('--output', type=Path, help='Optional JSON evidence file.')
    args = parser.parse_args(argv)
    result = check(args.root, smoke_cli=args.smoke_cli)
    payload = json.dumps(result, indent=2, ensure_ascii=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding='utf-8')
    print(payload, end='')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
