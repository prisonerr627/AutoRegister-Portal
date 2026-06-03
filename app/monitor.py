"""Always-on seat-fill Monitors.

A Monitor watches one section's live seat count and pings Discord when it changes
(mode='change') or drops below a threshold (mode='threshold'). Unlike the reg-window
Alerts (app/poller.py), Monitors are a SEPARATE feature that keeps running after the
dashboard tab closes: a single background loop polls every MONITOR_INTERVAL_SECONDS,
reading seats from the portal's Offered-Sections search (PortalSession.offered_sections)
so it never has to enter the registration workspace (no Select2 timer).

Single-session safety (AIUB allows ONE live session per account and rotates the auth
cookie ~every 5 min — a second concurrent login kicks the first): for each user the
loop BORROWS the live dashboard PortalSession from users.manager when one exists, and
only builds its OWN durable session (store="monitor", creds in monitor_users) when the
dashboard is closed. It also skips any user whose dashboard poller is actively engaged
(registration open / force-flow) so it never fights the live registration session.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from . import config, db
from .applog import log, traced
from .notify import discord_send
from .portal import LoginError, NeedCaptcha, PortalSession
from .schedule import matches_daytime_filter, minutes_to_str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


def _slots_summary(slots: list[dict]) -> str:
    """One-line 'Sun 11:20 AM-12:50 PM & Tue ...' for a notification."""
    return " & ".join(
        f"{s['day'][:3]} {minutes_to_str(s['start'])}-{minutes_to_str(s['end'])}"
        for s in (slots or [])
    )


def _match_section(row: dict, course: str, label: str) -> bool:
    """Does an offered-page row correspond to the monitored course + section label?
    Both course title and section label must match (case/space-insensitive)."""
    if not label:
        return False
    return _norm(row.get("course")) == _norm(course) and _norm(row.get("section")) == _norm(label)


class MonitorManager:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # The monitor's OWN durable sessions, keyed by username — used only when that
        # user has no live dashboard session to borrow.
        self._sessions: dict[str, PortalSession] = {}

    def start(self) -> None:
        if not self.task or self.task.done():
            self._stop.clear()
            self.task = asyncio.create_task(self._run())
            log.info("monitor: background loop started (interval=%ss)", config.MONITOR_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        if self.task:
            try:
                await self.task
            except Exception:  # noqa: BLE001
                pass
            self.task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._cycle()
            except Exception as e:  # noqa: BLE001
                log.exception("monitor: cycle error: %s", e)
            # Interruptible sleep — wakes immediately on stop().
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=config.MONITOR_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _cycle(self) -> None:
        rows = db.list_monitors(active_only=True)
        if not rows:
            return
        by_user: dict[str, list[dict]] = defaultdict(list)
        for m in rows:
            by_user[m["username"]].append(m)

        for username, mons in by_user.items():
            # Gate: don't fight a live registration session. If this user's dashboard
            # poller is engaged (window open / force-flow), skip them this cycle.
            if db.get_meta(username, "registration_open", False) or db.get_meta(username, "force_workspace", False):
                log.debug("monitor[%s]: dashboard engaged (open/force) — skipping cycle", username)
                continue
            session = await self._session_for(username)
            if session is None:
                for m in mons:
                    if m["status"] != "no login":
                        db.update_monitor(m["id"], {"status": "no login", "last_checked_at": _now()})
                continue
            # Fetch the (large) Offered page ONCE per user per cycle, then match all of
            # that user's monitors against the parsed result.
            try:
                sections = await session.offered_sections()
            except NeedCaptcha:
                for m in mons:
                    db.update_monitor(m["id"], {"status": "needs login", "last_checked_at": _now()})
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("monitor[%s]: offered-sections fetch failed: %s", username, e)
                for m in mons:
                    db.update_monitor(m["id"], {"status": "error", "last_checked_at": _now()})
                continue
            for m in mons:
                if self._stop.is_set():
                    return
                await self._check_one(session, username, m, sections)

    async def _session_for(self, username: str) -> PortalSession | None:
        """Pick the session to use for this user: BORROW the live dashboard session if
        one exists (single-session safety), else use/build the monitor's own durable
        session and log it in from monitor_users creds."""
        # Borrow the live dashboard session if present and authenticated.
        from .users import manager
        ctx = manager._contexts.get(username)
        if ctx is not None:
            if ctx.session.logged_in and ctx.session.cookies.get("NAABSUMSMVCFORMSAUTH"):
                return ctx.session
            # Dashboard context exists but isn't authenticated — let the dashboard
            # sort out its own login rather than racing a second one.
            return None

        sess = self._sessions.get(username)
        if sess is None:
            mu = db.get_monitor_user(username)
            if not mu or not mu.get("password"):
                return None
            sess = PortalSession(username, store="monitor")
            self._sessions[username] = sess
        if not (sess.logged_in and sess.cookies.get("NAABSUMSMVCFORMSAUTH")):
            try:
                await sess.auto_login()
                db.log_event(username, "info", f"🪑 Monitor logged in as {sess.display_name or username}")
            except NeedCaptcha:
                log.warning("monitor[%s]: own-session login needs a captcha — can't auto-solve", username)
                return None
            except LoginError as e:
                log.warning("monitor[%s]: own-session login failed: %s", username, e)
                return None
        return sess

    @traced
    async def _check_one(self, session: PortalSession, username: str, m: dict,
                         sections: list[dict]) -> None:
        if m["mode"] == "new_section":
            await self._check_new_section(session, username, m, sections)
            return
        course = m["course_title"]
        match = next((s for s in sections
                      if _match_section(s, course, m["section_label"])), None)
        if match is None:
            db.update_monitor(m["id"], {"status": "section not found", "last_checked_at": _now()})
            return

        count, capacity = match["filled"], match["capacity"]
        prev = m["last_count"]
        mode = m["mode"]
        armed = m["armed"]
        fire = False
        msg = ""

        if prev is None:
            # First observation — just seed; never alert on the baseline.
            pass
        elif mode == "change":
            if count != prev:
                fire = True
                msg = (f"🪑 SEAT CHANGE — {course} [{m['section_label']}] "
                       f"now {count}/{capacity} (was {prev})")
        elif mode == "threshold":
            n = m["threshold"]
            if n is not None:
                if armed and count < n:
                    fire = True
                    armed = False
                    msg = (f"🪑 SEATS BELOW {n} — {course} [{m['section_label']}] "
                           f"now {count}/{capacity} (was {prev})")
                elif count >= n:
                    armed = True  # climbed back; re-arm for the next crossing

        if fire and msg:
            tag = session.display_name or (db.get_monitor_user(username) or {}).get("display_name") or username
            await discord_send(f"[{tag}] {msg}")
            db.log_event(username, "info", msg)

        db.update_monitor(m["id"], {
            "last_count": count,
            "last_capacity": capacity,
            "armed": armed,
            "status": "watching",
            "last_checked_at": _now(),
        })

    @traced
    async def _check_new_section(self, session: PortalSession, username: str, m: dict,
                                 sections: list[dict]) -> None:
        """new_section mode: watch a COURSE (not one section) and ping Discord when a
        section matching the day/time filter either (a) appears with a label we've never
        seen, or (b) was previously full and just freed a seat. The set of matching
        labels + their last open/full state lives in `seen_sections`."""
        course = m["course_title"]
        days, t0, t1 = m.get("days") or [], m.get("time_start"), m.get("time_end")
        matching = [
            s for s in sections
            if _norm(s.get("course")) == _norm(course)
            and matches_daytime_filter(s.get("slots") or [], days, t0, t1)
        ]
        seen: dict = dict(m.get("seen_sections") or {})
        baseline = m["last_checked_at"] is None  # first cycle: seed, never alert

        fires: list[str] = []
        for s in matching:
            label = s["section"]
            count, capacity = s["filled"], s["capacity"]
            is_open = count < capacity
            when = _slots_summary(s.get("slots") or [])
            seats = f"{count}/{capacity}"
            if not baseline:
                if label not in seen:
                    fires.append(f"🆕 NEW SECTION — {course} [{label}] "
                                 f"{seats}{(' · ' + when) if when else ''}")
                elif is_open and seen[label] is False:
                    fires.append(f"🔓 SEAT OPENED — {course} [{label}] "
                                 f"now {seats}{(' · ' + when) if when else ''}")
            seen[label] = is_open

        if fires:
            tag = session.display_name or (db.get_monitor_user(username) or {}).get("display_name") or username
            for msg in fires:
                await discord_send(f"[{tag}] {msg}")
                db.log_event(username, "info", msg)

        status = "watching" if matching or baseline else "no match yet"
        db.update_monitor(m["id"], {
            "seen_sections": seen,
            "status": status,
            "last_checked_at": _now(),
        })


monitor_manager = MonitorManager()
