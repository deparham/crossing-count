"""The validation report as a PDF, from the same data as the PowerPoint.

The wizard gathers what a report says once (Wizard.report_data); report_pptx.py and this
module only lay it out. Page 1 is written for someone who will not read the method: what
the system did, in people, where and how sure, with what could not be determined. The
crossings and the snapshots follow, as in the PowerPoint.

How the count was made is not printed. It is the evidence for an argument with a vendor,
not something a customer needs in order to judge the number, and it is kept in full with
the validation's record (Wizard.result, the manifest and the saved report data).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .report_pptx import FOOTER, _value_size, card_value, gauge_ticks

# The app's look, as in the PowerPoint: white page, soft cards, navy text, one blue
NAVY = colors.HexColor("#0F172A")
BLUE = colors.HexColor("#2563EB")
BLUE_TEXT = colors.HexColor("#1D4ED8")
GREY = colors.HexColor("#667085")
CARD = colors.HexColor("#F8F9FB")
LINE = colors.HexColor("#EAECF0")
PILL = colors.HexColor("#EEF3FE")
PILL_LINE = colors.HexColor("#D7E3FC")
MAYBE = colors.HexColor("#8FB0F6")
TRACK = colors.HexColor("#D0DCF8")
BAR_GREY = colors.HexColor("#CBD5E1")
WARN = colors.HexColor("#B42318")
WIDTH = A4[0] - 1.2 * inch  # the text column

_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13, textColor=NAVY)
_SMALL = ParagraphStyle("small", parent=_BODY, fontSize=8.5, leading=11, textColor=GREY)
_META = ParagraphStyle("meta", parent=_BODY, fontSize=10, leading=13, textColor=GREY)
_TITLE = ParagraphStyle("title", parent=_BODY, fontName="Helvetica-Bold", fontSize=22, leading=26,
                        spaceAfter=8)
_CAPS = ParagraphStyle("caps", parent=_BODY, fontName="Helvetica-Bold", fontSize=8, leading=11,
                       textColor=GREY, alignment=TA_CENTER, spaceBefore=2, spaceAfter=8)
_HEAD = ParagraphStyle("head", parent=_BODY, fontName="Helvetica-Bold", fontSize=12, leading=16,
                       spaceAfter=6)
_H2 = ParagraphStyle("h2", parent=_BODY, fontName="Helvetica-Bold", fontSize=17, leading=21,
                     spaceBefore=4, spaceAfter=10)
_WARN = ParagraphStyle("warn", parent=_BODY, fontName="Helvetica-Bold", textColor=WARN)


def _p(text: str, style: ParagraphStyle = _BODY) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def _grid(rows: list[list[Any]], widths: list[float], header: bool = True) -> Table:
    """A table as the app draws one: muted capitals over the columns, hairlines between rows."""
    if header:
        rows = [[c.upper() if isinstance(c, str) else c for c in rows[0]], *rows[1:]]
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
             ("TEXTCOLOR", (0, 0), (-1, -1), NAVY),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
             ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    if header:
        style += [("TEXTCOLOR", (0, 0), (-1, 0), GREY), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7)]
    t.setStyle(TableStyle(style))
    return t


class _Pill(Flowable):  # type: ignore[misc]
    """A word in a pale-blue pill: CAMERA VALIDATION."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def wrap(self, avail_w: float, avail_h: float) -> tuple[float, float]:
        return avail_w, 22

    def draw(self) -> None:
        c = self.canv
        w = c.stringWidth(self.text, "Helvetica-Bold", 8) + 24
        c.setFillColor(PILL)
        c.setStrokeColor(PILL_LINE)
        c.setLineWidth(0.6)
        c.roundRect(0, 2, w, 18, 9, stroke=1, fill=1)
        c.setFillColor(BLUE_TEXT)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(12, 8.2, self.text)


