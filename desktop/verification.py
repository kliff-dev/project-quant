"""Execute packaged Streamlit pages through its local application protocol.

An HTTP health response only proves the listener started. This check requests
real script runs and inspects the generated page elements for exceptions.
It does not operate MT5, click native windows or initiate a replay/trade.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from streamlit.proto.BackMsg_pb2 import BackMsg
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
from websockets.sync.client import connect


from dashboard.navigation import PAGES


def verify_pages(url: str, timeout: float = 45) -> dict:
    """Check an owned loopback server; fail on an exception or empty page."""
    address = urlsplit(url)
    if address.scheme != 'http' or address.hostname != '127.0.0.1' or not address.port:
        raise ValueError('Verification requires an explicit local app URL.')
    uri = f'ws://127.0.0.1:{address.port}/_stcore/stream'
    results = {}
    widget_id = None
    with connect(uri, origin=url, subprotocols=['streamlit'], proxy=None,
                 open_timeout=10, max_size=32 * 1024 * 1024) as connection:
        for page in PAGES:
            request = BackMsg()
            state = request.rerun_script
            state.context_info.timezone = 'Asia/Manila'
            state.context_info.locale = 'en-US'
            state.context_info.url = url
            if widget_id:
                widget = state.widget_states.widgets.add()
                widget.id = widget_id
                widget.string_value = page
            connection.send(request.SerializeToString())
            elements = {}
            chart_traces = []
            errors = []
            demo_buttons = set()
            btc_buttons = set()
            status_light = False
            deadline = time.monotonic() + timeout
            found_navigation = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'Page execution timed out: {page}')
                message = ForwardMsg()
                message.ParseFromString(connection.recv(timeout=remaining))
                kind = message.WhichOneof('type')
                if kind == 'delta' and message.delta.HasField('new_element'):
                    element = message.delta.new_element
                    name = element.WhichOneof('type')
                    elements[name] = elements.get(name, 0) + 1
                    if name == 'exception':
                        errors.append(f'{element.exception.type}: {element.exception.message}')
                    elif name == 'button' and element.button.label in ('Activate DEMO trading', 'Stop new entries'):
                        demo_buttons.add(element.button.label)
                    elif name == 'button' and element.button.label in ('Activate BTC DEMO', 'Stop BTC entries'):
                        btc_buttons.add(element.button.label)
                    elif name == 'markdown' and any(label in element.markdown.body for label in
                            ('🟢 **ACTIVE — DEMO monitoring**', '🔴 **STATUS UNVERIFIED — DEMO monitoring**',
                             '🔴 **INACTIVE — DEMO monitoring**')):
                        status_light = True
                    elif name == 'radio' and element.radio.label == 'Workspace':
                        if tuple(element.radio.options) != PAGES:
                            raise RuntimeError('Packaged page navigation differs from the verified app.')
                        widget_id = element.radio.id
                        found_navigation = True
                    elif name == 'plotly_chart':
                        chart = json.loads(element.plotly_chart.spec)
                        chart_traces.append(len(chart.get('data', [])))
                elif kind == 'script_finished':
                    if errors:
                        raise RuntimeError(f'{page}: ' + '; '.join(errors))
                    if message.script_finished != ForwardMsg.FINISHED_SUCCESSFULLY:
                        raise RuntimeError(f'{page}: script did not finish successfully ({message.script_finished}).')
                    break
            if not found_navigation:
                raise RuntimeError(f'{page}: the app navigation was not generated.')
            if len(demo_buttons) != 2 or not status_light:
                raise RuntimeError(f'{page}: DEMO controls or the active/inactive status light were not generated.')
            if len(btc_buttons)!=2:
                raise RuntimeError(f'{page}: separate BTC controls were not generated.')
            if page == 'Market data & dates' and not any(chart_traces):
                raise RuntimeError('The saved market page did not generate a chart with data.')
            results[page] = dict(elements=elements, chart_trace_counts=chart_traces,
                                 script_finished=True, exceptions=0, demo_controls=True, btc_controls=True,status_light=True)
    return results
