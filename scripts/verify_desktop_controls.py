"""Exercise the frozen app's buttons against an isolated, non-trading worker.

The fixture has no engine or MT5 imports. It uses an actual workspace Python,
Windows job, Streamlit protocol and child process, without real broker access.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.monitor_control import read_object, write_object
from dashboard.research_model import process_alive
from desktop.runtime import OwnedJob
from streamlit.proto.BackMsg_pb2 import BackMsg
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
from websockets.sync.client import connect


FAKE_WORKER = '''from pathlib import Path
from datetime import datetime, timezone, timedelta
import argparse, json, os, time, uuid
p=argparse.ArgumentParser(); p.add_argument('--activation-id'); p.add_argument('--until'); a=p.parse_args()
root=Path(__file__).resolve().parents[1]; folder=root/'logs/monitor'; folder.mkdir(parents=True,exist_ok=True)
run=uuid.uuid4().hex
deadline=min(datetime.fromisoformat(a.until),datetime.now(timezone.utc)+timedelta(seconds=90))
state=dict(status='running',mode='dual_demo_series',entry_timing='user_activation_v1',run_id=run,
 activation_id=a.activation_id,pid=os.getpid(),deadline_utc=deadline.isoformat(),
 account=dict(demo_verified=True,trade_mode=0,broker='OFFLINE CONTROL TEST',server='NO BROKER CONNECTION'),
 open_position_count=0,pending_order_count=0,orders_sent=0,instruments={})
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
 state['status']='stopped'; state['stop_reason']='Offline fixture ended; no trades'; publish()
'''

FAKE_STOP = '''from pathlib import Path
import json
folder=Path(__file__).resolve().parents[1]/'logs/monitor'
state=json.loads((folder/'status.json').read_text())
(folder/'stop.json').write_text(json.dumps(dict(run_id=state['run_id'])))
print(json.dumps(dict(status='stop_requested',positions_closed=0)))
'''


def wait_for(check, timeout=15):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.1)
    raise TimeoutError('Isolated desktop control verification timed out.')


class FrozenServer:
    def __init__(self, exe, workspace):
        self.job = OwnedJob()
        self.process = None
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        with (workspace/'server.log').open('ab') as log:
            self.process = subprocess.Popen([str(exe), '--serve', '--workspace', str(workspace), '--port', str(port)],
                cwd=exe.parent/'_internal/source', stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
        self.job.assign(self.process)
        opener = build_opener(ProxyHandler({}))
        def healthy():
            if self.process.poll() is not None:
                raise RuntimeError('The frozen display server exited; inspect the fixture server.log.')
            with opener.open(self.url+'/_stcore/health', timeout=1) as response:
                return response.read() == b'ok'
        try:
            wait_for(healthy, 45)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.job.close()
        if self.process is not None:
            self.process.wait(timeout=10)


def rerun(connection, url, button=None):
    request = BackMsg()
    state = request.rerun_script
    state.context_info.timezone = 'Asia/Manila'
    state.context_info.locale = 'en-US'
    state.context_info.url = url
    if button:
        widget = state.widget_states.widgets.add()
        widget.id, widget.trigger_value = button, True
    connection.send(request.SerializeToString())
    result = dict(buttons={}, markdown=[])
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        message = ForwardMsg()
        message.ParseFromString(connection.recv(timeout=max(.1, deadline-time.monotonic())))
        kind = message.WhichOneof('type')
        if kind == 'delta' and message.delta.HasField('new_element'):
            element = message.delta.new_element
            name = element.WhichOneof('type')
            if name == 'exception':
                raise RuntimeError(element.exception.message)
            if name == 'button':
                result['buttons'][element.button.label] = dict(id=element.button.id, disabled=element.button.disabled)
            elif name == 'markdown':
                result['markdown'].append(element.markdown.body)
        elif kind == 'script_finished' and message.script_finished == ForwardMsg.FINISHED_SUCCESSFULLY:
            return result
    raise TimeoutError('The frozen UI did not complete its control run.')


def session(server):
    return connect(server.url.replace('http:', 'ws:')+'/_stcore/stream', origin=server.url,
                   subprotocols=['streamlit'], proxy=None, open_timeout=10, max_size=32*1024*1024)


def verify(exe, output):
    fixture = ROOT/'build'/('control-fixture-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    (fixture/'data/raw').mkdir(parents=True)
    (fixture/'.venv/Scripts').mkdir(parents=True)
    shutil.copy2(ROOT/'.venv/Scripts/python.exe', fixture/'.venv/Scripts/python.exe')
    shutil.copy2(ROOT/'.venv/pyvenv.cfg', fixture/'.venv/pyvenv.cfg')
    (fixture/'scripts').mkdir()
    (fixture/'scripts/app_monitor_session.py').write_text(FAKE_WORKER, encoding='utf-8')
    (fixture/'scripts/activate_demo.py').write_text(FAKE_STOP, encoding='utf-8')
    write_object(fixture/'config/activation_demo_plan.json', dict(offline_test=True))
    server = None
    status_path = fixture/'logs/monitor/status.json'
    result = dict(executable=str(exe), fixture=str(fixture), broker_connections=0, real_order_calls=0)
    try:
        server = FrozenServer(exe, fixture)
        with session(server) as connection:
            initial = rerun(connection, server.url)
            assert any('🔴' in text and 'INACTIVE' in text for text in initial['markdown'])
            assert not initial['buttons']['Activate DEMO trading']['disabled']
            assert not status_path.exists(), 'Opening the app must not launch a worker.'
            rerun(connection, server.url, initial['buttons']['Activate DEMO trading']['id'])
            running = wait_for(lambda: read_object(status_path) if read_object(status_path)['status']=='running' else None)
            active = rerun(connection, server.url)
            assert any('🟢' in text and 'ACTIVE' in text for text in active['markdown'])
            assert active['buttons']['Activate DEMO trading']['disabled']
            assert not active['buttons']['Stop new entries']['disabled']
            result.update(start_button=True, green_after_verified_heartbeat=True, duplicate_start_disabled=True,
                          no_auto_start=True, worker_pid=running['pid'])
        before = read_object(status_path)['heartbeat_utc']
        server.close()
        server = None
        wait_for(lambda: read_object(status_path)['heartbeat_utc'] > before)
        assert process_alive(running['pid']) is True
        result['worker_survived_display_server_close'] = True
        server = FrozenServer(exe, fixture)
        with session(server) as connection:
            active = rerun(connection, server.url)
            assert any('🟢' in text for text in active['markdown'])
            rerun(connection, server.url, active['buttons']['Stop new entries']['id'])
            wait_for(lambda: read_object(status_path)['status']=='stopped')
            inactive = rerun(connection, server.url)
            assert any('🔴' in text for text in inactive['markdown'])
            assert read_object(status_path)['orders_sent'] == 0
            result.update(stop_button=True, red_after_stop=True, positions_closed=0)
        result.update(passed=True, verified_at_utc=datetime.now(timezone.utc).isoformat())
        write_object(output, result)
        return result
    finally:
        if status_path.exists():
            state = read_object(status_path)
            write_object(fixture/'logs/monitor/stop.json', dict(run_id=state['run_id']))
        if server is not None:
            server.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'reports/desktop/controls_e2e_0_3_0.json')
    args = parser.parse_args()
    print(json.dumps(verify(args.exe.resolve(), args.output), indent=2))
