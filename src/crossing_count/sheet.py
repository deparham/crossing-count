"""The team's validation spreadsheet, updated without disturbing the rest of it.

One row a validation: its accuracy, the day of the footage, and a link to the folder it was
delivered to. Which sheet depends on the day the footage was taken - a Saturday or a Sunday
goes to the weekend sheet, any other day to the initial one - and a store already listed
there has its row filled in rather than a second row added.

    A Brand | B Store ID | C Store Name | D Region | E Validation Date | F Accuracy
    G Validation Link | H NOTES        (the weekend sheet carries four more, left alone)

A .xlsx is a zip of XML parts. Only the one sheet is rewritten and every other part is
copied across byte for byte, so the workbook's comments, conditional formatting, in-cell
checkboxes, pinned add-in and the document library's own metadata all survive: they are
never parsed, so they cannot be lost in the parsing. Reading the file with a spreadsheet
library and writing it back drops eighteen parts of this particular workbook, which is why
this module does not.

Nothing is written until someone has seen exactly what would change (plan), the file is
copied aside first (keep), and a workbook open in Excel is left alone.
"""

from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
ET.register_namespace("", M[1:-1])

WEEKEND = "2. Weekend Validation"
INITIAL = "1. Initial Validation"
COLUMNS = {"brand": "A", "code": "B", "store": "C", "region": "D",
           "date": "E", "accuracy": "F", "link": "G"}
EPOCH = date(1899, 12, 30)  # what a date means to a spreadsheet, the 1900 leap bug included
KEEP = 10  # copies of the workbook kept beside it before it is written to


class TrackerError(Exception):
    """A sheet that cannot be read or written, said plainly."""


def sheet_for(day: date) -> str:
    """The weekend sheet for footage taken on a Saturday or a Sunday, else the initial one."""
    return WEEKEND if day.weekday() >= 5 else INITIAL


def serial(day: date) -> int:
    return (day - EPOCH).days


