"""The team's validation spreadsheet: the right sheet, the right row, and the rest of the
workbook left exactly as it was."""

from __future__ import annotations

import io
import shutil
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from crossing_count import sheet

HEADERS = ["Brand", "Store ID", "Store Name", "Region", "Validation Date", "Accuracy",
           "Validation Link", "NOTES"]
SHEETS = ["1. Initial Validation", "2. Weekend Validation"]
# a part no spreadsheet library here understands: it stands for the real workbook's comments,
# checkboxes, pinned add-in and the document library's own metadata
ODDITY = "xl/featurePropertyBag/featurePropertyBag.xml"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
ODD_BYTES = b'<?xml version="1.0"?><FeaturePropertyBags><bag type="Checkbox"/></FeaturePropertyBags>'


def _sheet_xml(rows: list[tuple[int, list[str]]]) -> bytes:
    out = [f'<?xml version="1.0"?><worksheet xmlns="{NS}"><sheetData>']
    for n, cells in rows:
        out.append(f'<row r="{n}">')
        for k, text in enumerate(cells):
            if text:
                ref = f"{chr(ord('A') + k)}{n}"
                out.append(f'<c r="{ref}" s="7" t="inlineStr"><is><t>{text}</t></is></c>')
        out.append("</row>")
    return "".join([*out, "</sheetData></worksheet>"]).encode()


@pytest.fixture
def book(tmp_path: Path) -> Path:
    """A workbook shaped like the team's, carrying a part nothing here can parse."""
    path = tmp_path / "Store_installations_Validation.xlsx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships"><sheets>'
                   f'<sheet name="{SHEETS[0]}" sheetId="1" r:id="rId1"/>'
                   f'<sheet name="{SHEETS[1]}" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org'
                   '/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        # styles like the team's: 5 shows a date, 54 a percentage, 7 and 4 plain text. The
        # date and the percentage live on the cell here, not on the column.
        z.writestr("xl/styles.xml",
                   f'<?xml version="1.0"?><styleSheet xmlns="{NS}">'
                   '<numFmts count="1"><numFmt numFmtId="164" formatCode="d/mm/yyyy;@"/></numFmts>'
                   '<cellXfs count="55">'
                   + "".join(f'<xf numFmtId="{ {5: "164", 54: "9", 45: "0"}.get(i, "0") }"/>'
                             for i in range(55))
                   + '</cellXfs></styleSheet>')
        z.writestr("xl/sharedStrings.xml",
                   '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main"/>')
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml([
            (1, HEADERS), (2, ["Connor", "CN-144", "Port Macquarie", "NSW"])]))
        z.writestr("xl/worksheets/sheet2.xml", _sheet_xml([
            (1, HEADERS),
            (2, ["Johnny Bigg", "JB-401", "Werribee", "VIC"]),
            (3, ["Connor", "CN-167", "Settlement City", "NSW"]),
            (4, ["Connor", "CN-167", "Settlement City", "NSW"])]))  # the same store twice
        z.writestr(ODDITY, ODD_BYTES)
    return path


def test_which_sheet_the_day_belongs_to() -> None:
    assert sheet.sheet_for(date(2026, 9, 19)) == sheet.WEEKEND  # a Saturday
    assert sheet.sheet_for(date(2026, 9, 20)) == sheet.WEEKEND  # a Sunday
    for day in range(21, 26):  # Monday to Friday
        assert sheet.sheet_for(date(2026, 9, day)) == sheet.INITIAL


def test_a_store_already_listed_has_its_row_filled_in(book: Path) -> None:
    said = sheet.plan(book, code="JB-401", brand="Johnny Big", store="Werribee",
                        day=date(2026, 9, 19), accuracy=89.2, link="https://x/JB-401")
    assert said["sheet"] == sheet.WEEKEND and said["row"] == 2 and said["adding"] is False
    # only the three, and never the brand, the code or the store again
    assert {c["what"] for c in said["changes"]} == {"date", "accuracy", "link"}
    assert {c["to"] for c in said["changes"]} == {"46284", "0.8920", "https://x/JB-401"}

    sheet.write(book, said)
    after = sheet.rows(book, sheet.WEEKEND)
    assert after[2]["F"] == "0.8920" and after[2]["G"] == "https://x/JB-401"
    assert after[2]["A"] == "Johnny Bigg" and after[2]["C"] == "Werribee"  # left alone
    assert after[3]["B"] == "CN-167" and "F" not in after[3]  # no other row touched


