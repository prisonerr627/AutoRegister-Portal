"""FastAPI app: multi-user dashboard + control API.

Each browser is bound to an AIUB user via an httponly `sid` cookie; every request
resolves that user's isolated context (own portal session, cookies, alerts, toggles,
activity log, and background poller). Pollers for users with stored credentials are
resumed on startup.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .applog import log
from .catalog import Catalog
from .poller import _norm, _registered_sections
from .monitor import monitor_manager
from .notify import discord_send
from .portal import LoginError, NeedCaptcha
from .schedule import WEEKDAYS, find_clashes, make_slot, parse_time_to_minutes
from .users import UserContext, manager

STATIC_DIR = Path(__file__).resolve().parent / "static"
catalog: Catalog | None = None
# Serialise catalog (re)builds; flag a download in flight for the dashboard.
_catalog_lock = asyncio.Lock()
_catalog_refreshing = False


def _catalog_path() -> Path:
    """The active catalog file: the live JSON cache (parsed from the Offered page) if
    present, else the legacy xlsx override, else the bundled seed shipped with the app."""
    if config.CATALOG_CACHE.exists():
        return config.CATALOG_CACHE
    return config.CATALOG_OVERRIDE if config.CATALOG_OVERRIDE.exists() else config.CATALOG_XLSX


def _catalog_is_live() -> bool:
    return config.CATALOG_CACHE.exists() or config.CATALOG_OVERRIDE.exists()


def _catalog_age_seconds() -> Optional[float]:
    """Seconds since the active catalog file was last written, or None if missing."""
    p = _catalog_path()
    try:
        return max(0.0, time.time() - p.stat().st_mtime)
    except OSError:
        return None


def _catalog_info() -> dict:
    age = _catalog_age_seconds()
    return {
        "count": len(catalog.titles()) if catalog else 0,
        "age_seconds": round(age) if age is not None else None,
        "refreshing": _catalog_refreshing,
        "source": "live" if _catalog_is_live() else "bundled",
    }


def _load_catalog() -> None:
    """(Re)load the global catalog from whichever file is active: the live JSON cache
    (Offered-page parse), else a legacy xlsx (override or bundled seed)."""
    global catalog
    path = _catalog_path()
    try:
        if path.suffix == ".json":
            catalog = Catalog(json.loads(path.read_text(encoding="utf-8")))
        else:
            catalog = Catalog.load(path)
        log.info("loaded catalog: %d courses from %s", len(catalog.titles()), path)
    except Exception as e:  # noqa: BLE001
        if catalog is None:
            catalog = Catalog({})
        log.exception("catalog load failed (%s): %s", path, e)


async def refresh_catalog(session) -> dict:
    """Rebuild the catalog from the live Offered-Sections page (GET /Student/Section/
    Offered?q=…) via a logged-in session, persist it as the JSON cache, and reload the
    global catalog. Replaces the old ~30s DownloadOfferedReport xlsx path. Returns
    {ok, count, error}."""
    global _catalog_refreshing
    async with _catalog_lock:
        _catalog_refreshing = True
        try:
            html = await session.offered_html()
            cat = Catalog.from_offered_html(html)
            n = len(cat.titles())
            if n == 0:
                raise ValueError("offered page parsed to 0 courses")
            tmp = config.CATALOG_CACHE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cat.courses), encoding="utf-8")
            tmp.replace(config.CATALOG_CACHE)
            _load_catalog()
            log.info("catalog refreshed from Offered page: %d courses", n)
            return {"ok": True, "count": n}
        except Exception as e:  # noqa: BLE001
            log.exception("catalog refresh failed: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            _catalog_refreshing = False


def maybe_refresh_catalog(session) -> None:
    """Fire a background catalog refresh if the active file is stale (older than
    CATALOG_MAX_AGE_HOURS) and one isn't already running. Non-blocking.

    Still GATED off the live registration flow: the portal serialises every request
    within a session server-side (ASP.NET session lock), so even the quick (~2.5s)
    Offered-page fetch would briefly stall the poller/force-flow. We skip the
    auto-refresh while registration is open or force-flow is on (the poller's 'engaged'
    states) — the catalog isn't time-critical, and the manual button is always there."""
    if _catalog_refreshing:
        return
    user = getattr(session, "user", "") or ""
    if db.get_meta(user, "registration_open", False) or db.get_meta(user, "force_workspace", False):
        log.info("catalog stale but registration open / force-flow on — deferring refresh (avoid stalling poller)")
        return
    age = _catalog_age_seconds()
    if age is not None and age < config.CATALOG_MAX_AGE_HOURS * 3600:
        return
    log.info("catalog is stale (age=%ss) — scheduling background refresh",
             round(age) if age is not None else "missing")
    asyncio.create_task(refresh_catalog(session))


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("lifespan: startup begin")
    _load_catalog()
    await manager.resume_all()
    reaper = asyncio.create_task(_idle_reaper())
    monitor_manager.start()
    log.info("lifespan: startup complete (idle reaper + monitor loop started)")
    yield
    log.info("lifespan: shutdown begin")
    reaper.cancel()
    await monitor_manager.stop()
    await manager.stop_all()
    log.info("lifespan: shutdown complete")


