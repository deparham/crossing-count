"""The validation report as a PowerPoint file, laid out like iTOi's camera validation page.

Page 1 follows the original: logo and date, "<store> <code> Traffic System",
the CAMERA VALIDATION tag, the capture details, three cards (verified count,
system count, accuracy) and a detection-overlay frame. Before the cards it says
the result in words (which way, by how many people); after them what the sample
describes, what could not be known, and the record (validation ID, sampling,
specification, gold set, footage). The frame moves to a page of its own when page
1 has no room for it. The pages after it hold the details: every verified crossing
with its time, and a close-up of each one. How the count was made is not printed:
it is kept with the validation's record instead. A4 portrait,
like the PDF (report_pdf.py), which lays out the same data.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from lxml import etree
from PIL import Image as PILImage
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_TICK_MARK
from pptx.enum.dml import MSO_LINE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

A4_W, A4_H = Emu(7560000), Emu(10692000)  # 210 x 297 mm
# The app's look: white page, soft cards with hairline edges, navy text, one blue
NAVY = RGBColor(0x0F, 0x17, 0x2A)
BLUE = RGBColor(0x25, 0x63, 0xEB)
BLUE_TEXT = RGBColor(0x1D, 0x4E, 0xD8)
PILL_BG = RGBColor(0xEE, 0xF3, 0xFE)
PILL_LINE = RGBColor(0xD7, 0xE3, 0xFC)
CARD_BG = RGBColor(0xF8, 0xF9, 0xFB)
LINE = RGBColor(0xEA, 0xEC, 0xF0)  # card edges and table rules
TRACK = RGBColor(0xD0, 0xDC, 0xF8)  # the gauge's empty ticks, on the pale-blue card
MAYBE = RGBColor(0x8F, 0xB0, 0xF6)  # the gauge between "none of the unclear were real" and "all were"
BAR_GREY = RGBColor(0xCB, 0xD5, 0xE1)  # the system's columns
GREY = RGBColor(0x66, 0x70, 0x85)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
WARN_TEXT = RGBColor(0xB4, 0x23, 0x18)
SANS = "Arial"  # installed on every Mac and PC; Calibri is not on Macs without Office
NO_GRID = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"  # PowerPoint's table style "No Style, No Grid"
FOOTER = "iTOi Solutions  –  Confidential"
TABLE_TOP_MAX = 10.55  # inches: tables and pictures stay above the footer


def _box(slide: Any, left: float, top: float, width: float, height: float) -> Any:
    tb = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    return tb


def _write(shape: Any, text: str, size: float, *, bold: bool = False, color: RGBColor = NAVY,
           font: str = SANS, align: Any = PP_ALIGN.LEFT, spacing: float | None = None,
           anchor: Any = None) -> None:
    tf = shape.text_frame
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = font
    run.font.color.rgb = color
    if spacing:  # letter spacing, in hundredths of a point
        run._r.get_or_add_rPr().set("spc", str(round(spacing * 100)))
    if anchor is not None:
        tf.vertical_anchor = anchor


def _lines(shape: Any, lines: list[str], size: float, color: RGBColor = NAVY,
           bold_all: bool | None = None) -> None:
    """Paragraphs in a text box: the first one bold, or all or none of them (bold_all)."""
    tf = shape.text_frame
    tf.word_wrap = True
    for k, line in enumerate(lines):
        p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
        p.space_after = Pt(3)
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = k == 0 if bold_all is None else bold_all
        run.font.name = SANS
        run.font.color.rgb = color


def _signed(v: float | None) -> str:
    return "–" if v is None else f"{v:+.1f}%"


def _flat(shape: Any) -> Any:
    """No effects: the default theme gives every shape a drop shadow."""
    if shape._element.spPr.find(qn("a:effectLst")) is None:
        etree.SubElement(shape._element.spPr, qn("a:effectLst"))
    return shape


def _panel(slide: Any, left: float, top: float, width: float, height: float, fill: RGBColor,
           radius: float = 0.07, line: RGBColor | None = None) -> Any:
    """A rounded card; with `line`, a hairline edge (no shadows: the page prints clean)."""
    s = _flat(slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top),
                                     Inches(width), Inches(height)))
    s.adjustments[0] = radius
    s.fill.solid()
    s.fill.fore_color.rgb = fill
    if line is None:
        s.line.fill.background()
    else:
        s.line.color.rgb = line
        s.line.width = Pt(0.75)
    tf = s.text_frame
    tf.margin_left = tf.margin_right = Inches(0.15)
    tf.margin_top = tf.margin_bottom = Inches(0.08)
    return s


def _stroke(slide: Any, x1: float, y1: float, x2: float, y2: float, colour: RGBColor,
            width: float, round_cap: bool = False) -> Any:
    ln = _flat(slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1),
                                          Inches(x2), Inches(y2)))
    ln.line.color.rgb = colour
    ln.line.width = Pt(width)
    if round_cap:
        ln.line._get_or_add_ln().set("cap", "rnd")
    return ln


def gauge_ticks(lo: float | None, hi: float | None, n: int = 30) -> list[str]:
    """Half a ring of n ticks, left to right: "on" up to the accuracy, "maybe" up to the top of
    its range (unclear crossings), "off" after."""
    def upto(p: float | None) -> int:
        return 0 if p is None else round(n * max(0.0, min(100.0, float(p))) / 100)
    a, b = upto(lo), upto(hi if hi is not None else lo)
    return ["on" if k < a else "maybe" if k < b else "off" for k in range(n)]


def _gauge(slide: Any, cx: float, cy: float, r_out: float, lo: float | None,
           hi: float | None) -> None:
    ticks = gauge_ticks(lo, hi)
    colour = {"on": BLUE, "maybe": MAYBE, "off": TRACK}
    r_in = r_out * 0.74
    for k, state in enumerate(ticks):
        a = math.pi * (1 - (k + 0.5) / len(ticks))
        _stroke(slide, cx + r_in * math.cos(a), cy - r_in * math.sin(a),
                cx + r_out * math.cos(a), cy - r_out * math.sin(a), colour[state],
                2.6 if r_out > 0.8 else 2.0, round_cap=True)


def _picture(slide: Any, path: str, left: float, top: float, max_w: float,
             max_h: float, corner: float = 0.0) -> tuple[float, float]:
    """Add a picture fitted inside max_w x max_h, centred horizontally; return its size.
    corner: the radius of its rounded corners, in inches."""
    with PILImage.open(path) as im:
        w_px, h_px = im.size
    scale = min(max_w / w_px, max_h / h_px)
    w, h = w_px * scale, h_px * scale
    pic = slide.shapes.add_picture(path, Inches(left + (max_w - w) / 2), Inches(top), Inches(w),
                                   Inches(h))
    if corner:
        pic.auto_shape_type = MSO_SHAPE.ROUNDED_RECTANGLE
        av = pic._element.spPr.find(qn("a:prstGeom")).get_or_add_avLst()
        etree.SubElement(av, qn("a:gd"), name="adj",
                         fmla=f"val {min(50000, round(100000 * corner / min(w, h)))}")
    return w, h


def _pct(v: float | None) -> str:
    if v is None:
        return "–"
    return f"{v:.0f}%" if float(v).is_integer() else f"{v:.1f}%"


def _value_size(value: str, base: float) -> float:
    """Card numbers at full size, a range a little smaller, a word (INCOMPLETE) smaller still."""
    if value[:1].isalpha():
        return round(base * 0.4, 1)
    return base if len(value) <= 5 else round(base * 5 / len(value), 1)


IDENTITY = (("validation_id", "Validation ID"), ("sampling", "Sampling"),
            ("specification", "Ground truth"), ("gold_set", "Gold data set"),
            ("footage", "Footage"))


def identity_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    """What the result rests on, for page 1: a report made before finalising says it is a
    draft, where the validation ID will be."""
    ident = data.get("identity") or {}
    if not ident:
        return []
    return [(name, str(ident[k]) if ident.get(k) is not None else
             "draft: no ID until the validation is finalised" if k == "validation_id" else "–")
            for k, name in IDENTITY]


def card_value(r: dict[str, Any], complete: bool) -> tuple[str, str]:
    """The third card of a direction: its accuracy, or on too few crossings the difference in
    people, or INCOMPLETE."""
    unsure, span = int(r.get("unsure") or 0), r.get("accuracy_range")
    few = complete and r.get("rate") is False  # too few crossings for a percentage
    if not complete:  # footage nobody watched may hold people missing from the count
        value = "INCOMPLETE"
    elif few:  # the difference in people, in the slot a percentage would take
        value = f"{int(r['system']) - int(r['verified']):+d}".replace("-", "−")
    elif unsure and span:
        value = _pct(span[0]) if span[0] == span[1] else f"{_pct(span[0])[:-1]}–{_pct(span[1])}"
    else:
        value = _pct(r["accuracy"])
    return ("DIFFERENCE" if few else "SENSOR ACCURACY"), value


def _header(slide: Any, data: dict[str, Any], logo: Path | None) -> None:
    if logo is not None and logo.is_file():
        _picture(slide, str(logo), 0.52, 0.26, 2.0, 0.74)
    else:
        _write(_box(slide, 0.6, 0.38, 3.0, 0.5), "i to i solutions — image to intelligence", 13,
               bold=True, color=NAVY)
    _write(_box(slide, 4.0, 0.34, 3.73, 0.25), "OPTIMIZATION REPORT", 10, bold=True,
           color=BLUE, align=PP_ALIGN.RIGHT, spacing=2.5)
    date = _panel(slide, 6.33, 0.64, 1.4, 0.32, WHITE, radius=0.3, line=LINE)  # a date chip
    date.text_frame.margin_left = date.text_frame.margin_right = Inches(0.04)
    date.text_frame.margin_top = date.text_frame.margin_bottom = 0
    _write(date, data["report_date"], 11, color=NAVY, align=PP_ALIGN.CENTER,
           anchor=MSO_ANCHOR.MIDDLE)
    _stroke(slide, 0.53, 1.12, 7.73, 1.12, LINE, 0.75)


def _footer(slide: Any, data: dict[str, Any]) -> Any:
    vid = (data.get("identity") or {}).get("validation_id") or ""
    _stroke(slide, 0.53, 10.93, 7.73, 10.93, LINE, 0.75)
    _write(_box(slide, 0.63, 11.05, 4.6, 0.25),
           f"{FOOTER}  ·  {vid}" if vid else FOOTER, 9, color=GREY)
    return _box(slide, 4.6, 11.05, 3.04, 0.25)  # "Page k of N", written once all pages exist


def _cover(slide: Any, data: dict[str, Any]) -> bool:
    """Page 1. Returns whether the busiest moment's picture fitted on it."""
    title = " ".join(x for x in (data["store_name"], data["store_code"]) if x) + " Traffic System"
    size = 26.0 if len(title) <= 30 else max(16.0, 26.0 * 30 / len(title))  # stay on one line
    _write(_box(slide, 0.53, 1.32, 7.2, 0.55), title, size, bold=True)
    pill = _panel(slide, 0.53, 1.98, 2.05, 0.34, PILL_BG, radius=0.5, line=PILL_LINE)
    pill.text_frame.margin_left = pill.text_frame.margin_right = Inches(0.04)
    pill.text_frame.margin_top = pill.text_frame.margin_bottom = 0
    pill.text_frame.word_wrap = False
    _write(pill, "CAMERA VALIDATION", 9, bold=True, color=BLUE_TEXT, align=PP_ALIGN.CENTER,
           spacing=1.5, anchor=MSO_ANCHOR.MIDDLE)
    rows = data["directions"]
    captured = f"Captured {data['captured_date']} Cameras {data['location']} {data['time_range']}"
    if len(rows) == 1:
        captured += f"  ·  {rows[0]['label']}"
    meta = _box(slide, 0.53, 2.52, 7.2, 0.32)  # the store code, then when and where
    if data["store_code"]:
        _write(meta, f"{data['store_code']}  ·  ", 12, bold=True)
    _write(meta, captured, 12, color=GREY)
    top = 3.05

    one = len(rows) == 1
    card_h = 1.85 if one else 1.40
    kind = str(data.get("count_label", "VERIFIED COUNT")).split()[0]  # VERIFIED or MANUAL
    complete = bool(data.get("complete", True))
    for r in rows:
        tag = "" if one else f" {r['key'].upper()}"
        unsure = int(r.get("unsure") or 0)
        third, accuracy = card_value(r, complete)
        verified = f"{r['verified']}–{r['verified'] + unsure}" if unsure else str(r["verified"])
        cards = ((f"{kind}{tag or ' COUNT'}", verified),
                 (f"SYSTEM{tag or ' COUNT'}", str(r["system"])),
                 (f"{third}{tag}", accuracy))
        for j, (label, value) in enumerate(cards):
            left, hi = 0.53 + j * 2.48, j == 2  # the accuracy card: pale blue, with its gauge
            _panel(slide, left, top, 2.24, card_h, PILL_BG if hi else CARD_BG, radius=0.09,
                   line=PILL_LINE if hi else LINE)
            long = len(label) > 17  # "SENSOR ACCURACY OUT" fits the card only smaller
            _write(_box(slide, left + 0.2, top + 0.2, 1.95, 0.25), label, 8 if long else 9,
                   bold=True, color=BLUE_TEXT if hi else GREY, spacing=1 if long else 1.5)
            if hi and third == "SENSOR ACCURACY" and complete:
                span = r.get("accuracy_range")
                lo, up = span if unsure and span else (r["accuracy"], r["accuracy"])
                r_out, cy = (0.95, top + card_h - 0.24) if one else (0.68, top + card_h - 0.16)
                _gauge(slide, left + 1.12, cy, r_out, lo, up)
                _write(_box(slide, left, cy - (0.5 if one else 0.38), 2.24, 0.54 if one else 0.41),
                       value, _value_size(value, 22 if one else 15), bold=True,
                       align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.BOTTOM)
                continue
            _write(_box(slide, left + 0.2, top + (0.6 if one else 0.5), 1.95, 0.8 if one else 0.6),
                   value, _value_size(value, 46 if one else 34), bold=True,
                   anchor=MSO_ANCHOR.MIDDLE if value[:1].isalpha() else MSO_ANCHOR.TOP)
        top += card_h + 0.2
    if not complete:
        note = _box(slide, 0.53, top - 0.08, 7.2, 0.5)
        note.text_frame.word_wrap = True
        _write(note, "Validation incomplete: " + " ".join(data.get("incomplete", []))
               + " No accuracy is given.", 10, bold=True, color=WARN_TEXT)
        top += 0.5
    return _frame(slide, data, top)


