import json
from types import SimpleNamespace

import pytest

from app.storage import Storage
from app.models import FeatureSnapshot


@pytest.mark.asyncio
async def test_retention_bounds_telemetry_and_preserves_ledger(tmp_path):
    storage = Storage(tmp_path/'ledger.db')
    await storage.initialise()
    now = 4_000_000
    conn = storage._conn()
    conn.executemany('INSERT INTO features(ts,symbol,price,data_ready,payload) VALUES(?,?,?,?,?)',
                     [(now, 'BTC_USDT', 100, 1, '{}')]*50005)
    conn.execute('INSERT INTO service_events(ts,level,event,detail) VALUES(0,"INFO","old","")')
    conn.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                 ('keep','account','BTC_USDT','test','LONG',0,1,1,1,'TARGET','{"id":"keep"}'))
    conn.commit()
    await storage.save_account_states(1, {'account': {'balance': 1001}})
    await storage.maintain(now)
    assert conn.execute('SELECT count(*) FROM features').fetchone()[0] == 50000
    assert conn.execute('SELECT count(*) FROM service_events').fetchone()[0] == 0
    assert await storage.all_trades() == [{'id':'keep'}]
    assert (await storage.load_account_states())['account']['balance'] == 1001
    # Expiry cleanup is deliberately bounded to 5000 rows per maintenance pass.
    await storage.maintain(now+86401)
    assert conn.execute('SELECT count(*) FROM features').fetchone()[0] == 45000
    await storage.close()


@pytest.mark.asyncio
async def test_low_disk_skips_telemetry_but_persists_account(tmp_path, monkeypatch):
    storage = Storage(tmp_path/'ledger.db')
    await storage.initialise()
    monkeypatch.setattr('app.storage.shutil.disk_usage', lambda _: SimpleNamespace(free=1024))
    await storage.maintain(4_000_000)
    assert not storage.telemetry_enabled
    await storage.save_feature(FeatureSnapshot('BTC_USDT', 4_000_000, 100, 1, True))
    await storage.event(4_000_000,'INFO','skip')
    await storage.save_checkpoint(4_000_000, [], {'a': {'balance': 999}},
                                  [{'ts':4_000_000,'account':'a','reason':'skip'}])
    assert storage._conn().execute('SELECT count(*) FROM features').fetchone()[0] == 0
    assert await storage.recent_decisions() == []
    assert (await storage.load_account_states())['a']['balance'] == 999
    await storage.close()
