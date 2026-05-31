"""Read-only check of the REAL engaged sequence the poller runs:
auto_login -> start_status -> select2 -> wait(SELECT2_WAIT) -> get_prereg2 -> load_sections.

Does NOT register/unregister — purely observes. Uses the per-user PortalSession
(the old module-level `portal.session` singleton was removed in the multi-user
refactor). Credentials come from the env, like probe_timer.py.

Usage:
    PROBE_PASSWORD='...' python verify2.py
Env knobs: PROBE_USER (default 25-62595-2).
"""
import asyncio
import os
import time

from app import config
from app.portal import PortalSession
from app.schedule import routine_summary

USER = os.environ.get("PROBE_USER", "25-62595-2").strip()
PASSWORD = os.environ.get("PROBE_PASSWORD", "").strip()


def fp(s: PortalSession) -> str:
    v = s.cookies.get("NAABSUMSMVCFORMSAUTH", "")
    return (v[:12] + "…") if v else "(none)"


async def main() -> None:
    s = PortalSession(USER)
    if PASSWORD:
        s.set_credentials(USER, PASSWORD)
    t0 = time.time()

    # Reuse saved cookies if this user already has a valid jar; else log in.
    if not s.cookies.get("NAABSUMSMVCFORMSAUTH"):
        if not s.has_credentials():
            print("no saved cookie and no PROBE_PASSWORD set — cannot log in"); return
        print("no saved cookie -> logging in")
        await s.auto_login()
    print(f"[{time.time()-t0:5.1f}s] auth cookie: {fp(s)}  user={s.display_name or USER}")

    st = await s.start_status()
    not_allowed = not st["open"]
    print(f"[{time.time()-t0:5.1f}s] [Start] open={st['open']} not_allowed_today={not_allowed} "
          f"student={st.get('student', {}).get('Name', '?')}")

    sel = await s.select2()
    # Select2 302-bounces to Start while the window is closed (no confirm token); that
    # still starts the server-side timer, so GetPreReg2 works ~SELECT2_WAIT later.
    print(f"[{time.time()-t0:5.1f}s] [Select2] ok={sel['ok']} confirm_q={bool(sel.get('q'))}")

    wait = config.SELECT2_WAIT_SECONDS
    print(f"[{time.time()-t0:5.1f}s] waiting {wait}s for the server timer (cookie {fp(s)})…")
    await asyncio.sleep(wait)

    pre = await s.get_prereg2()
    if pre.get("HasError"):
        print(f"[{time.time()-t0:5.1f}s] [GetPreReg2] HasError: {pre.get('Message')}"); return
    courses = pre.get("RegisterableCourses", [])
    print(f"[{time.time()-t0:5.1f}s] [GetPreReg2] semester={(pre.get('Semester') or {}).get('Title')} "
          f"registerable_courses={len(courses)}")
    for cc in courses:
        print(f"   - {cc['Title']} [{cc.get('Status')}] offered={cc.get('OfferedCourseId')} "
              f"secs={len(cc.get('RegisterableSections', []))}")

    # Read-only LoadSections on a course whose sections aren't inlined yet.
    target = next((x for x in courses if not x.get("RegisterableSections")), courses[0] if courses else None)
    if target:
        print(f"\n[{time.time()-t0:5.1f}s] [LoadSections] {target['Title']} (read-only)")
        loaded = await s.load_sections(target)
        secs = (loaded.get("Data") or {}).get("RegisterableSections") or []
        for sec in secs[:6]:
            print(f"   [{sec.get('Title')}] {sec.get('StudentCount')}/{sec.get('Capacity')} "
                  f":: {routine_summary(sec.get('Routine', ''))}")
        if not secs:
            print("   (no sections published yet)")

    print(f"\ncookie after sequence: {fp(s)}\nDONE (read-only — no register/unregister).")


if __name__ == "__main__":
    asyncio.run(main())
