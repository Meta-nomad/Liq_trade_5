import sys,types,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
try:import dotenv
except ImportError:
 m=types.ModuleType('dotenv');m.load_dotenv=lambda:None;sys.modules['dotenv']=m
from app.universe import UniverseGate
from app.market import MarketState
class Tests(unittest.IsolatedAsyncioTestCase):
 async def test_denial_blocks_without_raising_and_recovers(self):
  symbols=('BTC_USDT','ETH_USDT');market=MarketState(symbols);gate=UniverseGate()
  async def denied(s):raise RuntimeError('403 Forbidden')
  delay=await gate.refresh(symbols,market,denied)
  self.assertEqual(delay,900);self.assertIn('403',gate.error)
  self.assertTrue(all(s.universe_valid_until==0 for s in market.symbols.values()))
  async def accepted(s):return ('BTC_USDT','UNCONFIGURED_USDT')
  self.assertEqual(await gate.refresh(symbols,market,accepted),3600)
  self.assertEqual(gate.error,'');self.assertEqual(gate.eligible_count,1)
  self.assertGreater(market.symbol('BTC_USDT').universe_valid_until,0)
  self.assertEqual(market.symbol('ETH_USDT').universe_valid_until,0)
  await gate.refresh(symbols,market,denied)
  self.assertEqual(market.symbol('BTC_USDT').universe_valid_until,0)
 async def test_empty_result_blocks(self):
  market=MarketState(('BTC_USDT',));gate=UniverseGate()
  async def empty(s):return ()
  self.assertEqual(await gate.refresh(('BTC_USDT',),market,empty),900)
  self.assertEqual(gate.eligible_count,0)
if __name__=='__main__':unittest.main()
