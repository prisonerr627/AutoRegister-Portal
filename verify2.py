"""Read-only test of the REAL sequence: Start -> Select2 -> wait ~2min -> GetPreReg2.
Does NOT register/unregister. Reuses the saved cookie jar (no re-login if valid)."""
import asyncio
import re
import httpx
from app import config, portal
from app.schedule import routine_summary

PORTAL = config.PORTAL_BASE
HDRS = {"User-Agent": config.USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
CONFIRM_RE = re.compile(r"/Student/Registration/Confirm\?q=([^\"'&\s]+)")


def fp():
    v = portal.session.cookies.get("NAABSUMSMVCFORMSAUTH", "")
    return (v[:12] + "…") if v else "(none)"


async def main():
    s = portal.session
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0, headers=HDRS) as c:
        # ensure session
        if not s.cookies.get("NAABSUMSMVCFORMSAUTH"):
            print("no saved cookie -> logging in")
            await s.auto_login()
        def merge(r):
            n = dict(r.cookies)
            if n: s.cookies.update(n); s._save_cookies()

        print("auth cookie:", fp())
        r = await c.get(f"{PORTAL}/Student/Registration/Start",
                        cookies=s.cookies, headers={"Referer": f"{PORTAL}/Student"})
        merge(r)
        html = r.text
        if 'name="UserName"' in html:
            print("session expired -> re-login"); await s.auto_login()
            r = await c.get(f"{PORTAL}/Student/Registration/Start", cookies=s.cookies); merge(r); html = r.text
        not_allowed = "You are not allowed to do registration Today" in html
        has_start = "Confirmation(" in html
        print(f"[Start] status={r.status_code} not_allowed_today={not_allowed} start_button={has_start}")

        r = await c.get(f"{PORTAL}/Student/Registration/Select2",
                        cookies=s.cookies, headers={"Referer": f"{PORTAL}/Student/Registration/Start"})
        merge(r)
        loc = r.headers.get("location", "")
        m = CONFIRM_RE.search(r.text)
        print(f"[Select2] status={r.status_code} len={len(r.text)} location={loc!r} confirm_q={bool(m)}")

        wait = config.SELECT2_WAIT_SECONDS
        print(f"\nwaiting {wait}s for the server timer (cookie now {fp()})…")
        await asyncio.sleep(wait)

        r = await c.get(f"{PORTAL}/Student/Registration/GetPreReg2",
                        cookies=s.cookies,
                        headers={"Referer": f"{PORTAL}/Student/Registration/Select2",
                                 "Accept": "application/json, text/plain, */*",
                                 "X-Requested-With": "XMLHttpRequest"})
        merge(r)
        print(f"[GetPreReg2] status={r.status_code} ct={r.headers.get('content-type','')}")
        try:
            j = r.json()
        except Exception:
            print("non-JSON body head:", r.text[:200]); return
        if j.get("HasError"):
            print("HasError:", j.get("Message")); return
        print("semester:", (j.get("Semester") or {}).get("Title"))
        courses = j.get("RegisterableCourses", [])
        print("registerable courses:", len(courses))
        for cc in courses:
            print(f"  - {cc['Title']} [{cc['Status']}] offered={cc.get('OfferedCourseId')} secs={len(cc.get('RegisterableSections',[]))}")
        # read-only LoadSections on a course with no sections shown
        target = next((x for x in courses if not x.get("RegisterableSections")), courses[0] if courses else None)
        if target:
            print(f"\n[LoadSections] {target['Title']} (read-only)")
            lr = await c.post(f"{PORTAL}/Student/Registration/LoadSections",
                              cookies=s.cookies,
                              headers={"Referer": f"{PORTAL}/Student/Registration/Select2",
                                       "Content-Type": "application/json;charset=UTF-8",
                                       "Origin": PORTAL, "Accept": "application/json, text/plain, */*"},
                              json={"course": target})
            merge(lr)
            data = (lr.json() or {}).get("Data") or {}
            for sec in (data.get("RegisterableSections") or [])[:6]:
                print(f"   [{sec['Title']}] {sec['StudentCount']}/{sec['Capacity']} :: {routine_summary(sec.get('Routine',''))}")
        print("\ncookie after sequence:", fp(), "\nDONE (read-only).")


asyncio.run(main())
