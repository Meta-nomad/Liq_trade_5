import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings, account_configs
from app.feeds.binance import BinanceFeed
from app.market import MarketState
from app.models import Side, Signal
from app.paper import PaperBroker
from app.storage import Storage


class Operations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = replace(Settings.from_env(), symbols=('BTC_USDT',), data_mode='synthetic')
        self.market = MarketState(self.settings.symbols)

    async def test_one_channel_cannot_hide_failure_of_other(self):
        feed = BinanceFeed(self.market, self.settings)
        feed._channel_state('trades', True)
        feed._channel_state('book', True)
        self.assertTrue(self.market.feeds['binance'].connected)
        self.market.symbol('BTC_USDT').book('binance').apply_snapshot([[100,1]],[[101,1]])
        feed._channel_state('book', False, 'connection lost')
        feed._channel_state('trades', True)
        self.assertFalse(self.market.feeds['binance'].connected)
        self.assertIn('connection lost', self.market.feeds['binance'].last_error)
        self.assertIsNone(self.market.symbol('BTC_USDT').book('binance').best_bid_ask())
        feed._channel_state('book', True)
        self.assertEqual(self.market.feeds['binance_book'].reconnects, 1)
        self.assertEqual(self.market.feeds['binance'].last_error, '')

    async def test_short_lived_connections_do_not_reset_backoff(self):
        delays = []; delay = 1
        for _ in range(6):
            delay = BinanceFeed._next_backoff(delay, 12)
            delays.append(delay)
        self.assertEqual(delays, [2,4,8,16,30,30])
        self.assertEqual(BinanceFeed._next_backoff(30, 61), 1)

    async def test_subscription_rejection_is_not_reported_as_online(self):
        feed = BinanceFeed(self.market, self.settings)
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, value): pass
            async def recv(self):
                feed._stop.set()
                return json.dumps({'code': 2, 'msg': 'invalid request'})
        with patch('app.feeds.binance.websockets.connect', return_value=Socket()):
            await feed._run_channel('book', feed.public_ws_url)
        self.assertFalse(self.market.feeds['binance_book'].connected)
        self.assertIn('invalid request', self.market.feeds['binance_book'].last_error)

    async def test_daily_halt_visible_and_not_bypassed_when_account_empty(self):
        broker = PaperBroker(self.market, self.settings, account_configs(self.settings))
        account = next(iter(broker.accounts.values()))
        account.stop_losses_today = 3
        account.halted_reason = 'daily stop-count 3'
        signal = Signal('BTC_USDT','composite','LIQUIDATION_EXHAUSTION',Side.LONG,95,.006,1.8,time.time())
        self.assertIsNone(broker.open_from_signal(account, signal, time.time()))
        summary = broker.account_summary(account)
        self.assertEqual(summary['entry_state'], 'halted')
        self.assertEqual(summary['positions_count'], 0)
        self.assertEqual(next(iter(broker.entry_decisions.values()))['reason'], 'daily stop-count 3')

    async def test_decisions_survive_restart_and_rollback_with_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'test.db'; store = Storage(path)
            await store.initialise()
            decision = dict(ts=1000, account='paper', reason='daily stop-count 3')
            await store.save_checkpoint(1000, [], {}, [decision])
            with self.assertRaises(ValueError):
                await store.save_checkpoint(1001, [], {'broken': {'value': float('nan')}},
                                            [{**decision, 'ts':1001}])
            await store.close()
            restored = Storage(path); await restored.initialise()
            self.assertEqual(await restored.recent_decisions(), [decision])
            await restored.close()

    async def test_lifecycle_cancel_marks_both_channels_offline(self):
        feed = BinanceFeed(self.market, self.settings)
        class Socket:
            def __init__(self): self.kind = ''; self.sent = False
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, raw): self.kind = json.loads(raw)['id']
            async def recv(self):
                if not self.sent:
                    self.sent = True
                    return json.dumps({'result': None, 'id': self.kind})
                await asyncio.Event().wait()
        with patch('app.feeds.binance.websockets.connect', side_effect=lambda *a,**kw: Socket()):
            task = asyncio.create_task(feed.run())
            await asyncio.sleep(.03)
            self.assertTrue(self.market.feeds['binance'].connected)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.market.feeds['binance_book'].connected)
        self.assertFalse(self.market.feeds['binance_trades'].connected)
