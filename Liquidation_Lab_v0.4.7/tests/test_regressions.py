import asyncio
import json
import sqlite3
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from app.config import Settings, account_configs
from app.feeds.bybit import BybitFeed
from app.feeds.mexc import MexcFeed
from app.market import MarketState
from app.models import FeatureSnapshot, Side, Signal
from app.paper import PaperBroker
from app.service import PaperTradingService
from app.storage import Storage
from app.strategy import CompositeFlowStrategy, MarketRegime
from app.universe import UniverseGate


class Regressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = replace(Settings.from_env(), symbols=('BTC_USDT',), data_mode='live')
        self.market = MarketState(self.settings.symbols)
        self.state = self.market.symbol('BTC_USDT')
        self.now = time.time()
        self.state.book('mexc').apply_snapshot([[100, 100]], [[100.01, 100]], ts=self.now)

    async def test_universe_does_not_invent_metadata(self):
        gate = UniverseGate()
        await gate.refresh_mexc_only(self.settings.symbols, self.market)
        self.assertEqual(gate.eligible_count, 0)
        self.assertFalse(self.state.contract_metadata_ready)
        self.assertEqual(self.state.contract_max_leverage, 0)

    async def test_universe_requires_permission_history_and_fresh_book(self):
        gate = UniverseGate()
        self.state.contract_metadata_ready = True
        self.state.api_allowed = False
        self.state.bootstrap_hours((int(self.now)-3600*(i+2), 100) for i in range(200))
        await gate.refresh_mexc_only(self.settings.symbols, self.market)
        self.assertEqual(gate.eligible_count, 0)
        self.state.api_allowed = True
        await gate.refresh_mexc_only(self.settings.symbols, self.market)
        self.assertEqual(gate.eligible_count, 1)
        self.state.hour_closes.clear()
        await gate.refresh_mexc_only(self.settings.symbols, self.market)
        self.assertEqual(gate.eligible_count, 0)
        self.assertEqual(self.state.universe_valid_until, 0)

    async def test_empty_and_incomplete_contract_responses_rejected(self):
        feed = MexcFeed(self.market, self.settings)
        for payload in ({}, {'symbol': 'BTC_USDT'}, {'symbol': 'OTHER_USDT'}):
            feed._get = AsyncMock(return_value=payload)
            with self.assertRaises((ValueError, KeyError)):
                await feed._load_contract('BTC_USDT')
            self.assertFalse(self.state.contract_metadata_ready)

    async def test_metadata_contract_units_and_regional_leverage(self):
        feed = MexcFeed(self.market, self.settings)
        feed._get = AsyncMock(return_value=dict(
            symbol='BTC_USDT', contractSize=.0001, maintenanceMarginRate=.005,
            maxLeverage=200, countryConfigContractMaxLeverage=50, state=0,
            quoteCoin='USDT', settleCoin='USDT', futureType=1, apiAllowed=True))
        await feed._load_contract('BTC_USDT')
        self.assertTrue(self.state.contract_metadata_ready)
        self.assertEqual(self.state.contract_max_leverage, 50)
        self.assertIsNone(self.state.book('mexc').best_bid_ask())
        await feed._handle(json.dumps(dict(channel='push.depth.full', symbol='BTC_USDT',
            ts=self.now*1000, data={'bids': [[100, 100]], 'asks': [[100.01, 100]]})))
        self.assertAlmostEqual(self.state.book('mexc').bids[100], .01)

    async def test_bybit_funding_cannot_overwrite_mexc(self):
        self.state.funding_rate = .001
        self.state.next_funding_at = 12345
        feed = BybitFeed(self.market, self.settings)
        await feed._handle({'topic': 'tickers.BTCUSDT', 'data': {
            'symbol': 'BTCUSDT', 'fundingRate': '.9', 'nextFundingTime': '99999999',
            'openInterest': '100'}})
        self.assertEqual(self.state.funding_rate, .001)
        self.assertEqual(self.state.next_funding_at, 12345)
        self.assertEqual(self.state.oi_history[-1][1], 100)

    async def test_no_liquidations_cannot_create_proxy_signal(self):
        f = FeatureSnapshot('BTC_USDT', self.now, 100, 1, True,
                            trade_count_60s=100, flow_fast=-.9, book_imbalance=-.8)
        strategy = CompositeFlowStrategy(self.settings)
        self.assertIsNone(strategy._reversal_candidate(self.state, f, MarketRegime('RANGE', 0, .5, 0), self.now))
        self.assertEqual(strategy.last_diagnostics['BTC_USDT']['reversal_blocker'], 'liquidation_too_few_events')

    async def test_storage_failure_cannot_start_a_different_account(self):
        with TemporaryDirectory() as d:
            storage = Storage(Path(d)/'paper.db')
            with patch('app.storage.sqlite3.connect', side_effect=sqlite3.OperationalError('denied')) as connect:
                with self.assertRaises(sqlite3.OperationalError):
                    await storage.initialise()
                self.assertEqual(connect.call_count, 1)
            self.assertEqual(storage.path, Path(d)/'paper.db')

    async def test_checkpoint_is_atomic_and_survives_restart(self):
        settings = replace(self.settings, data_mode='synthetic')
        broker = PaperBroker(self.market, settings, account_configs(settings))
        signal = Signal('BTC_USDT', 'composite', 'LIQUIDATION_EXHAUSTION', Side.LONG, 95, .006, 1.8, self.now)
        account = next(iter(broker.accounts.values()))
        position = broker.open_from_signal(account, signal, self.now)
        self.assertIsNotNone(position)
        with TemporaryDirectory() as d:
            path = Path(d)/'paper.db'
            storage = Storage(path)
            await storage.initialise()
            await storage.save_checkpoint(self.now, [], broker.snapshots())
            trade = broker.close_position(account, position, 'TEST', self.now+1)
            with self.assertRaises(ValueError):
                await storage.save_checkpoint(self.now+1, [trade], {'bad': {'value': float('nan')}})
            self.assertEqual(await storage.recent_trades(), [])
            restored = await storage.load_account_states()
            self.assertEqual(len(restored[account.name]['positions']), 1)
            await storage.save_checkpoint(self.now+1, [trade], broker.snapshots())
            await storage.close()
            storage = Storage(path)
            await storage.initialise()
            restored_broker = PaperBroker(self.market, settings, account_configs(settings))
            restored_broker.restore(await storage.load_account_states())
            self.assertEqual(restored_broker.accounts[account.name].positions, {})
            self.assertAlmostEqual(restored_broker.accounts[account.name].balance, account.balance)
            self.assertEqual(len(await storage.recent_trades()), 1)
            await storage.close()

    async def test_synthetic_service_starts_stops_and_restores(self):
        with TemporaryDirectory() as d:
            settings = replace(self.settings, data_mode='synthetic', db_path=Path(d)/'paper.db', evaluation_interval_seconds=.05)
            service = PaperTradingService(settings)
            await service.start()
            await asyncio.sleep(.2)
            self.assertGreater(service.last_engine_tick, 0)
            self.assertEqual(service.last_engine_error, '')
            balances = service.broker.snapshots()
            await service.stop()
            restored = PaperTradingService(settings)
            await restored.start()
            self.assertEqual(restored.broker.snapshots(), balances)
            await restored.stop()

    async def test_engine_failure_rolls_back_account_changes(self):
        with TemporaryDirectory() as d:
            settings = replace(self.settings, data_mode='synthetic', db_path=Path(d)/'paper.db', evaluation_interval_seconds=.05)
            service = PaperTradingService(settings)
            await service.storage.initialise()
            account = next(iter(service.broker.accounts.values()))
            before = account.balance
            def mutate(*args):
                account.balance -= 100
                return []
            service.broker.evaluate_positions = mutate
            service.storage.save_checkpoint = AsyncMock(side_effect=sqlite3.OperationalError('disk full'))
            task = asyncio.create_task(service._engine_loop())
            await asyncio.sleep(.02)
            self.assertEqual(service.broker.accounts[account.name].balance, before)
            self.assertEqual(service.last_engine_error, 'disk full')
            service._stopping = True
            await task
            await service.storage.close()

    async def test_health_http_status_reflects_engine_failure(self):
        from app import api
        with patch.object(api, 'service') as service:
            service.last_engine_tick = time.time()
            service.last_engine_error = ''
            self.assertEqual((await api.health()).status_code, 200)
            service.last_engine_error = 'disk full'
            self.assertEqual((await api.health()).status_code, 503)
            service.last_engine_error = ''
            service.last_engine_tick = time.time() - 60
            self.assertEqual((await api.health()).status_code, 503)

    async def test_excluded_symbols_do_not_keep_stale_warmup_diagnostics(self):
        from app.strategy import StrategyRouter
        router = StrategyRouter(self.settings)
        features = {symbol: FeatureSnapshot(symbol, 1000, 100, 1, True)
                    for symbol in ('BTC_USDT', 'ETH_USDT')}
        states = {symbol: self.market.symbol(symbol) for symbol in features}
        router.evaluate_all(states, features, 1000)
        self.assertEqual(set(router.diagnostics()), set(features))
        router.evaluate_all({'BTC_USDT': states['BTC_USDT']},
                            {'BTC_USDT': features['BTC_USDT']}, 1030)
        self.assertEqual(set(router.diagnostics()), {'BTC_USDT'})
        router.evaluate_all({}, {}, 1060)
        self.assertEqual(router.diagnostics(), {})
