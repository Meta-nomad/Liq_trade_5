import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings, account_configs
from app.market import MarketState
from app.models import Signal, Side, FeatureSnapshot
from app.paper import PaperBroker
from app.strategy import CompositeFlowStrategy, MarketRegime, StrategyRouter
from app.storage import Storage
from app.universe import UniverseGate


def broker():
    settings = replace(Settings.from_env(), data_mode='synthetic', symbols=('BTC_USDT',),
                       cooldown_seconds=900, stale_after_seconds=1000)
    market = MarketState(settings.symbols)
    seed(market, 100)
    return settings, market, PaperBroker(market, settings, account_configs(settings))


def seed(market, price):
    for venue in ('mexc', 'bybit'):
        market.symbol('BTC_USDT').book(venue).apply_snapshot([[price,10000]], [[price+.001,10000]],ts=1000)


def signal(side=Side.LONG):
    return Signal('BTC_USDT','composite','LIQUIDATION_EXHAUSTION',side,95,.006,1.8,1000,risk_pct=.5,exit_mode='fixed',max_holding_minutes=180)


def test_defaults_ignore_legacy_roi_switch(monkeypatch):
    monkeypatch.setenv('HIGH_LEVERAGE_LAB','true')
    monkeypatch.setenv('SYMBOLS','BTC_USDT')
    monkeypatch.delenv('FLOW_SYMBOLS', raising=False)
    settings=Settings.from_env()
    assert not settings.high_leverage_lab
    assert len(settings.symbols)==30
    assert len(account_configs(settings))==1


@pytest.mark.parametrize('side',[Side.LONG,Side.SHORT])
def test_net_target_and_risk(side):
    s,m,b=broker(); a=next(iter(b.accounts.values()))
    p=b.open_from_signal(a,signal(side),1000)
    assert p and p.initial_risk_usdt<=5
    # Net target scales with risk, not with margin or leverage.
    assert p.target_r==1.8
    seed(m,p.entry_price*(1+int(side)*.013))
    assert not b.evaluate_positions({},1100)
    seed(m,p.entry_price*(1+int(side)*.016))
    trades=b.evaluate_positions({},1200)
    assert len(trades)==1 and trades[0].reason=='TARGET'
    assert trades[0].r_multiple>=1.8
    assert abs(a.balance-(1000+trades[0].net_pnl))<1e-8
    assert not b.can_open(a,signal(side),1201)


def test_liquidation_buffer_applies_to_regular_mode():
    s,m,b=broker(); b.settings=replace(s,data_mode='live')
    st=m.symbol('BTC_USDT'); st.universe_valid_until=2000
    a=next(iter(b.accounts.values()))
    assert b.open_from_signal(a,signal(),1000) is None


def test_portfolio_cap_blocks_excess_risk():
    s,m,b=broker(); b.settings=replace(s,max_portfolio_risk_pct=.4)
    a=next(iter(b.accounts.values()))
    assert b.open_from_signal(a,signal(),1000) is None


def test_stop_and_time_exit():
    for move,age,reason in [(-.007,100,'STOP'),(0,10801,'TIME_STOP')]:
        s,m,b=broker(); b.settings=replace(s,stale_after_seconds=20000)
        a=next(iter(b.accounts.values())); p=b.open_from_signal(a,signal(),1000)
        seed(m,p.entry_price*(1+move))
        trades=b.evaluate_positions({},1000+age)
        assert len(trades)==1 and trades[0].reason==reason


def test_proxy_removed_and_old_regime_restored():
    s,m,b=broker(); strategy=CompositeFlowStrategy(s)
    f=FeatureSnapshot('BTC_USDT',1000,100,1,True,flow_fast=-.9,book_imbalance=-.9,trade_count_60s=200)
    assert strategy._reversal_candidate(m.symbol('BTC_USDT'),f,MarketRegime('RANGE',0,.5,0),1000) is None
    assert strategy.last_diagnostics['BTC_USDT']['reversal_blocker']=='no_observed_liquidations'
    assert strategy._reversal_candidate(m.symbol('BTC_USDT'),f,MarketRegime('TREND_UP',1,.8,0),1100) is None
    assert strategy.last_diagnostics['BTC_USDT']['reversal_blocker']=='reversal_wrong_regime'


def test_real_liquidation_signal_keeps_18r():
    s,m,b=broker(); strategy=CompositeFlowStrategy(s)
    f=FeatureSnapshot('BTC_USDT',1000,100,1,True,price_position=0,
        flow_fast=.9,flow_slow=.9,book_imbalance=.9,cross_venue_consensus=.9,
        liquidation_imbalance=-1,liquidation_notional_300s=100000,
        liquidation_events_300s=10,bybit_volume_300s=1000000)
    result=strategy._reversal_candidate(m.symbol('BTC_USDT'),f,MarketRegime('RANGE',0,.5,0),1000)
    assert result and result.side==Side.LONG and result.target_r==1.8


def test_diagnostics_do_not_freeze_excluded_symbols():
    s,m,b=broker(); router=StrategyRouter(s)
    router.composite.last_diagnostics['OLD']={'state':'warmup'}
    router.evaluate_all({}, {},1000)
    assert 'OLD' not in router.diagnostics()


def test_universe_never_invents_metadata():
    s,m,b=broker(); UniverseGate._mexc_fallback(s.symbols,m)
    assert not m.symbol('BTC_USDT').contract_metadata_ready


def test_export_and_restore(tmp_path):
    async def run():
        s,m,b=broker(); a=next(iter(b.accounts.values()))
        p=b.open_from_signal(a,signal(),1000)
        seed(m,102)
        trade=b.close_position(a,p,'TARGET',1100)
        db=Storage(tmp_path/'journal.db'); await db.initialise()
        await db.save_trade(trade)
        await db.save_account_states(1100,b.snapshots())
        await db.close()
        db=Storage(tmp_path/'journal.db'); await db.initialise()
        rows=await db.all_trades()
        assert len(rows)==1 and rows[0]['id']==trade.id
        _,_,restored=broker(); restored.restore(await db.load_account_states())
        assert next(iter(restored.accounts.values())).balance==a.balance
        await db.close()
    asyncio.run(run())


def test_dashboard_export_auth_and_service_start(tmp_path,monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from app import api
    from app.service import PaperTradingService
    settings=replace(Settings.from_env(),data_mode='synthetic',db_path=tmp_path/'service.db',
                     dashboard_token='test-secret',evaluation_interval_seconds=.05)
    monkeypatch.setattr(api,'settings',settings)
    monkeypatch.setattr(api,'service',PaperTradingService(settings))
    with TestClient(api.app) as client:
        time.sleep(.25)
        assert client.get('/api/trades/export').status_code==401
        headers={'x-dashboard-token':'test-secret'}
        r=client.get('/api/trades/export',headers=headers)
        assert r.status_code==200 and r.json()==[]
        status=client.get('/api/status',headers=headers).json()
        assert status['version']=='0.5.1' and status['live_trading_enabled'] is False
        assert status['last_engine_error']==''
        assert len(status['accounts'])==1
