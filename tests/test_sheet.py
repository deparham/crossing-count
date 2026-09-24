"""The team's validation spreadsheet: the right sheet, the right row, and the rest of the
workbook left exactly as it was."""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

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