def test_a_store_not_listed_yet_is_added_with_its_name(book: Path) -> None:
    said = sheet.plan(book, code="RW-409", brand="Rockwear", store="Chadstone",
                        day=date(2026, 9, 21), accuracy=100.0, link="https://x/RW-409")
    assert said["sheet"] == sheet.INITIAL and said["adding"] is True and said["row"] == 3
    assert {c["what"] for c in said["changes"]} == {"brand", "code", "store", "date",
                                                    "accuracy", "link"}
    sheet.write(book, said)
    added = sheet.rows(book, sheet.INITIAL)[3]
    assert added["A"] == "Rockwear" and added["B"] == "RW-409" and added["C"] == "Chadstone"
    assert added["F"] == "1.0000"
    assert "D" not in added  # the region is not something the tool knows, so it is left empty


def test_a_store_listed_twice_fills_in_the_last_of_them(book: Path) -> None:
    said = sheet.plan(book, code="CN-167", brand="Connor", store="Settlement City",
                        day=date(2026, 9, 19), accuracy=92.0, link="https://x/CN-167")
    assert said["row"] == 4 and said["adding"] is False
    sheet.write(book, said)
    rows = sheet.rows(book, sheet.WEEKEND)
    assert rows[4]["F"] == "0.9200" and "F" not in rows[3]  # the earlier one is left standing


def test_the_brand_keeps_the_spelling_the_sheet_uses(book: Path) -> None:
    """The drive calls it "Johnny Big" and the sheet calls it "Johnny Bigg". The sheet's own
    JB- rows settle it, which no comparison of the two names could."""
    listed = sheet.rows(book, sheet.WEEKEND)
    assert sheet.brand_as_listed(listed, "JB-777", "Johnny Big") == "Johnny Bigg"
    assert sheet.brand_as_listed(listed, "CN-999", "connor") == "Connor"
    assert sheet.brand_as_listed(listed, "TC-1", "Tarocash") == "Tarocash"  # no TC row: as given
    assert sheet.brand_as_listed(listed, "392", "Perri Cutten") == "Perri Cutten"  # no prefix


def test_nothing_else_in_the_workbook_is_disturbed(book: Path) -> None:
    """The reason this module writes the zip itself: a spreadsheet library loses the parts it
    cannot parse, and this workbook is mostly such parts."""
    before = {n: zipfile.ZipFile(book).read(n) for n in zipfile.ZipFile(book).namelist()}
    said = sheet.plan(book, code="JB-401", brand="Johnny Bigg", store="Werribee",
                        day=date(2026, 9, 19), accuracy=89.2, link="https://x/JB-401")
    sheet.write(book, said)
    after = {n: zipfile.ZipFile(book).read(n) for n in zipfile.ZipFile(book).namelist()}
    assert set(after) == set(before), set(before) ^ set(after)
    assert after[ODDITY] == ODD_BYTES  # byte for byte, never parsed
    changed = [n for n in before if before[n] != after[n]]
    assert changed == ["xl/worksheets/sheet2.xml"], changed  # the one sheet, and nothing else


def test_the_workbook_is_copied_aside_first(book: Path) -> None:
    said = sheet.plan(book, code="JB-401", brand="Johnny Bigg", store="Werribee",
                        day=date(2026, 9, 19), accuracy=89.2, link="https://x/JB-401")
    got = sheet.write(book, said)
    kept = Path(got["kept"])
    assert kept.is_file() and kept.parent.name == "CrossingCount backups"
    assert sheet.rows(kept, sheet.WEEKEND)[2].get("F") is None  # the copy is the old one


def test_a_workbook_open_in_excel_is_left_alone(book: Path) -> None:
    said = sheet.plan(book, code="JB-401", brand="Johnny Bigg", store="Werribee",
                        day=date(2026, 9, 19), accuracy=89.2, link="https://x/JB-401")
    (book.parent / f"~${book.name}").write_bytes(b"excel's lock")
    with pytest.raises(sheet.TrackerError, match="open in Excel"):
        sheet.write(book, said)
    assert sheet.plan(book, code="JB-401", brand="B", store="W", day=date(2026, 9, 19),
                        accuracy=1.0, link="x")["open_elsewhere"] is True