def _ticks(c: Canvas, cx: float, cy: float, r_out: float, lo: float | None, hi: float | None,
           width: float) -> None:
    """The accuracy gauge: half a ring of ticks (report_pptx.gauge_ticks)."""
    colour = {"on": BLUE, "maybe": MAYBE, "off": TRACK}
    ticks = gauge_ticks(lo, hi)
    c.setLineCap(1)
    c.setLineWidth(width)
    for k, state in enumerate(ticks):
        a = math.pi * (1 - (k + 0.5) / len(ticks))
        c.setStrokeColor(colour[state])
        c.line(cx + 0.74 * r_out * math.cos(a), cy + 0.74 * r_out * math.sin(a),
               cx + r_out * math.cos(a), cy + r_out * math.sin(a))


class _Cards(Flowable):  # type: ignore[misc]
    """Page 1's numbers, as on the PowerPoint's: per direction the count, the system's count,
    and the accuracy on a pale-blue card with its gauge."""

    GAP = 10

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__()
        rows, self.one = data["directions"], len(data["directions"]) == 1
        kind = str(data.get("count_label", "VERIFIED COUNT")).split()[0].title()
        complete = bool(data.get("complete", True))
        self.rows = []
        for r in rows:
            unsure, span = int(r.get("unsure") or 0), r.get("accuracy_range")
            label, value = card_value(r, complete)
            tag = f" · {r['label'].split()[-1]}"
            verified = f"{r['verified']}–{r['verified'] + unsure}" if unsure else str(r["verified"])
            lo, hi = span if unsure and span else (r.get("accuracy"), r.get("accuracy"))
            gauge = complete and label == "SENSOR ACCURACY"
            self.rows.append(((f"{kind} count{tag}", verified), (f"System count{tag}", str(r["system"])),
                              (f"{label.capitalize()}{tag}", value), (lo, hi) if gauge else None))
        self.card_h = (1.6 if self.one else 1.2) * inch

    def wrap(self, avail_w: float, avail_h: float) -> tuple[float, float]:
        self.width = avail_w
        return avail_w, len(self.rows) * self.card_h + (len(self.rows) - 1) * self.GAP

    def draw(self) -> None:
        c, h, gap = self.canv, self.card_h, self.GAP
        cw = (self.width - 2 * gap) / 3
        total = len(self.rows) * h + (len(self.rows) - 1) * gap
        for i, (*cards, gauge) in enumerate(self.rows):
            y = total - (i + 1) * h - i * gap
            for j, (label, value) in enumerate(cards):
                x, hi = j * (cw + gap), j == 2
                c.setFillColor(PILL if hi else CARD)
                c.setStrokeColor(PILL_LINE if hi else LINE)
                c.setLineWidth(0.6)
                c.roundRect(x, y, cw, h, 10, stroke=1, fill=1)
                c.setFillColor(BLUE_TEXT if hi else GREY)
                c.setFont("Helvetica-Bold", 8)
                c.drawString(x + 14, y + h - 22, label)
                c.setFillColor(NAVY)
                if hi and gauge:
                    r_out = min(cw / 2 - 16, h - 48)
                    cx, cy = x + cw / 2, y + (16 if self.one else 12)
                    _ticks(c, cx, cy, r_out, *gauge, 2.2 if self.one else 1.7)
                    size = _value_size(value, 20 if self.one else 13)
                    c.setFont("Helvetica-Bold", size)
                    c.drawCentredString(cx, cy + 6, value)
                    continue
                size = _value_size(value, 34 if self.one else 26)
                c.setFont("Helvetica-Bold", size)
                c.drawString(x + 14, y + h - 30 - size, value)


