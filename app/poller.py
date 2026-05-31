"""Background engine.

Keeps the portal session alive (frequent polls keep the rotating auth cookie
fresh), tracks whether registration is open, snapshots the registerable courses
for the dashboard, and evaluates alerts: notifying Discord on newly-open sections
and auto-joining when an alert is ticked (with clash handling).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from . import config, db, portal
from .applog import log, traced
from .notify import discord_send
from .portal import LoginError, NeedCaptcha
from .schedule import (find_clashes, matches_daytime_filter, parse_routine,
                       routine_summary, schedule_gap, slots_clash)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(title: str) -> str:
    return (title or "").strip().upper()


def _section_open(sec: dict) -> bool:
    return (
        not sec.get("Registered")
        and not sec.get("Dropped")
        and (sec.get("Capacity") or 0) > (sec.get("StudentCount") or 0)
    )


def _registered_sections(prereg: dict) -> list[dict]:
    """Currently-registered sections as clash-detection items."""
    out = []
    for course in prereg.get("RegisterableCourses", []):
        for sec in course.get("RegisterableSections", []):
            if sec.get("Registered") and not sec.get("Dropped"):
                out.append(
                    {
                        "title": course.get("Title", ""),
                        "section": sec.get("Title", ""),
                        "section_id": sec.get("ID"),
                        "slots": parse_routine(sec.get("Routine", "")),
                    }
                )
    return out


def _find_course(prereg: dict, alert: dict) -> dict | None:
    want_title = _norm(alert["course_title"])
    want_id = alert.get("course_id")
    for c in prereg.get("RegisterableCourses", []):
        if want_id and c.get("ID") == want_id:
            return c
        if _norm(c.get("Title", "")) == want_title:
            return c
    return None


def _section_matches(sec: dict, alert: dict) -> bool:
    ft = alert.get("filter_type", "any")
    if ft == "section":
        labels = [str(x).strip().upper() for x in alert.get("section_labels", [])]
        if not labels:
            return True
        return str(sec.get("Title", "")).strip().upper() in labels or \
            str(sec.get("ClassID", "")).strip().upper() in labels
    if ft == "daytime":
        slots = parse_routine(sec.get("Routine", ""))
        return matches_daytime_filter(
            slots, alert.get("days") or None,
            alert.get("time_start") or None, alert.get("time_end") or None,
        )
    return True  # 'any'


class Engine:
    """One background poller per logged-in user. Holds that user's PortalSession and
    namespaces all of its DB state by username."""

    def __init__(self, username: str, session: "portal.PortalSession"):
        self.user = username
        self.session = session
        self.task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Last login_count we reconciled Select2 against. Kept in memory (NOT the DB)
        # so it resets together with the session's in-memory login_count on restart;
        # a DB-persisted epoch would survive a restart and never match the fresh
        # count(0), wedging Select2 into a perpetual re-entry loop.
        self._login_epoch = session.login_count

    def start(self):
        if not self.task or self.task.done():
            self._stop.clear()
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self.task:
            try:
                await self.task
            except Exception:  # noqa: BLE001
                pass
            self.task = None

    def _dnote(self, msg: str) -> str:
        """Tag Discord messages with the user so a shared webhook stays readable."""
        return f"[{self.session.display_name or self.user}] {msg}"

    async def _ensure_session(self):
        s = self.session
        if not (s.logged_in and s.cookies.get("NAABSUMSMVCFORMSAUTH")):
            await s.auto_login()
            db.set_meta(self.user, "needs_captcha", False)
            db.set_meta(self.user, "login_error", None)
            db.log_event(self.user, "info", f"Logged in as {s.display_name or self.user}")
        # If a (re)login happened anywhere (here or transparently inside a request),
        # the prior Select2 workspace + 2-min timer no longer apply — redo them.
        if self._login_epoch != s.login_count:
            self._login_epoch = s.login_count
            db.set_meta(self.user, "select2_done", False)
            db.set_meta(self.user, "select2_at", None)

    async def _ensure_select2(self):
        if db.get_meta(self.user, "select2_done"):
            return
        # Hitting Select2 starts the server-side timer; GetPreReg2 then works ~2 min
        # later even if Select2 itself 302-bounced (window closed). We therefore start
        # the timer regardless of the redirect, and capture the Confirm token if the
        # workspace is fully open (200).
        res = await self.session.select2()
        db.set_meta(self.user, "select2_done", True)
        db.set_meta(self.user, "select2_at", time.time())
        if res.get("q"):
            db.set_meta(self.user, "confirm_q", res["q"])
        state = "open" if res.get("ok") else "redirected (window closed)"
        db.log_event(self.user, "info", f"Select2 {state} — GetPreReg2 unlocks in ~{config.SELECT2_WAIT_SECONDS}s")

    def _prereg_ready(self) -> bool:
        """GetPreReg2 is only callable ~2 min after Select2 (server timer)."""
        at = db.get_meta(self.user, "select2_at")
        return bool(at) and (time.time() - at) >= config.SELECT2_WAIT_SECONDS

    async def _run(self):
        db.log_event(self.user, "info", "Poller started")
        while not self._stop.is_set():
            interval = config.POLL_INTERVAL_CLOSED
            try:
                # Wait for the user to log in via the dashboard if we have no creds.
                if not self.session.has_credentials():
                    db.set_meta(self.user, "needs_login", True)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                db.set_meta(self.user, "needs_login", False)

                await self._ensure_session()
                status = await self.session.start_status()
                db.set_meta(self.user, "registration_open", status["open"])
                if status.get("student"):
                    db.set_meta(self.user, "student", status["student"])
                db.set_meta(self.user, "last_poll", _now())

                # Announce open/close transitions.
                if status["open"] != bool(db.get_meta(self.user, "was_open")):
                    db.set_meta(self.user, "was_open", status["open"])
                    db.log_event(self.user, "info", "Registration is OPEN" if status["open"] else "Registration is CLOSED")
                    if status["open"]:
                        await discord_send(self._dnote("🟢 **AIUB registration is OPEN** — engaging."))

                # By default we respect the AIUB flow (engage only when the window is
                # open). The user can force the registration flow to bypass the window.
                forced = bool(db.get_meta(self.user, "force_workspace"))
                engage = status["open"] or forced
                interval = config.POLL_INTERVAL_OPEN if engage else config.POLL_INTERVAL_CLOSED
                log.debug("poller[%s] cycle: open=%s forced=%s engage=%s interval=%ss",
                          self.user, status["open"], forced, engage, interval)

                if engage:
                    if forced and not status["open"] and not db.get_meta(self.user, "forced_logged"):
                        db.set_meta(self.user, "forced_logged", True)
                        db.log_event(self.user, "warn", "FORCE mode: entering registration flow despite closed window")
                    await self._ensure_select2()
                    if not self._prereg_ready():
                        remaining = int(config.SELECT2_WAIT_SECONDS - (time.time() - (db.get_meta(self.user, "select2_at") or 0)))
                        db.set_meta(self.user, "prereg_unlocks_in", max(0, remaining))
                        log.debug("poller[%s] engaged but prereg not ready yet (~%ss to unlock)", self.user, max(0, remaining))
                    else:
                        db.set_meta(self.user, "prereg_unlocks_in", 0)
                        prereg = await self.session.get_prereg2()
                        if prereg.get("HasError"):
                            log.warning("poller[%s] GetPreReg2 error: %s — re-entering Select2", self.user, prereg.get("Message"))
                            db.log_event(self.user, "warn", f"GetPreReg2: {prereg.get('Message')}; re-entering Select2")
                            db.set_meta(self.user, "select2_done", False)
                            db.set_meta(self.user, "select2_at", None)
                        else:
                            log.info("poller[%s] prereg ready: %d registerable course(s), semester=%s",
                                     self.user, len(prereg.get("RegisterableCourses", [])),
                                     (prereg.get("Semester") or {}).get("Title"))
                            # Carry forward already-loaded sections so a bare
                            # GetPreReg2 doesn't make them vanish between refreshes.
                            self._apply_sections_cache(prereg)
                            db.set_meta(self.user, "prereg", prereg)
                            db.set_meta(self.user, "semester", prereg.get("Semester"))
                            # In force mode, allow auto-join too (the point of bypassing).
                            await self._evaluate_alerts(prereg, window_open=status["open"] or forced)
                            # Force a full per-course load on the FIRST prereg of an
                            # engagement (cache empty) so every section appears at once;
                            # subsequent cycles are throttled to SECTIONS_REFRESH_SECONDS.
                            await self._refresh_all_sections(prereg, force=not db.get_meta(self.user, "sections_cache"))
                else:
                    # Idle — don't touch the registration workspace at all.
                    db.set_meta(self.user, "select2_done", False)
                    db.set_meta(self.user, "select2_at", None)
                    db.set_meta(self.user, "prereg_unlocks_in", None)
                    db.set_meta(self.user, "forced_logged", False)
                    db.set_meta(self.user, "sections_cache", None)
                    db.set_meta(self.user, "sections_loaded_at", None)
                    db.set_meta(self.user, "sections_next_at", None)

            except NeedCaptcha as e:
                db.set_meta(self.user, "needs_captcha", True)
                db.set_meta(self.user, "captcha_image", e.image_b64)
                db.log_event(self.user, "warn", "Login needs a manual captcha answer")
                await discord_send(
                    self._dnote("🧩 Login captcha could not be auto-solved — enter it on the dashboard."),
                    image_bytes=self.session._captcha_bytes,
                    filename="captcha.gif",
                )
                interval = 30
            except LoginError as e:
                db.set_meta(self.user, "login_error", str(e))
                db.log_event(self.user, "error", f"Login failed: {e}")
                interval = 30
            except Exception as e:  # noqa: BLE001
                db.log_event(self.user, "error", f"Poller error: {type(e).__name__}: {e}")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _apply_sections_cache(self, prereg: dict):
        """Overlay previously-loaded sections (cached by OfferedCourseId) onto a fresh
        GetPreReg2 so the dashboard never shows an empty sections list mid-cycle."""
        cache = db.get_meta(self.user, "sections_cache") or {}
        if not cache:
            return
        for c in prereg.get("RegisterableCourses", []):
            cached = cache.get(str(c.get("OfferedCourseId")))
            if cached:
                c["RegisterableSections"] = cached

    async def reload_all_sections(self) -> dict:
        """Manual trigger: immediately (re)load every course's sections from the
        current prereg snapshot, bypassing the 10s throttle."""
        prereg = db.get_meta(self.user, "prereg")
        if not prereg or not prereg.get("RegisterableCourses"):
            return {"ok": False, "error": "no registerable courses loaded yet"}
        await self._refresh_all_sections(prereg, force=True)
        return {"ok": True, "count": len(prereg.get("RegisterableCourses", []))}

    async def reload_course_sections(self, offered_course_id) -> dict:
        """Manual trigger for ONE course: (re)load just that course's sections and
        persist them into the prereg snapshot + cache (so the next /api/registerable
        shows them). Used by the per-course 'load sections' button."""
        prereg = db.get_meta(self.user, "prereg")
        if not prereg or not prereg.get("RegisterableCourses"):
            return {"ok": False, "error": "no registerable courses loaded yet"}
        want = str(offered_course_id)
        course = next((c for c in prereg["RegisterableCourses"]
                       if str(c.get("OfferedCourseId")) == want), None)
        if not course:
            return {"ok": False, "error": "course not found"}
        try:
            loaded = await self.session.load_sections(course)
            secs = (loaded.get("Data") or {}).get("RegisterableSections")
        except Exception as e:  # noqa: BLE001
            db.log_event(self.user, "error", f"LoadSections {course.get('Title')} failed: {e}")
            return {"ok": False, "error": str(e)}
        if secs is None:
            return {"ok": False, "error": "no sections returned"}
        course["RegisterableSections"] = secs
        cache = db.get_meta(self.user, "sections_cache") or {}
        cache[want] = secs
        db.set_meta(self.user, "sections_cache", cache)
        db.set_meta(self.user, "prereg", prereg)
        return {"ok": True, "count": len(secs)}

    async def _refresh_all_sections(self, prereg: dict, force: bool = False):
        """Live-load every course's bookable sections (throttled to SECTIONS_REFRESH_
        SECONDS unless forced) and cache them so the dashboard shows all sections +
        live seats."""
        last = db.get_meta(self.user, "sections_loaded_at") or 0
        if not force and time.time() - last < config.SECTIONS_REFRESH_SECONDS:
            return
        cache = db.get_meta(self.user, "sections_cache") or {}
        for course in prereg.get("RegisterableCourses", []):
            try:
                loaded = await self.session.load_sections(course)
                secs = (loaded.get("Data") or {}).get("RegisterableSections")
                if secs is not None:
                    course["RegisterableSections"] = secs
                    cache[str(course.get("OfferedCourseId"))] = secs
            except Exception as e:  # noqa: BLE001
                db.log_event(self.user, "error", f"LoadSections {course.get('Title')} failed: {e}")
        db.set_meta(self.user, "sections_cache", cache)
        db.set_meta(self.user, "prereg", prereg)
        now = time.time()
        db.set_meta(self.user, "sections_loaded_at", now)
        # Publish the REAL next-refresh time so the dashboard countdown matches the
        # poller's cadence. A refresh only fires on the first engaged poll tick once
        # the throttle has elapsed, so the effective period is SECTIONS_REFRESH_SECONDS
        # rounded UP to a whole POLL_INTERVAL_OPEN (e.g. 10s throttle + 4s ticks → 12s).
        iv = config.POLL_INTERVAL_OPEN
        period = -(-config.SECTIONS_REFRESH_SECONDS // iv) * iv if iv > 0 else config.SECTIONS_REFRESH_SECONDS
        db.set_meta(self.user, "sections_next_at", now + period)

    # ─── alert evaluation ────────────────────────────────────────────────
    async def _evaluate_alerts(self, prereg: dict, window_open: bool):
        alerts = db.list_alerts(self.user, active_only=True)
        if not alerts:
            return
        registered = _registered_sections(prereg)
        for alert in alerts:
            try:
                await self._evaluate_one(alert, prereg, registered, window_open)
            except Exception as e:  # noqa: BLE001
                db.log_event(self.user, "error", f"Alert #{alert['id']} error: {e}")

    async def _evaluate_one(self, alert: dict, prereg: dict, registered: list[dict], window_open: bool):
        course = _find_course(prereg, alert)
        if not course:
            return
        loaded = await self.session.load_sections(course)
        data = loaded.get("Data") or {}
        sections = data.get("RegisterableSections", []) or course.get("RegisterableSections", [])

        title = course.get("Title", alert["course_title"])
        matching_open = [s for s in sections if _section_open(s) and _section_matches(s, alert)]
        log.debug("alert#%s[%s] %s: %d section(s) loaded, %d open & matching (filter=%s auto_join=%s)",
                  alert["id"], self.user, title, len(sections), len(matching_open),
                  alert.get("filter_type"), alert.get("auto_join"))

        # Notify on each newly-open matching section (independent of auto-join).
        notified = set(alert.get("notified_section_ids", []))
        new_secs = [s for s in matching_open if s.get("ID") not in notified]
        for sec in new_secs:
            notified.add(sec.get("ID"))
            label = sec.get("Title", "?")
            seats = f"{sec.get('StudentCount')}/{sec.get('Capacity')}"
            routine = routine_summary(sec.get("Routine", ""))
            db.log_event(self.user, "info", f"OPEN {title} [{label}] {seats} {routine}")
            suffix = "" if window_open else " (will auto-join when the window opens)" if alert.get("auto_join") else ""
            await discord_send(self._dnote(f"🔔 **OPEN** {title} **[{label}]** ({seats}) — {routine}{suffix}"))
        if new_secs:
            db.update_alert(self.user, alert["id"], {"notified_section_ids": list(notified)})

        # Auto-join: pick the best section to join (one per alert).
        if alert.get("auto_join") and window_open and matching_open:
            sec = self._pick_join_section(alert, matching_open, registered)
            if sec:
                log.info("alert#%s[%s] %s: selected section [%s] to auto-join",
                         alert["id"], self.user, title, sec.get("Title"))
                await self._try_join(alert, course, sec, registered, prereg)
            else:
                log.info("alert#%s[%s] %s: no joinable (non-clashing) section — skipped",
                         alert["id"], self.user, title)
        elif alert.get("auto_join") and not window_open:
            log.debug("alert#%s[%s] %s: auto-join armed but window closed — alert only for now",
                      alert["id"], self.user, title)

    def _pick_join_section(self, alert: dict, candidates: list[dict], registered: list[dict]) -> dict | None:
        """Choose which open, matching section to auto-join.

        - 'specific section': honor the user's TYPED label order — "A, B" tries A first
          and only falls back to B when A isn't open. Stable sort, so equal-rank ties
          keep the portal's order.
        - 'day/time': first match in portal order (a window has no ranked list to honor).
        - 'any section': never drop — pick the NON-CLASHING section that packs tightest
          into the existing timetable (least idle gap via schedule_gap); if every open
          section clashes, join nothing (the user was still alerted).
        Clash/drop for the scoped modes is left to _try_join."""
        if not candidates:
            return None
        ft = alert.get("filter_type")
        if ft == "section":
            labels = [str(x).strip().upper() for x in alert.get("section_labels", [])]

            def _rank(sec: dict) -> int:
                title = str(sec.get("Title", "")).strip().upper()
                class_id = str(sec.get("ClassID", "")).strip().upper()
                for i, lbl in enumerate(labels):
                    if lbl in (title, class_id):
                        return i
                return len(labels)  # unranked (e.g. no labels typed) -> keep portal order

            return sorted(candidates, key=_rank)[0]
        if ft != "any":
            return candidates[0]  # day/time: no typed order to rank by
        reg_slots = [s for r in registered for s in r.get("slots", [])]
        scored = []
        for sec in candidates:
            cs = parse_routine(sec.get("Routine", ""))
            if not cs or slots_clash(cs, reg_slots):
                continue  # clashes with a registered class — skip (any-mode won't drop)
            scored.append((schedule_gap(cs, reg_slots), sec))
        if not scored:
            return None
        scored.sort(key=lambda t: t[0])
        return scored[0][1]

    @traced
    async def _try_join(self, alert, course, sec, registered, prereg):
        title = course.get("Title", alert["course_title"])
        label = sec.get("Title", "?")
        candidate_slots = parse_routine(sec.get("Routine", ""))
        clashes = find_clashes(candidate_slots, registered)
        log.info("_try_join[%s] %s [%s] policy=%s filter=%s clashes=%s",
                 self.user, title, label, alert.get("clash_policy"),
                 alert.get("filter_type"), [f"{c['title']} [{c['section']}]" for c in clashes])

        if clashes:
            # Dropping a registered course to make room is only allowed for a
            # deliberately-scoped alert (specific section(s) or a day/time window).
            # An "any section" alert is too broad to drop for — just alert & skip.
            allow_drop = (alert.get("clash_policy") == "unregister"
                          and alert.get("filter_type") != "any")
            if not allow_drop:
                msg = ", ".join(f"{c['title']} [{c['section']}]" for c in clashes)
                db.log_event(self.user, "warn", f"{title} [{label}] clashes with {msg} — alert only")
                await discord_send(self._dnote(f"⚠️ {title} **[{label}]** clashes with {msg} — not auto-joined."))
                return
            targets = {_norm(t) for t in alert.get("unregister_targets", [])}
            not_approved = [c for c in clashes if targets and _norm(c["title"]) not in targets]
            if not_approved:
                msg = ", ".join(f"{c['title']}" for c in not_approved)
                db.log_event(self.user, "warn", f"{title} [{label}] clashes with un-approved {msg} — skipped")
                await discord_send(self._dnote(f"⛔ {title} **[{label}]** clashes with {msg} (not approved to drop) — skipped."))
                return
            for c in clashes:
                if not c.get("section_id"):
                    continue
                res = await self.session.unregister_section(c["section_id"])
                db.log_event(self.user, "info", f"Dropped {c['title']} [{c['section']}] -> {res.get('IsSuccess')} err={res.get('Error')}")
                await discord_send(self._dnote(f"♻️ Dropped **{c['title']} [{c['section']}]** to free {title}."))

        res = await self.session.register_section(sec)
        if res.get("IsSuccess"):
            db.update_alert(self.user, alert["id"], {"status": "joined", "active": 0})
            db.log_event(self.user, "info", f"REGISTERED {title} [{label}]")
            await discord_send(self._dnote(f"✅ **Registered** {title} **[{label}]** — {routine_summary(sec.get('Routine',''))}"))
        else:
            err = res.get("Error") or res
            db.log_event(self.user, "error", f"Register {title} [{label}] failed: {err}")
            await discord_send(self._dnote(f"❌ Failed to register {title} [{label}]: {err}"))
