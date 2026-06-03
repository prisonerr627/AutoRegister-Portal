"""Offered-course catalog loaded from the 'Offered Course Report.xlsx'.

This is used ONLY as the list of course titles the college offers (to populate the
dashboard's alert picker) plus a reference of typical day/time slots. It is NOT a
source of truth for live timing or seat counts, and we deliberately do NOT key on
the spreadsheet's `Class ID` because that value is regenerated every semester.

The reader is pure-stdlib (zipfile + XML) so we need no openpyxl dependency.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from .schedule import Slot, make_slot

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# A section row title on the Offered page reads "<COURSE> [<SECTION>]"; the section is
# the LAST bracketed token (a course title may itself embed one, e.g. "... [MBA] [A]").
_OFFERED_TITLE_RE = re.compile(r"^(.*)\[([^\[\]]+)\]\s*$")

# Header column names -> our keys (matched case-insensitively).
_COL_MAP = {
    "course title": "title",
    "section": "section",
    "type": "type",
    "day": "day",
    "start time": "start",
    "end time": "end",
    "room": "room",
    "department": "department",
    "course code": "code",
    "capacity": "capacity",
}


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group()
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _read_rows(xlsx_path: Path) -> list[list[Optional[str]]]:
    z = zipfile.ZipFile(xlsx_path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    # First worksheet.
    sheet_name = next(n for n in z.namelist() if re.match(r"xl/worksheets/sheet1\.xml$", n))
    ws = ET.fromstring(z.read(sheet_name))
    rows: list[list[Optional[str]]] = []
    for r in ws.findall(f"{_NS}sheetData/{_NS}row"):
        cells: dict[int, Optional[str]] = {}
        for c in r.findall(f"{_NS}c"):
            ref = c.attrib["r"]
            v = c.find(f"{_NS}v")
            val = v.text if v is not None else None
            if c.attrib.get("t") == "s" and val is not None:
                val = shared[int(val)]
            cells[_col_index(ref)] = val
        max_c = max(cells) if cells else 0
        rows.append([cells.get(i) for i in range(1, max_c + 1)])
    return rows


def _clean_title(raw: str) -> str:
    """Strip a trailing ' [SECTION]' suffix from a course title."""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", (raw or "").strip()).strip()


class Catalog:
    def __init__(self, courses: dict[str, dict]):
        # courses: title -> {"title", "department", "sections": {label: {"type","slots":[Slot]}}}
        self.courses = courses

    @classmethod
    def load(cls, xlsx_path: Path) -> "Catalog":
        rows = _read_rows(xlsx_path)
        if not rows:
            return cls({})
        header = rows[0]
        idx: dict[str, int] = {}
        for i, name in enumerate(header):
            key = _COL_MAP.get((name or "").strip().lower())
            if key:
                idx[key] = i

        def cell(row, key):
            i = idx.get(key)
            return (row[i].strip() if i is not None and i < len(row) and row[i] else "")

        courses: dict[str, dict] = {}
        for row in rows[1:]:
            title = _clean_title(cell(row, "title"))
            if not title:
                continue
            course = courses.setdefault(
                title,
                {"title": title, "department": cell(row, "department"), "sections": {}},
            )
            section = cell(row, "section") or "?"
            sec = course["sections"].setdefault(
                section, {"section": section, "type": cell(row, "type"), "slots": []}
            )
            slot = make_slot(
                cell(row, "day"), cell(row, "start"), cell(row, "end"),
                cell(row, "type"), cell(row, "room"),
            )
            if slot:
                sec["slots"].append(slot)
        return cls(courses)

    @classmethod
    def from_offered_html(cls, html: str) -> "Catalog":
        """Build the catalog from the live Offered-Sections page
        (/Student/Section/Offered) instead of the xlsx download. The page renders one
        master table (Class ID · Title · Status · Capcity · Count · Time) listing every
        section; each Title is "<COURSE> [<SECTION>]" and each Time cell embeds a nested
        table of "<type> <day> <start> <end> <room>" slot rows. We read only the master
        table's DIRECT-CHILD rows so the nested time tables don't leak in. (No
        department column on this page, so it's left blank — the picker doesn't use it.)
        """
        soup = BeautifulSoup(html, "lxml")
        master = heads = None
        for t in soup.find_all("table"):
            hs = [th.get_text(strip=True) for th in t.find_all("th")]
            if "Count" in hs and any(h.startswith("Cap") for h in hs):
                master, heads = t, hs
                break
        if master is None:
            return cls({})
        title_i = heads.index("Title")
        time_i = heads.index("Time")
        need = max(title_i, time_i)
        courses: dict[str, dict] = {}
        for tr in master.find_all("tr", recursive=False):
            tds = tr.find_all("td", recursive=False)
            if len(tds) <= need:  # header / malformed row
                continue
            m = _OFFERED_TITLE_RE.match(tds[title_i].get_text(" ", strip=True))
            if not m:
                continue
            title = " ".join(m.group(1).split()).strip()
            section = m.group(2).strip()
            course = courses.setdefault(title, {"title": title, "department": "", "sections": {}})
            slots: list[Slot] = []
            sec_type = ""
            nested = tds[time_i].find("table")
            if nested:
                for r in nested.find_all("tr"):
                    cells = [c.get_text(" ", strip=True) for c in r.find_all("td")]
                    if len(cells) < 4:
                        continue
                    type_, day, start, end = cells[0], cells[1], cells[2], cells[3]
                    room = cells[4] if len(cells) > 4 else ""
                    slot = make_slot(day, start, end, type_, room)
                    if slot:
                        slots.append(slot)
                        sec_type = sec_type or type_
            course["sections"][section] = {"section": section, "type": sec_type, "slots": slots}
        return cls(courses)

    def titles(self) -> list[str]:
        return sorted(self.courses.keys())

    def get(self, title: str) -> Optional[dict]:
        return self.courses.get(_clean_title(title)) or self.courses.get(title)

    def search(self, query: str, limit: int = 50) -> list[str]:
        q = query.strip().lower()
        if not q:
            return self.titles()[:limit]
        return [t for t in self.titles() if q in t.lower()][:limit]

    def section_labels(self, title: str) -> list[str]:
        c = self.get(title)
        return sorted(c["sections"].keys()) if c else []
