from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets

from ..config import Settings
from ..market import MarketState, FeedStatus
from ..models import Side, TradeEvent


LOGGER = logging.getLogger(__name__)


class BinanceFeed:
    name = "binance"
    market_ws_url = "wss://fstream.binance.com/market/stream"
    public_ws_url = "wss://fstream.binance.com/public/stream"

    def __init__(self, market: MarketState, settings: Settings) -> None:
        self.market = market
        self.settings = settings
        self._stop = asyncio.Event()
        for kind in ('trades', 'book'):
            self.market.feeds.setdefault(f'binance_{kind}', FeedStatus(name=f'binance_{kind}'))

    def _channel_state(self, kind: str, connected: bool, error: str = '') -> None:
        name = f'binance_{kind}'
        if connected:
            self.market.feed_connected(name)
        else:
            self.market.feed_disconnected(name, error)
            for state in self.market.symbols.values():
                if kind == 'book':
                    book = state.book('binance')
                    book.updated_at = 0
                    book.bids.clear()
                    book.asks.clear()
                else:
                    state.trades['binance'].clear()
        channels = [self.market.feeds[f'binance_{k}'] for k in ('trades', 'book')]
        aggregate = self.market.feeds[self.name]
        aggregate.connected = all(s.connected for s in channels)
        aggregate.last_error = '; '.join(f'{s.name}: {s.last_error}' for s in channels if s.last_error)
        aggregate.reconnects = sum(s.reconnects for s in channels)

    @staticmethod
    def _next_backoff(previous: float, duration: float) -> float:
        return 1.0 if duration >= 60 else min(previous * 2, 30.0)

    async def run(self) -> None:
        await asyncio.gather(
            self._run_channel("trades", self.market_ws_url),
            self._run_channel("book", self.public_ws_url),
        )

    async def _run_channel(self, kind: str, url: str) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            connected_at = time.monotonic()
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    # A TCP handshake alone does not confirm the subscription.
                    connected_at = time.monotonic()
                    suffix = "aggTrade" if kind == "trades" else "bookTicker"
                    params = [f"{symbol.replace('_', '').lower()}@{suffix}" for symbol in self.settings.symbols]
                    await ws.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": kind}))
                    acknowledged = False
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30 if acknowledged else 10)
                        payload = json.loads(raw)
                        if payload.get('code') is not None or payload.get('error'):
                            raise RuntimeError(f'Binance subscription rejected: {payload}')
                        if payload.get('id') == kind:
                            if payload.get('result', 'missing') is not None:
                                raise RuntimeError(f'Unexpected subscription response: {payload}')
                            acknowledged = True
                            self._channel_state(kind, True)
                            continue
                        data = payload.get("data", payload)
                        if isinstance(data, dict):
                            await self._handle(kind, data)
            except asyncio.CancelledError:
                self._channel_state(kind, False, 'stopped')
                raise
            except Exception as exc:
                detail = str(exc) or type(exc).__name__
                self._channel_state(kind, False, detail)
                LOGGER.warning("Binance %s websocket reconnect in %.1fs: %s", kind, backoff, detail)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = self._next_backoff(backoff, time.monotonic() - connected_at - backoff)

    @staticmethod
    def _mexc_symbol(symbol: str) -> str:
        upper = symbol.upper()
        return f"{upper[:-4]}_USDT" if upper.endswith("USDT") else upper

    async def _handle(self, kind: str, data: dict[str, Any]) -> None:
        # After Binance's UM/CM stream merge, st=1 means USD-M.
        if data.get("st") not in (None, 1):
            return
        symbol = self._mexc_symbol(str(data.get("s") or data.get("ps") or ""))
        if symbol not in self.market.symbols:
            return
        ts = float(data.get("T") or data.get("E") or time.time() * 1000) / 1000.0
        self.market.feed_message(self.name, ts)
        self.market.feed_message(f'binance_{kind}', ts)
        state = self.market.symbol(symbol)
        if kind == "trades" and data.get("e") == "aggTrade":
            state.add_trade(
                TradeEvent(
                    symbol=symbol,
                    venue="binance",
                    price=float(data.get("p") or 0.0),
                    base_qty=float(data.get("nq") or data.get("q") or 0.0),
                    # m=true: buyer was maker, therefore seller was aggressor.
                    side=Side.SHORT if bool(data.get("m")) else Side.LONG,
                    ts=ts,
                )
            )
        elif kind == "book" and data.get("b") and data.get("a"):
            state.book("binance").apply_snapshot(
                [[data["b"], data.get("B") or 0.0]],
                [[data["a"], data.get("A") or 0.0]],
                version=int(data.get("u") or 0),
                ts=ts,
            )
            state.record_book_mid("binance", ts)

    async def stop(self) -> None:
        self._stop.set()
