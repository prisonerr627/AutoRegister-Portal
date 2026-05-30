"""FastAPI app: multi-user dashboard + control API.

Each browser is bound to an AIUB user via an httponly `sid` cookie; every request
resolves that user's isolated context (own portal session, cookies, alerts, toggles,
activity log, and background poller). Pollers for users with stored credentials are
resumed on startup.
"""
from __future__ import annotations

import asyncio
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
from .poller import _registered_sections
from .portal import LoginError, NeedCaptcha
from .schedule import WEEKDAYS, find_clashes, make_slot, parse_time_to_minutes
from .users import UserContext, manager

STATIC_DIR = Path(__file__).resolve().parent / "static"
catalog: Catalog | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global catalog
    log.info("lifespan: startup begin")
    try:
        catalog = Catalog.load(config.CATALOG_XLSX)
        log.info("loaded catalog: %d courses from %s", len(catalog.titles()), config.CATALOG_XLSX)
    except Exception as e:  # noqa: BLE001
        catalog = Catalog({})
        log.exception("catalog load failed: %s", e)
    await manager.resume_all()
    reaper = asyncio.create_task(_idle_reaper())
    log.info("lifespan: startup complete (idle reaper started)")
    yield
    log.info("lifespan: shutdown begin")
    reaper.cancel()
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
        return {"ok": True, "display_name": ctx.session.display_name}
    except NeedCaptcha as e:
        db.set_meta(u, "needs_captcha", True)
        return JSONResponse({"ok": False, "needs_captcha": True, "captcha_image": e.image_b64}, status_code=200)
    except LoginError as e:
        db.set_meta(u, "login_error", str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


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
    start_str = ts if parse_time_to_minutes(ts) is not None else "12:00 AM"
    end_str = te if parse_time_to_minutes(te) is not None else "11:59 PM"
    window_slots = [s for d in (days or WEEKDAYS) if (s := make_slot(d, start_str, end_str))]

    clashing = find_clashes(window_slots, registered)
    clash_titles = sorted({x["title"] for x in clashing})
    return {
        "mode": "window",
        "window": {"days": days, "time_start": ts, "time_end": te},
        "clashes": [{"title": x["title"], "section": x["section"]} for x in clashing],
        "clash_course_titles": clash_titles,
        "source": source,
        "registered_considered": [{"title": x["title"], "section": x["section"]} for x in registered],
    }


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
