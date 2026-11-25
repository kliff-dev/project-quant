"""Offline comparison of predeclared holding periods using cached broker H1 bars.

No MT5 import, connection or order path. Results are preliminary modeled outcomes,
not actual fills, account returns or evidence of a profitable trading strategy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from analytics.setup_research import signal_features, validate_bars

LONDON, NEW_YORK = ZoneInfo('Europe/London'), ZoneInfo('America/New_York')


def plan_entry(row, next_bid: float, spec: dict) -> dict | None:
    """Linear EUR/USD USD-account equivalent of the current reserved 2R sizing."""
    long = bool(row.pullback_long or row.breakout_long)
    short = bool(row.pullback_short or row.breakout_short)
    if long == short:
        return None
    direction = 1 if long else -1
    tick, size, step = Decimal('.00001'), Decimal('100000'), Decimal('.01')
    spread = Decimal(str(row.spread))*tick
    slip = Decimal(str(spec['slippage_points_each_side']))*tick
    if not 0 < spread <= Decimal('.0002'):
        return None
    bid = Decimal(str(next_bid))
    entry = bid + spread if long else bid
    atr = Decimal(str(row.atr14))
    # Decimal arithmetic keeps target/lot rounding comparable to live policy.
    stop = min(Decimal(str(row.low5))-Decimal('.1')*atr, entry-atr) if long else max(Decimal(str(row.high5))+spread+Decimal('.1')*atr, entry+atr)
    rounding = ROUND_FLOOR if long else ROUND_CEILING
    stop = (stop/tick).to_integral_value(rounding=rounding)*tick
    distance = direction*(entry-stop)
    if (spread+2*slip)/distance > Decimal('.1'):
        return None
    fee, budget = Decimal(str(spec['additional_cost_allowance_usd_per_trade'])), Decimal(str(spec['risk_budget_usd']))
    raw_lots = (budget-fee)/(size*(distance+2*slip))
    lots = min(Decimal(str(spec['max_lots'])), (raw_lots/step).to_integral_value(rounding=ROUND_FLOOR)*step)
    if lots < step:
        return None
    planned_risk = size*lots*(distance+2*slip)+fee
    raw_target = entry + direction*(2*slip+(2*planned_risk+fee)/(size*lots))
    target = (raw_target/tick).to_integral_value(rounding=ROUND_CEILING if long else ROUND_FLOOR)*tick
    pullback = bool(row.pullback_long if long else row.pullback_short)
    breakout = bool(row.breakout_long if long else row.breakout_short)
    return dict(direction=direction, reference_entry=float(entry), executed_entry=float(entry+direction*slip),
        stop=float(stop), target=float(target), lots=float(lots), planned_risk=float(planned_risk),
        fee=float(fee), slip=float(slip), point=float(tick), size=float(size),
        trigger='both' if pullback and breakout else 'pullback' if pullback else 'breakout')


def deadline_for(entry_time: pd.Timestamp, variant: str) -> pd.Timestamp:
    if variant == 'same_day_16_london':
        local = entry_time.tz_convert(LONDON)
        return local.replace(hour=16,minute=0,second=0,microsecond=0).tz_convert('UTC')
    return entry_time + pd.Timedelta(hours={'24h_cap':24,'72h_cap':72}[variant])


def exit_trade(data: pd.DataFrame, start: int, plan: dict, deadline: pd.Timestamp, ambiguity: str) -> dict | None:
    """Bid/approximated ask triggers; adverse gaps get worse available open fills."""
    direction, stop, target = plan['direction'], plan['stop'], plan['target']
    for j in range(start,len(data)):
        row = data.iloc[j]
        stamp = data.index[j]
        shift = row.spread*plan['point'] if direction == -1 else 0.
        opening, high, low = row.open+shift, row.high+shift, row.low+shift
        gap_stop = opening <= stop if direction == 1 else opening >= stop
        gap_target = opening >= target if direction == 1 else opening <= target
        if gap_stop:
            exit_price, reason, effective, ambiguous = opening-direction*plan['slip'], 'stop_gap', stamp, False
        elif gap_target:
            exit_price, reason, effective, ambiguous = target-direction*plan['slip'], 'target_gap', stamp, False
        elif stamp >= deadline:
            exit_price, reason, effective, ambiguous = opening-direction*plan['slip'], 'time_exit', stamp, False
        else:
            stop_hit = low <= stop if direction == 1 else high >= stop
            target_hit = high >= target if direction == 1 else low <= target
            if not stop_hit and not target_hit:
                continue
            ambiguous = bool(stop_hit and target_hit)
            target_first = target_hit and (not stop_hit or ambiguity == 'target_first')
            exit_price = (target if target_first else stop)-direction*plan['slip']
            reason = 'target' if target_first else 'stop'
            # Actual tick time is unknown. This upper bound allows a fresh
            # signal at the next close in the one-position sensitivity.
            effective = stamp+pd.Timedelta(hours=1)-pd.Timedelta(microseconds=1)
        pnl = direction*(exit_price-plan['executed_entry'])*plan['size']*plan['lots']-plan['fee']
        return dict(exit_time_utc=effective.isoformat(), exit_bar_open_utc=stamp.isoformat(), exit_price=float(exit_price),
            reason=reason, ambiguous_bar=ambiguous, modeled_pnl_before_swap=float(pnl), r_before_swap=float(pnl/plan['planned_risk']),
            holding_hours_upper_bound=float((effective-data.index[start]).total_seconds()/3600))
    return None


def rollover_units(entry: pd.Timestamp, exit_time: pd.Timestamp) -> int:
    units = 0
    local_start, local_end = entry.tz_convert(NEW_YORK), exit_time.tz_convert(NEW_YORK)
    day = local_start.date()
    while day <= local_end.date():
        stamp = pd.Timestamp(datetime.combine(day,datetime.min.time(),tzinfo=NEW_YORK).replace(hour=17))
        if day.weekday() < 5 and local_start < stamp <= local_end:
            units += 3 if day.weekday() == 2 else 1
        day += timedelta(days=1)
    return units


def summarize(frame: pd.DataFrame, charge: float) -> dict:
    pnl = frame.modeled_pnl_before_swap-charge*frame.lots/.01*frame.rollover_units
    r = pnl/frame.planned_risk
    wins, losses = pnl[pnl>1e-9], pnl[pnl < -1e-9]
    return dict(n=len(frame), win_rate_percent=float((pnl>1e-9).mean()*100),
        target_hit_percent=float(frame.reason.isin(['target','target_gap']).mean()*100),
        stop_hit_percent=float(frame.reason.isin(['stop','stop_gap']).mean()*100),
        time_exit_percent=float(frame.reason.eq('time_exit').mean()*100), mean_r=float(r.mean()),
        mean_win_r=float(r[pnl>1e-9].mean()) if len(wins) else None,
        mean_loss_r=float(r[pnl < -1e-9].mean()) if len(losses) else None,
        profit_factor=float(wins.sum()/-losses.sum()) if len(losses) else None,
        median_holding_hours_upper_bound=float(frame.holding_hours_upper_bound.median()),
        ambiguous_exit_count=int(frame.ambiguous_bar.sum()),
        before_unmodeled_financing=charge==0)


def run(spec: dict, data: pd.DataFrame) -> tuple[pd.DataFrame,list[dict]]:
    features = signal_features(data)
    candidates = features.loc[features.session & features.recent_is_consecutive & features.next_is_consecutive &
        (features.pullback_long|features.breakout_long|features.pullback_short|features.breakout_short)]
    buffer_end = data.index[-1]-pd.Timedelta(days=spec['common_endpoint_buffer_days'])
    rows, exclusions = [], []
    for candidate in candidates.itertuples():
        if candidate.decision_time > buffer_end:
            exclusions.append(dict(time=candidate.decision_time.isoformat(),reason='common_followup_buffer'))
            continue
        start = data.index.get_loc(candidate.Index)+1
        plan = plan_entry(candidate,float(data.open.iloc[start]),spec)
        if plan is None:
            exclusions.append(dict(time=candidate.decision_time.isoformat(),reason='risk_lot_or_cost_filter'))
            continue
        outcomes = []
        for ambiguity in spec['ambiguity_cases']:
            for variant in spec['exit_variants']:
                outcome = exit_trade(data,start,plan,deadline_for(candidate.decision_time,variant),ambiguity)
                if outcome is None:
                    break
                entry_time = data.index[start]
                exit_time = pd.Timestamp(outcome['exit_time_utc'])
                outcomes.append(dict(entry_time_utc=entry_time.isoformat(),variant=variant,ambiguity=ambiguity,
                    period='development' if entry_time<pd.Timestamp('2025-11-13',tz='UTC') else 'later_1' if entry_time<pd.Timestamp('2026-05-13',tz='UTC') else 'later_2',
                    **plan,**outcome,rollover_units=rollover_units(entry_time,exit_time)))
        if len(outcomes) != len(spec['ambiguity_cases'])*len(spec['exit_variants']):
            exclusions.append(dict(time=candidate.decision_time.isoformat(),reason='incomplete_followup'))
            continue
        rows.extend(outcomes)
    return pd.DataFrame(rows),exclusions


def main() -> None:
    spec_path=ROOT/'reports/market_analysis/holding_period_spec_2026-11-14.json'
    spec=json.loads(spec_path.read_text(encoding='utf-8'))
    manifest_path=ROOT/spec['input_manifest']
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    item=next(i for i in manifest['datasets'] if i['symbol']==spec['symbol'])
    contract=item['contract']
    if (contract['currency_base']!='EUR' or contract['currency_profit']!='USD' or
        contract['trade_contract_size']!=100000 or contract['point']!=.00001 or
        contract['trade_tick_size']!=.00001 or contract['volume_min']!=.01 or contract['volume_step']!=.01):
        raise ValueError('The linear historical sizing model requires the registered EUR/USD USD contract.')
    source=ROOT/item['file']
    if hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']:
        raise ValueError('Registered input checksum differs.')
    raw=pd.read_csv(source)
    raw.index=pd.DatetimeIndex(pd.to_datetime(raw.pop('timestamp_utc'),utc=True))
    data=validate_bars(raw)
    outcomes,excluded=run(spec,data)
    if outcomes.empty:
        raise ValueError('No eligible paired opportunities.')
    summaries=[]
    for (ambiguity,variant),group in outcomes.groupby(['ambiguity','variant']):
        # Paired opportunities overlap; sequential variant permits one open
        # position at a time, on a fixed starting risk basis (no compounding).
        sequential=[]
        busy_until=None
        for _,row in group.sort_values('entry_time_utc').iterrows():
            stamp=pd.Timestamp(row.entry_time_utc)
            if busy_until is not None and stamp<=busy_until:
                continue
            sequential.append(row.to_dict())
            busy_until=pd.Timestamp(row.exit_time_utc)
        modes={'paired':group,'one_position':pd.DataFrame(sequential)}
        for mode,frame in modes.items():
            for period in ['all','development','later_1','later_2']:
                part=frame if period=='all' else frame.loc[frame.period.eq(period)]
                if part.empty:
                    continue
                for charge in [0.,.1]:
                    summaries.append(dict(mode=mode,ambiguity=ambiguity,variant=variant,period=period,
                        swap_charge_usd_per_001lot_unit=charge,**summarize(part,charge)))
    output=ROOT/'reports/market_analysis/holding_period_2026-11-14'
    output.mkdir(parents=True,exist_ok=True)
    outcomes.to_csv(output/'outcomes.csv',index=False,float_format='%.10g')
    pd.DataFrame(summaries).to_csv(output/'summary.csv',index=False,float_format='%.10g')
    result=dict(study=spec['study'],generated_at_utc=datetime.now(timezone.utc).isoformat(),spec=spec,
        source_file=item['file'],source_sha256=item['sha256'],spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        feature_code_sha256=hashlib.sha256((ROOT/'analytics/setup_research.py').read_bytes()).hexdigest(),
        raw_entry_candidates=len(outcomes.entry_time_utc.unique())+len(excluded),eligible_common_entries=len(outcomes.entry_time_utc.unique()),
        exclusions=excluded,summaries=summaries)
    (output/'summary.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    selected=[s for s in summaries if s['period']=='all' and s['ambiguity']=='stop_first' and s['swap_charge_usd_per_001lot_unit']==0]
    print(json.dumps(dict(eligible_common_entries=result['eligible_common_entries'],summary=selected,output=str(output)),indent=2))


if __name__=='__main__':
    main()
