"""SQLite persistence: users, browser sessions, and per-user alerts / meta / events.

Multi-user: every row of `meta`, `alerts`, and `events` is namespaced by `user`
(the AIUB username). Credentials live in `users`, browser→user bindings in
`sessions`. Low write volume, so one synchronous connection guarded by a lock is
plenty.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import config

_LOCK = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_SCHEMA_VERSION = 2  # bump to force a one-time wipe + rebuild


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init(_conn)
    return _conn


def _init(c: sqlite3.Connection) -> None:
    version = c.execute("PRAGMA user_version").fetchone()[0]
    if version < _SCHEMA_VERSION:
        # One-time migration to the multi-user schema: drop the old single-user
        # tables and the shared cookie jar, then rebuild fresh.
        c.executescript("DROP TABLE IF EXISTS alerts; DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS events;")
        try:
            config.COOKIE_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        c.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            sid TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            course_title TEXT NOT NULL,
            course_id INTEGER,
            filter_type TEXT NOT NULL DEFAULT 'any',
            section_labels TEXT NOT NULL DEFAULT '[]',
            days TEXT NOT NULL DEFAULT '[]',
            time_start TEXT,
            time_end TEXT,
            auto_join INTEGER NOT NULL DEFAULT 0,
            clash_policy TEXT NOT NULL DEFAULT 'alert',
            unregister_targets TEXT NOT NULL DEFAULT '[]',
            active INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'watching',
            notified_section_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            user TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (user, key)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            ts TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL
        );
        """
    )
    c.commit()


# ─── users (credentials) ──────────────────────────────────────────────────

def set_user(username: str, password: str) -> None:
    with _LOCK:
        conn().execute(
            "INSERT INTO users(username,password,created_at) VALUES(?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET password=excluded.password",
            (username, password, _now()),
        )
        conn().commit()


def get_user(username: str) -> Optional[dict]:
    with _LOCK:
        row = conn().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def delete_user(username: str) -> None:
    with _LOCK:
        conn().execute("DELETE FROM users WHERE username=?", (username,))
        conn().commit()


def all_usernames() -> list[str]:
    with _LOCK:
        rows = conn().execute("SELECT username FROM users ORDER BY username").fetchall()
    return [r["username"] for r in rows]


# ─── browser sessions (sid -> username) ─────────────────────────────────────

def bind_sid(sid: str, username: str) -> None:
    with _LOCK:
        conn().execute(
            "INSERT INTO sessions(sid,username,created_at) VALUES(?,?,?) "
            "ON CONFLICT(sid) DO UPDATE SET username=excluded.username",
            (sid, username, _now()),
        )
        conn().commit()


def user_for_sid(sid: str) -> Optional[str]:
    if not sid:
        return None
    with _LOCK:
        row = conn().execute("SELECT username FROM sessions WHERE sid=?", (sid,)).fetchone()
    return row["username"] if row else None


def unbind_sid(sid: str) -> None:
    with _LOCK:
        conn().execute("DELETE FROM sessions WHERE sid=?", (sid,))
        conn().commit()


def unbind_user_sids(username: str) -> None:
    with _LOCK:
        conn().execute("DELETE FROM sessions WHERE username=?", (username,))
        conn().commit()


# ─── meta key/value (per user) ──────────────────────────────────────────────

def set_meta(user: str, key: str, value: Any) -> None:
    with _LOCK:
        conn().execute(
            "INSERT INTO meta(user,key,value) VALUES(?,?,?) "
            "ON CONFLICT(user,key) DO UPDATE SET value=excluded.value",
            (user, key, json.dumps(value)),
        )
        conn().commit()


def get_meta(user: str, key: str, default: Any = None) -> Any:
    with _LOCK:
        row = conn().execute("SELECT value FROM meta WHERE user=? AND key=?", (user, key)).fetchone()
    if not row or row["value"] is None:
        return default
    try:
        val = json.loads(row["value"])
    except json.JSONDecodeError:
        return default
    # A stored JSON null (e.g. set_meta(user, key, None)) falls back to the default
    # too, so callers passing default={} never get a None they have to guard.
    return default if val is None else val


def clear_meta(user: str) -> None:
    with _LOCK:
        conn().execute("DELETE FROM meta WHERE user=?", (user,))
        conn().commit()


# ─── events (per user) ───────────────────────────────────────────────────

