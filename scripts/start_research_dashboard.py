"""Open the local dashboard; only explicit DEMO control clicks change entry authority."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8502)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('Choose a local port between 1024 and 65535.')
    url = f'http://127.0.0.1:{args.port}'
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            parser.exit(1, f'Port {args.port} is already occupied; no existing process was changed. Use another --port.\n')
    folder = ROOT/'logs/dashboard'
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stdout = folder/f'stdout-{stamp}.log'
    stderr = folder/f'stderr-{stamp}.log'
    command = [sys.executable, '-B', '-m', 'streamlit', 'run', str(ROOT/'dashboard/research_app.py'),
               '--server.address', '127.0.0.1', '--server.port', str(args.port), '--server.headless', 'true',
               '--browser.gatherUsageStats', 'false']
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    with stdout.open('wb') as out, stderr.open('wb') as err:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                   start_new_session=os.name != 'nt')
    ready = False
    for _ in range(40):
        if process.poll() is not None:
            break
        try:
            with urlopen(url+'/_stcore/health', timeout=1) as response:
                ready = response.status == 200 and response.read() == b'ok'
            if ready:
                break
        except OSError:
            time.sleep(.25)
    record = dict(url=url, pid=process.pid, ready=ready, started_at_utc=datetime.now(timezone.utc).isoformat(),
                  stdout=str(stdout), stderr=str(stderr), scope='Local research and explicit DEMO activation controls; opening the dashboard does not activate trading')
    (folder/'server.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(record, indent=2))
    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(main())