async def _idle_reaper():
    """Periodically wipe users whose dashboard has gone silent (browser closed)."""
    while True:
        await asyncio.sleep(15)
        try:
            await manager.reap_idle()
        except Exception as e:  # noqa: BLE001
            print(f"[error] idle reaper: {e}")


app = FastAPI(title="AutoRegister Portal", lifespan=lifespan)


@app.middleware("http")
async def ensure_sid(request: Request, call_next):
    """Guarantee every browser carries a stable httponly `sid` cookie (its identity)."""
    sid = request.cookies.get("sid")
    fresh = None
    if not sid:
        sid = fresh = secrets.token_urlsafe(32)
    request.state.sid = sid
    # Trace every HTTP action (skip noisy static asset + favicon hits). Routine
    # dashboard pollers are logged at DEBUG so INFO stays readable; mutations and
    # one-off actions log at INFO.
    path = request.url.path
    traceable = not path.startswith("/static") and path != "/favicon.ico"
    _POLLS = {"/api/status", "/api/events", "/api/alerts", "/api/registerable"}
    lvl = 10 if (request.method == "GET" and path in _POLLS) else 20
    user = db.user_for_sid(sid) if sid else None
    t0 = time.time()
    if traceable:
        log.log(lvl, "HTTP %s %s (sid=%s user=%s)", request.method, path, sid[:8] if sid else "-", user or "-")
    try:
        response = await call_next(request)
    except Exception as e:  # noqa: BLE001
        log.exception("HTTP %s %s raised: %s", request.method, path, e)
        raise
    if traceable:
        log.log(lvl, "HTTP %s %s -> %s [%.0fms]", request.method, path, response.status_code, (time.time() - t0) * 1000)
    if fresh:
        response.set_cookie("sid", fresh, httponly=True, samesite="lax", max_age=31536000)
    return response


def require_auth(x_auth_token: Optional[str] = Header(None), token: Optional[str] = Query(None)):
    """Optional site-wide gate (DASHBOARD_TOKEN). Identity is separate (sid cookie)."""
    if config.DASHBOARD_TOKEN and config.DASHBOARD_TOKEN not in (x_auth_token, token):
        raise HTTPException(401, "bad token")


class Caller:
    def __init__(self, sid: str, ctx: Optional[UserContext]):
        self.sid = sid
        self.ctx = ctx


def caller(request: Request) -> Caller:
    sid = getattr(request.state, "sid", None) or request.cookies.get("sid") or ""
    ctx = manager.context_for_sid(sid)
    if ctx is not None:
        # Each authenticated request is a heartbeat; keeps the idle-wipe reaper at bay.
        manager.touch(ctx.username)
    return Caller(sid, ctx)


def _ctx(c: Caller) -> UserContext:
    if not c.ctx:
        raise HTTPException(401, "not logged in")
    return c.ctx


