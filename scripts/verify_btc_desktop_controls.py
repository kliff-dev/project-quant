"""Exercise frozen BTC buttons with an isolated worker that cannot trade.

The fixture contains only standard-library worker scripts, capability stubs and
empty research folders. It imports no broker module and uses no real account.
The packaged Streamlit application, actual button protocol and Windows child
process lifetime are exercised, including separation from Gold/EURUSD controls.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.monitor_control import read_object, write_object
from dashboard.research_model import process_alive
from scripts.verify_desktop_controls import FrozenServer, rerun, session, wait_for


FAKE_BTC_WORKER = '''from pathlib import Path
from datetime import datetime, timezone, timedelta
import argparse, json, os, time, uuid
p=argparse.ArgumentParser(); p.add_argument('--activation-id'); p.add_argument('--until'); a=p.parse_args()
root=Path(__file__).resolve().parents[1]; folder=root/'logs/btc_monitor'; folder.mkdir(parents=True,exist_ok=True)
run=uuid.uuid4().hex
deadline=min(datetime.fromisoformat(a.until),datetime.now(timezone.utc)+timedelta(seconds=180))
state=dict(status='running',mode='btc_demo_series',run_id=run,activation_id=a.activation_id,
 pid=os.getpid(),deadline_utc=deadline.isoformat(),
 account=dict(demo_verified=True,trade_mode=0,broker='OFFLINE BTC CONTROL TEST',server='NO BROKER CONNECTION'),
 open_position_count=0,pending_order_count=0,orders_sent=0,positions_closed=0,broker_connections=0)
def publish():
 state['heartbeat_utc']=datetime.now(timezone.utc).isoformat()
 temp=folder/'fixture.tmp'; temp.write_text(json.dumps(state))
 for attempt in range(50):
  try: temp.replace(folder/'status.json'); break
  except PermissionError:
   if attempt==49: raise
   time.sleep(.01)
try:
 while datetime.now(timezone.utc)<deadline:
  stop=folder/'stop.json'
  if stop.exists() and json.loads(stop.read_text()).get('run_id')==run: break
  publish(); time.sleep(.1)
finally:
 state['status']='stopped'; state['stop_reason']='Offline BTC fixture ended; no trades'; publish()
'''

GOLD_CAPABILITY_STUB = '''raise RuntimeError("Offline BTC verification must never activate the Gold/EURUSD fixture.")\n'''


def btc_light(elements, active):
    """Require BTC's own status label and its colored indicator together."""
    expected = '**ACTIVE' if active else '**INACTIVE'
    # Existing builds contain legacy UTF-8 mojibake; accepting both encodings
    # verifies the visible indicator without confusing the other session light.
    lamps = ('\U0001f7e2', 'ðŸŸ¢') if active else ('\U0001f534', 'ðŸ”´')
    return any('BTC DEMO monitoring' in text and expected in text
               and any(lamp in text for lamp in lamps) for text in elements['markdown'])


