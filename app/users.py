"""Per-user runtime registry for the multi-user dashboard.

Each AIUB user gets a `UserContext` holding their own `PortalSession` (own cookies
+ credentials) and `Engine` (own background poller). Browsers are bound to a user
by an httponly `sid` cookie via the `sessions` table in db. Contexts live in memory
and are (re)built from stored credentials on startup so sessions survive restarts.
"""
from __future__ import annotations

import time
from typing import Optional

from . import config, db
from .applog import log, traced
from .poller import Engine
from .portal import PortalSession


class UserContext:
    def __init__(self, username: str):
        self.username = username
        self.session = PortalSession(username)
        self.engine = Engine(username, self.session)

    def start(self):
        self.engine.start()

    async def stop(self):
        await self.engine.stop()


class UserManager:
    def __init__(self):
        self._contexts: dict[str, UserContext] = {}
        # Last time we saw an authenticated request for each user (the open dashboard
        # polls every few seconds). Drives the idle-wipe reaper.
        self._last_seen: dict[str, float] = {}

    def touch(self, username: str) -> None:
        """Mark a user's dashboard as alive (called on each authenticated request)."""
        self._last_seen[username] = time.time()

    def context(self, username: str) -> UserContext:
        """Get-or-create a user's context and ensure its poller is running."""
        username = (username or "").strip()
        ctx = self._contexts.get(username)
        if ctx is None:
            ctx = UserContext(username)
            self._contexts[username] = ctx
        ctx.start()
        return ctx

    def context_for_sid(self, sid: Optional[str]) -> Optional[UserContext]:
        username = db.user_for_sid(sid) if sid else None
        if not username:
            return None
        # Rebuild the context lazily if the process restarted but the binding/creds
        # persisted (only if the user still has stored credentials).
        if username not in self._contexts and not db.get_user(username):
            return None
        return self.context(username)

    @traced
    async def login(self, sid: str, username: str, password: str) -> UserContext:
        """Bind this browser to the user, persist creds, and start their poller."""
        username = (username or "").strip()
        log.info("manager.login: user=%s sid=%s (creds %s)", username, sid[:8], "set" if password else "unchanged")
        ctx = self.context(username)
        if password:
            ctx.session.set_credentials(username, password)
        db.bind_sid(sid, username)
        ctx.start()
        return ctx

    @traced
    async def _wipe(self, username: str, reason: str) -> None:
        """Stop the user's poller and erase all of their stored data (creds, cookies,
        alerts, meta) and browser bindings. Shared by explicit logout and idle-wipe."""
        log.warning("manager._wipe: user=%s reason=%s", username, reason)
        ctx = self._contexts.pop(username, None)
        if ctx is not None:
            await ctx.stop()
            ctx.session.clear_credentials()
        else:
            db.delete_user(username)
        db.unbind_user_sids(username)
        db.clear_alerts(username)
        db.clear_meta(username)
        self._last_seen.pop(username, None)
        db.log_event(username, "info", reason)

    async def logout(self, sid: str) -> None:
        """Unbind this browser; stop + wipe the user's session and data."""
        username = db.user_for_sid(sid)
        db.unbind_sid(sid)
        if not username:
            return
        await self._wipe(username, "Logged out; session + data cleared")

    async def reap_idle(self) -> None:
        """Wipe any logged-in user whose dashboard has gone silent past the timeout
        (browser/tab closed). A page refresh is a sub-second gap, so it's safe."""
        timeout = config.SESSION_IDLE_TIMEOUT
        if timeout <= 0:
            return
        now = time.time()
        for username in list(self._contexts.keys()):
            seen = self._last_seen.get(username)
            if seen is None:
                # First reaper pass after this user appeared — start their clock.
                self._last_seen[username] = now
                continue
            if now - seen > timeout:
                await self._wipe(username, f"Dashboard idle >{int(timeout)}s (closed) — session + data wiped")

    async def resume_all(self) -> None:
        """On startup, restore a context + poller for every user with stored creds."""
        users = db.all_usernames()
        log.info("manager.resume_all: restoring %d user(s): %s", len(users), users)
        for username in users:
            self.context(username)
            # Grace window so a restart doesn't instantly reap users before any
            # browser has had a chance to reconnect.
            self._last_seen[username] = time.time()

    async def stop_all(self) -> None:
        for ctx in list(self._contexts.values()):
            await ctx.stop()
        self._contexts.clear()


manager = UserManager()