FRAME_MIN_H = 2.2  # inches: a smaller frame shows nothing; it gets a page of its own instead


def _frame(slide: Any, data: dict[str, Any], top: float) -> bool:
    """The busiest moment's picture below `top`, if there is room. Returns whether it is placed
    (or there is none)."""
    frame = data.get("frame")
    if not frame or not Path(frame).is_file():
        return True
    img_top = top + 0.42
    if TABLE_TOP_MAX - 0.3 - img_top < FRAME_MIN_H:
        return False
    _write(_box(slide, 0.5, top, 7.27, 0.3),
           str(data.get("frames_title", "VALIDATION FRAMES — AUTOMATED DETECTION OVERLAY")),
           9.5, bold=True, color=GREY, align=PP_ALIGN.CENTER, spacing=2)
    _, h = _picture(slide, frame, 0.91, img_top, 6.45, TABLE_TOP_MAX - 0.3 - img_top,
                    corner=0.14)
    if data.get("frame_caption"):
        _write(_box(slide, 0.5, img_top + h + 0.06, 7.27, 0.25), data["frame_caption"], 9,
               color=GREY, align=PP_ALIGN.CENTER)
    return True


def _rule_below(cell: Any) -> None:
    """A hairline under the cell and no other edge, as the app's tables have."""
    tc_pr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        for old in tc_pr.findall(qn(tag)):
            tc_pr.remove(old)
    for k, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):  # the schema's order, first
        ln = etree.Element(qn(tag), w="0" if tag != "a:lnB" else "9525")
        if tag == "a:lnB":
            etree.SubElement(etree.SubElement(ln, qn("a:solidFill")), qn("a:srgbClr"), val=str(LINE))
        else:
            etree.SubElement(ln, qn("a:noFill"))
        tc_pr.insert(k, ln)


