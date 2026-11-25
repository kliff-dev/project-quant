"""Offline Gold native sizing, causal snapshots and actual guarded executor tests."""
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pandas as pd
import pytest

from test_demo_executor import rig
from test_gold_strategy import bars, signal
from engine import strategy_orders, gold_orders, gold_demo, demo_executor, demo_authorization, gold_connector
from engine.demo_executor import DemoExecutor, MAGIC, PLACE_CONFIRMATION
from engine.demo_series import DemoSeriesAuthorization
from engine.gold_demo import GoldDemoMonitor
from engine.market_monitor import MonitorConfig
from engine.gold_connector import GoldConnector
from engine.strategy_orders import StrategyPolicy, StrategyIntent


@pytest.fixture
def gold(rig, monkeypatch):
    clock = Record(now=datetime(2026, 11, 13, 10, 0, 30, tzinfo=timezone.utc))
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None): return clock.now if tz else clock.now.replace(tzinfo=None)
    for module in (demo_executor, gold_connector): monkeypatch.setattr(module, 'datetime', Frozen)
    monkeypatch.setattr(strategy_orders, 'utc_now', lambda: clock.now)
    monkeypatch.setattr(demo_authorization, 'utc_now', lambda: clock.now)
    rig.account.login, rig.account.server, rig.account.company = 464192812, 'Exness-MT5Trial17', 'Exness Technologies Ltd'
    info = rig.symbol
    info.name, info.currency_base, info.trade_contract_size = 'XAUUSDm', 'XAU', 100.
    info.point, info.trade_tick_size, info.digits, info.chart_mode = .001, .001, 3, 0
    rig.tick.bid, rig.tick.ask, rig.tick.time_msc = 3500., 3500.24, int(clock.now.timestamp()*1000)
    def pnl(side, symbol, lots, opening, closing):
        value = (Decimal(str(closing))-Decimal(str(opening)))*Decimal(str(lots))*100
        return float((value if side == 0 else -value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    rig.api.order_calc_profit.side_effect = pnl
    rig.api.TIMEFRAME_M5 = 5
    connector = GoldConnector(api=rig.api)
    connector.connect()
    policy = StrategyPolicy(target_mode='gold_intraday_v1', slippage_reserve_points_per_side=50)
    def send(request):
        price = rig.tick.ask if request['type'] == 0 else rig.tick.bid
        rig.ledger['orders'][111] = (Record(ticket=111, symbol='XAUUSDm', magic=MAGIC, type=request['type'], state=4, position_id=333),)
        rig.ledger['deals'][111] = (Record(ticket=222, order=111, position_id=333, symbol='XAUUSDm', magic=MAGIC,
            type=request['type'], entry=0, volume=request['volume'], price=price, profit=0., commission=0., fee=0., swap=0.,time_msc=int(clock.now.timestamp()*1000)),)
        rig.ledger['positions'] = [Record(ticket=444,identifier=333,symbol='XAUUSDm',magic=MAGIC,
            type=request['type'], volume=request['volume'], price_open=price, sl=request['sl'],tp=request['tp'])]
        return Record(retcode=10009, order=111, deal=222,volume=request['volume'],price=price,bid=rig.tick.bid,ask=rig.tick.ask,comment='Done',request_id=1,retcode_external=0)
    rig.api.order_send.side_effect = send
    yield Record(rig=rig, connector=connector, policy=policy, clock=clock)
    connector.close()


def candidate(gold, monkeypatch, side='buy', branch=4):
    features = signal(side)
    features.update(low3=3496., high3=3504., decision_time_utc='2026-11-13T10:00:00+00:00')
    context = dict(target_mode='gold_intraday_v1', research_spec_sha256=gold_orders.SPEC_SHA256,context_payload_sha256='b'*64)
    stop = gold_orders.frozen_stop(features, {'bid':3500.,'ask':3500.24}, .001,side)
    intent = StrategyIntent('XAUUSDm',side,stop,2.,features['decision_time_utc'],'a'*64,'pullback',context)
    raw = strategy_orders._build_strategy_protection(gold.rig.api,gold.rig.symbol,
        {'bid':3500.,'ask':3500.24}, {'balance':500.,'equity':500.,'currency':'USD'},intent,gold.policy,target_reward_r=branch)
    d = 1 if side=='buy' else -1
    price = Decimal(str(raw['take_profit']))+d*Decimal('.4')-(Decimal('.24') if d==-1 else 0)
    features['m15_swing_highs' if d==1 else 'm15_swing_lows'] = [dict(price=float(price),
        pivot_open_utc='2026-11-13T08:00:00Z',confirmed_at_utc='2026-11-13T08:45:00Z')]
    monkeypatch.setattr(gold_orders,'verified_context',lambda *args:features)
    return intent


@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('branch',[1,2,3,4])
def test_gold_native_contract_sizing_and_adaptive_target(gold,monkeypatch,side,branch):
    intent = candidate(gold,monkeypatch,side,branch)
    plan = strategy_orders.build_strategy_protection(gold.rig.api,gold.rig.symbol,
        {'bid':3500.,'ask':3500.24},{'balance':500.,'equity':500.,'currency':'USD'},intent,gold.policy)
    assert plan['selected_reward_r']==branch and plan['planned_reward_to_risk']>=branch
    assert plan['planned_loss_with_reserves']<=5 and plan['volume_lots']==.01
    assert plan['stop_loss']==intent.stop_loss and plan['stop_distance_pips'] is None
    assert plan['exit_policy']=='gold_adaptive_1_2_3_4'
    gold.rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('side',['buy','sell'])
def test_gold_authorized_entry_uses_checked_request_and_verifies_actual_deal_position(gold,monkeypatch,side):
    intent=candidate(gold,monkeypatch,side)
    path=gold.rig.directory/'gold_policy.json'
    path.write_text(json.dumps(asdict(gold.policy)))
    auth=DemoSeriesAuthorization.create(gold.connector,gold.rig.directory,path,
        expires_at=gold.clock.now+timedelta(hours=1),user_instruction='Manual Gold DEMO research session.')
    executor=DemoExecutor(gold.connector,gold.rig.directory,session_id='gold-fake',strategy_policy=gold.policy,strategy_intent=intent)
    preview=executor.prepare_entry()
    assert preview['ready']
    opened=executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert opened['status']=='open_verified' and opened['symbol']=='XAUUSDm'
    assert opened['protection']['selected_reward_r']==4 and opened['executed_volume']==.01
    assert gold.rig.api.order_send.call_args.args[0]==gold.rig.api.order_check.call_args.args[0]
    with pytest.raises((ValueError,PermissionError)):
        executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert gold.rig.api.order_send.call_count==1


@pytest.mark.parametrize('failure',['real','contest','volume','stale_signal','blackout'])
def test_gold_guard_failure_never_sends(gold,monkeypatch,failure):
    intent=candidate(gold,monkeypatch)
    if failure=='real':gold.rig.account.trade_mode=2
    if failure=='contest':gold.rig.account.trade_mode=1
    if failure=='volume':gold.rig.symbol.volume_min=.02
    if failure=='stale_signal':gold.clock.now+=timedelta(minutes=2)
    if failure=='blackout':gold.clock.now=gold.clock.now.replace(hour=13,minute=30)
    executor=DemoExecutor(gold.connector,gold.rig.directory,strategy_policy=gold.policy,strategy_intent=intent)
    with pytest.raises((ValueError,RuntimeError)):
        executor.prepare_entry()
    gold.rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('side',['buy','sell'])
def test_real_closed_gold_snapshot_roundtrip_and_tampering(gold,monkeypatch,side):
    frame=bars(side)
    rates=frame.copy()
    rates.insert(0,'time',[int(t.timestamp()) for t in frame.index])
    gold.rig.api.copy_rates_from_pos=Mock(return_value=rates.to_records(index=False))
    intent,decision=gold_orders.latest_gold_intent(gold.connector,gold.rig.directory,policy=gold.policy)
    assert intent is not None and decision['status']=='signal'
    assert gold_orders.verified_context(intent,gold.rig.symbol)['side']==side
    from analytics.trade_journal import replay_signal
    replay=replay_signal({'protection':{'policy':asdict(gold.policy),'strategy_intent':asdict(intent)}},gold.rig.directory)
    assert replay['status']=='verified' and replay['features']['side']==side
    path=__import__('pathlib').Path(intent.adaptive_context['input_file'])
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='missing or changed'):
        gold_orders.verified_context(intent,gold.rig.symbol)


def test_gold_m5_timestamp_does_not_expand_forex_timestamp_scope():
    with pytest.raises(ValueError):
        StrategyIntent('EURUSDm','buy',1.,.01,'2026-11-13T10:05:00Z','a'*64,'pullback')
    intent=StrategyIntent('XAUUSDm','buy',3495.,2.,'2026-11-13T10:05:00Z','a'*64,'pullback',{'target_mode':'gold_intraday_v1'})
    assert intent.decision_time_utc.endswith('10:05:00Z')


def test_gold_start_wrong_day_fails_before_native_access(tmp_path):
    native=Mock()
    with pytest.raises(ValueError,match='weekday'):
        gold_demo.start(tmp_path,now=lambda:datetime(2026,11,15,8,tzinfo=timezone.utc),connector_factory=native)
    native.assert_not_called()


def test_gold_series_accepts_five_minute_close_without_reusing_hour_slot(gold):
    path=gold.rig.directory/'policy.json'
    path.write_text(json.dumps(asdict(gold.policy)))
    auth=DemoSeriesAuthorization.create(gold.connector,gold.rig.directory,path,
        expires_at=gold.clock.now+timedelta(hours=1),user_instruction='Gold DEMO five-minute signals.')
    bound=DemoSeriesAuthorization(gold.rig.directory,auth.authorization_id,session_id='gold-minute',decision_time_utc='2026-11-13T10:05:00Z')
    assert bound.usage_path.name=='20261113T100500Z.json'


@pytest.mark.parametrize('side',['buy','sell'])
def test_gold_monitor_uses_separate_checkpoint_and_exports_verified_protective_exit(gold,monkeypatch,side):
    import csv
    from engine import market_monitor
    intent=candidate(gold,monkeypatch,side)
    path=gold.rig.directory/'policy.json'
    path.write_text(json.dumps(asdict(gold.policy)))
    auth=DemoSeriesAuthorization.create(gold.connector,gold.rig.directory,path,
        expires_at=gold.clock.now+timedelta(hours=1),user_instruction='Manual Gold demo research.')
    close=int(datetime.fromisoformat(intent.decision_time_utc).timestamp())
    gold.rig.api.copy_rates_from_pos=Mock(return_value=[{'time':close-600},{'time':close-300},{'time':close}])
    monkeypatch.setattr(market_monitor,'latest_strategy_intent',lambda *args,**kwargs:(intent,
        dict(status='signal',decision_time_utc=intent.decision_time_utc,reason='Synthetic Gold signal',orders_sent=0)))
    def monitor():
        return GoldDemoMonitor(gold.connector,gold.rig.directory,path,authorization_id=auth.authorization_id,
            config=MonitorConfig(1,10),now=lambda:gold.clock.now,emit=lambda _:None)
    first=monitor().run(once=True)
    assert first['status']=='stopped' and first['orders_sent']==1, first.get('stop_reason')
    assert (gold.rig.directory/'monitor/gold_state.json').exists()
    assert not (gold.rig.directory/'monitor/state.json').exists()
    position=gold.rig.ledger['positions'][0]
    order_type=0 if side=='buy' else 1
    profit=gold.rig.api.order_calc_profit(order_type,'XAUUSDm',position.volume,position.price_open,position.tp)
    gold.clock.now+=timedelta(seconds=60)
    gold.rig.tick.time_msc=int(gold.clock.now.timestamp()*1000)
    gold.rig.ledger['positions']=[]
    gold.rig.ledger['orders'][112]=(Record(ticket=112,symbol='XAUUSDm',type=1-order_type,state=4,position_id=333),)
    out=Record(ticket=223,order=112,position_id=333,symbol='XAUUSDm',type=1-order_type,entry=1,reason=5,
        volume=position.volume,price=position.tp,profit=profit,commission=0.,fee=0.,swap=0.,time_msc=int(gold.clock.now.timestamp()*1000))
    gold.rig.api.history_deals_get.side_effect=lambda *args,**kwargs:(*gold.rig.ledger['deals'][111],out)
    last=monitor()
    result=last.run(once=True)
    assert result['status']=='stopped' and result['closed_trades']==1, result.get('stop_reason')
    with last.trade_data_path.open(newline='') as handle:rows=list(csv.DictReader(handle))
    assert len(rows)==1 and rows[0]['symbol']=='XAUUSDm'
    assert rows[0]['target_mode']=='gold_intraday_v1' and rows[0]['selected_reward_r']=='4'
    assert rows[0]['target_spec_sha256']==gold_orders.SPEC_SHA256
    assert gold.rig.api.order_send.call_count==1
