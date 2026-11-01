"""Хранилище на SQLite: настройки, токены, сценарии, история состояний, журнал."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    device_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS history_dev_ts ON history(device_id, ts);
CREATE TABLE IF NOT EXISTS scenarios (
    id      TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
    ts      REAL NOT NULL,
    level   TEXT NOT NULL,
    source  TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _exec(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # --- key/value ----------------------------------------------------------

    def get_json(self, key: str, default: Any = None) -> Any:
        rows = self._exec("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_json(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def delete(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key=?", (key,))

    # --- история ------------------------------------------------------------

    def add_history(self, device_id: str, key: str, value: Any, ts: float | None = None) -> None:
        self._exec(
            "INSERT INTO history(device_id, key, value, ts) VALUES(?, ?, ?, ?)",
            (device_id, key, json.dumps(value, ensure_ascii=False), ts or time.time()),
        )

    def history(
        self, device_id: str, key: str | None = None, since: float = 0, limit: int = 2000
    ) -> list[dict[str, Any]]:
        sql = "SELECT key, value, ts FROM history WHERE device_id=? AND ts>=?"
        params: list[Any] = [device_id, since]
        if key:
            sql += " AND key=?"
            params.append(key)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._exec(sql, tuple(params))
        return [{"key": k, "value": json.loads(v), "ts": ts} for k, v, ts in reversed(rows)]

    def prune_history(self, older_than_days: float = 90) -> None:
        self._exec("DELETE FROM history WHERE ts < ?", (time.time() - older_than_days * 86400,))

    # --- сценарии -----------------------------------------------------------

    def list_scenarios(self) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self._exec("SELECT data FROM scenarios ORDER BY id")]

    def save_scenario(self, scenario: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO scenarios(id, data, updated) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
            (scenario["id"], json.dumps(scenario, ensure_ascii=False), time.time()),
        )

    def delete_scenario(self, scenario_id: str) -> None:
        self._exec("DELETE FROM scenarios WHERE id=?", (scenario_id,))

    # --- журнал -------------------------------------------------------------

    def log(self, source: str, message: str, level: str = "info") -> None:
        self._exec(
            "INSERT INTO log(ts, level, source, message) VALUES(?, ?, ?, ?)",
            (time.time(), level, source, message),
        )

    def recent_log(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._exec("SELECT ts, level, source, message FROM log ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"ts": t, "level": lv, "source": s, "message": m} for t, lv, s, m in rows]