def test_a_sheet_that_is_not_there(book: Path) -> None:
    with pytest.raises(sheet.TrackerError, match="not a sheet"):
        sheet.rows(book, "5. Nowhere")
    with pytest.raises(sheet.TrackerError, match="could not be read"):
        sheet.rows(book.parent / "no-such.xlsx", sheet.WEEKEND)


# ---------- finding the workbook, and the link that goes in it ----------

def test_the_copy_beside_the_real_workbook_is_not_the_one_chosen(book: Path) -> None:
    """Both of these sit in the delivered folder on the real drive. Writing the accuracy into
    the copy would lose it where nobody looks."""
    copy = book.parent / "Store_installations_Validation - Copy.xlsx"
    copy.write_bytes(book.read_bytes())
    assert [p.name for p in sheet.workbooks(book.parent)] == [book.name, copy.name]
    assert sheet.find_workbook(book.parent) == book


def test_two_workbooks_that_are_both_real_are_left_to_the_person(book: Path) -> None:
    other = book.parent / "Site_validation_tracker.xlsx"
    other.write_bytes(book.read_bytes())
    assert sheet.find_workbook(book.parent) is None  # nobody's spreadsheet is written to by guess
    assert len(sheet.workbooks(book.parent)) == 2  # both offered, for them to pick


def test_no_workbook_and_no_folder(tmp_path: Path) -> None:
    assert sheet.find_workbook(tmp_path) is None
    assert sheet.workbooks(tmp_path / "not there") == []


def _with_link(book: Path, url: str) -> Path:
    """The workbook with one row already carrying a link, as the real one does."""
    out = book.parent / "linked.xlsx"
    with zipfile.ZipFile(book) as z, zipfile.ZipFile(out, "w") as w:
        for n in z.namelist():
            blob = z.read(n)
            if n == "xl/worksheets/sheet2.xml":
                blob = _sheet_xml([(1, HEADERS),
                                   (2, ["Johnny Bigg", "JB-401", "Werribee", "VIC", "", "", url])])
            w.writestr(n, blob)
    return out


def test_the_host_is_learned_from_the_links_already_in_the_sheet(book: Path) -> None:
    linked = _with_link(book, "https://acme.sharepoint.com/:v:/s/validations/IQBRYizp?e=sQEzZR")
    assert sheet.host_in(linked) == "acme.sharepoint.com"
    assert sheet.host_in(book) == ""  # nothing in the sheet says, so nothing is invented


def test_the_link_is_the_synced_folder_s_own_web_address(book: Path) -> None:
    """Checked against what SharePoint itself reports as a delivered folder's webUrl."""
    linked = _with_link(book, "https://acme.sharepoint.com/:v:/s/validations/IQBRYizp?e=sQEzZR")
    folder = Path("/Users/x/Library/CloudStorage/OneDrive-AcmePtyLtd/validations - Documents"
                  "/External/RAG/Johnny Big/Johnny Big Knox City - JB-204")
    assert sheet.link_for(linked, folder) == (
        "https://acme.sharepoint.com/sites/validations/Shared%20Documents"
        "/External/RAG/Johnny%20Big/Johnny%20Big%20Knox%20City%20-%20JB-204")


def test_a_library_that_is_not_the_default_one_keeps_its_name(book: Path) -> None:
    linked = _with_link(book, "https://acme.sharepoint.com/:f:/s/x/IQBRYizp")
    folder = Path("/Users/x/Library/CloudStorage/OneDrive-AcmePtyLtd/validations - Footage"
                  "/RAG/Connor Werribee - CN-167")
    assert sheet.link_for(linked, folder).startswith(
        "https://acme.sharepoint.com/sites/validations/Footage/RAG/")


def test_no_link_rather_than_one_that_goes_nowhere(book: Path) -> None:
    linked = _with_link(book, "https://acme.sharepoint.com/:v:/s/validations/IQBRYizp")
    outside = Path("/Users/x/Desktop/RAG/Connor Werribee - CN-167")  # not on the synced drive
    assert sheet.link_for(linked, outside) == ""
    synced = Path("/Users/x/Library/CloudStorage/OneDrive-AcmePtyLtd/validations - Documents/RAG")
    assert sheet.link_for(book, synced) == ""  # no host to be learned from this one


