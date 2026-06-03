"""Reverse-engineer the SEARCH on /Student/Section/Offered?q=<token> so we can read
live seat counts (filled/capacity) per section WITHOUT entering the registration
workspace (no Select2 2-min timer). Read-only spike for the seat-Monitors feature.

It logs in, scrapes the dashboard for the Offered page's `q` token, GETs the Offered
page, dumps the search form + every script/ajax/table-looking endpoint, then tries a
few ways to actually run a search for a known course and dumps the response so we can
see if seat numbers are present and how a section row is shaped.

Usage:  PROBE_PASSWORD='...' [PROBE_COURSE='DATABASE'] .venv/bin/python probe_offered_search.py
"""
import asyncio
import json
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup

from app import config
from app.portal import PortalSession, PORTAL

USER = os.environ.get("PROBE_USER", config.PORTAL_USERNAME or "25-62595-2").strip()
PASSWORD = os.environ.get("PROBE_PASSWORD", config.PORTAL_PASSWORD or "").strip()
# Term to type into the search box. A short, common substring is fine.
COURSE = os.environ.get("PROBE_COURSE", "").strip()
OUT = Path("/tmp/probe_offered_search")
OUT.mkdir(parents=True, exist_ok=True)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def dump(name: str, text: str) -> Path:
    p = OUT / name
    p.write_text(text, encoding="utf-8", errors="replace")
    log(f"   saved {p} ({len(text)} bytes)")
    return p


def show_forms(html: str) -> list[dict]:
    """Print and return every <form> with its inputs/selects (name/type/value)."""
    soup = BeautifulSoup(html, "lxml")
    forms = []
    for f in soup.find_all("form"):
        fields = []
        for i in f.find_all(("input", "select", "textarea")):
            opts = [o.get("value") for o in i.find_all("option")] if i.name == "select" else None
            fields.append({
                "tag": i.name, "name": i.get("name"), "id": i.get("id"),
                "type": i.get("type"), "value": (i.get("value") or "")[:60],
                "options": opts[:8] if opts else None,
            })
        info = {"action": f.get("action"), "method": (f.get("method") or "GET").upper(),
                "id": f.get("id"), "fields": fields}
        forms.append(info)
        log(f" FORM action={info['action']!r} method={info['method']} id={info['id']!r}")
        for fl in fields:
            log(f"    {fl['tag']} name={fl['name']!r} id={fl['id']!r} type={fl['type']!r} "
                f"value={fl['value']!r}" + (f" options={fl['options']}" if fl['options'] else ""))
    return forms


def show_ajax_endpoints(html: str) -> list[str]:
    """Pull URL-ish strings out of inline JS that look like data/search/section calls."""
    hits = set()
    for m in re.finditer(r"""["'](/[A-Za-z][\w/]*(?:Section|Offered|Search|Get\w+|List|Data)[\w/]*)["']""", html):
        hits.add(m.group(1))
    # url: "...", ajax: { url: "..." }, $.get("..."), $.post("...")
    for m in re.finditer(r"""(?:url|action)\s*:\s*["']([^"']+)["']""", html, re.I):
        hits.add(m.group(1))
    for m in re.finditer(r"""\$\.(?:get|post|ajax)\s*\(\s*["']([^"']+)["']""", html, re.I):
        hits.add(m.group(1))
    out = sorted(hits)
    log("\n=== candidate ajax/data endpoints in page JS ===")
    for u in out:
        log(f"   {u}")
    return out


def seat_words(text: str) -> list[str]:
    """Lines mentioning seat-ish vocabulary, to eyeball where counts live."""
    pat = re.compile(r"seat|capacity|booked|enrol|filled|available|vacan|count|/\s*\d+", re.I)
    return [ln.strip() for ln in text.splitlines() if pat.search(ln) and ln.strip()][:60]


async def get(s: PortalSession, path: str, referer: str, **kw):
    return await s._request("GET", path, referer=referer, **kw)