def log_event(user: str, level: str, message: str) -> None:
    # Mirror every activity-feed event into the forensic trace log too.
    from .applog import log
    log.log({"error": 40, "warn": 30, "info": 20}.get(level, 20), "event[%s] %s", user, message)
    with _LOCK:
        conn().execute(
            "INSERT INTO events(user,ts,level,message) VALUES(?,?,?,?)", (user, _now(), level, message)
        )
        # Keep each user's log bounded.
        conn().execute(
            "DELETE FROM events WHERE user=? AND id < "
            "(SELECT MAX(id)-2000 FROM events WHERE user=?)",
            (user, user),
        )
        conn().commit()


def recent_events(user: str, limit: int = 100) -> list[dict]:
    with _LOCK:
        rows = conn().execute(
            "SELECT ts,level,message FROM events WHERE user=? ORDER BY id DESC LIMIT ?", (user, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# ─── alerts (per user) ───────────────────────────────────────────────────

_JSON_FIELDS = ("section_labels", "days", "unregister_targets", "notified_section_ids")


def _row_to_alert(row: sqlite3.Row) -> dict:
    d = dict(row)
    for f in _JSON_FIELDS:
        try:
            d[f] = json.loads(d[f])
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    d["auto_join"] = bool(d["auto_join"])
    d["active"] = bool(d["active"])
    return d


def list_alerts(user: str, active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM alerts WHERE user=?"
    if active_only:
        q += " AND active=1"
    q += " ORDER BY id"
    with _LOCK:
        rows = conn().execute(q, (user,)).fetchall()
    return [_row_to_alert(r) for r in rows]


def get_alert(user: str, alert_id: int) -> Optional[dict]:
    with _LOCK:
        row = conn().execute("SELECT * FROM alerts WHERE id=? AND user=?", (alert_id, user)).fetchone()
    return _row_to_alert(row) if row else None


def create_alert(user: str, data: dict) -> dict:
    fields = {
        "user": user,
        "course_title": data["course_title"],
        "course_id": data.get("course_id"),
        "filter_type": data.get("filter_type", "any"),
        "section_labels": json.dumps(data.get("section_labels", [])),
        "days": json.dumps(data.get("days", [])),
        "time_start": data.get("time_start"),
        "time_end": data.get("time_end"),
        "auto_join": 1 if data.get("auto_join") else 0,
        "clash_policy": data.get("clash_policy", "alert"),
        "unregister_targets": json.dumps(data.get("unregister_targets", [])),
        "active": 1 if data.get("active", True) else 0,
        "status": "watching",
        "notified_section_ids": "[]",
        "created_at": _now(),
    }
    cols = ",".join(fields)
    placeholders = ",".join("?" for _ in fields)
    with _LOCK:
        cur = conn().execute(
            f"INSERT INTO alerts({cols}) VALUES({placeholders})", tuple(fields.values())
        )
        conn().commit()
        new_id = cur.lastrowid
    from .applog import log
    log.info("create_alert[%s] #%s %s filter=%s auto_join=%s clash=%s sections=%s days=%s %s-%s",
             user, new_id, data.get("course_title"), data.get("filter_type"),
             bool(data.get("auto_join")), data.get("clash_policy"), data.get("section_labels"),
             data.get("days"), data.get("time_start"), data.get("time_end"))
    return get_alert(user, new_id)


def update_alert(user: str, alert_id: int, data: dict) -> Optional[dict]:
    allowed = {
        "course_title", "course_id", "filter_type", "section_labels", "days",
        "time_start", "time_end", "auto_join", "clash_policy", "unregister_targets",
        "active", "status", "notified_section_ids",
    }
    sets, vals = [], []
    for k, v in data.items():
        if k not in allowed:
            continue
        if k in _JSON_FIELDS:
            v = json.dumps(v)
        elif k in ("auto_join", "active"):
            v = 1 if v else 0
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        vals.extend([alert_id, user])
        with _LOCK:
            conn().execute(f"UPDATE alerts SET {','.join(sets)} WHERE id=? AND user=?", tuple(vals))
            conn().commit()
        # notified_section_ids churns every poll; log other changes at info, that at debug.
        from .applog import log
        keys = set(data) & allowed
        lvl = 10 if keys <= {"notified_section_ids"} else 20
        log.log(lvl, "update_alert[%s] #%s set %s", user, alert_id,
                {k: data[k] for k in keys if k != "notified_section_ids"} or list(keys))
    return get_alert(user, alert_id)


def delete_alert(user: str, alert_id: int) -> None:
    from .applog import log
    log.info("delete_alert[%s] #%s", user, alert_id)
    with _LOCK:
        conn().execute("DELETE FROM alerts WHERE id=? AND user=?", (alert_id, user))
        conn().commit()


def clear_alerts(user: str) -> int:
    with _LOCK:
        cur = conn().execute("DELETE FROM alerts WHERE user=?", (user,))
        conn().commit()
        return cur.rowcount