class _Bars(Flowable):  # type: ignore[misc]
    """Verified (blue) against the system (grey) per 15 minutes: rounded columns on a card."""

    def __init__(self, title: str, labels: list[str], verified: list[int],
                 system: list[int | None]) -> None:
        super().__init__()
        self.title, self.labels, self.verified, self.system = title, labels, verified, system

    def wrap(self, avail_w: float, avail_h: float) -> tuple[float, float]:
        self.width, self.height = avail_w, 2.1 * inch
        return self.width, self.height

    def draw(self) -> None:
        c, w, h = self.canv, float(self.width), float(self.height)
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.roundRect(0, 0, w, h, 10, stroke=1, fill=1)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(14, h - 22, self.title)
        x = w - 14
        for name, colour in (("System", BAR_GREY), ("Verified", BLUE)):  # the legend, from the right
            x -= c.stringWidth(name, "Helvetica", 8)
            c.setFillColor(GREY)
            c.setFont("Helvetica", 8)
            c.drawString(x, h - 21, name)
            x -= 12
            c.setFillColor(colour)
            c.roundRect(x, h - 22, 8, 8, 2, stroke=0, fill=1)
            x -= 14
        left, right, bottom, top = 36, w - 14, 24, h - 40
        vals = [v for v in [*self.verified, *self.system] if v is not None]
        raw = max(1, *vals) / 4
        p = 10.0 ** math.floor(math.log10(raw))
        step = next(m * p for m in (1, 1.5, 2, 2.5, 5, 10) if m * p >= raw)

        def y_of(v: float) -> float:
            return bottom + (top - bottom) * v / (4 * step)

        c.setFont("Helvetica", 7.5)
        for k in range(5):  # dashed gridlines, their values on the left
            yy = y_of(step * k)
            c.setStrokeColor(LINE)
            c.setDash(2, 3)
            c.line(left, yy, right, yy)
            c.setDash()
            c.setFillColor(GREY)
            c.drawRightString(left - 6, yy - 2.5, f"{step * k:g}")
        n = len(self.labels)
        slot, every = (right - left) / n, math.ceil(n / 10)
        bw = max(3.0, min(12.0, slot * 0.28))
        for i, (label, v, s) in enumerate(zip(self.labels, self.verified, self.system, strict=True)):
            cx = left + slot * (i + 0.5)
            for xb, val, colour in ((cx - bw - 1.2, v, BLUE), (cx + 1.2, s, BAR_GREY)):
                if val:
                    hb = y_of(val) - bottom
                    c.setFillColor(colour)
                    c.roundRect(xb, bottom, bw, hb, min(bw / 2, hb / 2), stroke=0, fill=1)
            if i % every == 0:
                c.setFillColor(GREY)
                c.drawCentredString(cx, bottom - 13, label)


def _picture(path: str, max_w: float, max_h: float) -> Image | None:
    p = Path(path)
    if not p.is_file():
        return None
    img = Image(str(p))
    scale = min(max_w / img.imageWidth, max_h / img.imageHeight)
    img.drawWidth, img.drawHeight = img.imageWidth * scale, img.imageHeight * scale
    return img


FRAME_MIN_H = 2.2 * inch  # a smaller frame shows nothing: it goes to the next page instead
FRAME_MAX_H = 4.2 * inch


class _Frame(Image):  # type: ignore[misc]
    """The busiest moment's picture, as large as the space left allows, up to FRAME_MAX_H;
    on a page with less than FRAME_MIN_H left, at full size on the next."""

    def __init__(self, path: str, reserve: float) -> None:
        super().__init__(path)
        self._reserve = reserve  # its title and caption, kept on the same page

    def wrap(self, avail_w: float, avail_h: float) -> tuple[float, float]:
        room = min(FRAME_MAX_H, avail_h - self._reserve)
        high = room if room >= FRAME_MIN_H else FRAME_MAX_H
        scale = min(avail_w / self.imageWidth, high / self.imageHeight)
        self.drawWidth, self.drawHeight = self.imageWidth * scale, self.imageHeight * scale
        return self.drawWidth, self.drawHeight


def _frame(data: dict[str, Any]) -> list[Any]:
    path = str(data.get("frame") or "")
    if not Path(path).is_file():
        return []
    title = str(data.get("frames_title") or "VALIDATION FRAMES — AUTOMATED DETECTION OVERLAY")
    # its title, the smallest useful picture and the caption on one page, or all on the next
    parts: list[Any] = [CondPageBreak(FRAME_MIN_H + 0.9 * inch), Spacer(1, 10), _p(title, _CAPS),
                        _Frame(path, 0.35 * inch)]
    if data.get("frame_caption"):
        parts.append(_p(data["frame_caption"], _SMALL))
    return parts