async def main() -> None:
    if not PASSWORD:
        log("set PROBE_PASSWORD (or PORTAL_PASSWORD in .env)"); return
    s = PortalSession(USER)
    s.set_credentials(USER, PASSWORD)
    log(f"logging in as {USER} ...")
    await s.auto_login()
    log(f"login OK as {s.display_name or USER}; cookies: {list(s.cookies.keys())}\n")

    # 1) Find the Offered page q token off the dashboard.
    home = await get(s, "/Student", referer=f"{PORTAL}/")
    dump("01_student.html", home.text)
    m = re.search(r"/Student/Section/Offered\?q=([^\"'<>\s]+)", home.text)
    if not m:
        log("!! no /Student/Section/Offered?q= link on /Student — dumping and bailing")
        return
    q = m.group(1)
    offered_path = f"/Student/Section/Offered?q={q}"
    log(f">> q token: {q}\n>> GET {offered_path}")

    offered = await get(s, offered_path, referer=f"{PORTAL}/Student")
    log(f"   status={offered.status_code} len={len(offered.text)}")
    dump("02_offered.html", offered.text)

    # 2) Inspect the page: search form + ajax endpoints + any seats already in HTML.
    log("\n=== FORMS on the Offered page ===")
    forms = show_forms(offered.text)
    endpoints = show_ajax_endpoints(offered.text)
    log("\n=== seat-ish lines already present in the Offered HTML ===")
    for ln in seat_words(offered.text)[:25]:
        log(f"   {ln[:160]}")

    # 3) Try to actually run a search. We attempt, in order:
    #    (a) submit each form with the course term filled into its text input(s);
    #    (b) hit each candidate ajax endpoint with common param names.
    term = COURSE or "A"  # 'A' is a broad hit if no course supplied
    log(f"\n=== attempting searches for term={term!r} ===")

    async def try_request(method, url, *, params=None, data=None, label=""):
        try:
            if method == "GET":
                r = await s._request("GET", url, params=params, referer=f"{PORTAL}{offered_path}")
            else:
                # form-encoded POST
                r = await s._request("POST", url, json_body=None, referer=f"{PORTAL}{offered_path}",
                                     expect_json=False)
            ct = r.headers.get("content-type", "")
            body = r.text
            looks_json = body.lstrip()[:1] in ("{", "[")
            has_seats = bool(re.search(r"seat|capacity|booked|enrol|filled|available|count", body, re.I))
            log(f" [{label}] {method} {url} -> {r.status_code} ct={ct!r} "
                f"len={len(body)} json={looks_json} seats={has_seats}")
            fn = re.sub(r"[^\w]+", "_", label or url)[:50] + (".json" if looks_json else ".html")
            dump(fn, body)
            return r, has_seats
        except Exception as e:
            log(f" [{label}] {method} {url} -> ERROR {type(e).__name__}: {e}")
            return None, False

    # (a) Forms — build a GET querystring / POST body with the term in each text field.
    for i, f in enumerate(forms):
        action = f["action"] or offered_path
        if not action.startswith("http") and not action.startswith("/"):
            action = "/" + action
        params = {}
        for fl in f["fields"]:
            if not fl["name"]:
                continue
            if fl["type"] in (None, "text", "search") or fl["tag"] in ("select", "textarea"):
                params[fl["name"]] = term
            elif fl["value"]:
                params[fl["name"]] = fl["value"]
        await try_request("GET", action.split("?")[0], params=params or None, label=f"form{i}")

    # (b) Candidate ajax endpoints with a grab-bag of param names.
    common = {"q": term, "term": term, "search": term, "searchText": term,
              "courseTitle": term, "title": term, "code": term, "Length": "6"}
    for ep in endpoints:
        if any(k in ep.lower() for k in ("section", "offered", "search", "data", "get", "list")):
            await try_request("GET", ep.split("?")[0], params=common, label=f"ep_{ep}")

    log(f"\nArtifacts in {OUT}/ — open 02_offered.html and any *.json to see the seat shape.")


if __name__ == "__main__":
    asyncio.run(main())
