from dataclasses import replace

import pandas as pd
import pytest

from analytics.btc_replay import ContractSpec, ReplaySettings, prepare_bars, replay, rounded_lots


def bars(rows=None):
    rows = rows or [(100, 104, 96, 100, 100), (100, 104, 96, 100, 100),
                    (100, 130, 96, 125, 100)]
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close', 'spread'],
                        index=pd.date_range('2026-01-01', periods=len(rows), freq='h', tz='UTC'))


def signals(frame, *, side='buy', stop=90, setup='B1', indices=(1,)):
    return pd.DataFrame([dict(setup_id=setup, side=side, stop_price=stop, reward_r=2.,
        decision_time_utc=frame.index[i], signal_open_utc=frame.index[i]-pd.Timedelta(hours=1),
        eligible_for_selection=True, selected_setup=setup) for i in indices])


CONTRACT = ContractSpec('BTCUSDm', .01, 1., 1., 100., 1.)
SETTINGS = ReplaySettings(initial_balance=10000., max_lots=100., max_friction_stop_fraction=1.)


def test_next_open_ask_entry_exact_2r_target_and_cash():
    frame = bars()
    result = replay(frame, signals(frame), CONTRACT, SETTINGS)
    trade = result['trades'][0]
    assert trade['entry_time_utc'] == frame.index[1].isoformat()
    assert trade['entry_price'] == 103  # Bid 100 + spread 1 + adverse slip 2.
    assert trade['target_price'] == 123  # 2R from requested Ask101 and stop90.
    assert trade['exit_price'] == 121  # Bid target minus adverse exit slippage.
    assert trade['lots'] == 6  # ($100 * .95) / ($13 stop + $2 slip).
    assert trade['net_pnl_usd'] == 108
    assert result['summary']['end_balance'] == 10108
    assert trade['commission_usd'] == 0


def test_short_exit_uses_ask_extremes():
    frame = bars([(100, 104, 96, 100, 100), (100, 104, 96, 100, 100),
                  (100, 109.5, 96, 105, 100)])
    result = replay(frame, signals(frame, side='sell', stop=110), CONTRACT, SETTINGS)
    trade = result['trades'][0]
    assert trade['entry_price'] == 98
    assert trade['exit_reason'] == 'stop_loss'  # Bid high below stop; Ask high above.
    assert trade['exit_price'] == 112
    assert trade['net_pnl_usd'] < 0


def test_both_levels_same_bar_stop_first_and_gap_worse_open():
    frame = bars([(100, 104, 96, 100, 100), (100, 104, 96, 100, 100),
                  (80, 150, 70, 100, 100)])
    trade = replay(frame, signals(frame), CONTRACT, SETTINGS)['trades'][0]
    assert trade['both_levels_hit']
    assert trade['exit_reason'] == 'stop_loss'
    assert trade['exit_price'] == 78
    assert trade['realized_r'] < -1  # Gaps can exceed planned risk.


def test_cannot_enter_second_signal_before_position_closes_same_bar():
    frame = bars()
    result = replay(frame, signals(frame, indices=(1, 2)), CONTRACT, SETTINGS)
    assert len(result['trades']) == 1
    assert result['decisions'][1]['reason'] == 'position_already_open'


def test_minimum_volume_never_rounded_up_to_make_a_trade():
    contract = ContractSpec('BTCUSDm', .01, 1., .01, 200., .01)
    assert rounded_lots(.00999, contract, .1) == 0
    assert rounded_lots(.029999, contract, .1) == .02
    frame = bars()
    result = replay(frame, signals(frame, stop=.01), CONTRACT,
                    replace(SETTINGS, initial_balance=500.))
    assert result['summary']['closed_trades'] == 0
    assert result['decisions'][0]['reason'] == 'minimum_lot_exceeds_risk_budget'


def test_cost_budget_gate_blocks_expensive_narrow_stop():
    frame = bars()
    result = replay(frame, signals(frame), CONTRACT,
                    replace(SETTINGS, max_friction_stop_fraction=.10))
    assert result['decisions'][0]['reason'] == 'spread_and_slippage_exceed_cost_budget'


def test_reserve_is_budget_not_a_cash_fee():
    frame = bars()
    a = replay(frame, signals(frame), CONTRACT, replace(SETTINGS, risk_reserve_fraction=0))
    b = replay(frame, signals(frame), CONTRACT, replace(SETTINGS, risk_reserve_fraction=.05))
    assert a['trades'][0]['lots'] == b['trades'][0]['lots'] == 6
    assert a['summary']['end_balance'] == b['summary']['end_balance']


def test_explicit_commission_and_financing_reduce_cash():
    frame = bars()
    setting = replace(SETTINGS, commission_bps_per_side=5, financing_usd_per_lot_day=20)
    result = replay(frame, signals(frame), CONTRACT, setting)
    trade = result['trades'][0]
    assert trade['commission_usd'] == pytest.approx((103 + 121) * trade['lots'] * .0005)
    assert trade['financing_usd'] == pytest.approx(20 * trade['lots'] * 2 / 24)
    assert trade['net_pnl_usd'] == pytest.approx(trade['gross_pnl_usd'] - trade['commission_usd'] - trade['financing_usd'])
    assert result['summary']['end_balance'] == pytest.approx(10000 + trade['net_pnl_usd'])