# ─── status & login ──────────────────────────────────────────────────────────
@app.get("/api/status", dependencies=[Depends(require_auth)])
async def status(c: Caller = Depends(caller)):
    if not c.ctx:
        return {
            "logged_in": False, "display_name": "", "has_credentials": False,
            "needs_login": True, "registration_open": False, "needs_captcha": False,
            "login_error": None, "student": {}, "semester": {}, "last_poll": None,
            "prereg_unlocks_in": None, "force_workspace": False,
            "proxy": config.PROXY_URL, "verify_tls": config.VERIFY_TLS, "confirm_q": None,
            "username": None, "sections_next_in": None,
            "sections_refresh_seconds": config.SECTIONS_REFRESH_SECONDS,
            "catalog": _catalog_info(),
        }
    s = c.ctx.session
    u = c.ctx.username
    # Seconds until the poller's next live section refresh. The poller publishes the
    # real target time (sections_next_at), which already accounts for the poll cadence;
    # None when idle / not yet loaded.
    next_at = db.get_meta(u, "sections_next_at")
    sections_next_in = max(0, round(next_at - time.time())) if next_at else None
    return {
        "logged_in": s.logged_in,
        "display_name": s.display_name,
        "has_credentials": s.has_credentials(),
        "needs_login": db.get_meta(u, "needs_login", not s.has_credentials()),
        "registration_open": db.get_meta(u, "registration_open", False),
        "needs_captcha": db.get_meta(u, "needs_captcha", False),
        "login_error": db.get_meta(u, "login_error"),
        "student": db.get_meta(u, "student", {}),
        "semester": db.get_meta(u, "semester", {}),
        "last_poll": db.get_meta(u, "last_poll"),
        "prereg_unlocks_in": db.get_meta(u, "prereg_unlocks_in"),
        "force_workspace": db.get_meta(u, "force_workspace", False),
        "proxy": config.PROXY_URL,
        "verify_tls": config.VERIFY_TLS,
        "confirm_q": db.get_meta(u, "confirm_q"),
        "username": s.username,
        "sections_next_in": sections_next_in,
        "sections_refresh_seconds": config.SECTIONS_REFRESH_SECONDS,
        "catalog": _catalog_info(),
    }


@app.get("/api/login/captcha", dependencies=[Depends(require_auth)])
async def login_captcha(c: Caller = Depends(caller)):
    if not c.ctx:
        return {"captcha_image": None, "needs_captcha": False}
    img = await c.ctx.session.prepare_login()
    return {"captcha_image": img, "needs_captcha": bool(img)}


@app.post("/api/login", dependencies=[Depends(require_auth)])
async def login(request: Request, payload: dict = Body(default={})):
    payload = payload or {}
    sid = request.state.sid
    answer = payload.get("answer")
    username = (payload.get("username") or "").strip()
    password = payload.get("password")
    if username:
        ctx = await manager.login(sid, username, password or "")
    else:
        ctx = manager.context_for_sid(sid)  # captcha resubmit on an existing attempt
    if not ctx or not ctx.session.has_credentials():
        return JSONResponse({"ok": False, "error": "enter your AIUB ID and password"}, status_code=200)
    u = ctx.username
    try:
        if answer:
            await ctx.session.submit_login(answer)
        else:
            await ctx.session.auto_login()
        db.set_meta(u, "needs_captcha", False)
        db.set_meta(u, "needs_login", False)
        db.set_meta(u, "login_error", None)
        ctx.start()
        # Keep the offered-course catalog current: refresh in the background if stale.
        maybe_refresh_catalog(ctx.session)
        return {"ok": True, "display_name": ctx.session.display_name}
    except NeedCaptcha as e:
        db.set_meta(u, "needs_captcha", True)
        return JSONResponse({"ok": False, "needs_captcha": True, "captcha_image": e.image_b64}, status_code=200)
    except LoginError as e:
        msg = str(e)
        db.set_meta(u, "login_error", msg)
        # A wrong captcha (auto-solve misread it) shouldn't dead-end with a hidden
        # field — re-arm the captcha with a fresh image so the user can read it and
        # retry manually instead of having to click Log in a second time.
        if "captcha" in msg.lower():
            try:
                img = await ctx.session.prepare_login()
            except Exception:
                img = None
            db.set_meta(u, "needs_captcha", bool(img))
            return JSONResponse(
                {"ok": False, "needs_captcha": bool(img), "captcha_image": img, "error": msg},
                status_code=200,
            )
        return JSONResponse({"ok": False, "error": msg}, status_code=200)