def _cell(cell: Any, text: str, *, header: bool = False) -> None:
    """A table cell: muted capitals on the header row, navy text below, hairlines between."""
    cell.text = text
    run = cell.text_frame.paragraphs[0].runs[0]
    run.font.size = Pt(7.5 if header else 9.5)
    run.font.bold = header
    run.font.name = SANS
    run.font.color.rgb = GREY if header else NAVY
    if header:  # shown in capitals; the text itself stays as written
        run._r.get_or_add_rPr().set("cap", "all")
        run._r.get_or_add_rPr().set("spc", "20")
    cell.fill.solid()
    cell.fill.fore_color.rgb = WHITE
    _rule_below(cell)
    cell.margin_left = cell.margin_right = Inches(0.08)
    cell.margin_top = cell.margin_bottom = Inches(0.03)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE


def _plain(shape: Any) -> Any:
    """A table without PowerPoint's banded style: the cells carry their own look."""
    tbl = shape.table
    tbl.first_row, tbl.horz_banding = True, False
    style = tbl._tbl.tblPr.find(qn("a:tableStyleId"))
    if style is None:
        style = etree.SubElement(tbl._tbl.tblPr, qn("a:tableStyleId"))
    style.text = NO_GRID
    return tbl


def _table(slide: Any, rows: list[dict[str, Any]], top: float) -> None:
    cols = (("#", 0.45), ("Time", 1.0), ("Camera", 1.75), ("Direction", 1.1),
            ("How it was found", 2.9))
    tbl = _plain(slide.shapes.add_table(len(rows) + 1, len(cols), Inches(0.53), Inches(top),
                                        Inches(7.2), Inches(0.26 * (len(rows) + 1))))
    for j, (name, width) in enumerate(cols):
        tbl.columns[j].width = Inches(width)
        _cell(tbl.cell(0, j), name, header=True)
    tbl.rows[0].height = Inches(0.3)
    for i, r in enumerate(rows, 1):
        for j, value in enumerate((str(r["n"]), r["time"], r["camera"], r["direction"],
                                   r["found"])):
            _cell(tbl.cell(i, j), value)
        tbl.rows[i].height = Inches(0.26)


