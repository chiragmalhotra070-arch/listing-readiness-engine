#!/usr/bin/env python3
"""Generate the listing demo document set (v2 realistic fixtures).

Creates 24 synthetic PDFs under demo_documents/:
- templates/ : blank field-structure templates (one per listing document type)
- completed/ : filled dummy documents with consistent fictional data

v2 replaces the v1 one-pagers with structurally realistic, multi-page
documents: paragraphed sections, checkbox matrices, terms grids, budget and
payment tables, signature blocks, per-page initials, and (for the completed
TDS/SPQ) visible hand-completion -- while keeping the v1 contract:

- same CLI surface: python demo_documents/generate_demo_documents.py
- same filenames: template-<key>.pdf / demo-<key>.pdf (the demo intake UI
  sample pack and _DEMO_FIXTURE_PROFILES are keyed by exact filename)
- completed documents carry the FICTIONAL TRAINING DATA marker so the mock
  LLM's demo-fixture profiles recognize them during demos
- templates carry the SYNTHETIC TEMPLATE disclaimer and never the marker
- deterministic content: re-running reproduces the same documents

COPYRIGHT BOUNDARY: the CAR forms (RLA, SA, AD, TDS, SPQ, AVID, WCMD) and
NHD-style reports are copyrighted / members-only material.  These documents
recreate STRUCTURE only -- section topics, field labels, matrices, page flow
-- in our own words.  No CAR or vendor prose is copied.  Statute-driven
topics (transfer disclosure, agency disclosure, lead, CO detectors) are
paraphrased around the required content.  Real HOA packages run 50-300+
pages; ours is a representative subset.  The SPQ special-topics pages are
best-effort approximations (see the builder comment).

Requires: pip install reportlab
"""

from __future__ import annotations

import math
import os
import random

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

# ---------------------------------------------------------------- constants

MARKER = "FICTIONAL TRAINING DATA"
COMPLETED_FOOTER = (
    "SYNTHETIC DEMO DOCUMENT \u2014 fictional data for demonstration only. "
    "Not an official California Association of Realtors form."
)
TEMPLATE_FOOTER = (
    "SYNTHETIC TEMPLATE \u2014 field structure for demo purposes only. "
    "Not an official California Association of Realtors form."
)

# Shared fictional identity -- must stay identical across all 12 documents.
ADDRESS = "123 Main St, Pasadena, CA 91101"
APN = "5842-018-024"
SELLER = "Jane Seller"
AGENT = "Alex Agent"
BROKERAGE = "Demo Realty"
AGENT_DRE = "01874567"
BROKER_DRE = "01998765"
LIST_DATE = "2026-09-15"
TDS_DATE = "2026-09-16"
AVID_DATE = "2026-09-17"
NHD_DATE = "2026-09-14"
TITLE_DATE = "2026-09-18"
HOA_DATE = "2026-09-20"
SOLAR_DATE = "2024-05-01"
LIST_PRICE = "$1,250,000"
LIST_END = "2027-03-15"
HOA_NAME = "Pasadena Oaks HOA"
TITLE_CO = "Demo Title Co"
SOLAR_PROVIDER = "SunRun"
LEGAL = (
    "Lot 24 of Block 7 of Elmwood Vista Tract, in the City of Pasadena, "
    "County of Los Angeles, State of California, per map recorded in Book 15, "
    "Page 82 of Maps, in the office of the County Recorder of said county."
)

PAGE_W, PAGE_H = LETTER
LEFT = 54
TOP = 58
BOTTOM = 62
RIGHT_MARGIN = 54
WIDTH = PAGE_W - LEFT - RIGHT_MARGIN

PEN = colors.Color(0.12, 0.16, 0.55)
INK = colors.black
RULE = colors.Color(0.45, 0.45, 0.48)
GRID = colors.Color(0.55, 0.55, 0.58)
HDR_BG = colors.Color(0.90, 0.90, 0.93)
STAMP = colors.Color(0.72, 0.10, 0.10)

_STYLES = {
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.3, leading=12.4),
    "bodyb": ParagraphStyle("bodyb", fontName="Helvetica-Bold", fontSize=9.3, leading=12.4),
    "bodyi": ParagraphStyle("bodyi", fontName="Helvetica-Oblique", fontSize=9.3, leading=12.4),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8.2, leading=10.6),
    "smallb": ParagraphStyle("smallb", fontName="Helvetica-Bold", fontSize=8.2, leading=10.6),
    "smalli": ParagraphStyle("smalli", fontName="Helvetica-Oblique", fontSize=8.2, leading=10.6),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15.5, leading=18.5),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10.6, leading=13.4),
    "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=9.6, leading=12.4),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.2, leading=10.4),
    "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.2, leading=10.4),
}


# ------------------------------------------------------------ numbered pages


class NumberedCanvas(canvas.Canvas):
    """Canvas that stamps 'Page N of M' plus the disclaimer footer on save."""

    def __init__(self, *args, disclaimer: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages: list[dict] = []
        self._disclaimer = disclaimer

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for index, state in enumerate(self._saved_pages):
            self.__dict__.update(state)
            self.setFont("Helvetica", 7)
            self.setFillColor(colors.Color(0.25, 0.25, 0.28))
            self.drawCentredString(PAGE_W / 2, 40, self._disclaimer)
            self.setFont("Helvetica", 7.5)
            self.drawRightString(PAGE_W - RIGHT_MARGIN, 27, f"Page {index + 1} of {total}")
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)


# --------------------------------------------------------------- layout engine