# ---------- through the pages ----------

def _validated(two_tile_video: dict[str, Any], tmp_path: Path, book: Path, *,
               crossings: int, sensor: int, day: str = "2026-09-19") -> Any:
    """A finished manual validation of CN-167, open in the app, with the workbook on the drive.

    The day comes from the export's own name, as it does on a real one. 2026-09-19 is a
    Saturday, so it is the weekend sheet, where this store is listed already: its row is
    filled in rather than a second one added.
    """
    from fastapi.testclient import TestClient

    from crossing_count.webapp import Setup
    from crossing_count.wizard import Wizard
    from crossing_count.wizard_app import create_wizard_app

    drive = tmp_path / "External" / "RAG"
    (drive / "Connor").mkdir(parents=True)
    shutil.copy2(book, drive / book.name)
    footage = tmp_path / "footage"
    footage.mkdir()
    video = footage / f"Export - CN-167 - {day}-104500 AEST to {day}-110000 AEST.mp4"
    shutil.copy2(two_tile_video["video"], video)
    w = Wizard(video, two_tile_video["dir"], tmp_path / "data")
    w.set_mode("manual")
    s = Setup(w.video, two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-167-PB1", "include": True},
                         {"picture": 1, "name": "CN-167-R2", "include": False}])
    w.set_direction("in")
    for k in range(crossings):
        w.manual_add("CN-167-PB1", 0.05 * k, "in")
    w.manual_watched("CN-167-PB1", 0.0, float(w.state["duration_s"]))
    w.manual_done()
    w.set_sensor({"in": sensor})
    w.set_store(name="Settlement City", code="CN-167", operator="Sam")
    w.make_report()

    client = TestClient(create_wizard_app(two_tile_video["dir"], tmp_path / "data",
                                          folders=[footage]))
    client.post("/api/open", json={"path": str(video)})
    assert client.post("/api/sheet", json={}).status_code == 400  # no folder set yet
    client.post("/api/settings", json={"examples_dir": "", "delivery_dir": str(drive)})
    return client, drive / book.name, video


