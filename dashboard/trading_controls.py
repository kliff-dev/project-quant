"""Button-triggered DEMO activation and a conservative, live status light."""
from datetime import datetime
from pathlib import Path
import subprocess

import streamlit as st

from dashboard import monitor_control as control


def _friendly_reason(reason):
    """Translate common engine diagnostics without changing their meaning."""
    text = str(reason)
    lowered = text.lower()
    if 'account_info() returned none' in lowered or 'demoaccountrequirederror' in lowered:
        return 'MT5 could not verify a DEMO account. Open MT5 and check that your demo account is connected.'
    if 'authorization failed' in lowered or 'service is not available' in lowered:
        return 'MT5 could not connect to the account server. Check the connection inside MT5 before activating again.'
    if 'heartbeat' in lowered and ('stale' in lowered or 'missing' in lowered or 'invalid' in lowered):
        return 'The monitor has not reported a reliable recent update. Its active status cannot be confirmed.'
    if 'monitor process is absent' in lowered:
        return 'The monitor is no longer running. Start a new DEMO session when you are ready.'
    if 'authority has expired' in lowered:
        return 'The session has ended. Start a new session to allow new entries; existing positions keep their SL/TP.'
    if 'status cannot be verified' in lowered or 'monitor process could not be verified' in lowered:
        return 'The app cannot confirm the monitor status. Review the details below before starting another session.'
    return text


def _request_error(action, exc):
    detail = str(exc)
    explanation = _friendly_reason(detail)
    if explanation == detail:
        explanation = ('Check that MT5 is connected to a DEMO account and the selected session end is within the next 24 hours.'
                       if action == 'activate' else
                       'Refresh the status and try stopping new entries again. Existing positions are not closed by this button.')
    st.error(f'Could not {action}. {explanation}')
    with st.expander('Request details'):
        st.code(detail, language=None)


@st.fragment(run_every='5s')
def trading_controls(root):
    root = Path(root)
    state = control.snapshot(root)
    status = state['status']
    with st.container(border=True):
        heading, timing = st.columns([2.7, 1.3], vertical_alignment='center')
        with heading:
            st.caption('DEMO SESSION')
            if state['active']:
                st.markdown('🟢 **ACTIVE — DEMO monitoring**')
            elif state.get('state') == 'UNVERIFIED':
                st.markdown('🔴 **STATUS UNVERIFIED — DEMO monitoring**')
            else:
                st.markdown('🔴 **INACTIVE — DEMO monitoring**')
        if status.get('deadline_utc'):
            try:
                deadline = datetime.fromisoformat(status['deadline_utc']).astimezone(control.MANILA)
                timing.caption(f"Session end · {deadline:%d %b, %I:%M %p} Manila")
            except (TypeError, ValueError):
                pass
        explanation = _friendly_reason(state['reason'])
        st.write(explanation)
        with st.expander('Manage DEMO session'):
            st.caption('Active means watching for a valid setup. It does not mean a position is open.')
            st.caption('Set when new entries must stop. All times are in Manila.')
            end = control.default_end()
            dates = st.columns(2)
            day = dates[0].date_input('End date (Manila)', value=end.date(), key='demo_end_date', disabled=not state['can_start'])
            clock = dates[1].time_input('End time (Manila)', value=end.time().replace(tzinfo=None), step=900,
                                       key='demo_end_time', disabled=not state['can_start'])
            actions = st.columns(2)
            activate = actions[0].button('Activate DEMO trading', key='activate_demo', type='primary',
                                         disabled=not state['can_start'], width='stretch')
            stop = actions[1].button('Stop new entries', key='stop_demo', disabled=not state['can_stop'], width='stretch')
            st.caption('Gold + EUR/USD · 1% baseline risk · Up to 2% for qualified minimum-lot setups · 8% combined ceiling.')
            st.caption('Five executable setups per market: trend reclaim, trend sweep, channel breakout, compression breakout and range re-entry. Read their rules in the setup lab.')
            st.caption('Up to two positions per market, four total. Each needs a fresh valid setup and available risk. Stopping or expiry keeps existing positions and their SL/TP.')
            if not state['capability']['available']:
                st.info('Session controls are unavailable in this workspace. Choose the configured Project Quant workspace to enable them.')
            if explanation != state['reason'] or not state['capability']['available']:
                with st.expander('Technical status details'):
                    st.code(state['reason'], language=None)
                    if not state['capability']['available']:
                        st.code(state['capability']['reason'], language=None)
    if activate:
        try:
            control.request_start(root, datetime.combine(day, clock, control.MANILA))
            st.info('Activation requested. The light turns green only after the DEMO checks and a fresh monitor heartbeat.')
            st.rerun()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            _request_error('activate', exc)
    if stop:
        try:
            control.request_stop(root, status.get('run_id'))
            st.info('Stop requested. Existing positions keep their SL/TP.')
            st.rerun()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            _request_error('stop new entries', exc)