def _reset_dashboard(user: str) -> None:
    """Flush a user's state to defaults: Force-flow OFF, alerts removed, registerable
    panel (prereg + section cache) cleared."""
    for k in ("force_workspace", "select2_done", "select2_at", "prereg",
              "sections_cache", "sections_loaded_at", "sections_next_at", "confirm_q"):
        db.set_meta(user, k, False if k in ("force_workspace", "select2_done") else None)
    n = db.clear_alerts(user)
    if n:
        db.log_event(user, "info", f"Reset to defaults: cleared {n} alert(s), force-flow off, panel cleared")


@app.post("/api/reset", dependencies=[Depends(require_auth)])
async def reset_dashboard(c: Caller = Depends(caller)):
    """Reset Force-flow, alerts, and the registerable panel to defaults (page exit)."""
    if c.ctx:
        _reset_dashboard(c.ctx.username)
    return {"ok": True}


@app.post("/api/logout", dependencies=[Depends(require_auth)])
async def logout(c: Caller = Depends(caller)):
    # Explicit log out honours "clear stored credentials": unlike the idle browser-close
    # wipe (which deliberately leaves Monitors running in the background), it also forgets
    # this user's seat-fill Monitors and their durable monitor credentials.
    username = db.user_for_sid(c.sid)
    if username:
        n = db.clear_monitors(username)
        db.delete_monitor_user(username)
        if n:
            db.log_event(username, "info", f"Log out: also removed {n} seat-fill monitor(s)")
    await manager.logout(c.sid)
    return {"ok": True}


# ─── live registration actions ────────────────────────────────────────────────
@app.get("/api/registerable", dependencies=[Depends(require_auth)])
async def registerable(c: Caller = Depends(caller)):
    if not c.ctx:
        return {"open": False, "semester": None, "courses": [], "registered": []}
    u = c.ctx.username
    prereg = db.get_meta(u, "prereg", {})
    return {
        "open": db.get_meta(u, "registration_open", False),
        "semester": prereg.get("Semester"),
        "courses": prereg.get("RegisterableCourses", []),
        "registered": _registered_sections(prereg),
    }


@app.post("/api/registerable/reload", dependencies=[Depends(require_auth)])
async def reload_sections(c: Caller = Depends(caller)):
    """Manually force an immediate reload of every course's sections."""
    return await _ctx(c).engine.reload_all_sections()


@app.post("/api/course/sections", dependencies=[Depends(require_auth)])
async def course_sections(course: dict = Body(...), c: Caller = Depends(caller)):
    res = await _ctx(c).session.load_sections(course)
    return res.get("Data") or {}


@app.post("/api/course/reload", dependencies=[Depends(require_auth)])
async def reload_one_course(payload: dict = Body(...), c: Caller = Depends(caller)):
    """Reload a single course's sections (by OfferedCourseId) and persist them."""
    cid = payload.get("offered_course_id", payload.get("id"))
    return await _ctx(c).engine.reload_course_sections(cid)


@app.post("/api/register", dependencies=[Depends(require_auth)])
async def register(section: dict = Body(...), c: Caller = Depends(caller)):
    ctx = _ctx(c)
    u = ctx.username
    label = section.get("Title", section.get("ID"))
    res = await ctx.session.register_section(section)
    db.log_event(u, "info" if res.get("IsSuccess") else "error",
                 f"Manual register {label} -> IsSuccess={res.get('IsSuccess')} Error={res.get('Error')}")
    return res


@app.post("/api/unregister", dependencies=[Depends(require_auth)])
async def unregister(payload: dict = Body(...), c: Caller = Depends(caller)):
    ctx = _ctx(c)
    u = ctx.username
    sid_ = payload.get("sectionID") or payload.get("section_id")
    res = await ctx.session.unregister_section(sid_)
    db.log_event(u, "info" if res.get("IsSuccess") else "error",
                 f"Manual unregister {sid_} -> IsSuccess={res.get('IsSuccess')} Error={res.get('Error')}")
    return res


@app.post("/api/force-workspace", dependencies=[Depends(require_auth)])
async def force_workspace(payload: dict = Body(default={}), c: Caller = Depends(caller)):
    """Toggle forcing the registration flow to bypass the AIUB open-window check."""
    u = _ctx(c).username
    enabled = bool((payload or {}).get("enabled"))
    db.set_meta(u, "force_workspace", enabled)
    if not enabled:
        db.set_meta(u, "select2_done", False)
        db.set_meta(u, "select2_at", None)
    db.log_event(u, "warn", f"Force-workspace {'ENABLED (bypassing window)' if enabled else 'disabled'}")
    return {"force_workspace": enabled}


