"""The team's validation spreadsheet: the right sheet, the right row, and the rest of the
workbook left exactly as it was."""

from __future__ import annotations

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


def test_too_few_crossings_for_a_percentage_puts_no_percentage_in(
        two_tile_video: dict[str, Any], tmp_path: Path, book: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The report refuses a percentage below validation.MIN_VERIFIED_FOR_PCT verified crossings.
    A number in a shared spreadsheet outlives the report it came from, so the sheet refuses it
    too - the date and the link still go in, and the accuracy cell is left as it was."""
    from crossing_count.export import sensor_accuracy
    from crossing_count.validation import MIN_VERIFIED_FOR_PCT, quote

    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    few = MIN_VERIFIED_FOR_PCT - 1
    assert quote(few, few)["rate"] is False
    assert sensor_accuracy(few, few)["accuracy_pct"] == 100.0  # there is one, and it is unquotable
    client, on_drive, _ = _validated(two_tile_video, tmp_path, book,
                                     crossings=few, sensor=few)

    got = client.get("/api/sheet").json()["sheet"]
    assert got["accuracy"]["pct"] is None and got["accuracy"]["why"]  # and it says why
    # no link either: tmp_path is not a synced drive, so there is no address to be had
    assert [c["what"] for c in got["plan"]["changes"]] == ["date"]
    was = sheet.rows(on_drive, sheet.WEEKEND)[got["plan"]["row"]].get(sheet.COLUMNS["accuracy"])
    client.post("/api/sheet", json={"link": "https://x/y"})
    row = sheet.rows(on_drive, sheet.WEEKEND)[got["plan"]["row"]]
    assert row.get(sheet.COLUMNS["accuracy"]) == was  # the accuracy cell untouched
    assert row[sheet.COLUMNS["link"]] == "https://x/y"


def test_a_percentage_the_person_types_in_goes_in_instead(book: Path) -> None:
    """The one offered is the lower direction's; what they put in the box is what is written."""
    said = sheet.plan(book, code="CN-167", brand="Connor", store="Settlement City",
                      day=date(2026, 9, 19), accuracy=87.5, link="")
    assert [(c["what"], c["to"]) for c in said["changes"]] == [("date", "46284"),
                                                              ("accuracy", "0.8750")]