class Doc:
    """Small flow-layout helper over a reportlab canvas."""

    def __init__(self, path: str, *, completed: bool, title: str, seed: int):
        self.completed = completed
        self.title = title
        self.disclaimer = COMPLETED_FOOTER if completed else TEMPLATE_FOOTER
        self.c = NumberedCanvas(path, pagesize=LETTER, disclaimer=self.disclaimer)
        self.rng = random.Random(seed)
        self.y = PAGE_H - TOP
        self._initials = False
        if completed:
            self._stamp_marker()

    # -- plumbing -----------------------------------------------------------

    def _bottom(self) -> float:
        return BOTTOM + (20 if self._initials else 0)

    def _stamp_marker(self) -> None:
        w = stringWidth(MARKER, "Helvetica-Bold", 9)
        self.c.setFillColor(STAMP)
        self.c.setFont("Helvetica-Bold", 9)
        self.c.drawString(PAGE_W - RIGHT_MARGIN - w, PAGE_H - 45, MARKER)
        self.c.setFillColor(INK)

    def new_page(self) -> None:
        self.c.showPage()
        self.y = PAGE_H - TOP
        self.c.setFillColor(colors.Color(0.3, 0.3, 0.33))
        self.c.setFont("Helvetica", 8)
        self.c.drawString(LEFT, PAGE_H - 34, self.title)
        self.c.setFont("Helvetica-Oblique", 8)
        self.c.drawRightString(PAGE_W - RIGHT_MARGIN, PAGE_H - 34, "(synthetic)")
        self.c.setFillColor(INK)
        if self._initials:
            self._initials_line()

    def _ensure(self, h: float) -> None:
        if self.y - h < self._bottom():
            self.new_page()

    def initials(self) -> None:
        """Per-page seller/broker initial lines (RLA, SPQ)."""
        self._initials = True
        self._initials_line()

    def _initials_line(self) -> None:
        y = BOTTOM + 12
        self.c.setFillColor(colors.Color(0.3, 0.3, 0.33))
        self.c.setFont("Helvetica", 7.5)
        self.c.drawString(LEFT, y, "Initials (seller): ____________")
        self.c.drawString(LEFT + 200, y, "Initials (broker): ____________")
        self.c.setFillColor(INK)

    def finish(self) -> None:
        self.c.showPage()
        self.c.save()

    # -- text ---------------------------------------------------------------

    def _para(self, text: str, style: str, *, indent: float = 0) -> float:
        avail = WIDTH - indent
        p = Paragraph(text, _STYLES[style])
        _, h = p.wrap(avail, 10_000)
        self._ensure(h + 2)
        p.drawOn(self.c, LEFT + indent, self.y - h)
        self.y -= h
        return h

    def p(self, text: str, *, style: str = "body", indent: float = 0, gap: float = 5) -> None:
        self._para(text, style, indent=indent)
        self.y -= gap

    def note(self, text: str, *, gap: float = 5) -> None:
        self.p(text, style="smalli", gap=gap)

    def h1(self, text: str, *, sub: str | None = None, gap: float = 8) -> None:
        self._para(text, "h1")
        self.y -= 3
        if sub:
            self._para(sub, "smalli")
        self.y -= gap

    def h2(self, text: str, *, rule: bool = True, gap_before: float = 8, gap: float = 5) -> None:
        self._ensure(gap_before + 20)
        self.y -= gap_before
        self._para(text, "h2")
        if rule:
            self.c.setStrokeColor(RULE)
            self.c.setLineWidth(0.7)
            self.c.line(LEFT, self.y - 1, LEFT + WIDTH, self.y - 1)
            self.y -= 3
        self.y -= gap

    def h3(self, text: str, *, gap_before: float = 5, gap: float = 3) -> None:
        self._ensure(gap_before + 15)
        self.y -= gap_before
        self._para(text, "h3")
        self.y -= gap

    def spacer(self, h: float = 8) -> None:
        self.y -= h

    def rule(self, *, gap: float = 6) -> None:
        self._ensure(6)
        self.c.setStrokeColor(RULE)
        self.c.setLineWidth(0.6)
        self.c.line(LEFT, self.y - 3, LEFT + WIDTH, self.y - 3)
        self.y -= 3 + gap

    def page_break(self) -> None:
        self.new_page()

    # -- fields -------------------------------------------------------------

    def kv(self, label: str, value: str | None, *, x: float | None = None) -> None:
        """Single-line label/value; None (template) renders a fill rule."""
        x = LEFT if x is None else x
        font = "Helvetica-Bold"
        lw = stringWidth(f"{label}: ", font, 9)
        self.c.setFont(font, 9)
        self.c.drawString(x, self.y - 9, f"{label}:")
        vx = x + lw
        if value is None:
            self.c.setStrokeColor(RULE)
            rule_w = min(240, PAGE_W - RIGHT_MARGIN - vx)
            self.c.line(vx + 2, self.y - 10, vx + 2 + rule_w, self.y - 10)
        else:
            self.c.setFont("Helvetica", 9)
            self.c.drawString(vx + 2, self.y - 9, value)
        self.y -= 14

    def kv2(self, a: tuple[str, str | None], b: tuple[str, str | None]) -> None:
        """Two label/value pairs on one row."""
        x1, x2 = LEFT, LEFT + WIDTH / 2 + 6
        y0 = self.y
        for (label, value), x, limit in (
            (a, x1, x2 - 14),
            (b, x2, PAGE_W - RIGHT_MARGIN),
        ):
            font = "Helvetica-Bold"
            lw = stringWidth(f"{label}: ", font, 9)
            self.c.setFont(font, 9)
            self.c.drawString(x, y0 - 9, f"{label}:")
            vx = x + lw
            if value is None:
                self.c.setStrokeColor(RULE)
                self.c.line(vx + 2, y0 - 10, min(vx + 150, limit), y0 - 10)
            else:
                self.c.setFont("Helvetica", 9)
                self.c.drawString(vx + 2, y0 - 9, value)
        self.y = y0 - 14

    def party_block(self) -> None:
        """Standard property/parties block -- blanks in templates."""
        val = (lambda v: v) if self.completed else (lambda v: None)
        self.kv("Property address", val(ADDRESS))
        self.kv2(("APN (Assessor's Parcel Number)", val(APN)), ("Seller", val(SELLER)))

    def fill_val(self, value: str) -> str | None:
        return value if self.completed else None

    # -- checkboxes ---------------------------------------------------------

    def _box(self, x: float, y: float, checked: bool, *, pen: bool) -> None:
        s = 8.6
        self.c.setStrokeColor(INK)
        self.c.setLineWidth(0.7)
        self.c.rect(x, y, s, s, stroke=1, fill=0)
        if checked:
            if pen:
                self.c.setStrokeColor(PEN)
                self.c.setLineWidth(1.1)
                j = self.rng.uniform(-0.8, 0.8)
                self.c.line(x - 1.4, y - 1.2, x + s + 1.6 + j, y + s + 1.4)
                self.c.line(x + s + 1.2, y - 1.4 + j, x - 1.6, y + s + 1.2)
                self.c.setStrokeColor(INK)
                self.c.setLineWidth(0.7)
            else:
                self.c.setLineWidth(0.9)
                self.c.line(x + 1.6, y + 1.6, x + s - 1.4, y + s - 1.4)
                self.c.line(x + s - 1.6, y + 1.6, x + 1.4, y + s - 1.4)
                self.c.setLineWidth(0.7)

    def check(self, label: str, state: bool | None = None, *, indent: float = 0, pen: bool = False) -> None:
        checked = bool(state) if self.completed else False
        self._ensure(14)
        y = self.y - 10
        x = LEFT + indent
        self._box(x, y, checked, pen=pen and checked)
        self.c.setFont("Helvetica", 9)
        self.c.drawString(x + 13, y + 1.6, label)
        self.y -= 13.6

    def check_cols(
        self,
        items: list[tuple[str, bool | None, bool]],
        cols: int = 2,
        *,
        indent: float = 0,
    ) -> None:
        """Grid of checkbox items. Item = (label, state, pen)."""
        col_w = (WIDTH - indent) / cols
        rows = math.ceil(len(items) / cols)
        self._ensure(rows * 13.6 + 2)
        for i, (label, state, pen) in enumerate(items):
            r, cix = divmod(i, cols)
            checked = bool(state) if self.completed else False
            x = LEFT + indent + cix * col_w
            y = self.y - 10 - r * 13.6
            self._box(x, y, checked, pen=pen and checked)
            self.c.setFont("Helvetica", 8.6)
            self.c.drawString(x + 12.5, y + 1.4, label)
        self.y -= rows * 13.6 + 2

    def yn(
        self,
        question: str,
        answer: bool | None,
        *,
        pen: bool = False,
        comment: str | None = None,
        indent: float = 0,
    ) -> None:
        """Question with right-side Yes/No boxes; completed may carry a pen answer."""
        ans_x = LEFT + WIDTH - 96
        q_w = ans_x - (LEFT + indent) - 10
        para = Paragraph(question, _STYLES["body"])
        _, qh = para.wrap(q_w, 10_000)
        self._ensure(max(qh, 15) + (11 if (comment and self.completed) else 0) + 2)
        top = self.y
        para.drawOn(self.c, LEFT + indent, top - qh)
        yb = top - 13
        self.c.setFont("Helvetica", 9)
        self.c.drawString(ans_x, yb, "Yes")
        self.c.drawString(ans_x + 44, yb, "No")
        mark_yes = bool(answer) if self.completed else False
        mark_no = (not bool(answer)) if (self.completed and answer is not None) else False
        self._box(ans_x + 22, yb - 0.5, mark_yes, pen=pen and mark_yes)
        self._box(ans_x + 64, yb - 0.5, mark_no, pen=pen and mark_no)
        if pen and self.completed and comment and comment.lower().startswith("corrected"):
            # Strike through the mis-marked side before the pen correction.
            pass
        self.y -= max(qh, 15) + 4
        if comment:
            if self.completed:
                self._pen_line(comment, indent=indent + 14)
            else:
                self.c.setStrokeColor(RULE)
                self.c.setDash([1, 3], 0)
                self.c.line(LEFT + indent + 14, self.y - 4, LEFT + WIDTH - 20, self.y - 4)
                self.c.setDash([], 0)
                self.y -= 13

    # -- pen / messy --------------------------------------------------------

    def _pen_line(self, text: str, *, indent: float = 14, size: float = 8.8) -> None:
        """Handwritten-style annotation (completed documents only)."""
        self._ensure(size + 6)
        self.c.saveState()
        angle = self.rng.uniform(-1.5, 1.5)
        self.c.translate(LEFT + indent, self.y - size)
        self.c.rotate(angle)
        self.c.setFillColor(PEN)
        self.c.setFont("Times-Italic", size)
        self.c.drawString(0, 0, text)
        self.c.restoreState()
        self.c.setFillColor(INK)
        self.y -= size + 4

    def pen_note(self, text: str | None, *, indent: float = 14, size: float = 8.8) -> None:
        """Comment line: handwriting when completed, dotted rule when blank."""
        if self.completed and text:
            self._pen_line(text, indent=indent, size=size)
        else:
            self._ensure(12)
            self.c.setStrokeColor(RULE)
            self.c.setDash([1, 3], 0)
            self.c.line(LEFT + indent, self.y - 5, LEFT + WIDTH - 20, self.y - 5)
            self.c.setDash([], 0)
            self.y -= 12

    def hand_strike(self, x: float, y: float, w: float) -> None:
        if not self.completed:
            return
        self.c.setStrokeColor(PEN)
        self.c.setLineWidth(1.0)
        self.c.line(x - 2, y - 2, x + w + 3, y + 2.4)
        self.c.setStrokeColor(INK)
        self.c.setLineWidth(0.7)

    # -- tables -------------------------------------------------------------

    def table(
        self,
        rows: list[list[str]],
        widths: list[float],
        *,
        header: bool = True,
        fs: float = 8.2,
        pad: float = 3.2,
        gap: float = 7,
    ) -> None:
        data = []
        for i, row in enumerate(rows):
            style = (
                _STYLES["cellb"]
                if (header and i == 0)
                else ParagraphStyle(f"cell{fs}x{i}", parent=_STYLES["cell"], fontSize=fs, leading=fs + 2.2)
            )
            data.append([Paragraph(str(cell), style) for cell in row])
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        cmds: list = [
            ("GRID", (0, 0), (-1, -1), 0.5, GRID),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), pad),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("TOPPADDING", (0, 0), (-1, -1), pad - 0.6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad - 0.6),
        ]
        if header:
            cmds.append(("BACKGROUND", (0, 0), (-1, 0), HDR_BG))
        t.setStyle(TableStyle(cmds))
        _, h = t.wrap(WIDTH, 10_000)
        self._ensure(h + 4)
        t.drawOn(self.c, LEFT, self.y - h)
        self.y -= h + gap

    # -- signatures ---------------------------------------------------------

    def sig(
        self,
        name: str | None,
        role: str,
        *,
        dre: str | None = None,
        signed: bool = False,
        x: float | None = None,
        w: float = 240,
        date_value: str | None = None,
    ) -> None:
        """Signature rule with printed name and date below.

        name=None renders an intentionally blank party line (buyer slots).
        signed controls whether a pen signature/date is drawn when completed.
        """
        x = LEFT if x is None else x
        self._ensure(44)
        y = self.y - 10
        self.c.setStrokeColor(INK)
        self.c.setLineWidth(0.7)
        self.c.line(x, y, x + w, y)
        if self.completed and signed and name:
            self.c.saveState()
            self.c.setFillColor(PEN)
            self.c.setFont("Times-Italic", 11.5)
            self.c.translate(x + 8, y + 3)
            self.c.rotate(self.rng.uniform(-1.8, 1.0))
            self.c.drawString(0, 0, name)
            self.c.restoreState()
            self.c.setFillColor(INK)
        elif name and not (self.completed and signed):
            self.c.setFont("Helvetica", 7.5)
            self.c.setFillColor(colors.Color(0.4, 0.4, 0.43))
            self.c.drawString(x + 4, y + 3, f"({name})")
            self.c.setFillColor(INK)
        self.c.setFont("Helvetica", 7.8)
        self.c.drawString(x, y - 10, role + (f"   DRE Lic. #{dre}" if dre else ""))
        dy = y - 24
        self.c.setFont("Helvetica", 7.8)
        self.c.drawString(x, dy, "Date:")
        date_x = x + stringWidth("Date:", "Helvetica", 7.8) + 4
        if self.completed and signed and date_value:
            self.c.setFillColor(PEN)
            self.c.setFont("Times-Italic", 9)
            self.c.drawString(date_x, dy - 0.5, date_value)
            self.c.setFillColor(INK)
        self.c.setStrokeColor(RULE)
        self.c.setLineWidth(0.6)
        self.c.line(date_x, dy - 3, x + w, dy - 3)
        self.c.setStrokeColor(INK)
        self.c.setLineWidth(0.7)
        self.y = y - 34

    def sig_row(self, sigs: list[dict]) -> None:
        """Draw several signature blocks side by side on one row."""
        y0 = self.y
        for kw in sigs:
            self.y = y0
            self.sig(**kw)
        self.y = y0 - 44

    def title_block(self, form_line: str, *, doc_no: str | None = None) -> None:
        """Standard first-page masthead for every document."""
        self.h1(self.title, sub=form_line)
        self.c.setFont("Helvetica", 7.5)
        self.c.setFillColor(colors.Color(0.3, 0.3, 0.33))
        right = doc_no or "Synthetic listing-document fixture"
        self.c.drawRightString(PAGE_W - RIGHT_MARGIN, PAGE_H - TOP - 12, right)
        self.c.setFillColor(INK)
        if not self.completed:
            self.note(TEMPLATE_FOOTER, gap=4)
        else:
            self.note("Prepared for demonstration. " + COMPLETED_FOOTER, gap=4)


# ------------------------------------------------------------------ builders
# (paraphrased structure; see module docstring for the copyright boundary)


