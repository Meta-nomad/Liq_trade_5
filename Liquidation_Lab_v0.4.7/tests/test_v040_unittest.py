"""Offline core tests. Optional dotenv loader stub only; network/service not tested."""
import sys,types,unittest,tempfile,time
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
try: import dotenv
except ImportError:
 m=types.ModuleType('dotenv');m.load_dotenv=lambda:None;sys.modules['dotenv']=m
from app.config import Settings,account_configs
from app.market import MarketState
from app.models import Signal,Side,FeatureSnapshot,LiquidationEvent
from app.paper import PaperBroker
from app.universe import exclusion
from app.strategy import CompositeFlowStrategy, MarketRegime

class Tests(unittest.TestCase):
 def setup_broker(self,live=False):
  s=replace(Settings.from_env(),symbols=('BTC_USDT',),data_mode='live' if live else 'synthetic',cooldown_seconds=0,stale_after_seconds=1000,high_leverage_lab=True)
  m=MarketState(s.symbols);self.seed(m,100,100.01);st=m.symbol('BTC_USDT');st.contract_metadata_ready=True;st.contract_max_leverage=200;st.maintenance_margin_rate=.005;st.universe_valid_until=2000
  return s,m,PaperBroker(m,s,account_configs(s))
 def seed(self,m,bid,ask):
  for venue in ('mexc','bybit'):m.symbol('BTC_USDT').book(venue).apply_snapshot([[bid,1000]],[[ask,1000]],ts=1000)
 def sig(self,side=Side.LONG):return Signal('BTC_USDT','composite','LIQUIDATION_EXHAUSTION',side,95,.006,1.8,1000,risk_pct=.5)
 def test_liquidation_gate_preserves_volatility_stop(self):
  s,m,b=self.setup_broker(True);opened=b.handle_signal(self.sig(),1000)
  self.assertEqual([p.leverage for p in opened],[20,50])
  self.assertTrue(all(p.initial_risk_usdt<=2.5 for p in opened))
 def test_mexc_flow_exhaustion_is_rejected_in_high_leverage_lab(self):
  s,m,b=self.setup_broker(True)
  signal=replace(self.sig(),setup='MEXC_FLOW_EXHAUSTION')
  opened=b.handle_signal(signal,1000)
  self.assertEqual(opened,[])
 def test_missing_metadata_blocks_all(self):
  s,m,b=self.setup_broker(True);m.symbol('BTC_USDT').contract_metadata_ready=False
  self.assertEqual(b.handle_signal(self.sig(),1000),[])
 def test_unknown_execution_depth_blocks_entry(self):
  s,m,b=self.setup_broker();m.symbol('BTC_USDT').book('mexc').asks={100.01:.00001}
  self.assertEqual(b.handle_signal(self.sig(),1000),[])
 def test_net_target_both_sides(self):
  for side in (Side.LONG,Side.SHORT):
   s,m,b=self.setup_broker();a=list(b.accounts.values())[1];p=b.open_from_signal(a,self.sig(side),1000);self.assertIsNotNone(p)
   # Cross target enough to include exact exit costs.
   price=p.entry_price*(1+int(side)*.055)
   self.seed(m,price,price+.001)
   trades=b.evaluate_positions({},1100)
   self.assertEqual(len(trades),1);self.assertEqual(trades[0].reason,'TARGET')
   self.assertGreaterEqual(trades[0].net_pnl/p.margin,2.5)
 def test_under_target_does_not_exit_at_250(self):
  s,m,b=self.setup_broker();a=list(b.accounts.values())[1];p=b.open_from_signal(a,self.sig(),1000)
  self.seed(m,p.entry_price*1.049,p.entry_price*1.049+.001)
  self.assertEqual(b.evaluate_positions({},1100),[])
 def test_one_position_per_account(self):
  s,m,b=self.setup_broker();a=list(b.accounts.values())[0];b.open_from_signal(a,self.sig(),1000)
  self.assertFalse(b.can_open(a,replace(self.sig(),symbol='ETH_USDT'),1001))
 def test_liquidation_absolute_and_relative_filters(self):
  s,m,b=self.setup_broker();strategy=CompositeFlowStrategy(s);st=m.symbol('BTC_USDT')
  f=FeatureSnapshot('BTC_USDT',1000,100,1,True,liquidation_imbalance=1,liquidation_notional_300s=100000,liquidation_events_300s=1,bybit_volume_300s=1000000)
  regime=MarketRegime('RANGE',0,.5,0)
  self.assertIsNone(strategy._reversal_candidate(st,f,regime,1000))
  self.assertEqual(strategy.last_diagnostics[st.symbol]['reversal_blocker'],'liquidation_too_few_events')
  f.liquidation_events_300s=5;f.liquidation_notional_300s=100
  self.assertIsNone(strategy._reversal_candidate(st,f,regime,1020))
  self.assertEqual(strategy.last_diagnostics[st.symbol]['reversal_blocker'],'liquidation_volume_too_small')
  f.liquidation_notional_300s=60000;f.bybit_volume_300s=100000000
  self.assertIsNone(strategy._reversal_candidate(st,f,regime,1040))
  self.assertEqual(strategy.last_diagnostics[st.symbol]['reversal_blocker'],'liquidation_volume_too_small')
 def test_universe_rejects_new_low_volume_delist(self):
  now=time.time();meta=dict(status='Trading',contractType='LinearPerpetual',quoteCoin='USDT',launchTime=(now-400*86400)*1000)
  tick=dict(turnover24h=20e6,bid1Price=100,ask1Price=100.01)
  self.assertEqual(exclusion(meta,tick,now),'')
  self.assertTrue(exclusion({**meta,'launchTime':now*1000},tick,now))
  self.assertTrue(exclusion(meta,{**tick,'turnover24h':1},now))
  self.assertTrue(exclusion({**meta,'deliveryTime':(now+86400)*1000},tick,now))
if __name__=='__main__':unittest.main()
