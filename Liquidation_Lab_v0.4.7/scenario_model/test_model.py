import unittest,tempfile
from pathlib import Path
from model import scenario,pnl_fraction,replay
class ModelTests(unittest.TestCase):
 def test_target_stop_net_costs_both_directions(self):
  for side in (-1,1):
   for l in (20,50,100,200):
    s=scenario(l,side)
    self.assertAlmostEqual(l*pnl_fraction(s['target_move_pct']/100,side,s['cost_per_side']),2.5)
    self.assertAlmostEqual(l*pnl_fraction(-s['stop_move_pct']/100,side,s['cost_per_side']),-.5)
 def test_risk_gate(self):
  self.assertEqual(scenario(200)['entry_gate'],'REJECT')
  self.assertEqual(scenario(50)['entry_gate'],'PASS_SCENARIO_ONLY')
 def test_gap_fills_at_observed_price(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'ticks.csv';p.write_text('ts,mark,bid,ask\n1,100,100,100\n2,98.9,98.9,98.9\n')
   r=replay(p,scenario(50),100)
   self.assertEqual(r['status'],'STOP');self.assertLess(r['net_pnl'],-5)
 def test_liquidation_not_reported_as_winning_trade(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'ticks.csv';p.write_text('ts,mark,bid,ask\n1,100,100,100\n2,98,106,106\n')
   r=replay(p,scenario(50),100)
   self.assertEqual(r['status'],'APPROX_LIQUIDATION');self.assertIsNone(r['net_pnl'])
if __name__=='__main__':unittest.main()