def build_listing_agreement(d: Doc) -> None:
    d.initials()
    d.title_block(
        "Exclusive Right to Sell \u2014 synthetic template modeled on the CAR Residential Listing Agreement",
        doc_no=f"Doc date: {LIST_DATE}",
    )
    v = d.fill_val
    d.party_block()
    d.kv2(("Listing price", v(LIST_PRICE)), ("Effective date", v(LIST_DATE)))
    d.kv2(("Expiration of listing", v(LIST_END)), ("Exclusive right to sell", v("Yes")))
    d.spacer(2)
    d.table(
        [
            ["Terms grid", "Value"],
            ["Property / APN", f"{v(ADDRESS) or '____'} / {v(APN) or '____'}"],
            ["Seller", v(SELLER) or "____"],
            ["Listing agent / broker", f"{v(AGENT) or '____'} \u2014 {v(BROKERAGE) or '____'}"],
            ["Price / term", f"{v(LIST_PRICE) or '____'}  \u00b7  {v(LIST_DATE) or '____'} to {v(LIST_END) or '____'}"],
        ],
        [150, WIDTH - 150],
    )
    d.h2("1. Grant of exclusive right to sell")
    d.p(
        "Seller grants Broker the exclusive authority to find a buyer ready, willing, and able to purchase the "
        "Property on the terms stated in this Agreement during the listing period. Seller will not grant the "
        "same authority to another broker for the same property during this period, and Seller's own efforts "
        "to find a buyer do not by themselves terminate Broker's right to compensation under Section 5."
    )
    d.h2("2. Broker's services")
    d.p(
        "Broker will market the Property through the services Broker ordinarily provides for residential "
        "listings, including preparation of listing materials, coordination of showings, and submission to "
        "the multiple listing service if Broker participates in one. Broker may assign licensees and "
        "unlicensed staff to perform parts of this work; Broker remains responsible for the listing."
    )
    d.h2("3. Seller's principal obligations")
    d.p(
        "Seller will keep the Property in reasonable condition for showings, will disclose to Broker known "
        "material facts affecting the Property or its value, will make title documents and service records "
        "available on request, and will cooperate in good faith with offers Broker presents. Seller will "
        "notify Broker promptly of any change in ownership, lien status, or occupancy."
    )
    d.page_break()
    d.h2("4. Condition of the Property and disclosures")
    d.p(
        "Seller is responsible for the disclosures Seller is required by law to make, including the transfer "
        "disclosure statement and any lead-based paint disclosure for pre-1978 housing. Broker will provide "
        "the statutory forms and will deliver copies of buyer-signed acknowledgments to Seller. Nothing in "
        "this Section makes Broker the guarantor of the Property's condition."
    )
    d.h2("5. Broker compensation")
    d.p(
        "If a sale closes during the listing period or within any protective period stated in Section 8, "
        "Seller will pay Broker the compensation agreed below. Compensation is a negotiated amount for "
        "services rendered and is not tied to the agency relationship Broker holds. Any cooperating-broker "
        "share is paid out of Broker's compensation."
    )
    d.table(
        [
            ["Compensation item", "Amount / rate"],
            ["Total compensation on sale", "____% of purchase price, or $____ minimum"],
            ["Cooperating broker share", "____% of total compensation"],
            ["Compensation due if Seller terminates without cause", "$____"],
            ["Protective period after listing expiration", "____ days"],
        ],
        [250, WIDTH - 250],
    )
    d.h2("6. Multiple listing service participation")
    d.p(
        "If Broker submits the listing to a multiple listing service, the listing will be governed by that "
        "service's rules in addition to this Agreement. Withdrawal or expiration of the listing under those "
        "rules does not waive compensation that has already been earned."
    )
    d.h2("7. Access, lockbox and showings")
    d.p(
        "Seller authorizes Broker to install a lockbox and to admit prospective buyers, cooperating "
        "licensees, and inspectors at reasonable hours, with notice to Seller where practicable. Seller may "
        "set blackout dates or other reasonable showing conditions by written notice to Broker."
    )
    d.page_break()
    d.h2("8. Additional or special provisions")
    d.p(
        "Any special term the parties agree to reduce to writing and initial here controls over the general "
        "terms of this Agreement. Verbal side agreements have no effect under this Section."
    )
    d.table(
        [
            ["Special provision", "Party initials"],
            ["(space for negotiated special terms)", "______ / ______"],
            ["(space for negotiated special terms)", "______ / ______"],
        ],
        [WIDTH - 140, 140],
    )
    d.h2("9. Protective provisions: title, liens and possession")
    d.p(
        "Seller will deliver marketable title free of liens or encumbrances not accepted by the buyer, will "
        "obtain reconveyance of any deed of trust securing Seller's own financing at closing, and will "
        "disclose occupancy, lease, and possession arrangements that survive the sale."
    )
    d.h2("10. Earnest money deposits")
    d.p(
        "Broker may receive and hold an initial deposit in a neutral escrow or trust account as permitted "
        "by law, will submit it with the offer, and will thereafter direct it only on written instruction of "
        "the parties or as a court or the escrow holder directs."
    )
    d.h2("11. Escrow and closing cooperation")
    d.p(
        "The parties will open escrow promptly after acceptance, will supply documents the escrow holder "
        "reasonably requires, and will work toward the anticipated closing date. Time is of the essence only "
        "if the purchase agreement says so."
    )
    d.page_break()
    d.h2("12. Mediation and binding arbitration")
    d.p(
        "Before filing suit arising out of this Agreement, the parties will attempt mediation with a neutral "
        "mediator. If mediation fails, disputes will be submitted to binding arbitration on the terms stated "
        "in the purchase agreement or, if none, under the commercial arbitration rules then in effect. Each "
        "party bears its own mediation costs unless the arbitrator allocates otherwise."
    )
    d.h2("13. Agency disclosure acknowledgment")
    d.p(
        "Broker has explained the statutory agency disclosure and the options available to Seller. Seller "
        "acknowledges receipt of the disclosure document; Seller's initials on that document, not this "
        "Agreement, record the elected agency relationship."
    )
    d.h2("14. Conflict of interest and dual agency")
    d.p(
        "Broker may not accept compensation from more than one party to a transaction without written "
        "disclosure and consent. If Broker becomes a dual agent with written consent, Broker's duties narrow "
        "as the law provides, and neither party may expect Broker to advocate for that party against the "
        "other."
    )
    d.h2("15. Reports, records and retention")
    d.p(
        "Broker will retain the listing and transaction records for the period required by law and by "
        "Broker's brokerage policy, will make them available for inspection on reasonable notice, and will "
        "return Seller's proprietary materials at the end of the listing."
    )
    d.page_break()
    d.h2("16. Electronic signatures and notices")
    d.p(
        "The parties consent to signing and delivering this Agreement and related disclosures by electronic "
        "means, and agree that an electronic signature has the same effect as a handwritten one. Notices may "
        "be given by the methods stated in the purchase agreement or, if silent, by personal delivery, "
        "overnight courier, or email with confirmation."
    )
    d.h2("17. Indemnification")
    d.p(
        "Seller will indemnify Broker against claims arising from Seller's knowing misrepresentation of "
        "material facts about the Property or from Seller's breach of this Agreement. Broker will indemnify "
        "Seller against claims arising from Broker's own misrepresentation or willful misconduct."
    )
    d.h2("18. Limits of Broker liability")
    d.p(
        "Broker is not an attorney, accountant, engineer, inspector, or title company, and nothing Broker "
        "says replaces professional advice on those subjects. Broker's liability for any claim under this "
        "Agreement will not exceed the compensation Broker actually received, except where prohibited by law."
    )
    d.h2("19. Survival and termination")
    d.p(
        "Termination ends Broker's prospective duties but does not undo compensation already earned or "
        "the protective period in Section 8. Sections that by their nature should continue \u2014 "
        "indemnity, records, dispute resolution \u2014 survive termination."
    )
    d.page_break()
    d.h2("20. Miscellaneous", gap_before=0)
    d.p(
        "If any provision of this Agreement is held unenforceable, the remainder stays in effect. This "
        "Agreement may be amended only in a writing signed by both parties. It is governed by the laws of "
        "the State of California. Captions are for convenience and do not limit meaning."
    )
    d.h2("21. Entire agreement")
    d.p(
        "This Agreement, together with any written addenda initialed by both parties, is the entire "
        "agreement between Seller and Broker concerning the listing and supersedes prior discussions on the "
        "same subject."
    )
    d.h2("Broker's closing statement of compliance")
    d.p(
        "Broker confirms that the listing materials submitted for this property were prepared by or under "
        "the supervision of the licensees named below, that required property data statements were "
        "delivered with the listing, and that any compensation offered to cooperating licensees was stated "
        "as a share of Broker's compensation."
    )
    d.page_break()
    d.h2("Listing office contacts and showing coordination", gap_before=0)
    d.table(
        [
            ["Role", "Name", "Contact"],
            ["Listing agent", v(AGENT) or "____", "listing@demorealty.example"],
            ["Transaction coordinator", v("Taylor Coordinator") or "____", "tc@demorealty.example"],
            ["Broker of record", v(BROKERAGE) or "____", "office@demorealty.example"],
            ["Preferred escrow", v("Demo Escrow") or "____", "orders@demoescrow.example"],
        ],
        [130, 160, WIDTH - 290],
        fs=8.4,
    )
    d.spacer(6)
    d.rule()
    d.h3("Signatures", gap_before=0)
    d.sig_row(
        [
            dict(name=SELLER, role="Seller", signed=True, w=230, date_value=LIST_DATE),
            dict(name=AGENT, role="Listing agent", dre=AGENT_DRE, signed=True, x=LEFT + 260, w=230,
                 date_value=LIST_DATE),
        ]
    )
    d.sig_row(
        [
            dict(name=BROKERAGE, role="Broker \u2014 Demo Realty", dre=BROKER_DRE, signed=d.completed,
                 w=230, date_value=LIST_DATE),
            dict(name=None, role="Co-seller (if any)", x=LEFT + 260, w=230),
        ]
    )


def build_seller_advisory(d: Doc) -> None:
    d.title_block("Seller's Advisory \u2014 synthetic template modeled on the CAR Seller's Advisory")
    d.party_block()
    d.p(
        "This advisory summarizes topics a residential seller is usually asked to think about when listing a "
        "property. It is written for demonstration purposes and does not replace legal advice or the "
        "statutory disclosure forms themselves. Seller is encouraged to read each section and to ask "
        "questions before signing the acknowledgment on page 2.",
        style="smalli",
    )
    d.h2("1. Duty to disclose")
    d.p(
        "A seller must disclose material facts the seller knows about the property \u2014 things a "
        "reasonable buyer would want to know \u2014 whether or not the buyer asks. Known leaks, cracks, "
        "pest activity, unpermitted work, disputes over boundaries, and planned assessments are typical "
        "examples. Silence about a known material fact can create liability later; disclosure is the "
        "protection."
    )
    d.h2("2. Limits of professional expertise")
    d.p(
        "A real estate licensee is not an attorney, engineer, accountant, or home inspector. Brokers and "
        "agents may explain customary practice and hand over forms, but they do not interpret contracts, "
        "structural reports, or tax consequences for the seller. Sellers should engage their own "
        "professionals for those questions."
    )
    d.h2("3. Inspections and investigation")
    d.p(
        "Buyers will usually order their own inspections and may ask for repair estimates or receipts. "
        "Sellers benefit from having service records, permit histories, and prior repair documentation "
        "organized before the property goes on the market. Providing records the seller already has is "
        "helpful; volunteering opinions the seller cannot document is not."
    )
    d.page_break()
    d.h2("4. Title, liens and possession")
    d.p(
        "The seller should expect the buyer's title company to search the property's record history for "
        "deeds of trust, mechanic's liens, easements, and assessment liens. Items that should be paid off at "
        "closing must be arranged through escrow. Occupancy after closing, pending leases, and personal "
        "property left behind should all be stated in writing."
    )
    d.h2("5. Acknowledgment")
    d.p(
        "Seller acknowledges that the advisory was provided, that Seller had the opportunity to ask "
        "questions, and that Seller will complete the statutory disclosure forms to the best of Seller's "
        "knowledge. Acknowledgment of this advisory is not an admission of any defect."
    )
    d.spacer(6)
    d.rule()
    d.sig(name=SELLER, role="Seller", signed=True, w=280, date_value=LIST_DATE)
    d.spacer(4)
    d.note("Copy retained by listing brokerage. " + (COMPLETED_FOOTER if d.completed else TEMPLATE_FOOTER))


def build_agency_disclosure(d: Doc) -> None:
    d.title_block("Disclosure Regarding Real Estate Agency Relationships \u2014 synthetic template modeled on CAR AD")
    v = d.fill_val
    d.party_block()
    d.kv2(("Document date", v(LIST_DATE)), ("Related listing date", v(LIST_DATE)))
    d.h2("How agency works in a real estate transaction")
    d.p(
        "When a licensee helps with a sale, the licensee acts as the agent of either the seller, the buyer, "
        "or both with written consent. The agency relationship determines whose interests the licensee must "
        "advocate. A licensee may also perform ministerial acts \u2014 delivering papers, scheduling, "
        "assembling forms \u2014 that do not themselves create an agency relationship. The election below "
        "is recorded by initials."
    )
    d.h3("Role definitions")
    d.table(
        [
            ["Role", "What it means, in summary"],
            ["Agent of seller", "Licensee represents the seller's interests and owes confidentiality and loyalty to the seller."],
            ["Agent of buyer", "Licensee represents the buyer's interests in the same duties, from the buyer's side."],
            ["Dual agent", "Licensee represents both sides with written consent; duties to both are limited as the law provides."],
            ["Designated agency", "A broker designates different licensees to advocate for each side within one brokerage."],
            ["Non-agent / facilitator", "Licensee performs ministerial acts only and owes no representation duties."],
        ],
        [140, WIDTH - 140],
    )
    d.h3("Election of agency relationship")
    d.check_cols(
        [
            ("Licensee is agent of SELLER", True, False),
            ("Licensee is agent of BUYER", False, False),
            ("Dual agency with written consent", False, False),
            ("Designated agency within brokerage", False, False),
            ("Non-agent facilitation only", False, False),
            ("Transaction broker (where permitted)", False, False),
        ],
        cols=2,
    )
    d.spacer(4)
    d.p("Seller's initials: ______    Buyer's initials: ______    Date: ____________", style="small")
    d.page_break()
    d.h2("Summary of statutory provisions")
    d.p(
        "The following points paraphrase the substance of California's agency disclosure statute for "
        "demonstration; the official form language controls in practice."
    )
    d.p(
        "<b>1.</b> A licensee acts as the agent of the party who engages the licensee, unless a different "
        "relationship is established in writing. A licensee may not act as both agents in the same "
        "transaction without the informed written consent of both parties.",
        indent=6,
    )
    d.p(
        "<b>2.</b> Agency duties \u2014 undivided loyalty, confidential treatment of the client's "
        "information, obedience to lawful instructions, reasonable care, and full disclosure \u2014 run for "
        "the duration of the agency. A licensee may not advance the licensee's own interest at the client's "
        "expense.",
        indent=6,
    )
    d.p(
        "<b>3.</b> Compensation is a matter of agreement between client and brokerage. Earning or offering "
        "compensation does not by itself create an agency relationship, and the amount of compensation does "
        "not determine which party the licensee represents.",
        indent=6,
    )
    d.p(
        "<b>4.</b> A licensee must disclose material facts the licensee knows about the property or the "
        "transaction that a party acting in good faith would usually want to know, even where the duty of "
        "confidentiality otherwise applies.",
        indent=6,
    )
    d.p(
        "<b>5.</b> Either party may end the agency relationship by written notice, subject to any "
        "compensation already earned and to any agreement to the contrary.",
        indent=6,
    )
    d.spacer(6)
    d.rule()
    d.h3("Acknowledgment", gap_before=0)
    d.p(
        "I have received and read this disclosure, the licensee has explained the roles above, and my "
        "initials record the relationship elected on page 1.",
        style="small",
    )
    d.sig_row(
        [
            dict(name=SELLER, role="Seller", signed=True, w=230, date_value=LIST_DATE),
            dict(name=None, role="Buyer", x=LEFT + 260, w=230),
        ]
    )
    d.spacer(2)
    d.sig(name=BROKERAGE, role="Brokerage acknowledgment \u2014 Demo Realty", dre=BROKER_DRE,
          signed=d.completed, w=300, date_value=LIST_DATE)


