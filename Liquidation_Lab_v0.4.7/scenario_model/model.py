"""Offline USDT linear-contract scenario model; no API and no live orders."""
import argparse, csv, json
from pathlib import Path

def pnl_fraction(move, side, cost, funding=0.0):
    # move is favourable signed return; funding positive means paid.
    return move - cost * (2 + side * move) - funding

def scenario(leverage, side=1, equity=1000, risk=.005, target=2.5,
             stop=.5, fee=.0008, slippage=.0001, mmr=.004, funding=0):
    cost=fee+slippage
    margin=equity*risk/stop
    take=(target/leverage+2*cost+funding)/(1-side*cost)
    loss=(stop/leverage-2*cost-funding)/(1-side*cost)
    # Illustrative isolated margin stress approximation, NOT exchange formula.
    liq=max(0,1/leverage-mmr-fee)
    buffer=.001 # 10 basis points additional separation
    valid=loss>0 and loss+buffer<liq and (side==1 or take<1)
    return dict(leverage=leverage,side='LONG' if side==1 else 'SHORT',
      margin=margin,notional=margin*leverage,target_move_pct=take*100,
      stop_move_pct=loss*100,approx_liquidation_move_pct=liq*100,
      target_pnl=margin*target,planned_stop_loss=margin*stop,
      entry_gate='PASS_SCENARIO_ONLY' if valid else 'REJECT',
      cost_per_side=cost,funding_fraction=funding,
      note='No measured win probability; liquidation is approximate; funding assumed')

def replay(path, s, entry, max_seconds=10800):
    """Replay ONE already selected entry. CSV: ts,mark,bid,ask,funding_fraction.
    ts: Unix seconds, strictly increasing. First row is entry observation.
    Funding column is incremental payment / initial notional (signed).
    Must use market snapshots from actual execution venue. No signal generation.
    """
    if s['entry_gate']!='PASS_SCENARIO_ONLY':
        return {'status':'REJECTED_SCENARIO'}
    side=1 if s['side']=='LONG' else -1
    n=s['notional'];cost=s['cost_per_side'];funding=0;last_ts=None;start=None
    with open(path,newline='') as f:
        for row in csv.DictReader(f):
            ts=float(row['ts']);mark=float(row['mark']);bid=float(row['bid']);ask=float(row['ask'])
            if last_ts is not None and ts<=last_ts: raise ValueError('timestamps must increase')
            if min(mark,bid,ask)<=0 or ask<bid: raise ValueError('invalid market prices')
            if start is None: start=ts
            last_ts=ts; funding+=float(row.get('funding_fraction') or 0)
            exit_price=bid if side==1 else ask
            move=side*(exit_price/entry-1)
            net=n*pnl_fraction(move,side,cost,funding)
            mark_move=side*(mark/entry-1)
            liq_distance=s['approx_liquidation_move_pct']/100-funding
            if mark_move<=-max(0,liq_distance):
                return dict(status='APPROX_LIQUIDATION',ts=ts,net_pnl=None,
                  note='Exact settlement requires exchange risk tier and fees; do not use as realised PnL')
            reason=('STOP' if net<=-s['planned_stop_loss'] else
                    'TARGET' if net>=s['target_pnl'] else
                    'TIME_STOP' if ts-start>=max_seconds else None)
            if reason: return dict(status=reason,ts=ts,exit_reference=exit_price,
                                  net_pnl=net,net_roi_pct=net/s['margin']*100)
    return dict(status='OPEN_AT_DATA_END',net_pnl=None,
                note='No closed trade; missing ticks may hide stops/liquidation')

def main():
    p=argparse.ArgumentParser();p.add_argument('--ticks');p.add_argument('--entry',type=float)
    p.add_argument('--leverage',type=int,default=50);p.add_argument('--side',choices=['LONG','SHORT'],default='LONG')
    p.add_argument('--out',default='results.json');a=p.parse_args()
    if a.ticks:
        if not a.entry or a.entry<=0:p.error('--entry must be positive')
        out=replay(a.ticks,scenario(a.leverage,1 if a.side=='LONG' else -1),a.entry)
    else:
        out={'kind':'SCENARIO_NOT_BACKTEST','scenarios':[scenario(l,side) for l in (20,50,100,200) for side in (1,-1)],
             'cost_stress':[scenario(l,slippage=.001, funding=.0001) for l in (20,50,100,200)]}
    Path(a.out).write_text(json.dumps(out,indent=2,ensure_ascii=False));print(json.dumps(out,indent=2,ensure_ascii=False))
if __name__=='__main__':main()
