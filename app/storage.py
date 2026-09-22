from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import shutil
import time
from pathlib import Path
from typing import Any

from .models import ClosedTrade, FeatureSnapshot, Signal


LOGGER = logging.getLogger(__name__)


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self.lock = asyncio.Lock()
        self.free_bytes = 0
        self.telemetry_enabled = True
        self._last_maintenance = 0.0

    async def initialise(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never silently replace an existing experiment with a temporary account.
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        # Applies to a new database; existing databases are not rebuilt here.
        self.connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                setup TEXT NOT NULL,
                side TEXT NOT NULL,
                score REAL NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);

            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                symbol TEXT NOT NULL,
                setup TEXT NOT NULL,
                side TEXT NOT NULL,
                opened_at REAL NOT NULL,
                closed_at REAL NOT NULL,
                net_pnl REAL NOT NULL,
                r_multiple REAL NOT NULL,
                reason TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account, closed_at DESC);

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                account TEXT NOT NULL,
                equity REAL NOT NULL,
                balance REAL NOT NULL,
                drawdown_pct REAL NOT NULL,
                return_pct REAL NOT NULL,
                positions_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_equity_account_ts
                ON equity_snapshots(account, ts DESC);

            CREATE TABLE IF NOT EXISTS features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                data_ready INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_features_symbol_ts ON features(symbol, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_features_ts ON features(ts);
            CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);

            CREATE TABLE IF NOT EXISTS account_states (
                name TEXT PRIMARY KEY,
                updated_at REAL NOT NULL,
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entry_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                account TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_ts ON entry_decisions(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON service_events(ts);
            """
        )
        self.connection.commit()

    async def maintain(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        if current - self._last_maintenance < 60:
            return
        self.free_bytes = shutil.disk_usage(self.path.parent).free
        self.telemetry_enabled = self.free_bytes >= 64*1024*1024
        if self.free_bytes < 8*1024*1024:
            # DELETE also needs journal space. Do not attempt emergency rebuilds
            # on a full volume; backup/space recovery is an operator action.
            self._last_maintenance = current
            return
        # Only disposable telemetry. Never prune trades or account_states.
        policies = {'features': (86400, 50000), 'signals': (7*86400, 20000),
                    'equity_snapshots': (30*86400, 50000),
                    'service_events': (7*86400, 10000),
                    'entry_decisions': (7*86400, 20000)}
        async with self.lock:
            conn = self._conn()
            for table, (age, cap) in policies.items():
                # Bounded transactions allow an already large database to drain
                # gradually without a full-database VACUUM or giant WAL.
                with conn:
                    conn.execute(f'DELETE FROM {table} WHERE id IN '
                                 f'(SELECT id FROM {table} WHERE ts < ? LIMIT 5000)',
                                 (current-age,))
                    conn.execute(f'DELETE FROM {table} WHERE id IN '
                                 f'(SELECT id FROM {table} WHERE id <= '
                                 f'(SELECT id FROM {table} ORDER BY id DESC LIMIT 1 OFFSET ?) LIMIT 5000)',
                                 (cap,))
            conn.execute('PRAGMA wal_checkpoint(PASSIVE)')
            conn.execute('PRAGMA incremental_vacuum(256)')
            self.free_bytes = shutil.disk_usage(self.path.parent).free
            self.telemetry_enabled = self.free_bytes >= 64*1024*1024
            self._last_maintenance = current

    async def all_trades(self) -> list[dict[str, Any]]:
        async with self.lock:
            rows = self._conn().execute('SELECT payload FROM trades ORDER BY closed_at,id').fetchall()
        return [json.loads(row['payload']) for row in rows]

    def _conn(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Storage is not initialised")
        return self.connection

    async def save_signal(self, signal: Signal) -> None:
        if not self.telemetry_enabled:
            return
        payload = signal.as_dict()
        async with self.lock:
            self._conn().execute(
                """INSERT INTO signals(ts,symbol,strategy,setup,side,score,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    signal.ts,
                    signal.symbol,
                    signal.strategy,
                    signal.setup,
                    signal.side.label,
                    signal.score,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._conn().commit()

    async def save_trade(self, trade: ClosedTrade) -> None:
        payload = trade.as_dict()
        async with self.lock:
            self._conn().execute(
                """INSERT OR REPLACE INTO trades(
                       id,account,symbol,setup,side,opened_at,closed_at,
                       net_pnl,r_multiple,reason,payload
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.id,
                    trade.account,
                    trade.symbol,
                    trade.setup,
                    trade.side.label,
                    trade.opened_at,
                    trade.closed_at,
                    trade.net_pnl,
                    trade.r_multiple,
                    trade.reason,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._conn().commit()

    async def save_checkpoint(self, ts, trades, payloads, decisions=()) -> None:
        """Commit closed trades and the corresponding balances/positions together."""
        async with self.lock:
            with self._conn():
                for decision in (decisions if self.telemetry_enabled else ()):
                    self._conn().execute(
                        'INSERT INTO entry_decisions(ts,account,reason,payload) VALUES(?,?,?,?)',
                        (decision['ts'], decision['account'], decision['reason'],
                         json.dumps(decision, ensure_ascii=False, allow_nan=False)),
                    )
                for trade in trades:
                    self._conn().execute(
                        """INSERT OR REPLACE INTO trades(
                        id,account,symbol,setup,side,opened_at,closed_at,
                        net_pnl,r_multiple,reason,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (trade.id, trade.account, trade.symbol, trade.setup,
                         trade.side.label, trade.opened_at, trade.closed_at,
                         trade.net_pnl, trade.r_multiple, trade.reason,
                         json.dumps(trade.as_dict(), ensure_ascii=False, allow_nan=False)),
                    )
                for name, payload in payloads.items():
                    self._conn().execute(
                        """INSERT INTO account_states(name,updated_at,payload) VALUES(?,?,?)
                        ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at,
                        payload=excluded.payload""",
                        (name, ts, json.dumps(payload, ensure_ascii=False, allow_nan=False)),
                    )

    async def save_feature(self, feature: FeatureSnapshot) -> None:
        if not self.telemetry_enabled:
            return
        async with self.lock:
            self._conn().execute(
                "INSERT INTO features(ts,symbol,price,data_ready,payload) VALUES(?,?,?,?,?)",
                (
                    feature.ts,
                    feature.symbol,
                    feature.price,
                    int(feature.data_ready),
                    json.dumps(feature.as_dict(), ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._conn().commit()

    async def recent_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.lock:
            rows = self._conn().execute(
                'SELECT payload FROM entry_decisions ORDER BY id DESC LIMIT ?',
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [json.loads(row['payload']) for row in rows]

    async def save_account_states(self, ts: float, payloads: dict[str, dict[str, Any]]) -> None:
        rows = [
            (name, ts, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            for name, payload in payloads.items()
        ]
        async with self.lock:
            self._conn().executemany(
                """INSERT INTO account_states(name,updated_at,payload) VALUES(?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                       updated_at=excluded.updated_at,
                       payload=excluded.payload""",
                rows,
            )
            self._conn().commit()

    async def load_account_states(self) -> dict[str, dict[str, Any]]:
        async with self.lock:
            rows = self._conn().execute("SELECT name,payload FROM account_states").fetchall()
        return {row["name"]: json.loads(row["payload"]) for row in rows}

    async def save_equity(self, ts: float, accounts: dict[str, dict[str, Any]]) -> None:
        if not self.telemetry_enabled:
            return
        rows = [
            (
                ts,
                name,
                float(data["equity"]),
                float(data["balance"]),
                float(data["drawdown_pct"]),
                float(data["return_pct"]),
                int(data["positions_count"]),
            )
            for name, data in accounts.items()
        ]
        async with self.lock:
            self._conn().executemany(
                """INSERT INTO equity_snapshots(
                       ts,account,equity,balance,drawdown_pct,return_pct,positions_count
                   ) VALUES(?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn().commit()

    async def event(self, ts: float, level: str, event: str, detail: str = "") -> None:
        if not self.telemetry_enabled:
            return
        async with self.lock:
            self._conn().execute(
                "INSERT INTO service_events(ts,level,event,detail) VALUES(?,?,?,?)",
                (ts, level, event, detail[:2_000]),
            )
            self._conn().commit()

    async def recent_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.lock:
            rows = self._conn().execute(
                "SELECT payload FROM trades ORDER BY closed_at DESC LIMIT ?", (max(1, min(limit, 1_000)),)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    async def recent_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.lock:
            rows = self._conn().execute(
                "SELECT payload FROM signals ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 1_000)),)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    async def equity_history(self, account: str, limit: int = 1_000) -> list[dict[str, Any]]:
        async with self.lock:
            rows = self._conn().execute(
                """SELECT ts,equity,balance,drawdown_pct,return_pct,positions_count
                   FROM equity_snapshots WHERE account=? ORDER BY ts DESC LIMIT ?""",
                (account, max(1, min(limit, 10_000))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    async def close(self) -> None:
        async with self.lock:
            if self.connection is not None:
                self.connection.commit()
                self.connection.close()
                self.connection = None