def _title(slide: Any, text: str) -> None:
    _write(_box(slide, 0.53, 1.32, 7.2, 0.5), text, 22, bold=True)


def _details(new_page: Any, data: dict[str, Any]) -> None:
    slide = new_page()
    _title(slide, "Crossing details")
    name = " ".join(x for x in (data["store_name"], data["store_code"]) if x)
    _write(_box(slide, 0.53, 1.85, 7.2, 0.3),
           f"{name}  ·  Captured {data['captured_date']} {data['time_range']}", 11, color=GREY)
    rows = data["crossings"]
    top = 2.3
    if not rows:
        _write(_box(slide, 0.62, top, 7.0, 0.9),
               data.get("crossings_note") or "No crossings were verified for this period.",
               12, color=GREY)
        return
    per_row = 0.26
    while rows:
        fit = max(1, int((TABLE_TOP_MAX - top - 0.3) / per_row))
        _table(slide, rows[:fit], top)
        rows = rows[fit:]
        if rows:
            slide = new_page()
            _title(slide, "Crossing details (continued)")
            top = 1.95


def _diff(acc: dict[str, Any] | None) -> str:
    """The system minus the verified count, and as a share of it: "+2 (+10%)"."""
    if not acc or acc.get("error") is None:
        return "–"
    err, pct = int(acc["error"]), acc.get("error_pct")
    return f"{err:+d}" if pct is None else f"{err:+d} ({pct:+.0f}%)"


