"""Reproducible checks of the implemented rules; not a historical backtest."""
import json
from dataclasses import replace
from pathlib import Path

from app.config import Settings, account_configs
from app.market import MarketState
from app.models import FeatureSnapshot, Side, Signal
from app.paper import PaperBroker
from app.strategy import CompositeFlowStrategy, MarketRegime


def calculate():
    settings = replace(Settings.from_env(), symbols=('BTC_USDT',), data_mode='live')
    rows = []
    for mmr in (.001, .005):
        for stop in (.006, .012, .018, .025):
            market = MarketState(settings.symbols)
            state = market.symbol('BTC_USDT')
            state.contract_metadata_ready = state.api_allowed = True
            state.contract_max_leverage = 500
            state.maintenance_margin_rate = mmr
            state.universe_valid_until = 2000
            state.book('mexc').apply_snapshot([[99.995,10000]],[[100.005,10000]],ts=1000)
            broker = PaperBroker(market, settings, account_configs(settings))
            signal = Signal('BTC_USDT','composite','LIQUIDATION_EXHAUSTION',Side.LONG,95,stop,1.8,1000)
            opened = {p.leverage:p for p in broker.handle_signal(signal,1000)}
            for account in broker.accounts.values():
                lev = account.max_leverage
                position = opened.get(lev)
                decision = broker.entry_decisions[f'{account.name}:BTC_USDT']
                rows.append(dict(mmr=mmr, stop_pct=stop*100, leverage=lev,
                    approx_liquidation_distance_pct=(1/lev-mmr-2*settings.taker_fee_rate)*100,
                    result=decision['reason'],
                    notional=position.notional if position else None,
                    initial_margin=position.margin if position else None,
                    planned_risk=position.initial_risk_usdt if position else None,
                    target_move_pct=(position.target_price/position.entry_price-1)*100 if position else None,
                    target_net_profit=2.5*position.margin if position else None))
    regimes = []
    for side in (Side.LONG,Side.SHORT):
        for name,direction in [('RANGE',0),('STRESS',-1),('TREND_UP',1),('TREND_DOWN',-1)]:
            feature = FeatureSnapshot('BTC_USDT',1000,100,1,True,
                price_position=0 if side == Side.LONG else 1,
                flow_fast=.6*side,flow_slow=.4*side,book_imbalance=.5*side,
                microprice_bps=2*side,cross_venue_consensus=.5*side,
                liquidation_imbalance=-.9*side,liquidation_notional_300s=75000,
                liquidation_events_300s=5,bybit_volume_300s=1000000,atr_pct=.002)
            strategy = CompositeFlowStrategy(settings)
            signal = strategy._reversal_candidate(state,feature,MarketRegime(name,direction,.5,0),1000)
            regimes.append(dict(side=side.label,regime=name,signal=bool(signal),
                score=signal.score if signal else None,
                blocker=strategy.last_diagnostics['BTC_USDT'].get('reversal_blocker','')))
    return {'kind':'deterministic_rule_checks_not_backtest', 'sizing':rows,'regimes':regimes}


if __name__ == '__main__':
    output = Path('research_checks.json')
    output.write_text(json.dumps(calculate(),ensure_ascii=False,indent=2),encoding='utf-8')
    print(output.resolve())