def _part(z: zipfile.ZipFile, name: str) -> str:
    rels = {r.get("Id"): r.get("Target")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    for sh in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{M}sheet"):
        if sh.get("name") == name:
            return "xl/" + str(rels[sh.get(f"{R}id")]).lstrip("/")
    raise TrackerError(f"{name!r} is not a sheet in this workbook.")


def _text(c: ET.Element, sst: list[str]) -> str:
    if c.get("t") == "inlineStr":
        return "".join(t.text or "" for t in c.iter(f"{M}t"))
    v = c.find(f"{M}v")
    if v is None or v.text is None:
        return ""
    return sst[int(v.text)] if c.get("t") == "s" else v.text


def rows(path: Path, sheet: str) -> dict[int, dict[str, str]]:
    """Every row of one sheet that holds anything, by row number, column letter to text."""
    try:
        with zipfile.ZipFile(path) as z:
            part = _part(z, sheet)
            sst = ["".join(t.text or "" for t in si.iter(f"{M}t"))
                   for si in ET.fromstring(z.read("xl/sharedStrings.xml"))]
            root = ET.fromstring(z.read(part))
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise TrackerError(f"{path.name} could not be read ({exc}).") from exc
    out: dict[int, dict[str, str]] = {}
    for r in root.find(f"{M}sheetData") or []:
        cells = {re.sub(r"\d", "", str(c.get("r"))): _text(c, sst) for c in r}
        cells = {k: v for k, v in cells.items() if v.strip()}
        if cells:
            out[int(str(r.get("r")))] = cells
    return out


def find(listed: dict[int, dict[str, str]], code: str) -> int | None:
    """The row this store is already on: the last of them, when it is on more than one."""
    want = _bare(code)
    hits = [n for n, cells in listed.items()
            if _bare(cells.get(COLUMNS["code"], "")) == want and n > 1]
    return hits[-1] if hits else None


def _bare(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def brand_as_listed(listed: dict[int, dict[str, str]], code: str, brand: str) -> str:
    """The spelling the sheet already uses for this brand.

    Found through the store code rather than the brand's name, because the two spellings are
    exactly what differs: the drive's folder says "Johnny Big" and the sheet says "Johnny
    Bigg", and no amount of ignoring case and punctuation bridges a missing letter. Every
    JB- store in the sheet, though, says what the sheet calls that brand. The commonest
    spelling among them wins, so one typo in one row does not decide it.
    """
    prefix = _prefix(code)
    said: dict[str, int] = {}
    if prefix:
        for cells in listed.values():
            if _prefix(cells.get(COLUMNS["code"], "")) == prefix:
                theirs = cells.get(COLUMNS["brand"], "").strip()
                if theirs:
                    said[theirs] = said.get(theirs, 0) + 1
    return max(said, key=lambda k: (said[k], k)) if said else brand


def _prefix(code: str) -> str:
    """"JB" from "JB-401", and nothing from a code that carries no letters."""
    m = re.match(r"\s*([A-Za-z]{2,4})[-_ ]?\d", str(code).strip())
    return m.group(1).upper() if m else ""


def open_elsewhere(path: Path) -> bool:
    """Whether Excel has the workbook open: it leaves a lock file beside it while it does."""
    return (path.parent / f"~${path.name}").exists()


def plan(path: Path, *, code: str, brand: str, store: str, day: date,
         accuracy: float | None, link: str) -> dict[str, Any]:
    """What would change, for someone to agree to before anything does."""
    sheet = sheet_for(day)
    listed = rows(path, sheet)
    at = find(listed, code)
    # the brand's spelling is looked for in both sheets: a brand can have no store at all in
    # the one this validation belongs to, and the sheets agree about what a brand is called
    everywhere = dict(listed)
    for other in (INITIAL, WEEKEND):
        if other != sheet:
            everywhere |= {n + 100_000: cells for n, cells in rows(path, other).items()}
    was = listed.get(at or 0, {})
    values = {"date": str(serial(day)),
              "accuracy": "" if accuracy is None else f"{accuracy / 100:.4f}",
              "link": link}
    if at is None:  # a store not listed yet needs saying who and where as well
        values |= {"brand": brand_as_listed(everywhere, code, brand),
                   "code": code, "store": store}
    return {"file": str(path), "sheet": sheet, "row": at or (max(listed) + 1 if listed else 2),
            "adding": at is None, "open_elsewhere": open_elsewhere(path),
            "changes": [{"column": COLUMNS[k], "what": k,
                         "from": was.get(COLUMNS[k], ""), "to": v}
                        for k, v in values.items() if v != ""]}


def keep(path: Path) -> Path:
    """A copy beside the workbook before it is written to, the last KEEP of them kept."""
    folder = path.parent / "CrossingCount backups"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{path.stem} {datetime.now().astimezone():%Y%m%d-%H%M%S}{path.suffix}"
    shutil.copy2(path, out)
    old = sorted(folder.glob(f"{path.stem} *{path.suffix}"))[:-KEEP]
    for p in old:
        p.unlink(missing_ok=True)
    return out


def write(path: Path, said: dict[str, Any]) -> dict[str, Any]:
    """Make the changes in `said` (from plan), having copied the workbook aside first."""
    if said["open_elsewhere"] or open_elsewhere(path):
        raise TrackerError(f"{path.name} is open in Excel. Close it and try again: writing to "
                           f"it now would lose whatever is being typed into it.")
    if not said["changes"]:
        return {"written": False, "why": "nothing to change"}
    kept = keep(path)
    row, want = int(said["row"]), {c["column"]: c["to"] for c in said["changes"]}
    numeric = {COLUMNS["date"], COLUMNS["accuracy"]}
    try:
        with zipfile.ZipFile(path) as z:
            part = _part(z, said["sheet"])
            names = z.namelist()
            blobs = {n: z.read(n) for n in names}
        root = ET.fromstring(blobs[part])
        data = root.find(f"{M}sheetData")
        if data is None:
            raise TrackerError(f"{said['sheet']!r} has no rows.")
        here = next((r for r in data if int(str(r.get("r"))) == row), None)
        if here is None:
            here = ET.SubElement(data, f"{M}row", {"r": str(row)})
        at = {c.get("r"): c for c in here}
        for col, text in want.items():
            ref = f"{col}{row}"
            c = at.get(ref)
            if c is None:
                c = ET.SubElement(here, f"{M}c", {"r": ref})
            for child in list(c):
                c.remove(child)
            c.attrib.pop("t", None)
            if col in numeric:
                ET.SubElement(c, f"{M}v").text = text
            else:  # inline, so the workbook's shared strings are not touched either
                c.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(c, f"{M}is"), f"{M}t").text = text
        here[:] = sorted(here, key=lambda c: (len(str(c.get("r"))), str(c.get("r"))))
        data[:] = sorted(data, key=lambda r: int(str(r.get("r"))))
        blobs[part] = ET.tostring(root, xml_declaration=True, encoding="UTF-8")
        tmp = path.with_name(path.name + ".writing")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as w:
            for n in names:  # every other part, byte for byte
                w.writestr(n, blobs[n])
        tmp.replace(path)
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise TrackerError(f"{path.name} could not be written ({exc}). It is unchanged; the "
                           f"copy made first is at {kept}.") from exc
    return {"written": True, "row": row, "sheet": said["sheet"], "kept": str(kept),
            "adding": said["adding"], "changes": said["changes"]}


# ---------- the workbook, and a link to put in it ----------
# The links already in the sheet are share links somebody copied out of SharePoint
# (".../:v:/s/validations/IQBRYizp...?e=sQEzZR"). The token in one is minted by the server
# when a person presses Copy link; it cannot be worked out from a path, so nothing here
# pretends to. What can be worked out is the folder's own web address, which opens the same
# folder for anyone who has the drive, and which a person can paste over.
SYNCED = re.compile(r"CloudStorage/OneDrive-[^/]+/(?P<site>[^/]+?) - (?P<library>[^/]+)/(?P<rest>.+)")
DEFAULT_LIBRARY = {"Documents": "Shared Documents"}  # what the default library is called on the web
COPYISH = re.compile(r"\b(cop(y|ies)|backup|old|draft|conflict)\b|\(\d+\)", re.IGNORECASE)


def workbooks(root: Path) -> list[Path]:
    """The spreadsheets in the folder finished work is delivered to, likeliest first.

    A name saying validation comes before one that does not, and a copy comes last however
    it is named: the real sheet and a "Store_installations_Validation - Copy.xlsx" sit side
    by side on this drive, and writing the accuracy into the copy would lose it quietly.
    """
    try:
        here = [p for p in sorted(root.iterdir())
                if p.suffix.lower() == ".xlsx" and not p.name.startswith(("~$", "."))]
    except OSError:
        return []
    return sorted(here, key=lambda p: (bool(COPYISH.search(p.stem)),
                                       "validation" not in p.stem.lower(), p.name.lower()))


def find_workbook(root: Path) -> Path | None:
    """The workbook to offer, when the folder settles which one it is on its own.

    One candidate that is not a copy, or one whose name says validation among copies that do
    not. Anything less clear than that and the person picks, because nobody's spreadsheet is
    written to by guess.
    """
    here = workbooks(root)
    real = [p for p in here if not COPYISH.search(p.stem)]
    return real[0] if len(real) == 1 else None


def host_in(path: Path) -> str:
    """The SharePoint host the sheet's own links point at, so none is invented here."""
    for name in (INITIAL, WEEKEND):
        try:
            listed = rows(path, name)
        except TrackerError:
            continue
        for cells in listed.values():
            m = re.match(r"https://([a-z0-9-]+\.sharepoint\.com)/",
                         cells.get(COLUMNS["link"], ""), re.IGNORECASE)
            if m:
                return m.group(1)
    return ""


def link_for(path: Path, folder: Path) -> str:
    """The web address of a folder on the synced drive, or "" when it cannot be known.

    The synced folder's name says which site and which library it is ("validations -
    Documents"), and the workbook's own links say which host. Anything else and this gives
    nothing back rather than a link that goes nowhere.
    """
    m = SYNCED.search(folder.as_posix())
    host = host_in(path)
    if not m or not host:
        return ""
    library = DEFAULT_LIBRARY.get(m["library"], m["library"])
    parts = [m["site"], library, *m["rest"].split("/")]
    return f"https://{host}/sites/" + "/".join(quote(p) for p in parts)