def _grid(slide: Any, header: list[str], rows: list[list[str]], top: float,
          widths: list[float]) -> float:
    tbl = _plain(slide.shapes.add_table(len(rows) + 1, len(header), Inches(0.53), Inches(top),
                                        Inches(sum(widths)), Inches(0.3 + 0.26 * len(rows))))
    for j, (name, width) in enumerate(zip(header, widths, strict=True)):
        tbl.columns[j].width = Inches(width)
        _cell(tbl.cell(0, j), name, header=True)
    tbl.rows[0].height = Inches(0.3)
    for i, row in enumerate(rows, 1):
        for j, value in enumerate(row):
            _cell(tbl.cell(i, j), value or "–")
        tbl.rows[i].height = Inches(0.26)
    return top + 0.3 + 0.26 * len(rows)


def _bare(chart: Any) -> None:
    """No fill and no edge on the chart itself: it sits on its own card."""
    space = chart._chartSpace
    for old in space.findall(qn("c:spPr")):
        space.remove(old)
    sp = etree.Element(qn("c:spPr"))
    etree.SubElement(sp, qn("a:noFill"))
    etree.SubElement(etree.SubElement(sp, qn("a:ln")), qn("a:noFill"))
    space.find(qn("c:chart")).addnext(sp)


def _chart(slide: Any, top: float, title: str, labels: list[str], verified: list[int],
           system: list[int | None]) -> float:
    """Verified (blue) against the system (grey) per 15 minutes, on a card: light dashed
    gridlines, no axis lines, muted labels."""
    data = CategoryChartData()
    data.categories = labels
    data.add_series("Verified", verified)
    data.add_series("System", system)
    _panel(slide, 0.53, top, 7.2, 2.35, WHITE, radius=0.06, line=LINE)
    chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.63), Inches(top + 0.06),
                                   Inches(7.0), Inches(2.23), data).chart
    _bare(chart)
    chart.font.size = Pt(8.5)
    chart.font.name = SANS
    chart.font.color.rgb = GREY
    chart.has_title = True
    chart.chart_title.text_frame.text = title
    head = chart.chart_title.text_frame.paragraphs[0].runs[0].font
    head.size, head.bold, head.color.rgb = Pt(11), True, NAVY
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.TOP
    chart.legend.include_in_layout = False
    chart.legend.font.size = Pt(8.5)
    plot = chart.plots[0]
    plot.gap_width, plot.overlap = 90, -12
    values, cats = chart.value_axis, chart.category_axis
    values.has_major_gridlines = True
    values.major_gridlines.format.line.color.rgb = LINE
    values.major_gridlines.format.line.dash_style = MSO_LINE.DASH
    values.format.line.fill.background()
    values.major_tick_mark = cats.major_tick_mark = XL_TICK_MARK.NONE
    cats.format.line.color.rgb = LINE
    for series, colour in zip(chart.series, (BLUE, BAR_GREY), strict=False):
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = colour
    return top + 2.5