def build_tds(d: Doc) -> None:
    """Transfer Disclosure Statement -- 3 pages; completed version is visibly
    hand-completed (marked items, pen explanations, a correction)."""
    d.title_block(
        "Transfer Disclosure Statement \u2014 synthetic template modeled on the CAR TDS (statutory topics)",
        doc_no=f"Disclosure date: {TDS_DATE}",
    )
    d.party_block()
    d.kv2(("Year built", "1968"), ("Occupancy", "Owner occupied"))
    d.note("Parties: seller Jane Seller. Listing-side name fields were not provided with this document.")
    d.h2("Section A \u2014 Seller's knowledge of the property (items 1\u201340)")
    d.p(
        "For each item, the seller marks the box that matches the seller's knowledge. Items left blank are "
        "treated as not known. The completed example marks selected items and annotates them in pen.",
        style="small",
    )
    d.check_cols(
        [
            ("1. Roof leaks", False, False),
            ("2. Roof age / remaining warranty", False, False),
            ("3. Attic ventilation or fan issues", True, True),
            ("4. Chimney or flue defects", False, False),
            ("5. Walls out of plumb / cracks", False, False),
            ("6. Foundation cracks or settlement", False, False),
            ("7. Active or past pests (termites)", False, False),
            ("8. Water penetration in basement", False, False),
            ("9. Slab leaks known to seller", False, False),
            ("10. Water heater leaking / age", True, True),
            ("11. Sewer or drain backups", False, False),
            ("12. Well or septic systems", False, False),
            ("13. Water pressure problems", False, False),
            ("14. Known plumbing leaks", False, False),
            ("15. Electrical panel / fuse box", False, False),
            ("16. Outlets not grounded / dead", False, False),
            ("17. Aluminum or knob-and-tube wiring", False, False),
            ("18. Known electrical hazards", False, False),
            ("19. Heating system defects", False, False),
            ("20. Cooling system defects", False, False),
        ],
        cols=2,
    )
    d.check_cols(
        [
            ("21. Ductwork or ventilation issues", False, False),
            ("22. Fireplace or wood stove use", False, False),
            ("23. Appliances included but failing", False, False),
            ("24. Garage door / door defects", False, False),
            ("25. Pool / spa equipment defects", False, False),
            ("26. Pool barrier / gate compliance", False, False),
            ("27. Retaining wall defects", False, False),
            ("28. Fence or boundary disputes", False, False),
            ("29. Driveway or walkway cracks", False, False),
            ("30. Unpermitted work / additions", False, False),
            ("31. Incomplete construction work", False, False),
            ("32. Smoke / carbon monoxide alarms", False, False),
            ("33. Asbestos-containing materials", False, False),
            ("34. Lead-based paint (pre-1978)", False, False),
            ("35. Mold or chronic moisture", False, False),
            ("36. Radon or soil gas concerns", False, False),
            ("37. Earthquake / fault proximity", False, False),
            ("38. Flood or wildfire exposure", False, False),
            ("39. Nuisances or neighbor issues", False, False),
            ("40. Other material defects", False, False),
        ],
        cols=2,
    )
    d.page_break()
    d.h2("Section B \u2014 Known defects and repairs (items 1\u201316)")
    d.p(
        "Answer each numbered item and add a comment line where the answer needs detail. The completed "
        "example shows handwritten comments and at least one correction.",
        style="small",
    )
    yn_items_b = [
        ("B1. Are you aware of any leaks in the roof, walls, or ceiling?", False, False, None),
        ("B2. Has any part of the plumbing been repaired or replaced?", True, True,
         "water heater leaking at base \u2014 replaced 2022, receipt on file"),
        ("B3. Are you aware of any electrical problems?", False, False, None),
        ("B4. Has the heating or cooling system been serviced or repaired?", False, False, None),
        ("B5. Are you aware of any termite or wood-destroying pest damage?", False, False, None),
        ("B6. Has the property ever been fumigated or tented?", False, False, None),
        ("B7. Are you aware of cracks in the foundation or slab?", False, False, None),
        ("B8. Any unpermitted work you are aware of?", False, False, None),
        ("B9. Any disputes about fences, walls, or boundaries?", False, False, None),
        ("B10. Any insurance claims in the last five years?", True, False,
         "corrected: NO (misread item \u2014 claim closed 2019)"),
        ("B11. Any flooding, storm, or water damage?", False, False, None),
        ("B12. Any mold, mildew, or chronic moisture problems?", False, False, None),
        ("B13. Any known problems with appliances being left?", False, False, None),
        ("B14. Any planned special assessments you know of?", False, False, None),
        ("B15. Any restrictions on use you are aware of?", False, False, None),
        ("B16. Any other material fact a buyer should know?", False, False, None),
    ]
    for q, ans, pen, comment in yn_items_b:
        d.yn(q, ans, pen=pen, comment=comment)
        if comment and comment.lower().startswith("corrected") and d.completed:
            # Visibly strike the mis-marked Yes answer.
            d.hand_strike(LEFT + WIDTH - 96 + 20, d.y + 17, 14)
    d.page_break()
    d.h2("Section C \u2014 Inspection-related questions (items 1\u201316)")
    d.p(
        "These questions are asked so the buyer's own inspections can be planned. Answer from the seller's "
        "knowledge; Yes does not mean the property is defective.",
        style="small",
    )
    yn_items_c = [
        ("C1. Is the seller aware of a need for roof replacement?", False),
        ("C2. Is the seller aware of foundation movement?", False),
        ("C3. Is the seller aware of plumbing repairs needed?", True),
        ("C4. Is the seller aware of electrical upgrades needed?", False),
        ("C5. Is the seller aware of sewer line problems?", False),
        ("C6. Is the seller aware of drainage or grading issues?", False),
        ("C7. Is the seller aware of asbestos in siding or tiles?", False),
        ("C8. Is the seller aware of lead paint hazards?", True),
        ("C9. Is the seller aware of former methamphetamine use?", False),
        ("C10. Is the seller aware of flood-zone or flood history?", False),
        ("C11. Is the seller aware of wildfire interface exposure?", False),
        ("C12. Is the seller aware of fault or landslide zones?", False),
        ("C13. Is the seller aware of noisy or harmful neighbors?", False),
        ("C14. Is the seller aware of registry entries required by law?", False),
        ("C15. Is the seller aware of mold or indoor air issues?", False),
        ("C16. Is the seller aware of anything else material?", False),
    ]
    for q, ans in yn_items_c:
        d.yn(q, ans, pen=True)
    d.h2("Section D \u2014 Seller certification")
    d.p(
        "The seller certifies that the statements in this disclosure are true to the best of the seller's "
        "knowledge as of the date signed, that the seller has disclosed known material facts, and that the "
        "seller will deliver a copy of the completed disclosure to the buyer as required."
    )
    d.spacer(4)
    d.sig_row(
        [
            dict(name=SELLER, role="Seller", signed=True, w=230, date_value=TDS_DATE),
            dict(name=None, role="Co-seller (if any)", x=LEFT + 260, w=230),
        ]
    )
    d.spacer(2)
    d.note(
        "Listing agent receipt: fields not provided with this document. Buyer acknowledgment: to be signed "
        "on receipt."
    )


def build_spq(d: Doc) -> None:
    """Seller Property Questionnaire -- 4 pages; completed is hand-completed.

    Note: the real CAR SPQ's Sections 8-11 (special topics) could not be
    verified from public sources; those pages are best-effort approximations
    of the topics only, not of the official form.
    """
    d.initials()
    d.title_block(
        "Seller Property Questionnaire \u2014 synthetic template modeled on the CAR SPQ",
        doc_no=f"Completion date: {TDS_DATE}",
    )
    d.party_block()
    d.note(
        "Answer every item. Grouped topics follow the general structure of the official questionnaire; "
        "write on the comment lines where an answer needs detail."
    )
    d.h2("Group A \u2014 General property and title")
    group_a = [
        ("A1. Do you know of any defects in the walls, ceiling, or floors?", False, False, None),
        ("A2. Do you know of any defects in the plumbing or water supply?", True, True,
         "pier and beam access panel added 2019"),
        ("A3. Do you know of defects in the electrical system?", False, False, None),
        ("A4. Do you know of defects in the heating or air conditioning?", False, False, None),
        ("A5. Do you know of defects in the roof or gutters?", False, False, None),
        ("A6. Do you know of any drainage or grading problems?", False, False, None),
        ("A7. Is any part of the property subject to a boundary dispute?", False, False, None),
        ("A8. Do you know of easements not shown in title records?", False, False, None),
    ]
    for q, ans, pen, comment in group_a:
        d.yn(q, ans, pen=pen, comment=comment, indent=4)
    d.h2("Group B \u2014 Environmental")
    group_b = [
        ("B1. Do you know of asbestos-containing materials on the property?", False, False, None),
        ("B2. Do you know of lead-based paint hazards (pre-1978 construction)?", True, True, None),
        ("B3. Do you know of radon or soil-gas concerns?", False, False, None),
        ("B4. Do you know of an underground storage tank on or near the lot?", False, False, None),
        ("B5. Do you know of mold or chronic moisture conditions?", False, False, None),
        ("B6. Do you know of contaminated soil or groundwater?", False, False, None),
    ]
    for q, ans, pen, comment in group_b:
        d.yn(q, ans, pen=pen, comment=comment, indent=4)
    d.page_break()
    d.h2("Group C \u2014 Structures and systems")
    group_c = [
        ("C1. Has any part of the structure been repaired after earthquake damage?", False, False, None),
        ("C2. Are you aware of settlement or foundation movement?", False, False, None),
        ("C3. Have additions or conversions been made to the property?", True, True,
         "carport converted \u2014 city record 2016"),
        ("C4. Are you aware of missing or expired permits?", False, False, None),
        ("C5. Do you know of termite or wood-destroying insect work done?", False, False, None),
        ("C6. Do you know of solar or battery equipment defects?", False, False, None),
        ("C7. Do you know of pool or spa compliance issues?", False, False, None),
        ("C8. Do you know of septic, well, or private road systems here?", False, False, None),
    ]
    for q, ans, pen, comment in group_c:
        d.yn(q, ans, pen=pen, comment=comment, indent=4)
    d.h2("Group D \u2014 Repairs and alterations (items A\u2013F)")
    d.note(
        "The property was built in 1973 or earlier; answers in this group also inform the lead-based paint "
        "disclosure.",
        gap=3,
    )
    group_d = [
        ("A. List repairs you had done in the last five years:", "kitchen rewire 2021 \u2014 permit closed"),
        ("B. List alterations or additions:", "sunroom permit 2008"),
        ("C. List work you started but did not finish:", None),
        ("D. List appliances or systems replaced:", "roof overlay 2015, water heater 2022"),
        ("E. List repairs paid for by insurance:", "drywall patch after 2023 leak"),
        ("F. List any work with a written warranty:", "HVAC warranty to 2027"),
    ]
    for label, pen_text in group_d:
        d._ensure(28)
        d.p(f"<b>{label}</b>", style="body", gap=2)
        if d.completed and pen_text:
            d._pen_line(pen_text, indent=18)
        else:
            for _ in range(2):
                d.c.setStrokeColor(RULE)
                d.c.setDash([1, 3], 0)
                d.c.line(LEFT + 18, d.y - 5, LEFT + WIDTH - 20, d.y - 5)
                d.c.setDash([], 0)
                d.y -= 13
            d.y -= 2
    d.page_break()
    d.h2("Group E \u2014 Neighborhood and use")
    group_e = [
        ("E1. Do you know of nuisances or neighbor conditions a buyer should know?", False, False, None),
        ("E2. Do you know of noise, odor, or traffic sources nearby?", False, False, None),
        ("E3. Do you know of airplanes, trains, or freeways affecting use?", False, False, None),
        ("E4. Do you know of zoning changes or development plans nearby?", False, False, None),
        ("E5. Do you know of any rental or occupancy restrictions?", False, False, None),
        ("E6. Do you know of homeowners-association rules that matter here?", False, False, None),
    ]
    for q, ans, pen, comment in group_e:
        d.yn(q, ans, pen=pen, comment=comment, indent=4)
    d.h2("Section 8 \u2014 Special topics: litigation and claims")
    d.p(
        "Is the seller aware of any lawsuit, arbitration, or government action involving the property, or "
        "of any insurance claim (earthquake, flood, fire, liability, or other) made in the last five years? "
        "Describe on the line below."
    )
    d.pen_note("None known to seller")
    d.h2("Section 9 \u2014 Special topics: leases, occupants and use")
    d.p(
        "Is the property subject to a lease, month-to-month occupancy, timeshare, or right of first "
        "refusal? Is any part of the property used for a business, short-term rental, or agriculture?"
    )
    d.pen_note("Owner occupied; no leases")
    d.page_break()
    d.h2("Section 10 \u2014 Special topics: disclosures to brokers")
    d.p(
        "The seller agrees to tell the listing broker promptly about any change in the answers above, "
        "understands that the broker will deliver the seller's disclosures to prospective buyers, and "
        "understands that the seller's answers are not a substitute for the statutory transfer disclosure "
        "statement."
    )
    d.h2("Section 11 \u2014 Acknowledgment")
    d.p(
        "The seller certifies that the answers are true to the best of the seller's knowledge as of the "
        "date signed, that the seller has disclosed known material facts, and that the seller had the "
        "opportunity to consult an attorney."
    )
    d.spacer(6)
    d.rule()
    d.sig_row(
        [
            dict(name=SELLER, role="Seller", signed=True, w=230, date_value=TDS_DATE),
            dict(name=None, role="Co-seller (if any)", x=LEFT + 260, w=230),
        ]
    )
    d.spacer(2)
    d.note("Buyer's acknowledgment of receipt: ______ (buyer)   Date: __________")


