from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class CacheDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, key)
                );
                CREATE TABLE IF NOT EXISTS pricing_debug (
                    station_id TEXT PRIMARY KEY,
                    station_name TEXT,
                    tesla_url TEXT,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    schedule_json TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS trip_shares (
                    id TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trip_shares_expires_at ON trip_shares(expires_at);
                """
            )

    def get(self, namespace: str, key: str, max_age_seconds: float | None = None) -> Any | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value_json, fetched_at FROM cache WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        if max_age_seconds is not None:
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            if age > max_age_seconds:
                return None
        return json.loads(row["value_json"])

    def set(self, namespace: str, key: str, value: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(value, default=str)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO cache(namespace,key,value_json,fetched_at) VALUES(?,?,?,?) "
                "ON CONFLICT(namespace,key) DO UPDATE SET value_json=excluded.value_json,fetched_at=excluded.fetched_at",
                (namespace, key, payload, now),
            )

    def create_trip_share(self, share_id: str, value: Any, ttl_days: int = 7) -> datetime:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=ttl_days)
        payload = json.dumps(value, default=str)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM trip_shares WHERE expires_at <= ?", (now.isoformat(),))
            conn.execute(
                "INSERT INTO trip_shares(id,value_json,created_at,expires_at) VALUES(?,?,?,?)",
                (share_id, payload, now.isoformat(), expires_at.isoformat()),
            )
        return expires_at

    def get_trip_share(self, share_id: str) -> tuple[Any, datetime] | None:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value_json, expires_at FROM trip_shares WHERE id=? AND expires_at > ?",
                (share_id, now.isoformat()),
            ).fetchone()
            if row is None:
                conn.execute("DELETE FROM trip_shares WHERE id=?", (share_id,))
                return None
        return json.loads(row["value_json"]), datetime.fromisoformat(row["expires_at"])

    def record_pricing_debug(
        self,
        station_id: str,
        station_name: str,
        tesla_url: str,
        status: str,
        schedule: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pricing_debug(station_id,station_name,tesla_url,fetched_at,status,schedule_json,error)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(station_id) DO UPDATE SET
                    station_name=excluded.station_name,
                    tesla_url=excluded.tesla_url,
                    fetched_at=excluded.fetched_at,
                    status=excluded.status,
                    schedule_json=excluded.schedule_json,
                    error=excluded.error
                """,
                (station_id, station_name, tesla_url, now, status, json.dumps(schedule) if schedule else None, error),
            )

    def pricing_debug_rows(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT station_id,station_name,tesla_url,fetched_at,status,schedule_json,error "
                "FROM pricing_debug ORDER BY fetched_at DESC"
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["schedule"] = json.loads(item.pop("schedule_json")) if item.get("schedule_json") else None
            out.append(item)
        return out