@app.post("/api/confirm", dependencies=[Depends(require_auth)])
async def confirm(c: Caller = Depends(caller)):
    ctx = _ctx(c)
    q = db.get_meta(ctx.username, "confirm_q")
    if not q:
        raise HTTPException(400, "no confirm token captured yet (open Select2 first)")
    return await ctx.session.confirm(q)


@app.get("/api/registered", dependencies=[Depends(require_auth)])
async def registered_home(semester: Optional[str] = None, c: Caller = Depends(caller)):
    """The student's registered courses for the target semester (scraped from Home)."""
    ctx = _ctx(c)
    return await ctx.session.registered_courses(
        semester or (db.get_meta(ctx.username, "semester") or {}).get("Title")
    )


# ─── catalog (course picker; global read-only) ─────────────────────────────────
@app.get("/api/catalog", dependencies=[Depends(require_auth)])
async def catalog_search(q: str = ""):
    return {"titles": (catalog.search(q) if catalog else [])}


@app.get("/api/catalog/sections", dependencies=[Depends(require_auth)])
async def catalog_sections(title: str):
    c = catalog.get(title) if catalog else None
    if not c:
        return {"title": title, "sections": []}
    sections = [
        {
            "section": label,
            "type": sec.get("type", ""),
            "routine": " & ".join(
                f"{s['day'][:3]} {s['start']//60:02d}:{s['start']%60:02d}-{s['end']//60:02d}:{s['end']%60:02d}"
                for s in sec["slots"]
            ),
        }
        for label, sec in sorted(c["sections"].items())
    ]
    return {"title": c["title"], "department": c.get("department", ""), "sections": sections}


@app.post("/api/catalog/refresh", dependencies=[Depends(require_auth)])
async def catalog_refresh(c: Caller = Depends(caller)):
    """Rebuild the catalog live from the portal's Offered-Sections page (using the
    caller's logged-in session). Fast (~2.5s) — replaces the old ~30s xlsx download."""
    ctx = _ctx(c)
    if not ctx.session.logged_in:
        return JSONResponse({"ok": False, "error": "log in first"}, status_code=200)
    res = await refresh_catalog(ctx.session)
    res["catalog"] = _catalog_info()
    return res


# ─── alerts CRUD ──────────────────────────────────────────────────────────────
@app.get("/api/alerts", dependencies=[Depends(require_auth)])
async def get_alerts(c: Caller = Depends(caller)):
    if not c.ctx:
        return {"alerts": []}
    return {"alerts": db.list_alerts(c.ctx.username)}


@app.post("/api/alerts", dependencies=[Depends(require_auth)])
async def add_alert(data: dict = Body(...), c: Caller = Depends(caller)):
    if not data.get("course_title"):
        raise HTTPException(400, "course_title required")
    return db.create_alert(_ctx(c).username, data)


@app.patch("/api/alerts/{alert_id}", dependencies=[Depends(require_auth)])
async def patch_alert(alert_id: int, data: dict = Body(...), c: Caller = Depends(caller)):
    a = db.update_alert(_ctx(c).username, alert_id, data)
    if not a:
        raise HTTPException(404, "not found")
    return a


@app.delete("/api/alerts/{alert_id}", dependencies=[Depends(require_auth)])
async def remove_alert(alert_id: int, c: Caller = Depends(caller)):
    db.delete_alert(_ctx(c).username, alert_id)
    return {"ok": True}


