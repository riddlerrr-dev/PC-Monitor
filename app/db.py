"""SQLite storage: users, status samples, events and settings."""
import copy
import json
import sqlite3
import threading
import time

from . import config

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_SETTINGS = {
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
    "warnings": [15, 5, 1],          # minutes before the end of allowed time
    "grace_seconds": 60,             # countdown when it's switched on outside allowed time
    "enforcement_enabled": True,     # master switch: monitor only when off
    "notify": {
        "ntfy_url": "",              # e.g. https://ntfy.sh/some-long-random-topic
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

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DATA_DIR / "pcmonitor.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(
            """
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
        )
        _conn.commit()
    return _conn


# ---------- settings ----------

def get_settings() -> dict:
    with _lock:
        rows = conn().execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: json.loads(r["value"]) for r in rows}
    out = copy.deepcopy(DEFAULT_SETTINGS)
    for k, v in stored.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def set_setting(key: str, value) -> None:
    with _lock:
        conn().execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        conn().commit()


# ---------- samples & events ----------

def add_sample(ts: int, online: bool, ssh: bool) -> None:
    with _lock:
        conn().execute("INSERT INTO samples(ts, online, ssh) VALUES(?,?,?)", (ts, int(online), int(ssh)))
        conn().commit()


def samples_between(start: int, end: int) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(
            "SELECT ts, online, ssh FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, end)
        ).fetchall()


def last_sample_before(ts: int):
    with _lock:
        return conn().execute(
            "SELECT ts, online, ssh FROM samples WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,)
        ).fetchone()


def add_event(type_: str, message: str = "", user: str | None = None, ts: int | None = None) -> dict:
    ts = ts or int(time.time())
    with _lock:
        cur = conn().execute(
            "INSERT INTO events(ts, type, message, user) VALUES(?,?,?,?)", (ts, type_, message, user)
        )
        conn().commit()
    return {"id": cur.lastrowid, "ts": ts, "type": type_, "message": message, "user": user}


def events_between(start: int, end: int) -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT id, ts, type, message, user FROM events WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, end)
        ).fetchall()
    return [dict(r) for r in rows]


def recent_events(limit: int = 100) -> list[dict]:
    with _lock:
        rows = conn().execute(
            "SELECT id, ts, type, message, user FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- users ----------

def list_users() -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT id, username, is_admin, created_at FROM users ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_user(user_id: int):
    with _lock:
        return conn().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(username: str):
    with _lock:
        return conn().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def create_user(username: str, password_hash: str, is_admin: bool) -> int:
    with _lock:
        cur = conn().execute(
            "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES(?,?,?,?)",
            (username, password_hash, int(is_admin), int(time.time())),
        )
        conn().commit()
        return cur.lastrowid


def set_password(user_id: int, password_hash: str) -> None:
    with _lock:
        conn().execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        conn().commit()


def delete_user(user_id: int) -> None:
    with _lock:
        conn().execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn().commit()


def user_count() -> int:
    with _lock:
        return conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]
