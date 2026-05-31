"""AIUB portal client: authenticated session + registration API wrappers.

Login flow and cookie-rotation handling are ported from the campusbuddies backend.
The portal rotates the `NAABSUMSMVCFORMSAUTH` auth cookie roughly every 5 minutes
via Set-Cookie; we merge `resp.cookies` after every request and persist the jar so
the session stays alive (and survives restarts). On detecting a logged-out response
we transparently re-login and retry once.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup

from . import config, db
from .applog import log, traced
from .captcha import solve_captcha
from .schedule import Slot, make_slot


PORTAL = config.PORTAL_BASE
_NOT_ALLOWED = "You are not allowed to do registration Today"
_CONFIRM_RE = re.compile(r"/Student/Registration/Confirm\?q=([^\"'&\s]+)")
# A course panel on /Student/Home reads "02178 - INTRODUCTION TO ... LAB [K]".
_HOME_COURSE_RE = re.compile(r"^\s*(\d+)\s*-\s*(.+?)\s*\[([^\]]+)\]\s*$")
# /Student/Registration rows carry the real class times (available WITHOUT the
# registration window): an <a>"00659-INTRODUCTION TO DATABASE [D]" plus one or more
# "(Theory) Time: Mon 3:0 PM - Mon 5:0 PM Room: 9211" spans. Minutes may be a single
# digit (3:0) and the meridian is sometimes omitted (Sun 8:0 - Sun 9:30).
_REG_COURSE_RE = re.compile(r"^\s*(\d+)\s*-\s*(.+?)\s*\[([^\]]+)\]\s*$")
# Bare /Student/Registration 302s to /Login regardless of session; the registration
# *summary* (with real class times) is only reachable via /Student/Registration?q=
# <token>, which the /Student dashboard always exposes (window open or closed).
_REG_Q_RE = re.compile(r"/Student/Registration\?q=[^\"'<>\s]+")
# The "Offered Course Report" xlsx is downloadable live (so the catalog never goes
# stale per semester). The Download button on /Student/Section/Offered?q=<token>
# points at /Common/Section/DownloadOfferedReport (no q token needed; the portal
# generates the xlsx server-side, ~30s). We hit that path directly, scraping the
# real href off the Offered page only as a fallback.
_OFFERED_Q_RE = re.compile(r"/Student/Section/Offered\?q=[^\"'<>\s]+")
_DOWNLOAD_REPORT_RE = re.compile(r"/Common/Section/DownloadOfferedReport[^\"'<>\s]*")
_REG_TIME_RE = re.compile(
    r"\((?P<type>[^)]*)\)\s*Time:\s*"
    r"(?P<d1>[A-Za-z]{3,})\s+(?P<t1>\d{1,2}:\d{1,2})\s*(?P<ap1>AM|PM)?\s*-\s*"
    r"(?P<d2>[A-Za-z]{3,})\s+(?P<t2>\d{1,2}:\d{1,2})\s*(?P<ap2>AM|PM)?"
    r"(?:\s*Room:\s*(?P<room>\S+))?",
    re.IGNORECASE,
)


def _norm_clock(t: str, ap: Optional[str]) -> str:
    """'3:0' + 'PM' -> '3:00 PM'. A missing meridian is inferred from the academic
    day: 8:00-11:59 reads as AM, everything else (12, 1-7) as PM."""
    h_str, _, m_str = t.partition(":")
    h, m = int(h_str), int(m_str or 0)
    ap = (ap or "").upper()
    if not ap:
        ap = "AM" if 8 <= h <= 11 else "PM"
    return f"{h}:{m:02d} {ap}"


class LoginError(Exception):
    pass


class NeedCaptcha(Exception):
    """Auto-solve failed; a human must supply the captcha answer."""

    def __init__(self, image_b64: str, log: str = ""):
        super().__init__("captcha required")
        self.image_b64 = image_b64
        self.log = log


def _parse_login_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    token = soup.find("input", {"name": "__RequestVerificationToken"})
    captcha = soup.find("input", {"id": "CaptchaDeText"})
    errors = [e.get_text(strip=True) for e in soup.select("small.text-danger") if e.get_text(strip=True)]
    return {
        "verification_token": token["value"] if token else None,
        "captcha_de_text": captcha["value"] if captcha else None,
        "errors": errors,
    }


def _scrape_student(html: str) -> dict:
    """Pull the Student ID / Name / CGPA / credits table on the Start page."""
    soup = BeautifulSoup(html, "lxml")
    info: dict[str, str] = {}
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        cells = [td.get_text(strip=True) for td in tds]
        # rows look like [label, value] (the first row also has a rowspan image cell)
        if len(cells) >= 2:
            label, value = cells[-2], cells[-1]
            if label and value and label.lower() in (
                "student id", "name", "cgpa", "credit completed"
            ):
                info[label] = value
    name = ""
    for p in soup.select("p.navbar-text"):
        small = p.select_one("small")
        if "Welcome" in p.get_text() and small:
            name = small.get_text(strip=True)
    if name:
        info.setdefault("Name", name)
    return info


class PortalSession:
    def __init__(self, username: str):
        # The AIUB username is this session's user key (one PortalSession per user).
        self.user = (username or "").strip()
        self.username = self.user
        self.password = ""
        self.cookies: dict[str, str] = {}
        self.display_name = ""
        self.logged_in = False
        self.login_count = 0  # bumped on each successful login; lets the poller
        self._lock = asyncio.Lock()  # detect a re-login and redo Select2.
        self._login_tokens: dict = {}
        self._login_cookies: dict = {}
        self._captcha_bytes: Optional[bytes] = None
        self._load_credentials()
        self._load_cookies()

    # ─── credentials ─────────────────────────────────────────────────────
    def _load_credentials(self) -> None:
        u = db.get_user(self.user)
        if u and u.get("password"):
            self.password = u["password"]

    def set_credentials(self, username: str, password: str) -> None:
        # username is fixed to this context's user key; only the password varies.
        self.password = password or ""
        db.set_user(self.user, self.password)

    def clear_credentials(self) -> None:
        self.password = ""
        self.logged_in = False
        self.display_name = ""
        self.cookies = {}
        self._save_cookies()
        db.delete_user(self.user)

    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    # ─── cookie persistence (per user, in DB meta) ───────────────────────
    def _load_cookies(self) -> None:
        self.cookies = db.get_meta(self.user, "cookies", {}) or {}
        self.logged_in = bool(self.cookies.get("NAABSUMSMVCFORMSAUTH"))

    def _save_cookies(self) -> None:
        db.set_meta(self.user, "cookies", self.cookies)

    def _merge(self, resp: httpx.Response) -> None:
        new = dict(resp.cookies)
        if new:
            old_auth = self.cookies.get("NAABSUMSMVCFORMSAUTH")
            self.cookies.update(new)
            self._save_cookies()
            # The portal re-issues NAABSUMSMVCFORMSAUTH ~every 5 min to keep the
            # session alive; surface that in the activity log so it's visible.
            fresh = new.get("NAABSUMSMVCFORMSAUTH")
            if fresh and fresh != old_auth:
                db.log_event(self.user, "info", "🔄 Session cookie refreshed (auth cookie rotated — session kept alive)")

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        kwargs = dict(
            follow_redirects=False,
            timeout=timeout,
            verify=config.VERIFY_TLS,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if config.PROXY_URL:
            # Route portal traffic through Burp / an intercepting proxy.
            kwargs["proxy"] = config.PROXY_URL
        return httpx.AsyncClient(**kwargs)

    # ─── login ───────────────────────────────────────────────────────────
    async def prepare_login(self) -> Optional[str]:
        """GET the login page; if it serves a captcha, fetch the image. Returns the
        captcha image as base64 (or None when no captcha is required)."""
        async with self._client() as client:
            resp = await client.get(f"{PORTAL}/")
            self._login_cookies = dict(resp.cookies)
            self._login_tokens = _parse_login_page(resp.text)
            self._captcha_bytes = None
            guid = self._login_tokens.get("captcha_de_text")
            if not guid:
                return None
            cap = await client.get(
                f"{PORTAL}/DefaultCaptcha/Generate",
                params={"t": guid},
                cookies=self._login_cookies,
                headers={"Referer": f"{PORTAL}/"},
            )
            self._login_cookies.update(dict(cap.cookies))
            if cap.status_code == 200:
                self._captcha_bytes = cap.content
                import base64

                return base64.b64encode(cap.content).decode()
        return None

    @traced
    async def submit_login(self, captcha_answer: str = "") -> bool:
        """Submit credentials with the given captcha answer using the tokens/cookies
        captured by prepare_login()."""
        tokens = self._login_tokens
        if not tokens.get("verification_token"):
            await self.prepare_login()
            tokens = self._login_tokens
        async with self._client() as client:
            resp = await client.post(
                f"{PORTAL}/",
                data={
                    "__RequestVerificationToken": tokens.get("verification_token"),
                    "UserName": self.username,
                    "Password": self.password,
                    "fingerPrint": "-",
                    "CaptchaDeText": tokens.get("captcha_de_text", ""),
                    "CaptchaInputText": captcha_answer or "",
                },
                cookies=self._login_cookies,
                headers={
                    "Referer": f"{PORTAL}/",
                    "Origin": PORTAL,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            self._login_cookies.update(dict(resp.cookies))
            if resp.status_code not in (301, 302):
                errors = _parse_login_page(resp.text).get("errors", [])
                raise LoginError("; ".join(errors) or "login failed (no redirect)")
            # Follow the post-login redirect chain to settle cookies.
            loc = resp.headers.get("location", "")
            html = ""
            hops = 0
            while loc and hops < 5:
                if not loc.startswith("http"):
                    loc = f"{PORTAL}{loc}"
                r = await client.get(loc, cookies=self._login_cookies, headers={"Referer": f"{PORTAL}/"})
                self._login_cookies.update(dict(r.cookies))
                html = r.text
                if r.status_code not in (301, 302):
                    break
                loc = r.headers.get("location", "")
                hops += 1
        # Promote the freshly-authenticated cookies to the live jar.
        self.cookies = dict(self._login_cookies)
        self._save_cookies()
        self.logged_in = bool(self.cookies.get("NAABSUMSMVCFORMSAUTH"))
        if html:
            name = _scrape_student(html).get("Name", "")
            if name:
                self.display_name = name
        if not self.logged_in:
            raise LoginError("login did not yield an auth cookie")
        self.login_count += 1
        return True

    @traced
    async def auto_login(self) -> bool:
        """Full non-interactive login. Raises NeedCaptcha if auto-solve fails."""
        if not self.has_credentials():
            raise LoginError("no credentials — log in via the dashboard")
        captcha_b64 = await self.prepare_login()
        answer = ""
        if self._captcha_bytes:
            answer, log = await solve_captcha(self._captcha_bytes)
            if not answer:
                raise NeedCaptcha(captcha_b64 or "", log)
        return await self.submit_login(answer)

    # ─── request helpers ─────────────────────────────────────────────────
    @staticmethod
    def _is_login_page(html: str) -> bool:
        """True only for the actual AIUB login page. Keyed on the password field
        (and captcha), NOT on __RequestVerificationToken: that anti-forgery token
        rides on every logged-in form page — including the /Student/Registration
        summary — so requiring it previously false-tripped a 'session expired' on
        every clash check, forcing a needless re-login that kicked the user's
        browser (single-session). A content page never carries a Password input."""
        return 'name="UserName"' in html and (
            'name="Password"' in html or 'id="CaptchaDeText"' in html
        )

    @staticmethod
    def _logged_out(resp: httpx.Response, expect_json: bool) -> bool:
        loc = resp.headers.get("location", "")
        if resp.status_code in (301, 302):
            # Login redirect, or the single-session kick ("logged into another
            # browser") which bounces to '/?message=...'. A normal Select2->Start
            # bounce (loc='/Student/Registration/Start') is NOT a logout.
            if "/Login" in loc or loc in ("/", f"{PORTAL}/") or loc.startswith("/?") or "message=" in loc:
                return True
        if expect_json:
            ct = resp.headers.get("content-type", "")
            body = resp.text.lstrip()
            if "html" in ct or body.startswith("<"):
                return True
        else:
            # An HTML page that IS the login form means our session expired.
            if PortalSession._is_login_page(resp.text):
                return True
        return False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        expect_json: bool = False,
        referer: Optional[str] = None,
        retry_login: bool = True,
        timeout: float = 30.0,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{PORTAL}{path}"
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        if expect_json:
            headers["Accept"] = "application/json, text/plain, */*"
            headers["X-Requested-With"] = "XMLHttpRequest"
        if json_body is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
            headers["Origin"] = PORTAL

        t0 = time.time()
        async with self._client(timeout) as client:
            resp = await client.request(
                method, url, params=params, json=json_body,
                cookies=self.cookies, headers=headers,
            )
        self._merge(resp)
        loc = resp.headers.get("location", "")
        log.debug("portal[%s] %s %s -> %s%s [%.0fms]", self.user, method, path,
                  resp.status_code, f" loc={loc}" if loc else "", (time.time() - t0) * 1000)

        if retry_login and self._logged_out(resp, expect_json):
            self.logged_in = False
            log.warning("portal[%s] logged-out detected on %s %s (status=%s loc=%s) — re-logging in",
                        self.user, method, path, resp.status_code, loc or "-")
            db.log_event(self.user, "warn", f"⚠️ Session expired/kicked on {method} {path} — re-logging in")
            await self.auto_login()  # may raise NeedCaptcha / LoginError
            db.log_event(self.user, "info", f"✅ Re-logged in as {self.display_name or self.username}; retrying request")
            log.info("portal[%s] re-login OK, retrying %s %s", self.user, method, path)
            return await self._request(
                method, path, params=params, json_body=json_body,
                expect_json=expect_json, referer=referer, retry_login=False,
                timeout=timeout,
            )
        return resp

    async def _get_json(self, path: str, **kw) -> Any:
        resp = await self._request("GET", path, expect_json=True,
                                   referer=f"{PORTAL}/Student/Registration/Select2", **kw)
        return resp.json()

    async def _post_json(self, path: str, body: dict) -> Any:
        resp = await self._request(
            "POST", path, json_body=body, expect_json=True,
            referer=f"{PORTAL}/Student/Registration/Select2",
        )
        return resp.json()

    # ─── registration API ────────────────────────────────────────────────
    async def start_status(self) -> dict:
        """Poll the Start page. Returns {open: bool, student: {...}}."""
        async with self._lock:
            resp = await self._request("GET", "/Student/Registration/Start", referer=f"{PORTAL}/Student")
        html = resp.text
        # The reliable signal is the absence of the "not allowed today" alert; the
        # Confirmation() JS is present in the page even when registration is closed.
        is_open = _NOT_ALLOWED not in html and not self._is_login_page(html)
        return {"open": is_open, "student": _scrape_student(html)}

    async def select2(self) -> dict:
        """Enter the registration workspace; scrape the Confirm `q` token."""
        async with self._lock:
            resp = await self._request("GET", "/Student/Registration/Select2", referer=f"{PORTAL}/Student/Registration/Start")
        m = _CONFIRM_RE.search(resp.text)
        return {"q": m.group(1) if m else None, "ok": resp.status_code == 200}

    async def get_prereg2(self) -> dict:
        async with self._lock:
            return await self._get_json("/Student/Registration/GetPreReg2")

    async def load_sections(self, course: dict) -> dict:
        async with self._lock:
            return await self._post_json("/Student/Registration/LoadSections", {"course": course})

    @traced
    async def register_section(self, section: dict) -> dict:
        log.warning("portal[%s] register_section section=%s (LIVE)", self.user, section.get("ID"))
        async with self._lock:
            return await self._post_json("/Student/Registration/RegisterSection", {"section": section})

    @traced
    async def unregister_section(self, section_id: int) -> dict:
        log.warning("portal[%s] unregister_section id=%s (LIVE)", self.user, section_id)
        async with self._lock:
            return await self._post_json("/Student/Registration/UnRegisterSection", {"sectionID": section_id})

    @staticmethod
    def _parse_course_list(html: str) -> list[dict]:
        """Parse the registered-course panels out of a Student Home / CourseList page."""
        soup = BeautifulSoup(html, "lxml")
        out: list[dict] = []
        for panel in soup.select(".StudentCourseList .course-list-panel"):
            first_line = panel.get_text("\n", strip=True).split("\n")[0]
            m = _HOME_COURSE_RE.match(first_line)
            if not m:
                continue
            labels = [l.get_text(strip=True) for l in panel.select("label.label")]
            out.append({
                "code": m.group(1),
                "title": m.group(2).strip(),
                "section": m.group(3).strip(),
                "section_status": labels[0] if labels else "",
                "status": labels[1] if len(labels) > 1 else "",
            })
        return out

    async def registered_courses(self, semester_title: Optional[str] = None) -> dict:
        """Scrape /Student/Home's Registration panel for the courses+sections the
        student is registered in for a given semester. Works regardless of the
        registration window (no GetPreReg2 needed), so it's usable for clash checks
        while registration is closed. Picks the matching semester from the dropdown
        (by title), else the latest (last) option."""
        async with self._lock:
            resp = await self._request("GET", "/Student/Home", referer=f"{PORTAL}/Student")
        soup = BeautifulSoup(resp.text, "lxml")
        options: list[dict] = []
        sel = soup.find("select", {"id": "SemesterDropDown"})
        if sel:
            for opt in sel.find_all("option"):
                options.append({
                    "title": opt.get_text(strip=True),
                    "url": opt.get("value", ""),
                    "selected": opt.has_attr("selected"),
                })
        target = None
        if semester_title:
            want = semester_title.strip().lower()
            target = next((o for o in options if want in o["title"].lower() or o["title"].lower() in want), None)
        if target is None and options:
            target = options[-1]  # latest semester is last in the dropdown
        # The Home page already renders the *selected* semester's list; fetch the
        # CourseList partial only when we need a different semester.
        list_html = resp.text
        if target and target.get("url") and not target.get("selected"):
            url = target["url"] if target["url"].startswith("http") else f"{PORTAL}{target['url']}"
            async with self._lock:
                r2 = await self._request("GET", url, referer=f"{PORTAL}/Student/Home")
            list_html = r2.text
        return {
            "semester": (target or {}).get("title"),
            "options": [o["title"] for o in options],
            "courses": self._parse_course_list(list_html),
        }

    @staticmethod
    def _parse_registration_schedule(html: str) -> list[dict]:
        """Parse the registered courses + their real class times out of a
        /Student/Registration?q= summary page. Returns
        [{code,title,section,slots:[Slot,...]}]. Each course is an
        <a>"01530-COMPLEX VARIABLE... [A]"</a> whose enclosing cell/row also holds the
        "(Theory) Time: Sun 11:20 - Sun 12:50 PM Room: ..." spans — so we key on the
        course anchor itself, not any particular table CSS class."""
        soup = BeautifulSoup(html, "lxml")
        out: list[dict] = []
        for a in soup.find_all("a"):
            m = _REG_COURSE_RE.match(a.get_text(strip=True))
            if not m:
                continue
            cell = a.find_parent("td") or a.find_parent("tr") or a.parent
            text = cell.get_text(" ", strip=True)
            slots: list[Slot] = []
            for tm in _REG_TIME_RE.finditer(text):
                s = make_slot(
                    tm.group("d1"),
                    _norm_clock(tm.group("t1"), tm.group("ap1")),
                    _norm_clock(tm.group("t2"), tm.group("ap2")),
                    tm.group("type") or "",
                    tm.group("room") or "",
                )
                if s:
                    slots.append(s)
            out.append({
                "code": m.group(1), "title": m.group(2).strip(),
                "section": m.group(3).strip(), "slots": slots,
            })
        return out

    async def registered_schedule(self, semester_title: Optional[str] = None) -> dict:
        """Scrape the registration summary for the registered courses WITH real class
        times for a given semester. Works regardless of the registration window (no
        GetPreReg2/Select2), so clash checks need neither Force-flow nor the catalog.
        Targets the matching semester from the dropdown (by title), else the latest
        (last) option — which is what the portal defaults away from.

        Bare GET /Student/Registration always 302s to /Login (it has no `q`); the page
        is only reachable via /Student/Registration?q=<token>. The /Student dashboard
        always carries such a link, so we bootstrap from there, then follow the
        SemesterDropDown (whose option values are themselves /Student/Registration?q=)."""
        async with self._lock:
            home = await self._request("GET", "/Student", referer=f"{PORTAL}/")
        m = _REG_Q_RE.search(home.text)
        if not m:
            return {"semester": None, "options": [], "courses": []}
        async with self._lock:
            resp = await self._request("GET", m.group(0), referer=f"{PORTAL}/Student")
        soup = BeautifulSoup(resp.text, "lxml")
        options: list[dict] = []
        sel = soup.find("select", {"id": "SemesterDropDown"})
        if sel:
            for opt in sel.find_all("option"):
                options.append({
                    "title": opt.get_text(strip=True),
                    "url": opt.get("value", ""),
                    "selected": opt.has_attr("selected"),
                })
        target = None
        if semester_title:
            want = semester_title.strip().lower()
            target = next((o for o in options if want in o["title"].lower() or o["title"].lower() in want), None)
        if target is None and options:
            target = options[-1]  # latest semester is last in the dropdown
        list_html = resp.text
        if target and target.get("url") and not target.get("selected"):
            url = target["url"] if target["url"].startswith("http") else f"{PORTAL}{target['url']}"
            async with self._lock:
                r2 = await self._request("GET", url, referer=f"{PORTAL}/Student/Registration")
            list_html = r2.text
        return {
            "semester": (target or {}).get("title"),
            "options": [o["title"] for o in options],
            "courses": self._parse_registration_schedule(list_html),
        }

    async def confirm(self, q: str) -> dict:
        async with self._lock:
            resp = await self._request("GET", f"/Student/Registration/Confirm?q={q}",
                                       referer=f"{PORTAL}/Student/Registration/Select2")
        return {"status": resp.status_code, "text": resp.text[:500]}

    # ─── offered-course report (live catalog xlsx) ────────────────────────
    @staticmethod
    def _is_xlsx(body: bytes) -> bool:
        # .xlsx is a zip archive — magic bytes "PK\x03\x04".
        return body[:4] == b"PK\x03\x04"

    @traced
    async def download_offered_report(self) -> bytes:
        """Download the portal's live 'Offered Course Report.xlsx' and return the raw
        bytes (same format catalog.py already parses). Needs only a valid session — the
        Download link carries no q token. The portal builds the file on the fly (~30s),
        so this uses a long timeout. Fast path hits /Common/Section/DownloadOfferedReport
        directly; if that ever stops returning an xlsx we scrape the real Download href
        off the Offered page (which itself needs the dashboard's q token).

        ⚠️ The portal serializes ALL requests within a session server-side (ASP.NET
        session lock), so this ~30s call freezes the poller regardless of how we issue
        it — callers MUST gate it off the open window / force-flow (see maybe_refresh_
        catalog) rather than rely on a client-side trick."""
        async with self._lock:
            resp = await self._request(
                "GET", "/Common/Section/DownloadOfferedReport",
                referer=f"{PORTAL}/Student", timeout=180.0,
            )
        if resp.status_code == 200 and self._is_xlsx(resp.content):
            return resp.content

        # Fallback: discover the Offered page (q token lives on the dashboard), then
        # scrape its Download href and follow that.
        async with self._lock:
            home = await self._request("GET", "/Student", referer=f"{PORTAL}/")
        qm = _OFFERED_Q_RE.search(home.text)
        if not qm:
            raise LoginError("could not find the Offered-courses link on the dashboard")
        async with self._lock:
            page = await self._request("GET", qm.group(0), referer=f"{PORTAL}/Student")
        dm = _DOWNLOAD_REPORT_RE.search(page.text)
        if not dm:
            raise LoginError("could not find the Download link on the Offered-courses page")
        async with self._lock:
            resp = await self._request(
                "GET", dm.group(0), referer=f"{PORTAL}{qm.group(0)}", timeout=180.0,
            )
        if resp.status_code == 200 and self._is_xlsx(resp.content):
            return resp.content
        raise LoginError(
            f"Offered report download did not return an xlsx "
            f"(status={resp.status_code}, content-type={resp.headers.get('content-type','')})"
        )