@app.post("/api/alerts/clash-check", dependencies=[Depends(require_auth)])
async def clash_check(payload: dict = Body(...), c: Caller = Depends(caller)):
    """Report which registered courses a chosen day/time window overlaps. Clash is
    pure time overlap — course-agnostic. Portal-only, never the catalog: the
    registered baseline is the live workspace when loaded, else the
    /Student/Registration summary (real times, no window/Force-flow needed)."""
    ctx = _ctx(c)
    prereg = db.get_meta(ctx.username, "prereg", {}) or {}

    # Registered baseline (the 'against' set). Prefer the live workspace (carries
    # section_id, matches auto-join); else scrape the registration summary page, which
    # has the real class times without the window. Either way: portal, never catalog.
    registered = _registered_sections(prereg)
    source = "registration workspace"
    if not registered:
        try:
            sched = await ctx.session.registered_schedule(payload.get("semester"))
            registered = [{"title": rc["title"], "section": rc["section"],
                           "section_id": None, "slots": rc["slots"]}
                          for rc in sched.get("courses", [])]
            source = f"registration summary · {sched.get('semester') or '?'}"
        except Exception as e:  # noqa: BLE001
            db.log_event(ctx.username, "warn", f"Registration-summary scrape failed: {e}")

    # Candidate = the day/time window the user picked (days + From/To). A missing
    # bound is treated as open (whole day); no day selected means every day.
    days = [d for d in (payload.get("days") or []) if d]
    ts = (payload.get("time_start") or "").strip()
    te = (payload.get("time_end") or "").strip()
    # An EMPTY window (no days AND no time bounds — e.g. a section-scoped alert that has
    # no day/time filter) is not "all day, every day": treating it that way trivially
    # overlaps every registered course, a useless flood. With no real window we skip the
    # time-overlap check entirely and report only the same-course supersede below.
    has_window = bool(days or ts or te)
    if has_window:
        start_str = ts if parse_time_to_minutes(ts) is not None else "12:00 AM"
        end_str = te if parse_time_to_minutes(te) is not None else "11:59 PM"
        window_slots = [s for d in (days or WEEKDAYS) if (s := make_slot(d, start_str, end_str))]
    else:
        window_slots = []

    clashing = find_clashes(window_slots, registered)
    # Same-course supersede (mirrors poller._try_join so the preview matches what
    # auto-join actually does): the portal forbids holding two sections of one course at
    # once, even with NO time overlap — so a section of the alert's OWN course you're
    # already in is a drop candidate the pure time-overlap check above can't catch.
    # Surface it separately when the UI sends the chosen course title.
    want = _norm(payload.get("course_title") or "")
    clashing_ids = {id(x) for x in clashing}
    same_course = [r for r in registered
                   if want and _norm(r.get("title")) == want and id(r) not in clashing_ids]
    drop_targets = clashing + same_course
    clash_titles = sorted({x["title"] for x in drop_targets})
    return {
        "mode": "window",
        "has_window": has_window,
        "window": {"days": days, "time_start": ts, "time_end": te},
        "clashes": [{"title": x["title"], "section": x["section"]} for x in clashing],
        "same_course": [{"title": x["title"], "section": x["section"]} for x in same_course],
        "clash_course_titles": clash_titles,
        "source": source,
        "registered_considered": [{"title": x["title"], "section": x["section"]} for x in registered],
    }


# ─── seat-fill monitors CRUD ───────────────────────────────────────────────────
@app.get("/api/monitors", dependencies=[Depends(require_auth)])
async def get_monitors(c: Caller = Depends(caller)):
    if not c.ctx:
        return {"monitors": []}
    monitors = db.list_monitors(c.ctx.username)
    for m in monitors:
        routine = ""
        if catalog:
            entry = catalog.get(m["course_title"])
            if entry:
                sec = entry["sections"].get(m["section_label"])
                if sec:
                    routine = " & ".join(
                        f"{s['day'][:3]} {s['start']//60:02d}:{s['start']%60:02d}-{s['end']//60:02d}:{s['end']%60:02d}"
                        for s in sec["slots"]
                    )
        m["routine"] = routine
    u = c.ctx.username
    return {
        "monitors": monitors,
        # Lets the panel show why monitors are paused + the opt-in toggle without a
        # second round-trip to /api/status.
        "run_when_open": db.get_meta(u, "monitor_when_open", False),
        "registration_open": db.get_meta(u, "registration_open", False),
        "force_workspace": db.get_meta(u, "force_workspace", False),
    }


@app.post("/api/monitors/run-when-open", dependencies=[Depends(require_auth)])
async def monitors_run_when_open(payload: dict = Body(default={}), c: Caller = Depends(caller)):
    """Opt in/out of running seat monitors while the registration window is open (or
    force-flow is on). When ON, the monitor borrows the live registration session (no
    second login) — it may add a brief delay to a poll but never kicks the session."""
    u = _ctx(c).username
    enabled = bool((payload or {}).get("enabled"))
    db.set_meta(u, "monitor_when_open", enabled)
    db.log_event(u, "info",
                 f"🪑 Run monitors during open registration {'ENABLED' if enabled else 'disabled'}")
    return {"run_when_open": enabled}