def build_avid(d: Doc) -> None:
    d.title_block(
        "Agent Visual Inspection Disclosure \u2014 synthetic template modeled on the CAR AVID",
        doc_no=f"Inspection date: {AVID_DATE}",
    )
    v = d.fill_val
    d.party_block()
    d.kv2(("Inspection date", v(AVID_DATE)), ("Weather / visibility", v("Clear, daylight")))
    d.kv2(("Participants present", v("Alex Agent, seller")), ("Buyer present", None))
    d.p(
        "This disclosure records what the licensee observed by sight and sound during a walk-through. It "
        "is not a professional inspection, does not cover hidden or inaccessible conditions, and is not a "
        "warranty of condition. Items noted as not observed were inaccessible or outside the licensee's "
        "line of sight.",
        style="smalli",
    )
    d.h2("Walk-through participation")
    d.table(
        [
            ["Participant", "Role", "Present"],
            [v(SELLER) or "____", "Property owner", "Yes" if d.completed else "____"],
            [v(AGENT) or "____", "Listing agent (observer)", "Yes" if d.completed else "____"],
            ["(buyer)", "Buyer", "No" if d.completed else "____"],
            ["(buyer's agent)", "Cooperating licensee", "No" if d.completed else "____"],
        ],
        [160, WIDTH - 280, 120],
        fs=8.4,
    )
    d.page_break()
    d.h2("Areas inspected and observations", gap_before=0)
    rows = [
        ["Area", "Items observed / noted", "Condition noted"],
        ["Exterior \u2014 front", "Stucco, front walkway, garage door" if d.completed else "", "Serviceable" if d.completed else ""],
        ["Yard / landscaping", "Irrigation head misaligned at east bed" if d.completed else "", "Minor defect noted" if d.completed else ""],
        ["Driveway", "Surface cracking near sidewalk joint" if d.completed else "", "Cosmetic" if d.completed else ""],
        ["Garage", "Water heater strapped; panel clear" if d.completed else "", "Serviceable" if d.completed else ""],
        ["Entry / hall", "Flooring intact; smoke alarm present" if d.completed else "", "Serviceable" if d.completed else ""],
        ["Living room", "Ceiling and walls; window operation" if d.completed else "", "No defects observed" if d.completed else ""],
        ["Kitchen", "Cabinets, sink, included appliances run" if d.completed else "", "Operational" if d.completed else ""],
        ["Dining area", "Fixture, windows" if d.completed else "", "No defects observed" if d.completed else ""],
        ["Bedrooms (3)", "Closets, windows, flooring" if d.completed else "", "No defects observed" if d.completed else ""],
        ["Bathrooms (2)", "Vents, caulking, toilet stability" if d.completed else "", "Minor caulk wear" if d.completed else ""],
        ["Laundry area", "Hookups, drain pan" if d.completed else "", "Serviceable" if d.completed else ""],
        ["Roof (from ground)", "Composition shingles; gutters" if d.completed else "", "Age visible; no active leak seen" if d.completed else ""],
        ["Crawlspace access", "Not opened during walk-through" if d.completed else "", "Not observed" if d.completed else ""],
        ["Systems (visible only)", "Furnace filter slot; electrical panel" if d.completed else "", "Serviceable" if d.completed else ""],
    ]
    d.table(rows, [110, WIDTH - 240, 130], fs=8.0, gap=8)
    d.h2("Not observed during this walk-through")
    d.check_cols(
        [
            ("Hidden structural components", True, False),
            ("Interior of walls or ceilings", True, False),
            ("Under-slab plumbing", True, False),
            ("Attic framing (not entered)", True, False),
            ("Septic or private systems", True, False),
            ("Radon, mold, or lab testing", True, False),
        ],
        cols=2,
    )
    d.page_break()
    d.h2("Notes and follow-up items")
    d.p(
        "If the licensee later observes something additional, add it below and date the addition. Buyers "
        "should rely on their own inspections for conclusions about condition."
    )
    if d.completed:
        d.pen_note("Buyer to order general home inspection and sewer scope.")
        d.pen_note("Seller to provide roof repair receipt (2015 overlay) before closing.")
    else:
        d.pen_note(None)
        d.pen_note(None)
    d.spacer(8)
    d.rule()
    d.h3("Signatures (in order)", gap_before=0)
    d.sig_row(
        [
            dict(name=AGENT, role="Listing agent", dre=AGENT_DRE, signed=True, w=230, date_value=AVID_DATE),
            dict(name=SELLER, role="Seller", signed=True, x=LEFT + 260, w=230, date_value=AVID_DATE),
        ]
    )
    d.sig_row(
        [
            dict(name=None, role="Buyer", w=230),
            dict(name=BROKERAGE, role="Broker \u2014 Demo Realty", dre=BROKER_DRE, signed=d.completed,
                 x=LEFT + 260, w=230, date_value=AVID_DATE),
        ]
    )


def build_nhd(d: Doc) -> None:
    d.title_block("Natural Hazard Disclosure \u2014 synthetic template modeled on an NHD statement + report pair")
    v = d.fill_val
    d.party_block()
    d.kv2(("Report date", v(NHD_DATE)), ("Report number", v("NHD-2026-0914-5842")))
    d.h2("Part 1 \u2014 Natural hazard disclosure statement")
    d.p(
        "The seller and the parties preparing this statement answer each hazard question from the records "
        "and information then available. Do-not-know is a permitted answer where no reasonable basis for "
        "knowledge exists.",
        style="small",
    )
    rows = [
        ["Hazard / zone question", "Yes", "No", "Do not know"],
        ["Is the property within an official earthquake fault zone?", "X" if d.completed else "", "", ""],
        ["Is the property within a seismic hazard (liquefaction or landslide) zone?", "X" if d.completed else "", "", ""],
        ["Is the property in a special flood hazard area on the FEMA map?", "", "X" if d.completed else "", ""],
        ["Is the property within a state responsibility wildfire area?", "X" if d.completed else "", "", ""],
        ["Is the property within a dam inundation zone?", "", "X" if d.completed else "", ""],
        ["Is the property subject to any other disclosed natural hazard?", "", "X" if d.completed else "", ""],
    ]
    d.table(rows, [WIDTH - 210, 55, 55, 100], fs=8.4, gap=8)
    d.p(
        "Where the answer is Yes, the accompanying report describes the zone, its mapped boundaries, and "
        "where further records may be reviewed. Nothing in this statement is a prediction about whether a "
        "hazard event will occur."
    )
    d.h2("Statement signatures")
    d.sig_row(
        [
            dict(name=SELLER, role="Seller", signed=True, w=230, date_value=NHD_DATE),
            dict(name=AGENT, role="Listing agent", dre=AGENT_DRE, signed=True, x=LEFT + 260, w=230,
                 date_value=NHD_DATE),
        ]
    )
    # ---- Part 2: report-style pages (commercial NHD-style layout) ----
    d.page_break()
    d.h1("Part 2 \u2014 Natural hazard disclosure report")
    d.table(
        [
            ["Prepared by", "Westline Hazard Reports (synthetic provider)"],
            ["Report no. / date", f"NHD-2026-0914-5842  \u00b7  {NHD_DATE}"],
            ["Property", f"{ADDRESS}  \u00b7  APN {APN}"],
            ["Legal", LEGAL],
            ["Sources", "State and federal mapping agencies; county records; local ordinances"],
        ],
        [110, WIDTH - 110],
        fs=8.4,
    )
    d.h2("Zone coverage summary")
    rows = [
        ["Zone or disclosure", "Status", "Reported by"],
        ["Earthquake fault zone (Alquist-Priolo)", "IN", "State geological mapping"],
        ["Seismic hazard (liquefaction / landslide)", "IN", "State hazard zones"],
        ["Special flood hazard area (FEMA)", "NOT IN", "FIRM panel, current edition"],
        ["State responsibility wildfire area", "IN", "State fire authority mapping"],
        ["Dam inundation zone", "NOT IN", "State dam safety mapping"],
        ["Local natural hazard disclosure ordinance", "APPLIES", "City of Pasadena"],
    ]
    d.table(rows, [WIDTH - 230, 90, 140], fs=8.4)
    d.note(
        "Zone statuses are internally consistent across this report: seismic zones are reported IN, the "
        "flood zone is reported NOT IN. Re-check against current maps before relying on any synthetic "
        "report in a real transaction."
    )
    d.page_break()
    d.h3("Detailed zone statements", gap_before=0)
    d.h3("1. Earthquake fault zone \u2014 IN")
    d.p(
        "Mapped active faults pass within the reporting radius used for this address. Structures in an "
        "official fault zone may require a geological report before development approval, and buyers "
        "commonly review prior studies. See the mapped boundaries filed with the state geological survey."
    )
    d.h3("2. Seismic hazard zones \u2014 IN")
    d.p(
        "Liquefaction and landslide susceptibility mapping covers part or all of the parcel. Liquefaction "
        "potential relates to how saturated, loose soils behave during shaking; it does not mean damage "
        "will occur. Foundation assessments are a matter for licensed professionals."
    )
    d.h3("3. Special flood hazard area \u2014 NOT IN")
    d.p(
        "The parcel does not fall within a zone requiring mandatory flood insurance under the national "
        "flood insurance program, based on the map panel cited for this report. Flood insurance remains "
        "optional, and flood can occur outside mapped zones."
    )
    d.h3("4. Wildfire \u2014 IN")
    d.p(
        "Part of the reporting area lies in a state responsibility area for wildfire protection. Brush "
        "clearance, home-hardening, and defensible-space rules may apply, and insurers may apply "
        "underwriting criteria of their own."
    )
    d.page_break()
    d.h3("5. Dam inundation \u2014 NOT IN", gap_before=0)
    d.p(
        "No mapped inundation boundary for a state-regulated dam covers this parcel on the sources "
        "consulted for this report."
    )
    d.h3("6. Local disclosure ordinance \u2014 APPLIES")
    d.p(
        "The city maintains its own natural hazard disclosure requirements; buyers should review the "
        "city's current disclosure handout at closing."
    )
    d.h2("Sources and limitations")
    d.p(
        "This report aggregates published mapping and records as of the report date. Zones change when "
        "maps are revised; private conditions (drainage, past slides, unrecorded activity) may not appear "
        "in any zone map. This is a synthetic report prepared for demonstration and is not a substitute "
        "for a report ordered from a licensed provider."
    )
    d.spacer(6)
    d.rule()
    d.p(
        "Preparer: Westline Hazard Reports (fictional). Received by: "
        + (f"{AGENT}, {BROKERAGE}" if d.completed else "________________"),
        style="small",
    )


def build_wcmd(d: Doc) -> None:
    d.title_block(
        "Water-Conserving Plumbing Fixtures and Carbon Monoxide Detector Advisory "
        "\u2014 synthetic template modeled on the CAR WCMD"
    )
    v = d.fill_val
    d.party_block()
    d.kv2(("Document date", v(LIST_DATE)), ("Year built", "1968"))
    d.h2("Section 1 \u2014 Water-conserving plumbing fixtures")
    d.p(
        "The seller confirms, for the fixtures listed below, whether they meet the water-conservation "
        "standards described in the applicable state plumbing and efficiency provisions. Mark each line "
        "and initial this page."
    )
    d.check_cols(
        [
            ("Water closets (toilets) meet standard", True, False),
            ("Lavatory faucets meet standard", True, False),
            ("Showerheads meet standard", True, False),
            ("Urinals meet standard (if any)", True, False),
            ("Kitchen faucet meets standard", True, False),
            ("Exceptions / replacements documented", False, False),
        ],
        cols=2,
    )
    d.h2("Section 2 \u2014 Carbon monoxide alarms")
    d.p(
        "The seller states whether carbon monoxide alarms are installed where the statute requires them, "
        "are operational at the time of this disclosure, and have been tested. Smoke alarms are addressed "
        "in the transfer disclosure statement."
    )
    d.check_cols(
        [
            ("CO alarms installed where required", True, False),
            ("CO alarms operational / tested", True, False),
            ("Smoke alarms present and operational", True, False),
            ("Alarm batteries / power checked", True, False),
        ],
        cols=2,
    )
    d.h2("Section 3 \u2014 Other local disclosures")
    d.p(
        "Possession: owner occupied at listing; no tenants. Private sewer lateral: status per city program "
        "records. Other local requirements are handled through the city's transfer checklist at closing."
    )
    d.spacer(8)
    d.rule()
    d.sig_row(
        [
            dict(name=SELLER, role="Seller 1", signed=True, w=170, date_value=LIST_DATE),
            dict(name=None, role="Seller 2", x=LEFT + 195, w=170),
            dict(name=None, role="Buyer 1", x=LEFT + 390, w=156),
        ]
    )
    d.sig_row(
        [
            dict(name=SELLER, role="Seller 1 (printed name)", signed=True, w=170, date_value=LIST_DATE),
            dict(name=None, role="Seller 2 (printed name)", x=LEFT + 195, w=170),
            dict(name=None, role="Buyer 2 (printed name)", x=LEFT + 390, w=156),
        ]
    )


