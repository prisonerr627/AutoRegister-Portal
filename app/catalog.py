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

from .schedule import Slot, make_slot

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

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