def _breakdown(new_page: Any, data: dict[str, Any]) -> None:
    """Verified against the system per 15-minute interval and per camera, where there is
    more than one; charts where the system's interval numbers are known."""
    b = data.get("breakdown") or {}
    ivs, cams, dirs = b.get("intervals") or [], b.get("cameras") or [], b.get("dirs") or []
    if not ivs and not cams:
        return
    title = "Traffic by 15 minutes" if ivs else "Traffic by camera"
    slide = new_page()
    _title(slide, title)
    top = 1.95

    def room(height: float) -> None:
        nonlocal slide, top
        if top + height > TABLE_TOP_MAX:
            slide = new_page()
            _title(slide, f"{title} (continued)")
            top = 1.95

    cols = [f"{d['label'].split()[-1]} {k}" for d in dirs for k in ("verified", "system", "difference")]
    first = 1.55
    widths = [first, *[(7.2 - first) / len(cols)] * len(cols)]

    def rows_of(items: list[dict[str, Any]]) -> list[list[str]]:
        out = []
        for it in items:
            row = [it["label"] + (" (part)" if it.get("partial") else "")]
            for d in dirs:
                system = (it.get("sensor") or {}).get(d["key"])
                row += [str(it["verified"][d["key"]]), "–" if system is None else str(system),
                        _diff((it.get("accuracy") or {}).get(d["key"]))]
            out.append(row)
        return out

    if levels := (data.get("levels") or {}).get("rows") or []:
        room(0.95 + 0.26 * len(levels))
        _write(_box(slide, 0.53, top, 7.2, 0.3), "Error by traffic level", 12, bold=True)
        top = _grid(slide, ["Traffic level", "Intervals", "Verified", "System", "Error", "Bias",
                            "WAPE"],
                    [[str(r["level"]), str(r["intervals"]), str(r["truth"]), str(r["system"]),
                      f"{int(r['error']):+d}", _signed(r.get("bias_pct")), _pct(r.get("wape_pct"))]
                     for r in levels], top + 0.35, [1.8, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9])
        box = _box(slide, 0.53, top + 0.06, 7.2, 0.4)
        box.text_frame.word_wrap = True
        _write(box, "Each 15-minute interval is placed by its own verified crossings per "
               "camera-hour: quiet under 40, normal under 120, busy under 240, heavy from 240.",
               9, color=GREY)
        top += 0.55

    if ivs:
        if any(it.get("sensor") for it in ivs):
            for d in dirs:
                room(2.5)
                top = _chart(slide, top, d["label"], [it["label"].split(" - ")[0] for it in ivs],
                             [it["verified"][d["key"]] for it in ivs],
                             [(it.get("sensor") or {}).get(d["key"]) for it in ivs])
        room(0.6 + 0.26 * len(ivs))
        top = _grid(slide, ["Time", *cols], rows_of(ivs), top, widths) + 0.3
    if cams:
        room(0.6 + 0.26 * len(cams))
        top = _grid(slide, ["Camera", *cols], rows_of(cams), top, widths) + 0.3
    for note in b.get("notes") or []:
        room(0.5)
        box = _box(slide, 0.53, top, 7.2, 0.45)
        box.text_frame.word_wrap = True
        _write(box, note, 10, color=GREY)
        top += 0.5