def _summary(data: dict[str, Any]) -> list[Any]:
    """Page 1: the numbers, the accuracy, and a picture of a busy moment.

    What the numbers rest on and what could not be known are worked out and kept with the
    validation's record; they are not printed for the customer.
    """
    name = " ".join(x for x in (data["store_name"], data["store_code"]) if x)
    out: list[Any] = [_p(f"{name} Traffic System", _TITLE), _Pill("CAMERA VALIDATION"),
                      Spacer(1, 6)]
    out.append(_p(f"Captured {data['captured_date']} · {data['location']} · "
                  f"{data['time_range']}", _META))
    out += [Spacer(1, 14), _Cards(data), Spacer(1, 10)]
    if not data.get("complete", True):
        out.append(_p("Validation incomplete: " + " ".join(data.get("incomplete") or [])
                      + " No accuracy is given.", _WARN))
    return [*out, *_frame(data)]


def _pct(v: Any) -> str:
    return "–" if v is None else f"{float(v):+.1f}%"


def _breakdown(data: dict[str, Any]) -> list[Any]:
    b = data.get("breakdown") or {}
    ivs, cams, dirs = b.get("intervals") or [], b.get("cameras") or [], b.get("dirs") or []
    levels = (data.get("levels") or {}).get("rows") or []
    if not ivs and not cams:
        return []
    out: list[Any] = [PageBreak(), _p("Traffic by 15 minutes" if ivs else "Traffic by camera", _H2)]
    if levels:
        out.append(_p("Error by traffic level", _HEAD))
        out.append(_grid([["Traffic level", "Intervals", "Verified", "System", "Error", "Bias", "WAPE"]]
                         + [[str(r["level"]), str(r["intervals"]), str(r["truth"]), str(r["system"]),
                             f"{int(r['error']):+d}", _pct(r.get("bias_pct")),
                             "–" if r.get("wape_pct") is None else f"{r['wape_pct']}%"]
                            for r in levels], [1.6 * inch] + [(WIDTH - 1.6 * inch) / 6] * 6))
        out.append(Spacer(1, 10))
    cols = [f"{d['label'].split()[-1]} {k}" for d in dirs for k in ("verified", "system", "diff.")]

    def rows_of(items: list[dict[str, Any]]) -> list[list[str]]:
        rows = []
        for it in items:
            row = [it["label"] + (" (part)" if it.get("partial") else "")]
            for d in dirs:
                acc = (it.get("accuracy") or {}).get(d["key"]) or {}
                sys_n = (it.get("sensor") or {}).get(d["key"])
                diff = "–" if acc.get("error") is None else (
                    f"{int(acc['error']):+d}" + ("" if acc.get("error_pct") is None
                                                 else f" ({acc['error_pct']:+.0f}%)"))
                row += [str(it["verified"][d["key"]]), "–" if sys_n is None else str(sys_n), diff]
            rows.append(row)
        return rows

    widths = [1.5 * inch] + [(WIDTH - 1.5 * inch) / max(1, len(cols))] * len(cols)
    if ivs and any(it.get("sensor") for it in ivs):  # as in the PowerPoint: a chart per direction
        for d in dirs:
            out += [_Bars(d["label"], [it["label"].split(" - ")[0] for it in ivs],
                          [it["verified"][d["key"]] for it in ivs],
                          [(it.get("sensor") or {}).get(d["key"]) for it in ivs]), Spacer(1, 10)]
    if ivs:
        out += [_grid([["Time", *cols], *rows_of(ivs)], widths), Spacer(1, 10)]
    if cams:
        out += [_grid([["Camera", *cols], *rows_of(cams)], widths), Spacer(1, 10)]
    out += [_p(n, _SMALL) for n in b.get("notes") or []]
    return out


