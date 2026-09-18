"""Deterministic liquidation -> feature -> signal -> paper fill -> close checks."""
import json
import time
import unittest
from dataclasses import replace

from app.config import Settings, account_configs
from app.feeds.bybit import BybitFeed
from app.market import MarketState
from app.models import Side, TradeEvent, MinuteBar
from app.orderbook import OrderBook
from app.paper import PaperBroker
from app.strategy import StrategyRouter


class Pipeline(unittest.IsolatedAsyncioTestCase):
    async def test_both_liquidation_directions_reach_real_strategy_and_broker(self):
        for side in (Side.LONG, Side.SHORT):
            with self.subTest(side=side):
                now = time.time()
                settings = replace(Settings.from_env(), symbols=('BTC_USDT',),
                    data_mode='live', startup_warmup_seconds=0, regime_confirm_seconds=0)
                market = MarketState(settings.symbols)
                state = market.symbol('BTC_USDT')
                state.contract_metadata_ready = True
                state.contract_max_leverage = 200
                state.api_allowed = True
                state.maintenance_margin_rate = .005
                state.universe_valid_until = now+3600
                state.bootstrap_hours((int(now)-3600*i, 100) for i in range(201, 0, -1))
                minute = int(now//60)*60
                state.bootstrap_minutes(MinuteBar(minute-60*i,100,100.02,99.98,100,1000)
                                        for i in range(40,0,-1))
                # A 30-minute extreme, followed by aggressive trades in the reversal direction.
                for i in range(1801):
                    price = 101-i/1800 if side == Side.LONG else 99+i/1800
                    state.record_price('mexc', price, now-1801+i)
                for venue in ('mexc', 'bybit'):
                    state.book(venue).apply_snapshot(
                        [[99.995, 1000 if side == Side.LONG else 100]],
                        [[100.005, 100 if side == Side.LONG else 1000]], ts=now)
                    for i in range(30):
                        state.add_trade(TradeEvent('BTC_USDT', venue, 100, 10, side, now-30+i))
                feed = BybitFeed(market, settings)
                # Bybit Buy = liquidated long -> selling pressure -> LONG reversal.
                await feed._handle({'topic': 'allLiquidation.BTCUSDT', 'ts': now*1000,
                    'data': [{'s': 'BTCUSDT', 'S': 'Buy' if side == Side.LONG else 'Sell',
                              'p': '100', 'v': '150', 'T': (now-i)*1000} for i in range(5)]})
                feature = state.features(now, settings.stale_after_seconds)
                self.assertTrue(feature.data_ready)
                self.assertEqual(feature.liquidation_events_300s, 5)
                self.assertEqual(feature.liquidation_notional_300s, 75000)
                self.assertEqual(feature.liquidation_imbalance, -int(side))
                router = StrategyRouter(settings)
                signals = router.evaluate_all(market.symbols, {'BTC_USDT': feature}, now)
                self.assertEqual(len(signals), 1, router.diagnostics())
                signal = signals[0]
                self.assertEqual(signal.setup, 'LIQUIDATION_EXHAUSTION')
                self.assertEqual(signal.side, side)
                broker = PaperBroker(market, settings, account_configs(settings))
                positions = broker.handle_signal(signal, now)
                self.assertEqual([p.leverage for p in positions], [20, 50])
                position = positions[1]
                target = position.entry_price*(1+int(side)*.055)
                state.book('mexc').apply_snapshot([[target-.001,1000]], [[target+.001,1000]], ts=now+1)
                trades = broker.evaluate_positions({}, now+1)
                target_trades = [t for t in trades if t.leverage == 50]
                self.assertEqual(len(target_trades), 1)
                self.assertEqual(target_trades[0].reason, 'TARGET')
                self.assertGreaterEqual(target_trades[0].net_roi_pct, 250)

    async def test_exit_depth_walks_quantity_not_entry_notional(self):
        book = OrderBook('mexc', 'BTC_USDT')
        book.apply_snapshot([[100, 1], [90, 1]], [[101, 1], [110, 1]])
        self.assertAlmostEqual(book.quantity_impact_bps(Side.SHORT, 2), 500)
        self.assertAlmostEqual(book.quantity_impact_bps(Side.LONG, 2), (105.5/101-1)*10000)
        self.assertEqual(book.quantity_impact_bps(Side.LONG, 2.01), float('inf'))

    async def test_crossed_and_nonfinite_book_cannot_supply_bbo(self):
        book = OrderBook('mexc', 'BTC_USDT')
        book.apply_snapshot([[102,1]],[[101,1]])
        self.assertIsNone(book.best_bid_ask())
        book.apply_snapshot([[100,float('inf')]],[[101,1]])
        self.assertIsNone(book.best_bid_ask())

    async def test_rejection_explains_impossible_leverage(self):
        settings = replace(Settings.from_env(), symbols=('BTC_USDT',), data_mode='live')
        market = MarketState(settings.symbols)
        state = market.symbol('BTC_USDT')
        state.universe_valid_until = 2000
        state.contract_metadata_ready = state.api_allowed = True
        state.contract_max_leverage = 500
        broker = PaperBroker(market,settings,account_configs(settings))
        from app.models import Signal
        signal = Signal('BTC_USDT','composite','LIQUIDATION_EXHAUSTION',Side.LONG,95,.006,1.8,1000)
        account = list(broker.accounts.values())[-1]
        self.assertIsNone(broker.open_from_signal(account,signal,1000))
        decision = broker.entry_decisions[f'{account.name}:BTC_USDT']
        self.assertEqual(decision['reason'],'stop_outside_liquidation_buffer')
        self.assertLess(decision['liquidation_distance_pct'],decision['required_stop_buffer_pct'])
