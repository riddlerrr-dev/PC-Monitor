"""SQLite storage: users, status samples, events and settings."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_SETTINGS: dict[str, Any] = {
    # Per-day allowed window. mode: "window" (start-end), "all" (all day), "none" (not allowed).
    # If end <= start the window runs past midnight into the next day.
    "schedule": {
        **{d: {"mode": "window", "start": "16:00", "end": "21:00"} for d in DAYS[:4]},
        "fri": {"mode": "window", "start": "16:00", "end": "22:30"},
        "sat": {"mode": "window", "start": "09:00", "end": "22:30"},
        "sun": {"mode": "window", "start": "09:00", "end": "21:00"},
    },
    # Holiday mode replaces every day's rule while enabled (optionally until a date, inclusive).
    "holiday": {"enabled": False, "mode": "window", "start": "09:00", "end": "23:00", "until": None},
    "block": {"active": False, "until": None, "by": None},
    "extension": {"from": None, "until": None, "by": None},
    "warnings": [15, 5, 1],  # minutes before the end of allowed time
    "grace_seconds": 60,  # countdown when it's switched on outside allowed time
    "enforcement_enabled": True,  # master switch: monitor only when off
    "notify": {
        "ntfy_url": "",  # e.g. https://ntfy.sh/some-long-random-topic
        "ntfy_token": "",
        "events": ["power_on_outside", "ssh_unreachable", "shutdown_failed"],
    },
}

# Event types (also used as annotation categories in the UI)
EVENT_TYPES = {
    "power_on": "Turned on",
    "power_on_outside": "Turned on outside allowed time",
    "power_off": "Turned off",
    "warning": "Warning sent",
    "shutdown_sent": "Shutdown sent",
    "shutdown_failed": "Shutdown failed",
    "ssh_unreachable": "On, but SSH not responding",
    "ssh_restored": "SSH responding again",
    "block_on": "Blocked",
    "block_off": "Unblocked",
    "extension": "Extra time",
    "schedule_changed": "Schedule changed",
    "holiday_on": "Holiday mode on",
    "holiday_off": "Holiday mode off",
    "settings_changed": "Settings changed",
    "enforcement_off": "Enforcement paused",
    "enforcement_on": "Enforcement resumed",
    "monitor_started": "Monitor started",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER NOT NULL,
    online INTEGER NOT NULL,
    ssh INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    user TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

Row = dict[str, Any]


class Database:
    """One SQLite connection, shared by the web requests and the monitor loop.

    Each instance is independent, so tests can create one per test (or use ":memory:").
    """

    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Row]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> Row | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    # ---------- settings ----------

    def get_settings(self) -> dict[str, Any]:
        """Stored settings merged over the defaults (so new settings appear automatically)."""
        stored = {r["key"]: json.loads(r["value"]) for r in self._all("SELECT key, value FROM settings")}
        out = copy.deepcopy(DEFAULT_SETTINGS)
        for k, v in stored.items():
            if isinstance(out.get(k), dict) and isinstance(v, dict):
                out[k].update(v)
            else:
                out[k] = v
        return out

    def set_setting(self, key: str, value: Any) -> None:
        self._write(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ---------- samples & events ----------

    def add_sample(self, ts: int, online: bool, ssh: bool) -> None:
        self._write("INSERT INTO samples(ts, online, ssh) VALUES(?,?,?)", (ts, int(online), int(ssh)))

    def add_samples(self, rows: list[tuple[int, bool, bool]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples(ts, online, ssh) VALUES(?,?,?)", [(t, int(o), int(s)) for t, o, s in rows]
            )
            self._conn.commit()

    def samples_between(self, start: int, end: int) -> list[Row]:
        return self._all("SELECT ts, online, ssh FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, end))

    def last_sample_before(self, ts: int) -> Row | None:
        return self._one("SELECT ts, online, ssh FROM samples WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,))

    def add_event(self, type_: str, message: str = "", user: str | None = None, ts: int | None = None) -> Row:
        ts = ts or int(time.time())
        cur = self._write("INSERT INTO events(ts, type, message, user) VALUES(?,?,?,?)", (ts, type_, message, user))
        return {"id": cur.lastrowid, "ts": ts, "type": type_, "message": message, "user": user}

    def events_between(self, start: int, end: int) -> list[Row]:
        return self._all(
            "SELECT id, ts, type, message, user FROM events WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, end)
        )

    def recent_events(self, limit: int = 100) -> list[Row]:
        return self._all("SELECT id, ts, type, message, user FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    # ---------- users ----------

    def list_users(self) -> list[Row]:
        return self._all("SELECT id, username, is_admin, created_at FROM users ORDER BY id")

    def get_user(self, user_id: int) -> Row | None:
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_user_by_name(self, username: str) -> Row | None:
        return self._one("SELECT * FROM users WHERE username = ?", (username,))

    def create_user(self, username: str, password_hash: str, is_admin: bool) -> int:
        cur = self._write(
            "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES(?,?,?,?)",
            (username, password_hash, int(is_admin), int(time.time())),
        )
        return int(cur.lastrowid or 0)

    def set_password(self, user_id: int, password_hash: str) -> None:
        self._write("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))

    def delete_user(self, user_id: int) -> None:
        self._write("DELETE FROM users WHERE id = ?", (user_id,))

    def user_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0