def _details(data: dict[str, Any]) -> list[Any]:
    out: list[Any] = [PageBreak(), _p("Crossing details", _H2)]
    rows = data.get("crossings") or []
    if not rows:
        return [*out, _p(data.get("crossings_note")
                         or "No crossings were verified for this period.")]
    out.append(_grid([["#", "Time", "Camera", "Direction", "How it was found"]]
                     + [[str(r["n"]), r["time"], r["camera"], r["direction"], _p(r["found"], _SMALL)]
                        for r in rows],
                     [0.45 * inch, 0.9 * inch, 1.5 * inch, 1.0 * inch, WIDTH - 3.85 * inch]))
    return out


def _snapshots(data: dict[str, Any]) -> list[Any]:
    thumbs = data.get("thumbs") or []
    if not thumbs:
        return []
    cell = WIDTH / 3
    cells: list[Any] = []
    for th in thumbs:
        pic = _picture(th["path"], cell - 8, 1.9 * inch)
        cells.append([pic, _p(th["label"], _SMALL)] if pic is not None else [_p(th["label"], _SMALL)])
    grid = [cells[k:k + 3] + [""] * (3 - len(cells[k:k + 3])) for k in range(0, len(cells), 3)]
    return [PageBreak(), _p("Crossing snapshots", _H2),
            Table(grid, colWidths=[cell] * 3, style=[("VALIGN", (0, 0), (-1, -1), "TOP")])]


def build_pdf(data: dict[str, Any], out: Path, logo: Path | None = None,
              compress: bool = True) -> Path:
    """Write the report as an A4 PDF. compress=False keeps its text readable in the file."""
    out.parent.mkdir(parents=True, exist_ok=True)

    class Numbered(Canvas):  # type: ignore[misc]  # "Page k of N": the total is only known once every page is laid out
        def __init__(self, *args: Any, **kw: Any) -> None:
            super().__init__(*args, **kw)
            self._pages: list[dict[str, Any]] = []

        def showPage(self) -> None:  # reportlab's name
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            for state in self._pages:
                self.__dict__.update(state)
                self._decorate(len(self._pages))
                super().showPage()
            super().save()

        def _decorate(self, total: int) -> None:
            w, h = A4
            if logo is not None and logo.is_file():
                self.drawImage(str(logo), 0.6 * inch, h - 0.95 * inch, width=1.6 * inch,
                               height=0.55 * inch, preserveAspectRatio=True, mask="auto")
            self.setFillColor(BLUE)
            self.setFont("Helvetica-Bold", 8.5)
            self.drawRightString(w - 0.6 * inch, h - 0.5 * inch, "OPTIMIZATION REPORT")
            date = str(data.get("report_date", ""))  # in a chip, like the app's date
            chip = self.stringWidth(date, "Helvetica", 9) + 20
            self.setStrokeColor(LINE)
            self.setFillColor(colors.white)
            self.setLineWidth(0.6)
            self.roundRect(w - 0.6 * inch - chip, h - 0.86 * inch, chip, 18, 9, stroke=1, fill=1)
            self.setFillColor(NAVY)
            self.setFont("Helvetica", 9)
            self.drawCentredString(w - 0.6 * inch - chip / 2, h - 0.86 * inch + 5.8, date)
            self.line(0.6 * inch, h - 1.02 * inch, w - 0.6 * inch, h - 1.02 * inch)
            self.line(0.6 * inch, 0.64 * inch, w - 0.6 * inch, 0.64 * inch)
            self.setFillColor(GREY)
            self.setFont("Helvetica", 8.5)
            vid = (data.get("identity") or {}).get("validation_id") or ""
            self.drawString(0.6 * inch, 0.45 * inch,
                            f"{FOOTER}  ·  {vid}" if vid else FOOTER)
            self.drawRightString(w - 0.6 * inch, 0.45 * inch,
                                 f"Page {self._pageNumber} of {total}")

    name = " ".join(x for x in (data["store_name"], data["store_code"]) if x)
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=1.15 * inch, bottomMargin=0.8 * inch,
                            title=f"{name} camera validation",
                            author="CrossingCount", pageCompression=1 if compress else 0)
    doc.build([*_summary(data), *_breakdown(data), *_details(data), *_snapshots(data)],
              canvasmaker=Numbered)
    return out
