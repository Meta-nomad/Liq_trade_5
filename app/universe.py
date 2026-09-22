"""Public-data eligibility, not fraud detection. Unknown metadata fails closed."""
import logging,time
from .config import DEFAULT_SYMBOLS
LOG=logging.getLogger(__name__)

def exclusion(meta,ticker,now):
    if not meta or meta.get('status')!='Trading' or meta.get('contractType')!='LinearPerpetual' or meta.get('quoteCoin')!='USDT':
        return 'inactive_or_wrong_contract'
    if meta.get('isPreListing') or float(meta.get('deliveryTime') or 0)>0:return 'prelisting_or_delisting'
    launch=float(meta.get('launchTime') or 0)/1000
    if launch<=0 or now-launch<180*86400:return 'younger_than_180d_or_unknown'
    if float(ticker.get('turnover24h') or 0)<10_000_000:return 'turnover_below_10m'
    bid=float(ticker.get('bid1Price') or 0);ask=float(ticker.get('ask1Price') or 0)
    if bid<=0 or ask<=bid or (ask-bid)/((ask+bid)/2)*10000>10:return 'spread_above_10bps_or_unknown'
    return ''

async def screen(symbols):
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        async def get(path,params):
            r=await client.get('https://api.bybit.com'+path,params=params);r.raise_for_status();d=r.json()
            if d.get('retCode')!=0:raise RuntimeError('Universe metadata rejected by exchange')
            return d['result']
        metas={};cursor='';seen=set()
        while True:
            d=await get('/v5/market/instruments-info',dict(category='linear',limit=1000,cursor=cursor))
            metas.update({x['symbol']:x for x in d['list']});cursor=d.get('nextPageCursor','')
            if not cursor:break
            if cursor in seen:raise RuntimeError('Repeated universe pagination cursor')
            seen.add(cursor)
        d=await get('/v5/market/tickers',{'category':'linear'});ticks={x['symbol']:x for x in d['list']}
    out=[];now=time.time()
    for s in dict.fromkeys(symbols):
        reason='outside_reviewed_candidate_list' if s not in DEFAULT_SYMBOLS else exclusion(metas.get(s.replace('_',''),{}),ticks.get(s.replace('_',''),{}),now)
        LOG.info('UNIVERSE symbol=%s result=%s',s,reason or 'ELIGIBLE_BYBIT')
        if not reason:out.append(s)
    if not out:raise RuntimeError('No eligible contracts; refusing to trade')
    return tuple(out)


class UniverseGate:
    """Background eligibility; a transport denial never enables trading."""
    def __init__(self):
        self.error = "pending_verification"
        self.checked_at = 0.0
        self.eligible_count = 0
        self.eligible_symbols: set[str] = set()

    @staticmethod
    def _mexc_fallback(symbols, market):
        """Fail-closed local screen used when Bybit REST is unavailable.

        It deliberately requires MEXC contract metadata, 200 hourly closes,
        an allowed API flag and a fresh, tight MEXC book.  It is weaker than
        the Bybit 24h turnover/age screen, so it is only a transport fallback.
        """
        now = time.time()
        eligible = set()
        for symbol in symbols:
            state = market.symbol(symbol)
            if symbol not in DEFAULT_SYMBOLS:
                continue
            if not state.contract_metadata_ready or not state.api_allowed:
                continue
            closed_hours = [(ts, price) for ts, price in state.hour_closes
                            if ts + 3600 <= now and price > 0]
            if len(closed_hours) < 200:
                continue
            bbo = state.book("mexc").best_bid_ask()
            if not bbo:
                continue
            bid, ask = bbo
            mid = (bid + ask) / 2.0
            if mid <= 0 or ask <= bid or (ask - bid) / mid * 10000 > 10:
                continue
            if not state.book("mexc").is_fresh(now, 30.0):
                continue
            eligible.add(symbol)
        return eligible

    async def refresh(self, symbols, market, checker=screen):
        try:
            eligible = set(await checker(symbols))
            # Every result is still constrained to configured candidates.
            eligible.intersection_update(symbols)
            if not eligible:
                raise RuntimeError("No eligible contracts")
        except Exception as exc:
            fallback = self._mexc_fallback(symbols, market)
            if fallback:
                self.error = ""
                self.checked_at = time.time()
                self.eligible_symbols = fallback
                self.eligible_count = len(fallback)
                for symbol in symbols:
                    market.symbol(symbol).universe_valid_until = self.checked_at + 1800 if symbol in fallback else 0
                LOG.warning("UNIVERSE FALLBACK venue=MEXC eligible=%d/%d reason=%s", len(fallback), len(symbols), exc)
                return 900
            self.error = str(exc)
            self.eligible_count = 0
            self.eligible_symbols = set()
            self.checked_at = time.time()
            for symbol in symbols:
                market.symbol(symbol).universe_valid_until = 0
            LOG.error("UNIVERSE BLOCKED trading=OFF error=%s retry_in=900s", self.error)
            return 900
        self.error = ""
        self.checked_at = time.time()
        self.eligible_symbols = eligible
        self.eligible_count = len(eligible)
        for symbol in symbols:
            market.symbol(symbol).universe_valid_until = self.checked_at+7200 if symbol in eligible else 0
        LOG.info("UNIVERSE VERIFIED eligible=%d/%d",len(eligible),len(symbols))
        return 3600

    async def refresh_mexc_only(self, symbols, market):
        """Single-venue eligibility; no Bybit REST dependency."""
        eligible = self._mexc_fallback(symbols, market)
        self.checked_at = time.time()
        self.eligible_symbols = eligible
        self.eligible_count = len(eligible)
        if not eligible:
            self.error = "No MEXC-ready contracts"
            for symbol in symbols:
                market.symbol(symbol).universe_valid_until = 0
            LOG.warning("UNIVERSE BLOCKED trading=OFF error=%s retry_in=60s", self.error)
            return 60
        self.error = ""
        for symbol in symbols:
            market.symbol(symbol).universe_valid_until = self.checked_at + 1800 if symbol in eligible else 0
        LOG.info("UNIVERSE VERIFIED venue=MEXC_ONLY eligible=%d/%d", len(eligible), len(symbols))
        return 300
