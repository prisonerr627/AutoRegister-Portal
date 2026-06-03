"""Investigate /Student/Section/Offered?q= — can the Offered Course Report xlsx be
downloaded programmatically (so we stop depending on the static catalog xlsx)?

Read-only: logs in, scrapes the dashboard for the Offered page's `q` token, GETs
the Offered page, and dumps every form / script / download-looking endpoint so we
can see how the in-page "download" button is wired. Saves artifacts to /tmp.

Usage:  PROBE_PASSWORD='...' .venv/bin/python probe_offered.py
"""
import asyncio
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup

from app import config
from app.portal import PortalSession, PORTAL

USER = os.environ.get("PROBE_USER", "25-62595-2").strip()
PASSWORD = os.environ.get("PROBE_PASSWORD", "").strip()
OUT = Path("/tmp/probe_offered")
OUT.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg, flush=True)


def dump(name: str, text: str) -> Path:
    p = OUT / name
    p.write_text(text, encoding="utf-8", errors="replace")
    log(f"   saved {p} ({len(text)} bytes)")
    return p


def find_links(html: str, needle: str) -> list[str]:
    """All hrefs / urls in the html that mention `needle` (case-insensitive)."""
    found = set()
    for m in re.finditer(r"""(?:href|src|action|url|value)\s*[=:]\s*["']?([^"'<>\s)]+)""", html, re.I):
        u = m.group(1)
        if needle.lower() in u.lower():
            found.add(u)
    # also bare occurrences in JS
    for m in re.finditer(r"""(/Student/[A-Za-z/]*%s[^"'<>\s)]*)""" % re.escape(needle), html, re.I):
        found.add(m.group(1))
    return sorted(found)


async def main() -> None:
    if not PASSWORD:
        log("set PROBE_PASSWORD"); return
    s = PortalSession(USER)
    s.set_credentials(USER, PASSWORD)
    log(f"logging in as {USER} ...")
    await s.auto_login()
    log(f"login OK as {s.display_name or USER}; cookies: {list(s.cookies.keys())}\n")

    # 1) Dashboard — hunt for any link to Section/Offered (to learn the q token).
    home = await s._request("GET", "/Student", referer=f"{PORTAL}/")
    dump("01_student.html", home.text)
    offered_links = find_links(home.text, "Section/Offered")
    section_links = find_links(home.text, "/Student/Section")
    log(f"[/Student] Section/Offered links: {offered_links}")
    log(f"[/Student] any /Student/Section links: {section_links[:10]}\n")

    # 2) Also check the Start page + the registration summary (other q sources).
    for path, ref in (("/Student/Registration/Start", f"{PORTAL}/Student"),):
        try:
            r = await s._request("GET", path, referer=ref)
            ol = find_links(r.text, "Section/Offered")
            if ol:
                log(f"[{path}] Section/Offered links: {ol}")
                offered_links += [x for x in ol if x not in offered_links]
        except Exception as e:
            log(f"[{path}] error: {e}")

    # 3) Try to reach the Offered page.
    q_token = None
    for u in offered_links:
        m = re.search(r"[?&]q=([^&\"'<>\s]+)", u)
        if m:
            q_token = m.group(1); break

    if not q_token:
        log("\n!! No q token for /Student/Section/Offered discovered on the dashboard.")
        log("   Trying a bare GET to see what the portal says (expected to 302 to /Login).")
        bare = await s._request("GET", "/Student/Section/Offered", referer=f"{PORTAL}/Student", retry_login=False)
        log(f"   bare GET status={bare.status_code} loc={bare.headers.get('location','')}")
        dump("02_offered_bare.html", bare.text)
        return

    log(f"\n>> q token found: q={q_token}")
    offered = await s._request("GET", f"/Student/Section/Offered?q={q_token}", referer=f"{PORTAL}/Student")
    log(f">> GET /Student/Section/Offered?q=... status={offered.status_code}")
    dump("03_offered.html", offered.text)

    # 4) Analyse the Offered page: forms, export/download endpoints, datatable config.
    soup = BeautifulSoup(offered.text, "lxml")
    log("\n=== FORMS ===")
    for f in soup.find_all("form"):
        inputs = [(i.get("name"), i.get("type"), i.get("value", "")[:40]) for i in f.find_all(("input", "select"))]
        log(f" form action={f.get('action')!r} method={f.get('method')!r} id={f.get('id')!r}")
        for nm, ty, val in inputs:
            log(f"    input name={nm!r} type={ty!r} value={val!r}")

    log("\n=== download/export/excel/xls/report endpoints in HTML+JS ===")
    for needle in ("download", "export", "excel", ".xls", "Report", "ExportTo", "GetExcel", "Generate"):
        hits = find_links(offered.text, needle)
        if hits:
            log(f" [{needle}] -> {hits}")

    log("\n=== <a> buttons mentioning download/export/excel ===")
    for a in soup.find_all(("a", "button")):
        txt = a.get_text(" ", strip=True)
        if re.search(r"download|export|excel|xls|report", txt, re.I) or \
           re.search(r"download|export|excel|xls", (a.get("href", "") + a.get("id", "") + a.get("class", [""])[0] if a.get("class") else ""), re.I):
            log(f" {a.name} text={txt!r} href={a.get('href')!r} id={a.get('id')!r} class={a.get('class')} onclick={a.get('onclick')!r}")

    log("\n=== <script src> on the page ===")
    for sc in soup.find_all("script", src=True):
        log(f" script src={sc.get('src')}")

    # 5) Actually hit the download endpoint and validate the payload.
    dl_url = None
    for a in soup.find_all("a"):
        if a.get_text(strip=True).lower() == "download" and a.get("href"):
            dl_url = a.get("href"); break
    if dl_url:
        log(f"\n=== DOWNLOADING {dl_url} ===")
        for variant in (dl_url, dl_url.split("?")[0]):  # with and without ?Length=6
            r = await s._request("GET", variant, referer=f"{PORTAL}/Student/Section/Offered?q={q_token}")
            ct = r.headers.get("content-type", "")
            cd = r.headers.get("content-disposition", "")
            body = r.content
            magic = body[:4]
            is_xlsx = magic[:2] == b"PK"
            log(f" GET {variant}")
            log(f"   status={r.status_code} len={len(body)} content-type={ct!r}")
            log(f"   content-disposition={cd!r}")
            log(f"   magic={magic!r} -> {'XLSX/zip' if is_xlsx else ('HTML' if body[:1] in (b'<',) else 'other')}")
            if is_xlsx:
                p = OUT / "DownloadedOfferedReport.xlsx"
                p.write_bytes(body)
                log(f"   saved {p}")
                # Validate against catalog.py's reader.
                try:
                    from app.catalog import Catalog
                    cat = Catalog.load(p)
                    log(f"   catalog.py parsed it: {len(cat.titles())} course titles")
                    sample = cat.titles()[:3]
                    log(f"   sample titles: {sample}")
                    for t in sample[:1]:
                        log(f"   sample sections for {t!r}: {cat.section_labels(t)}")
                except Exception as e:
                    log(f"   !! catalog.py FAILED to parse it: {type(e).__name__}: {e}")
                break

    log(f"\nArtifacts in {OUT}/ — inspect 03_offered.html for the rest.")


if __name__ == "__main__":
    asyncio.run(main())
