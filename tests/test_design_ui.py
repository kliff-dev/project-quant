"""UI behavior tests: navigation, evidence freshness and chart presentation."""
from pathlib import Path
import json

from streamlit.testing.v1 import AppTest

from dashboard.overview import saved_summary
from dashboard.ui import style_chart
import plotly.graph_objects as go

ROOT=Path(__file__).resolve().parents[1]


def test_overview_empty_workspace_navigation_without_activation(tmp_path,monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(tmp_path))
    from dashboard import monitor_control
    monkeypatch.setattr(monitor_control,'request_start',lambda *args:(_ for _ in ()).throw(AssertionError('No automatic activation')))
    app=AppTest.from_file(str(ROOT/'dashboard/research_app.py'),default_timeout=30).run()
    assert not app.exception and not app.error
    assert app.radio(key='workspace').value=='Overview'
    metrics={item.label:item.value for item in app.metric}
    assert metrics['DEMO session']=='Inactive'
    assert metrics['Completed strategy trades']=='Unknown'
    assert metrics['Recorded strategy result']=='Unknown'
    app.button(key='home_Seven-setup research lab').click().run()
    assert not app.exception and app.radio(key='workspace').value=='Seven-setup research lab'
    assert any('No research data is saved' in item.value for item in app.warning)


def test_overview_does_not_treat_journal_money_as_live_equity(tmp_path,monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(tmp_path))
    path=tmp_path/'data/processed/trade_journal.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'generated_at_utc':'2026-06-01T08:00:00Z',
        'strategy_summary':{'completed':3,'realized_net_pnl_usd':-2.5}}))
    app=AppTest.from_file(str(ROOT/'dashboard/research_app.py'),default_timeout=30).run()
    assert not app.exception
    metrics={item.label:item.value for item in app.metric}
    assert metrics['DEMO session']=='Inactive'
    assert metrics['Completed strategy trades']=='3'
    assert metrics['Recorded strategy result']=='$-2.50'
    assert any('not a live balance or equity feed' in item.value for item in app.caption)
    assert 'balance' not in {key.lower() for key in metrics}


def test_overview_uses_verified_snapshot_for_active_label(tmp_path,monkeypatch):
    from dashboard import monitor_control
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(tmp_path))
    state=dict(active=False,state='UNVERIFIED',reason='Stale heartbeat',can_start=False,can_stop=False,status={},
        capability={'available':True})
    monkeypatch.setattr(monitor_control,'snapshot',lambda root:state)
    app=AppTest.from_file(str(ROOT/'dashboard/research_app.py'),default_timeout=30).run()
    assert not app.exception
    assert next(x.value for x in app.metric if x.label=='DEMO session')=='Unverified'
    state.update(active=True,state='ACTIVE',reason='Watching for setups')
    app.run()
    assert not app.exception
    assert next(x.value for x in app.metric if x.label=='DEMO session')=='Active'


def test_bad_journal_is_unknown_not_zero(tmp_path):
    p=tmp_path/'data/processed/trade_journal.json';p.parent.mkdir(parents=True)
    p.write_text('incomplete JSON')
    assert saved_summary(tmp_path)['journal']=={}


def test_chart_theme_preserves_trade_data_and_uses_local_fonts():
    from dashboard.ui import apply_theme
    # Register through an actual Streamlit context to exercise the same helper.
    app=AppTest.from_string('from dashboard.ui import apply_theme\napply_theme()').run()
    assert not app.exception
    chart=go.Figure(go.Bar(x=['R1'],y=[7]))
    result=style_chart(chart,height=300)
    assert result.data[0].y[0]==7 and result.layout.height==300
    assert result.layout.template.layout.paper_bgcolor=='#0b111b'
    assert 'Segoe UI' in result.layout.template.layout.font.family