@app.post("/api/monitors", dependencies=[Depends(require_auth)])
async def add_monitor(data: dict = Body(...), c: Caller = Depends(caller)):
    """Create a seat-fill monitor and arm its durable credential store so it keeps
    running after the dashboard tab closes. Requires a logged-in session (so we can
    capture the password + cookies for the background loop's own session)."""
    ctx = _ctx(c)
    sess = ctx.session
    if not (sess.logged_in and sess.has_credentials()):
        raise HTTPException(400, "log in first so the monitor can keep your session alive")
    if not data.get("course_title"):
        raise HTTPException(400, "course_title required")
    mode = data.get("mode", "change")
    if mode not in ("threshold", "change", "new_section"):
        raise HTTPException(400, "mode must be 'threshold', 'change' or 'new_section'")
    # new_section watches a whole COURSE (any matching label), so it needs no specific
    # section; the others watch one named section.
    if mode != "new_section" and not data.get("section_label"):
        raise HTTPException(400, "section_label required")
    threshold = data.get("threshold")
    if mode == "threshold":
        try:
            threshold = int(threshold)
        except (TypeError, ValueError):
            raise HTTPException(400, "threshold (a seat number) is required for threshold mode")
    else:
        threshold = None
    days = data.get("days") or [] if mode == "new_section" else []
    time_start = data.get("time_start") if mode == "new_section" else None
    time_end = data.get("time_end") if mode == "new_section" else None
    u = ctx.username
    # Arm the durable store: copy the live session's password + current cookie jar so
    # the background loop can log in independently once the dashboard closes.
    db.set_monitor_user(u, password=sess.password, cookies=sess.cookies,
                        display_name=sess.display_name or None)
    mon = db.create_monitor(u, {
        "course_title": data["course_title"],
        "course_code": data.get("course_code"),
        "section_label": str(data.get("section_label") or "*").strip(),
        "mode": mode,
        "threshold": threshold,
        "days": days,
        "time_start": time_start,
        "time_end": time_end,
    })
    if mode == "new_section":
        win = (f"{'/'.join(d[:3] for d in days) if days else 'any day'} "
               f"{time_start or 'start'}–{time_end or 'end'}")
        desc = f"new section · {win}"
    else:
        desc = "below " + str(threshold) if mode == "threshold" else "any change"
    db.log_event(u, "info",
                 f"🪑 Monitor created: {mon['course_title']} "
                 f"[{mon['section_label']}] ({desc})")
    return mon


@app.patch("/api/monitors/{monitor_id}", dependencies=[Depends(require_auth)])
async def patch_monitor(monitor_id: int, data: dict = Body(...), c: Caller = Depends(caller)):
    u = _ctx(c).username
    # Capture the prior active state so we can Discord-alert on a genuine toggle.
    prev = db.get_monitor(monitor_id, user=u)
    m = db.update_monitor(monitor_id, data, user=u)
    if not m:
        raise HTTPException(404, "not found")
    if "active" in data and prev is not None and bool(prev["active"]) != bool(m["active"]):
        verb = "turned ON" if m["active"] else "turned OFF"
        msg = (f"🪑 Monitor {verb}: {m['course_title']} [{m['section_label']}]")
        tag = (c.ctx.session.display_name
               or (db.get_monitor_user(u) or {}).get("display_name") or u)
        await discord_send(f"[{tag}] {msg}")
        db.log_event(u, "info", msg)
    return m


@app.delete("/api/monitors/{monitor_id}", dependencies=[Depends(require_auth)])
async def remove_monitor(monitor_id: int, c: Caller = Depends(caller)):
    u = _ctx(c).username
    db.delete_monitor(monitor_id, user=u)
    # When the last monitor goes, forget the durable monitor credentials too.
    if not db.list_monitors(u):
        db.delete_monitor_user(u)
    return {"ok": True}


@app.get("/api/events", dependencies=[Depends(require_auth)])
async def events(limit: int = 100, c: Caller = Depends(caller)):
    if not c.ctx:
        return {"events": []}
    return {"events": db.recent_events(c.ctx.username, limit)}


# ─── dashboard ────────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
