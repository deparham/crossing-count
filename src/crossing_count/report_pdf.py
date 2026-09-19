"""The validation report as a PDF, from the same data as the PowerPoint.

The wizard gathers what a report says once (Wizard.report_data); report_pptx.py and this
module only lay it out. Page 1 is written for someone who will not read the method: what
the system did, in people, where and how sure, with what could not be determined. The
method, the crossings and the snapshots follow, as in the PowerPoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    CondPageBreak,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .report_pptx import FOOTER, card_value, identity_rows

NAVY = colors.HexColor("#101D3B")
TEAL = colors.HexColor("#1F9BB3")
GREY = colors.HexColor("#4A5568")
CARD = colors.HexColor("#EEF4F6")
ROW = colors.HexColor("#F5F8F9")
WARN = colors.HexColor("#B03A2E")
WIDTH = A4[0] - 1.2 * inch  # the text column

_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13, textColor=NAVY)
_SMALL = ParagraphStyle("small", parent=_BODY, fontSize=8.5, leading=11, textColor=GREY)
_TITLE = ParagraphStyle("title", parent=_BODY, fontName="Times-Bold", fontSize=22, leading=26)
_TAG = ParagraphStyle("tag", parent=_BODY, fontName="Helvetica-Bold", fontSize=9, textColor=TEAL,
                      spaceBefore=2, spaceAfter=10)
_HEAD = ParagraphStyle("head", parent=_BODY, fontName="Helvetica-Bold", fontSize=13, leading=17,
                       spaceAfter=6)
_H2 = ParagraphStyle("h2", parent=_BODY, fontName="Times-Bold", fontSize=16, leading=20,
                     spaceBefore=6, spaceAfter=8)
_WARN = ParagraphStyle("warn", parent=_BODY, fontName="Helvetica-Bold", textColor=WARN)


def _p(text: str, style: ParagraphStyle = _BODY) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def _grid(rows: list[list[Any]], widths: list[float], header: bool = True) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
             ("TEXTCOLOR", (0, 0), (-1, -1), NAVY),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, ROW]),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5)]
    t.setStyle(TableStyle(style))
    return t


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
    parts: list[Any] = [CondPageBreak(FRAME_MIN_H + 0.9 * inch), Spacer(1, 10), _p(title, _TAG),
                        _Frame(path, 0.35 * inch)]
    if data.get("frame_caption"):
        parts.append(_p(data["frame_caption"], _SMALL))
    return parts


def _summary(data: dict[str, Any]) -> list[Any]:
    """Page 1: the result in plain words, then how sure, then what it rests on."""
    out: list[Any] = [_p(f"{data['store_name']} {data['store_code']} Traffic System", _TITLE),
                      _p("CAMERA VALIDATION", _TAG)]
    out += [_p(t, _HEAD) for t in data.get("headline") or []]
    ident = identity_rows(data)
    if ident:
        out.append(_grid([[_p(k, _SMALL), _p(v, _SMALL)] for k, v in ident],
                         [1.2 * inch, WIDTH - 1.2 * inch], header=False))
        out.append(Spacer(1, 8))
    out.append(_p(f"Captured {data['captured_date']} · {data['location']} · "
                  f"{data['time_range']}", _SMALL))
    out.append(Spacer(1, 8))
    kind = str(data.get("count_label", "VERIFIED COUNT")).split()[0].title()
    rows: list[list[Any]] = [["", f"{kind} count", "System count", "Result"]]
    for r in data["directions"]:
        unsure = int(r.get("unsure") or 0)
        verified = f"{r['verified']}–{r['verified'] + unsure}" if unsure else str(r["verified"])
        label, value = card_value(r, bool(data.get("complete", True)))
        rows.append([r["label"], verified, str(r["system"]), f"{value}  ({label.title()})"])
    table = _grid(rows, [1.4 * inch, 1.3 * inch, 1.3 * inch, WIDTH - 4.0 * inch])
    table.setStyle(TableStyle([("FONT", (1, 1), (-1, -1), "Helvetica-Bold", 11)]))
    out += [table, Spacer(1, 8)]
    for t in [data.get("scope"), *(data.get("sample_notes") or []),
              (data.get("levels") or {}).get("text")]:
        if t:
            out.append(_p(t))
    for t in data.get("caveats") or []:
        out.append(_p(t, _WARN))
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
    if ivs:
        out += [_grid([["Time", *cols], *rows_of(ivs)], widths), Spacer(1, 10)]
    if cams:
        out += [_grid([["Camera", *cols], *rows_of(cams)], widths), Spacer(1, 10)]
    out += [_p(n, _SMALL) for n in b.get("notes") or []]
    return out


def _details(data: dict[str, Any]) -> list[Any]:
    out: list[Any] = [PageBreak(), _p("How this was counted", _H2)]
    out += [_p(line) for line in data.get("method") or []]
    out += [Spacer(1, 10), _p("Crossing details", _H2)]
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
            self.setFillColor(TEAL)
            self.setFont("Helvetica-Bold", 9)
            self.drawRightString(w - 0.6 * inch, h - 0.55 * inch, "OPTIMIZATION REPORT")
            self.setFillColor(GREY)
            self.setFont("Helvetica", 9)
            self.drawRightString(w - 0.6 * inch, h - 0.75 * inch, str(data.get("report_date", "")))
            self.drawString(0.6 * inch, 0.45 * inch, FOOTER)
            self.drawRightString(w - 0.6 * inch, 0.45 * inch,
                                 f"Page {self._pageNumber} of {total}")

    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=1.15 * inch, bottomMargin=0.8 * inch,
                            title=f"{data['store_name']} {data['store_code']} camera validation",
                            author="CrossingCount", pageCompression=1 if compress else 0)
    doc.build([*_summary(data), *_breakdown(data), *_details(data), *_snapshots(data)],
              canvasmaker=Numbered)
    return out