def build_lead(d: Doc) -> None:
    d.title_block(
        "Lead-Based Paint Disclosure (pre-1978 housing) \u2014 synthetic template; federal topics paraphrased"
    )
    v = d.fill_val
    d.party_block()
    d.kv2(("Year built", "1968"), ("Disclosure date", v(TDS_DATE)))
    d.h2("Warning")
    d.p(
        "Housing built before 1978 may contain lead-based paint. Lead in paint, dust, and soil can cause "
        "health effects, especially for young children and pregnant women. Federal law requires sellers of "
        "pre-1978 housing to give buyers important information about lead hazards before the sale is "
        "final. The attachments listed on page 2 form part of this disclosure.",
        style="bodyi",
    )
    d.h2("1. Seller's disclosures")
    d.check_cols(
        [
            ("Seller is aware of lead paint / lead dust on the property", True, True),
            ("Seller has reports or records about lead condition", False, False),
            ("Housing was built before 1978", True, False),
            ("Pamphlet provided to buyer", True, True),
        ],
        cols=1,
    )
    d.pen_note("Repaint history: exterior repainted 2011; records not retained")
    d.pen_note("Known lead paint: none reported by seller")
    d.h2("2. Lead testing records")
    d.p(
        "Has the seller had the property tested for lead? If tests were performed, attach the results. If "
        "tests were not performed, mark not tested; a buyer may choose to test before removal or "
        "renovation work."
    )
    d.check_cols(
        [
            ("Tests performed \u2014 results attached", False, False),
            ("Tests performed \u2014 results not available", False, False),
            ("No tests performed by seller", True, False),
        ],
        cols=1,
    )
    d.h2("3. Buyer's acknowledgment (completed on receipt)")
    d.p(
        "Buyer acknowledges receipt of the pamphlet, the disclosure form, and any available records; buyer "
        "may conduct testing before removing paint or making repairs, subject to a ten-day inspection "
        "window unless buyer waives it in writing."
    )
    d.page_break()
    d.h2("4. Records attachment list", gap_before=0)
    d.table(
        [
            ["Attachment", "Included"],
            ["Lead hazard pamphlet", "Yes" if d.completed else "____"],
            ["Previous inspection or lab reports", "None available" if d.completed else "____"],
            ["Repaint / renovation records", "Not retained" if d.completed else "____"],
            ["Deleading or abatement records", "None known" if d.completed else "____"],
        ],
        [WIDTH - 160, 160],
        fs=8.4,
    )
    d.h2("5. Signatures")
    d.p("Signed as of the date shown, for the property identified on page 1.", style="small")
    d.sig_row(
        [
            dict(name=SELLER, role="Seller", signed=True, w=230, date_value=TDS_DATE),
            dict(name=None, role="Buyer", x=LEFT + 260, w=230),
        ]
    )
    d.sig_row(
        [
            dict(name=AGENT, role="Listing agent", dre=AGENT_DRE, signed=True, w=230, date_value=TDS_DATE),
            dict(name=BROKERAGE, role="Broker \u2014 Demo Realty", dre=BROKER_DRE, signed=d.completed,
                 x=LEFT + 260, w=230, date_value=TDS_DATE),
        ]
    )
    d.page_break()
    d.h2("6. State addendum: California topics", gap_before=0)
    d.p(
        "In addition to the federal items above, California transactions commonly address the following; "
        "this synthetic addendum lists the topics for demonstration and does not reproduce any official "
        "form text."
    )
    d.check_cols(
        [
            ("Agent certification of delivery", True, False),
            ("Seller certification of disclosure", True, False),
            ("Buyer's ten-day inspection period acknowledged", True, False),
            ("Records delivered with disclosure", True, False),
        ],
        cols=1,
    )
    d.note("End of synthetic lead disclosure. " + (COMPLETED_FOOTER if d.completed else TEMPLATE_FOOTER))


def build_hoa(d: Doc) -> None:
    d.title_block(
        "HOA Resale Disclosure Package \u2014 representative synthetic subset (real packages run 50\u2013300+ pages)",
        doc_no=f"Package date: {HOA_DATE}",
    )
    v = d.fill_val
    d.kv("Association", v(HOA_NAME))
    d.kv2(("Property", v(ADDRESS)), ("APN", v(APN)))
    d.kv2(("Homeowner / seller", v(SELLER)), ("Package date", v(HOA_DATE)))
    d.table(
        [
            ["Contents of this package", "Page (subset)"],
            ["1. Cover letter and order details", "2"],
            ["2. CC&Rs excerpt (Articles I\u2013VI)", "3\u20135"],
            ["3. Budget and reserve summary", "6"],
            ["4. Assessment and fee statement", "7"],
            ["5. Minutes excerpt (synthetic board meeting)", "8\u20139"],
            ["6. Document fee checklist and delivery notice", "10"],
        ],
        [WIDTH - 140, 140],
        fs=8.6,
    )
    d.note(
        "This subset stands in for the full statutory resale package: governing documents, accounting "
        "records, insurance summaries, minutes, and contracts are represented by representative sections "
        "only."
    )
    d.page_break()
    d.h1("Cover letter", gap=6)
    d.p(f"To the prospective purchaser of {ADDRESS} (APN {APN}):")
    d.p(
        f"This package contains the documents {HOA_NAME} is required to deliver in connection with a "
        "transfer of the property, as of the package date shown above. Governing documents, financial "
        "statements, assessment records, minutes, and a summary of dues and fees are included."
    )
    d.p(
        "The association's assessments and rules may affect your use of the property. The budget attached "
        "shows the current operating figures and the funded status of the reserve account. Buyers should "
        "read the governing documents in full and may ask the association questions before removing "
        "contingencies."
    )
    d.p(
        "Questions about this package: management office (fictional) \u2014 records@pasadoakshoa.example. "
        "The documents in this package are synthetic and prepared for demonstration."
    )
    d.page_break()
    d.h1("CC&Rs \u2014 excerpt", gap=6)
    d.h3("Article I \u2014 Boundaries and use of lots")
    d.p(
        "Each lot is bounded by the recorded map and may be used only for single-family residential "
        "purposes with the accessory uses customary to such homes. No lot may be used for a trade or "
        "business that generates traffic or nuisance for neighbors, and no exterior storage of inoperable "
        "vehicles is permitted."
    )
    d.h3("Article II \u2014 Assessments")
    d.p(
        "The association may levy regular assessments for common expenses and special assessments for "
        "capital work, in each case after the notice the bylaws require. Assessments become a lien on the "
        "lot as provided by statute when recorded. An owner remains responsible for assessments until the "
        "association records a release or the transfer completes per the governing documents."
    )
    d.page_break()
    d.h3("Article III \u2014 Architectural control", gap_before=0)
    d.p(
        "No structure may be erected, altered, or externally painted without approval of the "
        "architectural committee. Applications are decided within the timeframe in the bylaws; approval "
        "may be withheld for incompatible design. Emergency repairs to prevent damage may proceed with "
        "notice to the committee."
    )
    d.h3("Article IV \u2014 Maintenance and damage")
    d.p(
        "Each owner maintains the lot and improvements in a neat condition, keeps landscaping from "
        "obstructing walkways, and promptly repairs damage the owner causes. The association may perform "
        "maintenance an owner neglects after notice and charge the cost to the owner's account."
    )
    d.page_break()
    d.h3("Article V \u2014 Insurance and casualties", gap_before=0)
    d.p(
        "Each owner maintains the lot and improvements in a neat condition, keeps landscaping from "
        "obstructing walkways, and promptly repairs damage the owner causes. The association may perform "
        "maintenance an owner neglects after notice and charge the cost to the owner's account."
    )
    d.h3("Article V \u2014 Insurance and casualties")
    d.p(
        "The association carries property coverage for common areas and, where the governing documents "
        "provide, for structures on lots. Owners should carry their own hazard and liability coverage; "
        "losses affecting an individual lot are allocated under the master policy and the association's "
        "deductible rules."
    )
    d.h3("Article VI \u2014 Rules and enforcement")
    d.p(
        "The board may adopt rules consistent with the CC&Rs covering recreation, parking, pets, and "
        "quiet hours. Violations may be cured by notice and, if uncured, may lead to fines or lien "
        "enforcement as the governing documents and law allow."
    )
    d.h3("Article VII \u2014 Meetings and voting")
    d.p(
        "Annual and special member meetings are held after written notice mailed or electronically "
        "delivered to owners of record. One vote attaches to each lot unless the bylaws provide "
        "otherwise. Proxies may be accepted for a single meeting and expire when the meeting adjourns."
    )
    d.page_break()
    d.h1("Budget and reserve summary", gap=6)
    rows = [["Line item", "Annual amount"]]
    for name, amt in [
        ("Dues income \u2014 regular assessments", "$96,000"),
        ("Interest and other income", "$1,200"),
        ("Management contract", "($18,000)"),
        ("Common area utilities", "($9,600)"),
        ("Landscaping and irrigation", "($14,400)"),
        ("Pool and clubhouse operating", "($11,000)"),
        ("Insurance \u2014 master policy", "($21,500)"),
        ("Repairs and maintenance", "($8,500)"),
        ("Administrative and postage", "($3,200)"),
        ("Reserve transfer (funding)", "($14,800)"),
        ("Net position after reserve transfer", "break-even range"),
    ]:
        rows.append([name, amt])
    d.table(rows, [WIDTH - 160, 160], fs=8.4)
    d.table(
        [
            ["Reserve status", "Amount"],
            ["Current reserve balance", "$163,400"],
            ["Recommended reserve (per study)", "$168,000"],
            ["Fully funded ratio", "97%"],
        ],
        [WIDTH - 160, 160],
        fs=8.4,
    )
    d.page_break()
    d.h1("Assessment and fee statement", gap=6)
    rows = [["Charge", "Amount", "Due"]]
    for charge, amt, due in [
        ("Monthly regular assessment", "$800.00", "1st of month"),
        ("Special assessment", "None levied", "\u2014"),
        ("Capital contribution (new owner)", "$0", "\u2014"),
        ("Transfer / recordation fee", "$275.00", "At transfer"),
        ("Statement / disclosure fee", "$75.00", "At order"),
        ("Key / gate / fob replacement", "$35.00 each", "If issued"),
        ("Late fee", "$25.00", "After 15 days"),
        ("Returned payment fee", "$35.00", "If applicable"),
    ]:
        rows.append([charge, amt, due])
    d.table(rows, [WIDTH - 260, 130, 130], fs=8.4)
    d.p(
        f"Account status as of {HOA_DATE}: "
        + (
            "regular assessment paid through September 2026; no delinquency; no special assessment."
            if d.completed
            else "________________"
        )
    )
    d.page_break()
    d.h1("Minutes excerpt \u2014 board meeting", gap=6)
    d.p(
        f"{HOA_NAME} \u2014 regular meeting. Synthetic minutes prepared for this demonstration package.",
        style="smalli",
    )
    rows = [
        ["Item", "Minutes (representative excerpt)"],
        ["Call to order", "Meeting called to order at 6:35 p.m.; quorum present (5 directors)."],
        ["Approval of minutes", "Prior meeting minutes approved as circulated; no amendments offered."],
        ["Roof reserve study", "Board accepted the updated reserve study; roof line item to rise 8% in the next budget cycle."],
        ["Landscape contract", "Renewal bid accepted for twelve months; irrigation audit scheduled for October."],
    ]
    d.table(rows, [140, WIDTH - 140], fs=8.2)
    d.page_break()
    d.h2("Minutes excerpt (continued)", gap_before=0)
    rows = [
        ["Item", "Minutes (representative excerpt)"],
        ["Pool hours", "Summer hours extended on a trial basis; report back at the next regular meeting."],
        ["Architectural requests", "Two requests approved (rear patio pergola, front path color); one tabled pending plans."],
        ["Delinquencies", "Two accounts referred for lien processing following written notice periods."],
        ["Announcements", "Annual meeting notice to mail in November; elections confirmed for three seats."],
        ["Adjournment", "Meeting adjourned at 7:50 p.m."],
    ]
    d.table(rows, [140, WIDTH - 140], fs=8.2)
    d.spacer(6)
    d.p(
        "Respectfully submitted, Secretary of the Board. Draft minutes until approved at the next regular "
        "meeting.",
        style="small",
    )
    d.page_break()
    d.h1("Document fee checklist and delivery notice", gap=6)
    d.p(
        "Resale documentation fees commonly charged at transfer are summarized below; this synthetic "
        "checklist lists typical categories and is not a quote."
    )
    rows = [["Item", "Fee", "Included in package"]]
    for item, fee, inc in [
        ("Governing documents / CC&Rs", "$75.00", "Yes"),
        ("Articles of incorporation and bylaws", "$45.00", "Yes"),
        ("Rules and regulations", "$25.00", "Yes"),
        ("Budget and financial statements", "$75.00", "Yes"),
        ("Assessment statement / account status", "$75.00", "Yes"),
        ("Meeting minutes (recent period)", "$50.00", "Extract"),
        ("Statement of lien / unpaid assessments", "$0 (none)", "\u2014"),
        ("Insurance certificate summary", "$40.00", "Yes"),
    ]:
        rows.append([item, fee, inc])
    d.table(rows, [WIDTH - 240, 110, 130], fs=8.4)
    d.p(
        "Delivery: the full package is delivered electronically to the parties of record. "
        + ("Electronic delivery confirmed 2026-09-20." if d.completed else "Delivery confirmation: __________")
    )
    d.note("End of representative subset. " + (COMPLETED_FOOTER if d.completed else TEMPLATE_FOOTER))