def _snapshots(new_page: Any, data: dict[str, Any]) -> None:
    thumbs = data["thumbs"]
    per_page, cols, col_w, size = 12, 3, 2.33, 1.85
    for k in range(0, len(thumbs), per_page):
        slide = new_page()
        _title(slide, "Crossing snapshots")
        for idx, th in enumerate(thumbs[k:k + per_page]):
            row, col = divmod(idx, cols)
            left = 0.64 + col * col_w + 0.05
            top = 1.95 + row * 2.2
            _, h = _picture(slide, th["path"], left, top, col_w - 0.1, size,  # keeps its shape
                            corner=0.1)
            _write(_box(slide, left, top + h + 0.04, col_w - 0.1, 0.25), th["label"], 9,
                   color=GREY, align=PP_ALIGN.CENTER)


def build_report(data: dict[str, Any], out: Path, logo: Path | None = None) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = A4_W, A4_H
    blank = prs.slide_layouts[6]
    page_boxes: list[Any] = []

    def new_page() -> Any:
        slide = prs.slides.add_slide(blank)
        _header(slide, data, logo)
        page_boxes.append(_footer(slide, data))
        return slide

    fitted = _cover(new_page(), data)
    _breakdown(new_page, data)
    _details(new_page, data)
    if not fitted:  # the first page is full: the picture before the snapshots, at full size
        slide = new_page()
        _title(slide, "Validation frame")
        _frame(slide, data, 1.95)
    _snapshots(new_page, data)
    for k, box in enumerate(page_boxes, 1):
        _write(box, f"Page {k} of {len(page_boxes)}", 9, color=GREY, align=PP_ALIGN.RIGHT)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out