def test_open_position_marked_without_fabricating_closed_trade():
    frame = bars().iloc[:2]
    result = replay(frame, signals(frame), CONTRACT, SETTINGS)
    assert result['summary']['closed_trades'] == 0
    assert result['summary']['open_positions'] == 1
    assert result['open_position'] is not None
    assert result['summary']['end_equity'] < result['summary']['end_balance']


def test_individual_retains_conflicting_raw_signal_combined_skips():
    frame = bars()
    raw = signals(frame)
    raw['eligible_for_selection'] = False
    raw['selected_setup'] = None
    assert len(replay(frame, raw, CONTRACT, SETTINGS, setup_id='B1')['trades']) == 1
    assert replay(frame, raw, CONTRACT, SETTINGS)['summary']['technical_candidates'] == 0


def test_missing_next_open_cannot_move_entry_to_later_bar():
    frame = bars()
    raw = signals(frame)
    frame = frame.drop(frame.index[1])
    result = replay(frame, raw, CONTRACT, SETTINGS)
    assert result['summary']['closed_trades'] == 0
    assert result['decisions'][0]['reason'] == 'missing_entry_candle'


def test_future_bars_do_not_change_earlier_completed_outcome():
    frame = bars()
    first = replay(frame, signals(frame), CONTRACT, SETTINGS)
    extra = pd.DataFrame([[200, 220, 180, 210, 100]], columns=frame.columns,
                         index=[frame.index[-1] + pd.Timedelta(hours=1)])
    second = replay(pd.concat([frame, extra]), signals(frame), CONTRACT, SETTINGS)
    assert first['trades'] == second['trades']


@pytest.mark.parametrize('mutation', ['duplicate', 'naive', 'unaligned', 'bad_high', 'negative_spread', 'nan'])
def test_invalid_data_fail_closed(mutation):
    frame = bars()
    if mutation == 'duplicate':
        frame.index = [frame.index[0]] * len(frame)
    elif mutation == 'naive':
        frame.index = frame.index.tz_localize(None)
    elif mutation == 'unaligned':
        frame.index += pd.Timedelta(minutes=5)
    elif mutation == 'bad_high':
        frame.iloc[0, frame.columns.get_loc('high')] = 90
    elif mutation == 'negative_spread':
        frame.iloc[0, frame.columns.get_loc('spread')] = -1
    else:
        frame.iloc[0, frame.columns.get_loc('close')] = float('nan')
    with pytest.raises(ValueError):
        prepare_bars(frame)


def test_invalid_reward_and_nonfinite_balance_fail_closed():
    frame = bars()
    raw = signals(frame)
    raw['reward_r'] = -1
    with pytest.raises(ValueError, match='reward'):
        replay(frame, raw, CONTRACT, SETTINGS)
    with pytest.raises(ValueError, match='balance'):
        replace(SETTINGS, initial_balance=float('nan'))


def test_quote_atr_floor_and_outward_tick_rounding_match_native_geometry():
    frame = bars()
    raw = signals(frame, stop=90.005)
    raw['atr14'] = 10.
    result = replay(frame.iloc[:2], raw, CONTRACT, SETTINGS)
    opened = result['open_position']
    assert opened['requested_entry_price'] == 101
    assert opened['source_frozen_stop_price'] == 90.005
    assert opened['stop_price'] == 86  # Quote101 minus 1.5ATR15.
    assert opened['target_price'] == 131
    assert opened['planned_risk_usd'] <= opened['risk_budget_usd'] * .95


def test_next_quote_already_beyond_frozen_stop_rejects_instead_of_moving_it():
    frame = bars([(100, 104, 96, 100, 100), (85, 87, 80, 84, 100)])
    raw = signals(frame, stop=90)
    raw['atr14'] = 10.
    result = replay(frame, raw, CONTRACT, SETTINGS)
    assert result['decisions'][0]['reason'] == 'price_already_crossed_frozen_stop'
    assert result['summary']['open_positions'] == 0


def test_archive_requires_checksum_and_native_linear_profit_verification(tmp_path):
    import hashlib
    import json
    from scripts.research_btc import load_archive
    source = tmp_path / 'BTCUSDm_H1.csv'
    bars().reset_index(names='timestamp_utc').to_csv(source, index=False)
    manifest = dict(symbol='BTCUSDm', price_basis='bid', account_currency='USD',
        point=.01, contract_size=1., volume_min=.01, volume_max=200., volume_step=.01,
        csv_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        profit_calculation_verification=dict(volume=.01, price_change_usd=100., buy_profit=1., sell_profit=1.))
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest))
    frame, contract, _, _ = load_archive(tmp_path)
    assert len(frame) == 3 and contract.contract_size == 1
    manifest['profit_calculation_verification']['buy_profit'] = 10
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='profit calculation'):
        load_archive(tmp_path)
    manifest['csv_sha256'] = 'changed'
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='checksum'):
        load_archive(tmp_path)