def test_the_whole_way_through_the_pages(two_tile_video: dict[str, Any], tmp_path: Path,
                                         book: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the folder, see the row that would change, write it. Nothing changes until asked."""
    from crossing_count.wizard import Wizard

    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    # 40 verified, the sensor counting 36: four fewer, 90.0%
    client, on_drive, video = _validated(two_tile_video, tmp_path, book,
                                        crossings=40, sensor=36)
    drive = on_drive.parent

    got = client.get("/api/sheet").json()["sheet"]
    assert got["file"] == book.name and got["accuracy"]["pct"] == 90.0
    day = date.fromisoformat(got["day"])
    assert day == date(2026, 9, 19) and got["plan"]["sheet"] == sheet.WEEKEND
    assert got["plan"]["adding"] is False and got["plan"]["row"] == 4  # the last CN-167 row
    before = (drive / book.name).read_bytes()

    client.get("/api/sheet")  # asking again changes nothing either
    assert (drive / book.name).read_bytes() == before

    done = client.post("/api/sheet", json={"link": "https://acme.sharepoint.com/:f:/s/v/IQx"}).json()
    assert done["written"] is True
    listed = sheet.rows(drive / book.name, done["sheet"])[done["row"]]
    assert listed[sheet.COLUMNS["accuracy"]] == "0.9000"
    assert listed[sheet.COLUMNS["date"]] == str(sheet.serial(day))
    assert listed[sheet.COLUMNS["link"]] == "https://acme.sharepoint.com/:f:/s/v/IQx"
    assert listed[sheet.COLUMNS["code"]] == "CN-167"
    kept = list((drive / "CrossingCount backups").iterdir())  # the workbook as it was
    assert len(kept) == 1 and kept[0].read_bytes() == before

    said = client.get("/api/sheet").json()["sheet"]
    assert said["written"]["row"] == done["row"]  # the page says so afterwards
    trail = Wizard(video, two_tile_video["dir"], tmp_path / "data")
    assert [d["action"] for d in trail.state["decisions"]].count("sheet_updated") == 1


def test_an_accuracy_on_one_crossing_still_goes_in(
        two_tile_video: dict[str, Any], tmp_path: Path, book: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The spreadsheet takes every accuracy the tool can work out, however few crossings it
    rests on: a blank cell there reads as "not done" rather than "too few to quote". The panel
    says what it rests on, and the report is unchanged - it still prints the counts and the
    difference below validation.MIN_VERIFIED_FOR_PCT."""
    from crossing_count.validation import quote

    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    assert quote(1, 1)["rate"] is False  # the report would print no percentage for this
    client, on_drive, _ = _validated(two_tile_video, tmp_path, book, crossings=1, sensor=1)

    got = client.get("/api/sheet").json()["sheet"]
    assert got["accuracy"]["pct"] == 100.0 and got["accuracy"]["thin"]  # in, and said to be thin
    assert "accuracy" in [c["what"] for c in got["plan"]["changes"]]
    client.post("/api/sheet", json={"link": "https://x/y"})
    row = sheet.rows(on_drive, sheet.WEEKEND)[got["plan"]["row"]]
    assert row[sheet.COLUMNS["accuracy"]] == "1.0000"
    assert row[sheet.COLUMNS["link"]] == "https://x/y"


def test_a_percentage_the_person_types_in_goes_in_instead(book: Path) -> None:
    """The one offered is the lower direction's; what they put in the box is what is written."""
    said = sheet.plan(book, code="CN-167", brand="Connor", store="Settlement City",
                      day=date(2026, 9, 19), accuracy=87.5, link="")
    assert [(c["what"], c["to"]) for c in said["changes"]] == [("date", "46284"),
                                                              ("accuracy", "0.8750")]


# ---------- what an added row looks like, and where the copies go ----------

def _styles(path: Path, sheet_name: str, row: int) -> dict[str, str]:
    """The style index on each cell of one row: what decides how it is displayed."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(sheet._part(z, sheet_name)))
    for r in root.find(f"{sheet.M}sheetData") or []:
        if int(str(r.get("r"))) == row:
            return {str(c.get("r"))[:1]: str(c.get("s") or "") for c in r}
    return {}


def _styled(book: Path) -> Path:
    """The workbook with the real one's formatting: the percentage and the date live on the
    cell, not on the column, so a cell made from nothing shows 0.937 where the column shows 94%."""
    out = book.parent / "styled.xlsx"
    body = [f'<?xml version="1.0"?><worksheet xmlns="{NS}"><sheetData>']
    body.append('<row r="1">' + "".join(
        f'<c r="{chr(ord("A") + k)}1" s="7" t="inlineStr"><is><t>{h}</t></is></c>'
        for k, h in enumerate(HEADERS)) + "</row>")
    for n in range(2, 6):  # E, F and G carry the date, percentage and link styles per cell,
        body.append(f'<row r="{n}">'  # as the team's workbook has them
                    f'<c r="A{n}" s="4" t="inlineStr"><is><t>Connor</t></is></c>'
                    f'<c r="B{n}" s="4" t="inlineStr"><is><t>CN-1{n:02d}</t></is></c>'
                    f'<c r="C{n}" s="4" t="inlineStr"><is><t>Somewhere</t></is></c>'
                    f'<c r="E{n}" s="5"/><c r="F{n}" s="54"/><c r="G{n}" s="45"/></row>')
    body.append("</sheetData></worksheet>")
    made = "".join(body).encode()
    with zipfile.ZipFile(book) as z, zipfile.ZipFile(out, "w") as w:
        for part in z.namelist():
            w.writestr(part, made if part == "xl/worksheets/sheet2.xml" else z.read(part))
    return out


def test_an_added_row_keeps_the_column_s_percentage_and_date_format(book: Path) -> None:
    """Otherwise the accuracy reads 0.937 and the date 46287 on every row the tool adds, while
    the rows the team typed read 94% and 22/09/2026."""
    styled = _styled(book)
    assert _styles(styled, sheet.WEEKEND, 3)["F"] == "54"  # what the column looks like
    said = sheet.plan(styled, code="RW-409", brand="Rockwear", store="Chadstone",
                      day=date(2026, 9, 19), accuracy=93.7, link="https://x/RW-409")
    assert said["adding"] is True
    sheet.write(styled, said)
    now = _styles(styled, sheet.WEEKEND, said["row"])
    assert now["F"] == "54" and now["E"] == "5" and now["G"] == "45"


def test_a_row_already_there_keeps_its_own_formatting(book: Path) -> None:
    styled = _styled(book)
    said = sheet.plan(styled, code="CN-103", brand="Connor", store="Somewhere",
                      day=date(2026, 9, 19), accuracy=88.0, link="https://x/CN-103")
    assert said["adding"] is False
    sheet.write(styled, said)
    assert _styles(styled, sheet.WEEKEND, said["row"])["F"] == "54"


def test_the_copies_go_to_the_library_root_not_the_folder_the_team_works_in(
        book: Path, tmp_path: Path) -> None:
    """Beside the workbook they turn up in everyone's External/RAG and sync to the whole team."""
    drive = (tmp_path / "Library/CloudStorage/OneDrive-AcmePtyLtd/validations - Documents"
             / "External" / "RAG")
    drive.mkdir(parents=True)
    here = drive / book.name
    shutil.copy2(book, here)
    assert sheet.library_root(here) == drive.parent.parent
    assert sheet.backups(here) == drive.parent.parent / "CrossingCount backups"

    said = sheet.plan(here, code="JB-401", brand="Johnny Bigg", store="Werribee",
                      day=date(2026, 9, 19), accuracy=89.2, link="https://x/JB-401")
    kept = Path(sheet.write(here, said)["kept"])
    assert kept.parent == drive.parent.parent / "CrossingCount backups"
    assert not (drive / "CrossingCount backups").exists()  # not where the team works


def test_a_workbook_that_is_not_on_a_synced_drive_keeps_its_copies_beside_it(book: Path) -> None:
    assert sheet.library_root(book) is None
    assert sheet.backups(book) == book.parent / "CrossingCount backups"


def test_the_date_takes_a_format_that_shows_a_date_not_the_nearest_cell(book: Path) -> None:
    """The team types many of their dates as text ("16/9/2026"), and those cells are General.
    Going by the nearest cell alone gives a new row a date cell reading 46287."""
    out = book.parent / "mixed.xlsx"
    rows = ['<row r="1">' + "".join(
        f'<c r="{chr(ord("A") + k)}1" s="7" t="inlineStr"><is><t>{h}</t></is></c>'
        for k, h in enumerate(HEADERS)) + "</row>"]
    rows.append('<row r="2"><c r="B2" s="4" t="inlineStr"><is><t>CN-100</t></is></c>'
                '<c r="E2" s="5"><v>46284</v></c></row>')  # a real date, further away
    for n in (3, 4):  # nearer, but text typed into a General cell
        rows.append(f'<row r="{n}"><c r="B{n}" s="4" t="inlineStr"><is><t>CN-10{n}</t></is></c>'
                    f'<c r="E{n}" s="45" t="inlineStr"><is><t>16/9/2026</t></is></c></row>')
    body = f'<?xml version="1.0"?><worksheet xmlns="{NS}"><sheetData>{"".join(rows)}'
    with zipfile.ZipFile(book) as z, zipfile.ZipFile(out, "w") as w:
        for part in z.namelist():
            w.writestr(part, (body + "</sheetData></worksheet>").encode()
                       if part == "xl/worksheets/sheet2.xml" else z.read(part))

    said = sheet.plan(out, code="RW-409", brand="Rockwear", store="Chadstone",
                      day=date(2026, 9, 19), accuracy=93.7, link="https://x")
    sheet.write(out, said)
    assert _styles(out, sheet.WEEKEND, said["row"])["E"] == "5"  # the one that shows a date


def test_a_store_waiting_to_be_validated_is_not_written_over(book: Path) -> None:
    """Under the validated stores the workbook lists the ones still to do: brand, store and
    region filled in, the code column empty. A new row goes after those, never onto them."""
    out = book.parent / "waiting.xlsx"
    rows = ['<row r="1">' + "".join(
        f'<c r="{chr(ord("A") + k)}1" s="7" t="inlineStr"><is><t>{h}</t></is></c>'
        for k, h in enumerate(HEADERS)) + "</row>",
        ('<row r="2"><c r="A2" s="4" t="inlineStr"><is><t>Connor</t></is></c>'
         '<c r="B2" s="4" t="inlineStr"><is><t>CN-100</t></is></c></row>')]
    for n in (3, 4, 5):  # waiting: a brand and a store, no code
        rows.append(f'<row r="{n}"><c r="A{n}" s="4" t="inlineStr"><is><t>Connor</t></is></c>'
                    f'<c r="C{n}" s="4" t="inlineStr"><is><t>Northland</t></is></c></row>')
    body = f'<?xml version="1.0"?><worksheet xmlns="{NS}"><sheetData>{"".join(rows)}'
    with zipfile.ZipFile(book) as z, zipfile.ZipFile(out, "w") as w:
        for part in z.namelist():
            w.writestr(part, (body + "</sheetData></worksheet>").encode()
                       if part == "xl/worksheets/sheet2.xml" else z.read(part))

    said = sheet.plan(out, code="RW-409", brand="Rockwear", store="Chadstone",
                      day=date(2026, 9, 19), accuracy=93.7, link="https://x")
    assert said["row"] == 6 and said["adding"] is True  # after them, not onto row 3
    sheet.write(out, said)
    still = sheet.rows(out, sheet.WEEKEND)
    assert still[3] == {"A": "Connor", "C": "Northland"}  # untouched
    assert still[6]["B"] == "RW-409"


def test_the_row_put_aside_for_a_store_is_the_one_filled_in(book: Path) -> None:
    """The team lists a store before it is validated - brand, store and region, no code - and
    fills that row in when it is done. A second row at the bottom is not where they look."""
    out = book.parent / "putaside.xlsx"
    rows = ['<row r="1">' + "".join(
        f'<c r="{chr(ord("A") + k)}1" s="7" t="inlineStr"><is><t>{h}</t></is></c>'
        for k, h in enumerate(HEADERS)) + "</row>"]
    for n, (bd, st) in enumerate([("Connor", "Northland"), ("Johnny Bigg", "Northland"),
                                  ("Connor", "Knox")], start=2):
        rows.append(f'<row r="{n}"><c r="A{n}" s="4" t="inlineStr"><is><t>{bd}</t></is></c>'
                    f'<c r="C{n}" s="4" t="inlineStr"><is><t>{st}</t></is></c>'
                    f'<c r="D{n}" s="4" t="inlineStr"><is><t>VIC</t></is></c></row>')
    body = f'<?xml version="1.0"?><worksheet xmlns="{NS}"><sheetData>{"".join(rows)}'
    with zipfile.ZipFile(book) as z, zipfile.ZipFile(out, "w") as w:
        for part in z.namelist():
            w.writestr(part, (body + "</sheetData></worksheet>").encode()
                       if part == "xl/worksheets/sheet2.xml" else z.read(part))

    said = sheet.plan(out, code="CN-212", brand="Connor", store="Northland",
                      day=date(2026, 9, 19), accuracy=94.4, link="https://x/CN-212")
    assert said["row"] == 2 and said["waiting"] is True and said["adding"] is False
    # only the code is missing from that row: their own brand and store spelling stay
    assert sorted(c["what"] for c in said["changes"]) == ["accuracy", "code", "date", "link"]
    sheet.write(out, said)
    now = sheet.rows(out, sheet.WEEKEND)
    assert now[2]["B"] == "CN-212" and now[2]["F"] == "0.9440"
    assert now[3] == {"A": "Johnny Bigg", "C": "Northland", "D": "VIC"}  # the other brand's
    assert 5 not in now  # nothing added at the bottom


def test_a_store_waiting_twice_over_is_left_to_the_person(book: Path) -> None:
    """Two rows for the same store and nothing says which. Better a new row they can see than
    the wrong one of theirs filled in."""
    out = book.parent / "twice.xlsx"
    rows = ['<row r="1">' + "".join(
        f'<c r="{chr(ord("A") + k)}1" s="7" t="inlineStr"><is><t>{h}</t></is></c>'
        for k, h in enumerate(HEADERS)) + "</row>"]
    for n in (2, 3):
        rows.append(f'<row r="{n}"><c r="A{n}" s="4" t="inlineStr"><is><t>Connor</t></is></c>'
                    f'<c r="C{n}" s="4" t="inlineStr"><is><t>Northland</t></is></c></row>')
    body = f'<?xml version="1.0"?><worksheet xmlns="{NS}"><sheetData>{"".join(rows)}'
    with zipfile.ZipFile(book) as z, zipfile.ZipFile(out, "w") as w:
        for part in z.namelist():
            w.writestr(part, (body + "</sheetData></worksheet>").encode()
                       if part == "xl/worksheets/sheet2.xml" else z.read(part))
    said = sheet.plan(out, code="CN-212", brand="Connor", store="Northland",
                      day=date(2026, 9, 19), accuracy=94.4, link="https://x")
    assert said["adding"] is True and said["row"] == 4


def test_the_brand_tells_two_waiting_rows_at_one_centre_apart(book: Path) -> None:
    """Four brands are often waiting at the same shopping centre, and their spelling differs
    from ours by a letter: the drive says "Johnny Big" and the sheet "Johnny Bigg"."""
    listed = {2: {"A": "Connor", "C": "Northland"}, 3: {"A": "Johnny Bigg", "C": "Northland"},
              4: {"A": "Rockwear", "C": "Knox"}}
    assert sheet.waiting(listed, "Connor", "Northland") == 2
    assert sheet.waiting(listed, "Johnny Big", "Northland") == 3  # a letter apart, still theirs
    assert sheet.waiting(listed, "TAROCASH", "Northland") is None  # not waiting there at all


def test_the_sheet_s_shorter_store_name_still_matches(book: Path) -> None:
    """The sheet says "Knox" where RetailNext says "Knox City". Two centres that merely share
    a word are not the same store, though, and neither starts the other."""
    listed = {2: {"A": "Johnny Bigg", "C": "Knox"}, 3: {"A": "Connor", "C": "Pacific Epping"}}
    assert sheet.waiting(listed, "Johnny Big", "Knox City") == 2
    assert sheet.waiting(listed, "Connor", "Epping") is None  # a different centre
    assert sheet.waiting({2: {"A": "Connor", "C": "Knox"}}, "Connor", "Box Hill") is None


def test_a_row_that_already_has_a_code_is_not_treated_as_waiting(book: Path) -> None:
    """CN-167 is on the weekend sheet with its code. Another store at the same centre must not
    take that row just because the names match."""
    listed = sheet.rows(book, sheet.WEEKEND)
    assert sheet.waiting(listed, "Connor", "Settlement City") is None


def test_the_workbook_keeps_its_identity_on_a_synced_drive(book: Path) -> None:
    """Written over itself, not replaced by a new file renamed into place. On OneDrive the
    rename leaves a different file where the tracked one was, and the change can sit there
    without ever going up."""
    was = book.stat().st_ino
    said = sheet.plan(book, code="JB-401", brand="Johnny Bigg", store="Werribee",
                      day=date(2026, 9, 19), accuracy=89.2, link="https://x/JB-401")
    sheet.write(book, said)
    assert book.stat().st_ino == was  # the same file OneDrive has been watching
    assert sheet.rows(book, sheet.WEEKEND)[said["row"]]["F"] == "0.8920"
    assert not list(book.parent.glob("*.writing"))  # and nothing left beside it


def test_a_workbook_that_cannot_be_rebuilt_whole_is_not_written_over(book: Path) -> None:
    """The bytes only go down once they are a whole workbook: a half-written one on a shared
    drive is everyone's problem."""
    with zipfile.ZipFile(book) as z:
        names, blobs = z.namelist(), {n: z.read(n) for n in z.namelist()}
    assert sheet.rebuild(names, blobs)  # all there: fine

    short = {n: b for n, b in blobs.items() if n != ODDITY}  # one part lost on the way through
    with pytest.raises(sheet.TrackerError, match="1 part"):
        sheet.rebuild(names, short)


def test_every_part_survives_the_rebuild(book: Path) -> None:
    with zipfile.ZipFile(book) as z:
        names, blobs = z.namelist(), {n: z.read(n) for n in z.namelist()}
    with zipfile.ZipFile(io.BytesIO(sheet.rebuild(names, blobs))) as out:
        assert out.namelist() == names
        assert out.read(ODDITY) == ODD_BYTES  # the part nothing here can parse, byte for byte