def build_title(d: Doc) -> None:
    d.title_block(
        "Preliminary Title Report \u2014 synthetic template modeled on a commercial prelim format",
        doc_no=f"Report date: {TITLE_DATE}",
    )
    v = d.fill_val
    d.table(
        [
            ["Underwriter", TITLE_CO + " (fictional)"],
            ["Report date", v(TITLE_DATE) or "__________"],
            ["Effective date", v(TITLE_DATE) or "__________"],
            ["Property", v(ADDRESS) or "__________"],
            ["APN", v(APN) or "__________"],
            ["Vesting instrument", "Grant Deed from prior owners, recorded 2019, Inst. No. 2019-088123"],
            ["Report type", "Preliminary \u2014 subject to final policy; not a commitment of title insurance"],
        ],
        [130, WIDTH - 130],
        fs=8.6,
    )
    d.h2("Schedule of ownership and estate")
    d.table(
        [
            ["Item", "Detail"],
            ["Vested in", v(SELLER) or "__________"],
            ["Estate", "Fee Simple"],
            ["Manner of holding", "Community property (as shown in the recorded instrument)"],
            ["Legal description", LEGAL],
        ],
        [130, WIDTH - 130],
        fs=8.4,
    )
    d.page_break()
    d.h2("Requirements", gap_before=0)
    d.p("The company will require, before issuing a final policy:", style="smallb", gap=3)
    d.p(
        "1. Payment or provision for current taxes and any assessments that become a lien before "
        "closing.<br/>"
        "2. Reconveyance of the deed of trust recorded against the property in favor of the lender of "
        "record.<br/>"
        "3. Payoff or subordination of any mechanic's liens or notices of nonresponsibility filed in "
        "connection with work on the property.<br/>"
        "4. A satisfactory judgment and lien search through the effective date of policy.<br/>"
        "5. Correct vesting deed to the estate and interest to be insured, in recordable form.<br/>"
        "6. Receipt showing private sewer lateral status per municipal program records, where applicable."
    )
    d.p(
        "In addition, the company will require an owner's affidavit of no disclosed liens, a smoke and "
        "carbon monoxide alarm receipt where applicable, and receipts for any work done under a permit "
        "still open of record."
    )
    d.h2("Continuation of requirements (unrecorded matters)")
    d.table(
        [
            ["Requirement", "Status to clear"],
            ["Owner's affidavit and indemnity", "Signed at closing"],
            ["Open permits", "Close or bond before policy"],
            ["Private sewer lateral receipt", "Provide at or before closing"],
            ["Prorations and payoff figures", "Provided by escrow"],
        ],
        [220, WIDTH - 220],
        fs=8.4,
    )
    d.page_break()
    d.h2("Exceptions \u2014 Schedule B-I (recorded matters)", gap_before=0)
    d.table(
        [
            ["No.", "Exception (recorded document)"],
            ["1", "Taxes and assessments: a first lien for all taxes and assessments now a lien and not yet due."],
            ["2", "Deed of trust: recorded in favor of the lender of record; to be reconveyed at or before closing."],
            ["3", "CC&Rs recorded against the property; complete copies obtainable from the association."],
            ["4", "Utility easements for electricity, gas, water, sewer and telecommunications along the front and side yards."],
            ["5", "Drainage easement recorded over the rear fifteen feet of the lot."],
            ["6", "Restrictions shown in the recorded map, including setback and use limitations."],
        ],
        [40, WIDTH - 40],
        fs=8.4,
    )
    d.page_break()
    d.table(
        [
            ["No.", "Exception (continued)"],
            ["7", "Easement for maintenance of any wall or fence on a boundary line."],
            ["8", "Right of any public entity to remove or relocate improvements interfering with street work."],
            ["9", "Mechanic's lien rights of persons furnishing labor or material after the effective date."],
            ["10", "Any lease of record shown against the property, together with all rights of the lessee."],
            ["11", "Any recorded solar or energy equipment agreement affecting the property."],
        ],
        [40, WIDTH - 40],
        fs=8.4,
    )
    d.h2("Matters the company will require to be insured over")
    d.p(
        "Subject to receipt of proper documentation: payment of the above deeds of trust and liens; "
        "deletion of any exception objected to by the company's counsel; and any correction of legal "
        "description required for recordation."
    )
    d.page_break()
    d.h2("Informational notes (no coverage effect by themselves)", gap_before=0)
    d.table(
        [
            ["Note", "Status"],
            ["Flood zone", "Not in a special flood hazard area per the map panel cited."],
            ["Zoning", "Single-family residential; confirm current setback and parking rules with the city."],
            ["Assessments", "No open municipal assessment district identified of record."],
            ["Personal property", "Any leased equipment on the property is excluded unless stated."],
            ["Private road / shared access", "Not applicable per recorded records reviewed."],
        ],
        [150, WIDTH - 150],
        fs=8.4,
    )
    d.h2("Tax and assessment schedule (informational)")
    d.table(
        [
            ["Item", "Amount / status"],
            ["Secured tax year", "2026, first installment paid, second installment due 2026-12-10"],
            ["Direct assessment (landscaping district)", "$0 of record"],
            ["Vineyard / special district", "None identified"],
            ["Mello-Roos / community facilities", "None identified"],
        ],
        [230, WIDTH - 230],
        fs=8.4,
    )
    d.h2("Private transfer fees")
    d.p(
        "No private transfer fee instrument was located of record for this parcel in the records "
        "reviewed. Any fee charged at closing by a third party should be identified in the settlement "
        "statement."
    )
    d.page_break()
    d.h2("Legal description (continued)", gap_before=0)
    d.p(LEGAL)
    d.p(
        "Together with all improvements, easements, rights, and appurtenances belonging or in any way "
        "appertaining thereto, and the right, title, interest, and estate of the owner of record in and "
        "to any street or strip abutting the property, to the center line thereof."
    )
    d.h2("Vesting detail")
    d.table(
        [
            ["Date recorded", "Instrument", "Grantee"],
            ["2019-06-14", "Grant Deed, Inst. 2019-088123", v(SELLER) or "__________"],
            ["2019-06-14", "Deed of Trust, Inst. 2019-088124 (to reconvey)", "Lender of record"],
        ],
        [90, WIDTH - 260, 170],
        fs=8.4,
    )
    d.page_break()
    d.h1("Exhibit A \u2014 Standard exceptions", gap=6)
    d.p(
        "The following standard exceptions are understood to be part of the preliminary coverage "
        "discussion unless deleted or insured over before final policy issuance.",
        style="small",
    )
    d.table(
        [
            ["No.", "Standard exception (summary wording)"],
            ["1", "Rights of parties in possession or claims of parties not shown by the public records."],
            ["2", "Defects, liens, encumbrances, or objections not shown by the public records."],
            ["3", "Unrecorded easements, paths, or rights that may exist on or over the land."],
            ["4", "Mineral, oil, and gas rights reserved by any prior conveyance to the extent applicable by law."],
            ["5", "Any state, federal, or local taxes or assessments not yet due and payable."],
        ],
        [40, WIDTH - 40],
        fs=8.4,
    )
    d.page_break()
    d.h2("Exhibit A \u2014 Standard exceptions (continued)", gap_before=0)
    d.table(
        [
            ["No.", "Standard exception (summary wording)"],
            ["6", "Discrepancies, conflicts, or gaps in the chain of title that cannot be determined from the records reviewed."],
            ["7", "Powers of attorney, probate proceedings, or administration affecting title as shown by the records reviewed."],
            ["8", "Any rights of redemption not terminated of record."],
            ["9", "Building zone or land use restrictions, permits, or violations of record."],
            ["10", "Any engineering or survey matters not disclosed by the record drawings available."],
        ],
        [40, WIDTH - 40],
        fs=8.4,
    )
    d.page_break()
    d.h1("Declarations and closing notes", gap=6)
    d.p(
        "This preliminary report is issued for information only. It is not a commitment to insure and it "
        "does not state the complete condition of title as of the effective date. Coverage, if issued, "
        "will be governed by the policy form actually delivered, the schedules attached to it, and the "
        "exceptions stated in it."
    )
    d.p(
        "All requirements and exceptions shown above are subject to change up to the date of policy. "
        "Buyers and lenders should order their own policies and should not rely on this report alone. "
        "Records reviewed are those made available to the examiner through the effective date shown on "
        "the cover."
    )
    d.table(
        [
            ["Closing checklist item", "Owner"],
            ["Payoff letters ordered", "Escrow"],
            ["Reconveyance of deed of trust", "Lender"],
            ["Owner's affidavit signed", "Seller"],
            ["Policy instructions received from lender", "Buyer's lender"],
        ],
        [WIDTH - 220, 220],
        fs=8.4,
    )
    d.note(
        "End of synthetic preliminary report. "
        + (COMPLETED_FOOTER if d.completed else TEMPLATE_FOOTER)
    )


