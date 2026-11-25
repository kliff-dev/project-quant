"""BTC page and controls never trade or retrieve data without a click."""
from pathlib import Path
import json

from streamlit.testing.v1 import AppTest
from dashboard.btc_workspace import actual_history, saved_data

ROOT=Path(__file__).resolve().parents[1]


def test_saved_btc_page_has_four_families_and_cost_results(monkeypatch):
    from dashboard import btc_control
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(ROOT))
    monkeypatch.setattr(btc_control,'request_start',lambda *a,**k:(_ for _ in ()).throw(AssertionError('No automatic BTC activation')))
    app=AppTest.from_file(str(ROOT/'dashboard/research_app.py'),default_timeout=40).run()
    app.radio(key='workspace').set_value('BTC research & DEMO').run()
    assert not app.exception
    assert app.button(key='activate_btc').label=='Activate BTC DEMO'
    assert app.button(key='stop_btc').label=='Stop BTC entries'
    assert len(app.multiselect(key='btc_enabled_setups').options)==4
    assert any('not verified all-cost' in item.value for item in app.warning)
    assert any('One-percent price filter' in item.value for item in app.markdown)
    assert len(app.get('plotly_chart'))==2


def test_empty_btc_workspace_has_disabled_controls_and_clear_missing_data(tmp_path,monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(tmp_path))
    app=AppTest.from_file(str(ROOT/'dashboard/research_app.py'),default_timeout=30).run()
    app.radio(key='workspace').set_value('BTC research & DEMO').run()
    assert not app.exception
    assert app.button(key='activate_btc').disabled
    assert any('No verified BTC archive' in item.value for item in app.warning)
    assert any('No BTC DEMO attempts' in item.value for item in app.info)


def test_btc_history_preserves_broker_evidence_not_simulated_results(tmp_path):
    path=tmp_path/'logs/btc_monitor/attempts/a.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(status='closed_verified',plan=dict(intent=dict(selected_setup='B2'),request=dict(volume=.01)),
        fill=dict(position_ticket=123,price=100),closed=dict(net_pnl_usd=-1.2))))
    row=actual_history(tmp_path).iloc[0]
    assert row.Position==123 and row.Net_USD==-1.2 and row.Setup=='B2'