def verify(exe: Path, output: Path):
    fixture = ROOT / 'build' / ('btc-control-fixture-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    (fixture / 'data/raw').mkdir(parents=True)
    (fixture / '.venv/Scripts').mkdir(parents=True)
    shutil.copy2(ROOT / '.venv/Scripts/python.exe', fixture / '.venv/Scripts/python.exe')
    shutil.copy2(ROOT / '.venv/pyvenv.cfg', fixture / '.venv/pyvenv.cfg')
    scripts = fixture / 'scripts'
    scripts.mkdir()
    (scripts / 'app_btc_session.py').write_text(FAKE_BTC_WORKER, encoding='utf-8')
    (scripts / 'activate_btc_demo.py').write_text('# Offline capability marker only.\n', encoding='utf-8')
    (scripts / 'app_monitor_session.py').write_text(GOLD_CAPABILITY_STUB, encoding='utf-8')
    (scripts / 'activate_demo.py').write_text(GOLD_CAPABILITY_STUB, encoding='utf-8')
    write_object(fixture / 'config/btc_demo_plan.json', dict(offline_test=True))
    write_object(fixture / 'config/activation_demo_plan.json', dict(offline_test=True))
    status_path = fixture / 'logs/btc_monitor/status.json'
    gold_status_path = fixture / 'logs/monitor/status.json'
    server = None
    running = None
    result = dict(executable=str(exe), fixture=str(fixture), broker_connections=0,
                  real_order_calls=0, positions_closed=0, fixture_only=True)
    try:
        server = FrozenServer(exe, fixture)
        with session(server) as connection:
            initial = rerun(connection, server.url)
            assert btc_light(initial, False), 'BTC must have its own red INACTIVE light.'
            assert not initial['buttons']['Activate BTC DEMO']['disabled']
            assert not initial['buttons']['Activate DEMO trading']['disabled'], 'Gold capability stub must be recognized.'
            assert initial['buttons']['Stop BTC entries']['disabled']
            assert not status_path.exists() and not gold_status_path.exists(), 'Opening the app must not launch either worker.'
            rerun(connection, server.url, initial['buttons']['Activate BTC DEMO']['id'])
            running = wait_for(lambda: read_object(status_path) if read_object(status_path)['status'] == 'running' else None)
            active = rerun(connection, server.url)
            assert btc_light(active, True), 'BTC requires its own green ACTIVE label after a verified heartbeat.'
            assert active['buttons']['Activate BTC DEMO']['disabled'], 'Duplicate BTC activation must be disabled.'
            assert not active['buttons']['Stop BTC entries']['disabled']
            assert active['buttons']['Activate DEMO trading']['disabled'], 'BTC must block the separate Gold/EURUSD launcher.'
            assert not gold_status_path.exists()
            assert running['mode'] == 'btc_demo_series'
            assert running['account']['demo_verified'] is True and running['account']['trade_mode'] == 0
            remaining = (datetime.fromisoformat(running['deadline_utc']) - datetime.now(timezone.utc)).total_seconds()
            assert 0 < remaining <= 180
            result.update(no_auto_start=True, btc_start_button=True, btc_green_verified_heartbeat=True,
                duplicate_btc_start_disabled=True, gold_start_disabled_while_btc_active=True,
                gold_worker_not_started=True, worker_pid=running['pid'], bounded_demo_deadline=True)
        before = read_object(status_path)['heartbeat_utc']
        server.close()
        server = None
        wait_for(lambda: read_object(status_path)['heartbeat_utc'] > before)
        assert process_alive(running['pid']) is True
        result['btc_worker_survived_display_server_close'] = True
        server = FrozenServer(exe, fixture)
        with session(server) as connection:
            active = rerun(connection, server.url)
            assert btc_light(active, True)
            assert active['buttons']['Activate DEMO trading']['disabled']
            rerun(connection, server.url, active['buttons']['Stop BTC entries']['id'])
            wait_for(lambda: read_object(status_path)['status'] == 'stopped')
            wait_for(lambda: process_alive(running['pid']) is False)
            inactive = rerun(connection, server.url)
            assert btc_light(inactive, False), 'BTC must return to its own red INACTIVE label.'
            assert inactive['buttons']['Stop BTC entries']['disabled']
            assert not inactive['buttons']['Activate BTC DEMO']['disabled']
            assert not inactive['buttons']['Activate DEMO trading']['disabled']
            final = read_object(status_path)
            assert final['orders_sent'] == final['positions_closed'] == final['broker_connections'] == 0
            assert final['open_position_count'] == final['pending_order_count'] == 0
            assert not gold_status_path.exists()
            result.update(btc_stop_button=True, btc_red_after_stop=True,
                both_activation_buttons_reenabled=True, orders_sent=0, open_positions=0, pending_orders=0)
        result.update(passed=True, verified_at_utc=datetime.now(timezone.utc).isoformat())
        write_object(output, result)
        return result
    finally:
        if status_path.exists():
            state = read_object(status_path)
            write_object(fixture / 'logs/btc_monitor/stop.json', dict(run_id=state['run_id']))
            if state.get('status') == 'running':
                wait_for(lambda: read_object(status_path)['status'] == 'stopped', timeout=5)
        if server is not None:
            server.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/desktop/btc_controls_e2e_0_6_0.json')
    args = parser.parse_args()
    print(json.dumps(verify(args.exe.resolve(), args.output), indent=2))