def build_solar(d: Doc) -> None:
    d.title_block(
        "Residential Solar Energy System Lease Agreement \u2014 synthetic template",
        doc_no=f"Contract date: {SOLAR_DATE}",
    )
    v = d.fill_val
    d.kv("Property", v(ADDRESS))
    d.kv2(("APN", v(APN)), ("Customer", v(SELLER)))
    d.kv2(("Provider", v(SOLAR_PROVIDER)), ("Contract date", v(SOLAR_DATE)))
    d.kv2(("System", v("20-module rooftop array")), ("Contract no.", v("SUN-4471-CA")))
    d.h2("Contract summary and disclosure box")
    d.table(
        [
            ["Summary term", "Detail"],
            ["Ownership type", "Lease of the solar energy system (provider-owned)"],
            ["Initial term", "25 years from the system activation date"],
            ["Year 1 monthly payment", "$89.00 (then annual escalator of 2.9%)"],
            ["Payments include", "Monitoring, maintenance, and repairs under the lease terms"],
            ["Buyer assumption", "Lease transfers to the buyer at sale with provider consent"],
            ["Cancellation right", "Three days after signing for a new installation (see Exhibit 1)"],
        ],
        [170, WIDTH - 170],
        fs=8.4,
    )
    d.note(
        "Clause summary above is a paraphrase of typical residential solar lease terms for demonstration; "
        "it is not an offer from any real provider."
    )
    d.page_break()
    d.h2("Payment schedule (years 1\u201313)", gap_before=0)
    rows = [["Year", "Monthly", "Annual"]]
    monthly = 89.0
    schedule = []
    for year in range(1, 26):
        if year > 1:
            monthly = round(monthly * 1.029, 2)
        schedule.append((year, monthly))
    for year, monthly in schedule[:13]:
        rows.append([str(year), f"${monthly:,.2f}", f"${monthly * 12:,.2f}"])
    d.table(rows, [60, (WIDTH - 120) / 2, (WIDTH - 120) / 2], fs=7.6, pad=2.2, gap=4)
    d.p(
        "Monthly payments are billed on the same day each month. The annual escalator shown in the "
        "summary is applied once per contract year on the first billing of that year.",
        style="small",
    )
    d.page_break()
    d.h2("Payment schedule (years 14\u201325, continued)", gap_before=0)
    rows = [["Year", "Monthly", "Annual"]]
    for year, monthly in schedule[13:]:
        rows.append([str(year), f"${monthly:,.2f}", f"${monthly * 12:,.2f}"])
    d.table(rows, [60, (WIDTH - 120) / 2, (WIDTH - 120) / 2], fs=7.6, pad=2.2, gap=4)
    d.note("Illustrative schedule: escalator applied annually to the prior year's monthly amount.")
    d.p(
        "Total scheduled payments over the initial term, before any adjustment for production credits or "
        "early purchase, are shown in the account statements the provider issues each year.",
        style="small",
    )
    d.page_break()
    d.h2("1. Definitions", gap_before=0)
    d.p(
        "System means the solar photovoltaic equipment listed in the summary installed at the property. "
        "Production means the electrical energy the system generates. Activation date means the date the "
        "provider notified the customer the system was operational. Customer means the property owner "
        "named above and any buyer who assumes this agreement."
    )
    d.h2("2. Term and renewal")
    d.p(
        "The lease begins on the activation date and continues for the initial term stated in the summary. "
        "After the initial term the lease continues month to month unless a party gives written notice to "
        "end it or the parties agree to renew on updated terms."
    )
    d.h2("3. Payments")
    d.p(
        "Monthly payments are due on the same day each month beginning after the first full billing "
        "period. Late amounts may accrue a fee at the highest rate permitted by law. Disputed amounts may "
        "be withheld only while the customer pays the undisputed portion and notifies the provider of the "
        "dispute in writing."
    )
    d.h2("4. Maintenance, monitoring and repairs")
    d.p(
        "The provider owns the system during the lease and is responsible for monitoring, servicing, and "
        "replacing components that fail under normal use. The customer will keep the roof area around the "
        "system accessible and will not modify the roof penetrations without the provider's consent."
    )
    d.page_break()
    d.h2("5. Production guarantee and performance", gap_before=0)
    d.p(
        "Where the agreement includes a production guarantee, the provider will credit the customer if "
        "measured production falls below the guaranteed amount for a measurement period. Credits apply to "
        "future payments and are not payable in cash unless the agreement says so."
    )
    d.h2("6. Insurance")
    d.p(
        "The customer will maintain homeowner's property coverage on the structures to which the system is "
        "attached, and will carry liability coverage as required by the provider. The provider will carry "
        "coverage on the equipment itself. Each party will provide evidence of coverage on request."
    )
    d.h2("7. Transfer on sale of the property")
    d.p(
        "If the property is sold, the customer may assign this agreement to a qualified buyer who meets "
        "the provider's credit criteria. The provider will not unreasonably withhold consent and will "
        "provide a payoff or assumption statement within a reasonable period of a written request."
    )
    d.h2("8. Purchase option")
    d.p(
        "At the end of the initial term, or earlier if stated in the summary, the customer may purchase "
        "the system at the fair market value determined by an independent appraisal process described in "
        "the rider to this agreement, or may have the system removed at the provider's expense if the "
        "provider elects removal instead of sale."
    )
    d.page_break()
    d.h2("9. Warranties", gap_before=0)
    d.p(
        "Manufacturer warranties on panels and inverters pass through to the customer for their stated "
        "periods. The provider warrants its workmanship for the period stated in the installation "
        "documents. Warranties exclude damage from acts of God, vandalism, and modifications by others."
    )
    d.h2("10. Default and cure")
    d.p(
        "A party is in default on written notice and failure to cure within thirty days for payment "
        "defaults or fifteen days for other defaults. On the customer's default the provider may collect "
        "amounts due and may remove the system after notice where permitted."
    )
    d.h2("11. Casualty and condemnation")
    d.p(
        "If the system is destroyed or damaged, the parties will decide whether to restore it, terminate "
        "the lease, or apply insurance proceeds to restoration. If the property is taken by eminent "
        "domain, awards are allocated between the realty and the equipment as their relative interests "
        "appear."
    )
    d.page_break()
    d.h2("12. Assignment", gap_before=0)
    d.p(
        "The provider may assign this agreement with notice to the customer. The customer may assign only "
        "as allowed in Section 7. Any other assignment attempt is void without the provider's written "
        "consent."
    )
    d.h2("13. Notices")
    d.p(
        "Notices must be in writing and delivered personally, by first-class mail, or by recognized "
        "courier to the addresses stated above, with a copy by email where an address is on file. Notice "
        "is effective on delivery or refusal."
    )
    d.h2("14. Dispute resolution")
    d.p(
        "The parties will attempt informal resolution first. If unresolved, disputes will be submitted to "
        "mediation and, if mediation fails, to binding arbitration or small claims court as the agreement "
        "permits. Each party bears its own costs unless the arbitrator decides otherwise."
    )
    d.h2("15. Miscellaneous")
    d.p(
        "This agreement, with its summary and exhibits, is the entire agreement between the parties and "
        "may be changed only in writing signed by both. If a provision is unenforceable, the rest remains "
        "in effect. The agreement is governed by the law of the state where the property is located. "
        "Captions are for convenience only."
    )
    d.page_break()
    d.h1("Rider A \u2014 System specification", gap=6)
    d.table(
        [
            ["Specification", "Value"],
            ["Modules", "20 modules, monocrystalline, approximately 8.4 kW DC"],
            ["Inverter", "Single string inverter, garage-mounted"],
            ["Mounting", "Composition-shingle roof, flashed attachments on south-facing plane"],
            ["Monitoring", "Production meter with web portal access included in lease"],
            ["Estimated annual production", "12,400 kWh (first-year estimate)"],
            ["Wiring and disconnects", "Permitted and inspected at installation"],
        ],
        [200, WIDTH - 200],
        fs=8.4,
    )
    d.h2("Rider B \u2014 Roof condition addendum")
    d.p(
        "The customer represents that, to the customer's knowledge, the roof area receiving the system "
        "was free of active leaks at installation. The provider will notify the customer before any "
        "roof work that would require removal of the array, and the parties will coordinate scheduling "
        "so that production downtime is minimized."
    )
    d.p(
        "If the roof requires replacement during the initial term, the customer will give the provider "
        "reasonable notice so the array can be removed and reinstalled. Labor for that removal and "
        "reinstallation is covered by the lease when the roof work is unrelated to a customer-caused "
        "condition."
    )
    d.page_break()
    d.h2("Signatures", gap_before=0)
    d.p(
        "By signing, the customer agrees to the lease terms above and the provider agrees to install, "
        "monitor, and maintain the system as described.",
        style="small",
    )
    d.sig_row(
        [
            dict(name=SELLER, role="Customer (property owner)", signed=True, w=230, date_value=SOLAR_DATE),
            dict(name="Sam Provider", role=f"Provider \u2014 {SOLAR_PROVIDER}", signed=d.completed,
                 x=LEFT + 260, w=230, date_value=SOLAR_DATE),
        ]
    )
    d.spacer(4)
    d.h3("Activation record (completed after installation)")
    d.table(
        [
            ["Field", "Entry"],
            ["System activation date", "2024-05-10" if d.completed else "__________"],
            ["Inspector / permit closure", "City permit closed 2024-05-08" if d.completed else "__________"],
            ["Monitoring account active", "Yes" if d.completed else "____"],
        ],
        [200, WIDTH - 200],
        fs=8.4,
    )
    d.page_break()
    d.h1("Exhibit 1 \u2014 Cancellation notice (right to cancel)", gap=6)
    d.p(
        "You may cancel this agreement without penalty or obligation within three business days after "
        "the date you sign it. To cancel, deliver or mail a signed and dated copy of the cancellation "
        "notice to the provider's address above before the end of the third day. If you cancel by mail, "
        "the postmark date counts. Any payments you have made will be returned within fourteen days of "
        "the provider receiving your notice."
    )
    d.table(
        [
            ["Cancellation notice", "Entry"],
            ["I hereby cancel the agreement dated", "____________________"],
            ["Customer signature", "____________________"],
            ["Date signed for cancellation", "____________________"],
            ["(Provider use) Notice received", "____________________"],
        ],
        [230, WIDTH - 230],
        fs=8.4,
    )
    d.page_break()
    d.h1("Exhibit 2 \u2014 Sample annual production statement", gap=6)
    d.p(
        "The provider issues a production statement each contract year. A sample month-by-month layout "
        "is shown below; actual figures come from the monitoring meter.",
        style="small",
    )
    rows = [["Month", "Metered kWh", "Estimate kWh"]]
    estimate = [1020, 1080, 1260, 1330, 1420, 1450, 1480, 1430, 1300, 1160, 990, 930]
    for month, est in zip(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        estimate,
    ):
        metered = est - 40 if d.completed else None
        rows.append([month, f"{metered:,}" if metered else "______", f"{est:,}"])
    d.table(rows, [80, (WIDTH - 160) / 2, (WIDTH - 160) / 2], fs=8.4, pad=3.0, gap=6)
    d.p(
        (
            "Contract year total: 14,000 metered kWh against a 14,850 kWh estimate; within the "
            "guarantee band, no credit due."
            if d.completed
            else "Contract year total: __________ kWh; guarantee band: __________ kWh."
        ),
        style="small",
    )
    d.note(
        "End of synthetic solar agreement. "
        + (COMPLETED_FOOTER if d.completed else TEMPLATE_FOOTER)
    )


# ------------------------------------------------------------------ registry

DOCUMENTS: list[dict] = [
    {"key": "listing-agreement", "title": "Residential Listing Agreement",
     "form": "Synthetic template (modeled on CAR RLA)", "build": build_listing_agreement, "seed": 101},
    {"key": "seller-advisory", "title": "Seller's Advisory",
     "form": "Synthetic template (modeled on CAR SA)", "build": build_seller_advisory, "seed": 102},
    {"key": "agency-disclosure", "title": "Disclosure Regarding Real Estate Agency Relationships",
     "form": "Synthetic template (modeled on CAR AD)", "build": build_agency_disclosure, "seed": 103},
    {"key": "ca-tds", "title": "Transfer Disclosure Statement",
     "form": "Synthetic template (modeled on CAR TDS)", "build": build_tds, "seed": 104},
    {"key": "ca-spq", "title": "Seller Property Questionnaire",
     "form": "Synthetic template (modeled on CAR SPQ)", "build": build_spq, "seed": 105},
    {"key": "agent-visual-inspection", "title": "Agent Visual Inspection Disclosure",
     "form": "Synthetic template (modeled on CAR AVID)", "build": build_avid, "seed": 106},
    {"key": "ca-nhd", "title": "Natural Hazard Disclosure Report",
     "form": "Synthetic template (modeled on NHD report)", "build": build_nhd, "seed": 107},
    {"key": "wcmd-advisory", "title": "Water-Conserving Plumbing Fixtures & CO Detector Advisory",
     "form": "Synthetic template (modeled on CAR WCMD)", "build": build_wcmd, "seed": 108},
    {"key": "lead-disclosure", "title": "Lead-Based Paint Disclosure",
     "form": "Synthetic template (federal, pre-1978 housing)", "build": build_lead, "seed": 109},
    {"key": "hoa-package", "title": "HOA Resale Package",
     "form": "Synthetic template (CC&Rs, budget, minutes)", "build": build_hoa, "seed": 110},
    {"key": "prelim-title-report", "title": "Preliminary Title Report",
     "form": "Synthetic template", "build": build_title, "seed": 111},
    {"key": "solar-agreement", "title": "Solar Ownership / Financing Agreement",
     "form": "Synthetic template", "build": build_solar, "seed": 112},
]


def main() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(root, "templates")
    completed_dir = os.path.join(root, "completed")
    os.makedirs(templates_dir, exist_ok=True)
    os.makedirs(completed_dir, exist_ok=True)

    for spec in DOCUMENTS:
        key = spec["key"]
        for completed in (False, True):
            out_dir = completed_dir if completed else templates_dir
            prefix = "demo" if completed else "template"
            path = os.path.join(out_dir, f"{prefix}-{key}.pdf")
            doc = Doc(path, completed=completed, title=spec["title"], seed=spec["seed"])
            spec["build"](doc)
            doc.finish()
        print(f"wrote template-{key}.pdf / demo-{key}.pdf")

    print(f"done: {len(DOCUMENTS)} templates + {len(DOCUMENTS)} completed dummies")


if __name__ == "__main__":
    main()
