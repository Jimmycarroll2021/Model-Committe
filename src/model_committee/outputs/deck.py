"""16:9 board deck. Every text box is sized with a conservative text-fit estimate: font shrinks first, then
long lists paginate onto continuation slides, and only as a last resort is text truncated with an ellipsis.
Tables never exceed ROWS_PER_TABLE body rows per slide."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.slide import Slide
from pptx.util import Emu, Inches, Pt

from model_committee.outputs import (
    CLASSIFICATION_LABEL,
    CLASSIFICATION_ORDER,
    STANCE_LABEL,
    STATUS_LABEL,
    claims_by_class,
    consensus_text,
    fmt_confidence,
    fmt_cost,
    fmt_duration,
    fmt_int,
    member_names,
    member_order,
    plain,
    raised_by,
    report_date,
    total_tokens,
    truncate,
)
from model_committee.schemas import (
    DISAGREEMENT_HANDLING,
    CanonicalClaim,
    Classification,
    MemberReport,
    MemberStatus,
    RunConfig,
    Synthesis,
    ValidationReport,
)

SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN = 0.6
CONTENT_TOP = 1.45
CONTENT_BOTTOM = 6.8
CONTENT_W = SLIDE_W - 2 * MARGIN
ROWS_PER_TABLE = 8

NAVY = RGBColor(0x1F, 0x3A, 0x5F)
INK = RGBColor(0x1C, 0x1D, 0x1F)
INK_2 = RGBColor(0x4A, 0x4D, 0x52)
INK_3 = RGBColor(0x73, 0x77, 0x7E)
RULE = RGBColor(0xE2, 0xE2, 0xDE)
PANEL = RGBColor(0xF3, 0xF4, 0xF6)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
ALERT_BG, ALERT_FG = RGBColor(0xF8, 0xD9, 0xD9), RGBColor(0x8F, 0x1D, 0x1D)

CLASS_COLORS = {  # (fill, text)
    Classification.agreement: (RGBColor(0xE3, 0xF1, 0xE7), RGBColor(0x1D, 0x5E, 0x33)),
    Classification.disagreement: (RGBColor(0xFB, 0xEA, 0xDB), RGBColor(0x8A, 0x3B, 0x0C)),
    Classification.unique: (RGBColor(0xE2, 0xEC, 0xF8), RGBColor(0x1F, 0x4A, 0x7C)),
    Classification.contested: (RGBColor(0xEC, 0xEC, 0xEC), RGBColor(0x4B, 0x4F, 0x55)),
}

# ---------------------------------------------------------------- text fitting


@dataclass
class Para:
    text: str
    size: float = 1.0  # relative to the box's chosen font size
    bold: bool = False
    color: RGBColor = INK
    space_after: float = 6.0  # points


def _char_w(pt: float, bold: bool) -> float:
    """Conservative average glyph width in inches for the default sans theme font."""
    return pt * (0.54 if bold else 0.5) / 72


def _height(paras: list[Para], width: float, pt: float) -> float:
    total = 0.0
    for i, p in enumerate(paras):
        size = pt * p.size
        per_line = max(int(width / _char_w(size, p.bold)), 1)
        lines = sum(max(math.ceil(len(chunk) / per_line), 1) for chunk in p.text.split("\n"))
        total += lines * size * 1.2 / 72 + (p.space_after / 72 if i < len(paras) - 1 else 0)
    return total


def _fit(paras: list[Para], width: float, height: float, max_pt: float, min_pt: float) -> tuple[float, list[Para]]:
    """Largest font size that fits; if none does, truncate at min_pt until it fits."""
    inner_w, inner_h = width - 0.2, height - 0.1  # text frame insets
    pt = max_pt
    while pt >= min_pt:
        if _height(paras, inner_w, pt) <= inner_h:
            return pt, paras
        pt -= 1
    paras = [Para(**vars(p)) for p in paras]
    while _height(paras, inner_w, min_pt) > inner_h:
        last = paras[-1]
        if len(last.text) > 40 or len(paras) == 1:
            shorter = truncate(last.text, int(len(last.text) * 0.8))
            if shorter == last.text:  # cannot shrink further: keep the text rather than emit an empty box
                break
            last.text = shorter
        else:
            paras.pop()
            paras[-1].text = truncate(paras[-1].text, max(len(paras[-1].text) - 2, 1))
    return min_pt, paras


def _textbox(
    slide: Slide,
    x: float,
    y: float,
    w: float,
    h: float,
    paras: list[Para],
    *,
    max_pt: float = 18,
    min_pt: float = 10,
    anchor: MSO_ANCHOR = MSO_ANCHOR.TOP,
    align: PP_ALIGN = PP_ALIGN.LEFT,
) -> None:
    paras = [p for p in paras if p.text.strip()]
    if not paras:
        return
    pt, paras = _fit(paras, w, h, max_pt, min_pt)
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.1)
    tf.margin_top = tf.margin_bottom = Inches(0.05)
    for i, p in enumerate(paras):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align
        para.space_after = Pt(p.space_after)
        run = para.add_run()
        run.text = p.text
        run.font.size = Pt(round(pt * p.size, 1))
        run.font.bold = p.bold
        run.font.color.rgb = p.color


def _rect(slide: Slide, x: float, y: float, w: float, h: float, fill: RGBColor, line: RGBColor | None = None):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = line
        shape.line.width = Pt(0.75)
    shape.shadow.inherit = False
    return shape


def _paginate(
    items: list[list[Para]], width: float, height: float, pt: float, max_items: int
) -> list[list[list[Para]]]:
    """Greedy pack of multi-paragraph items into pages that fit at font size pt."""
    pages: list[list[list[Para]]] = [[]]
    for item in items:
        candidate = pages[-1] + [item]
        flat = [p for it in candidate for p in it]
        if pages[-1] and (len(candidate) > max_items or _height(flat, width - 0.2, pt) > height - 0.1):
            pages.append([item])
        else:
            pages[-1].append(item)
    return pages


# ---------------------------------------------------------------- deck scaffolding


class _Deck:
    def __init__(self, objective: str):
        self.prs = Presentation()
        self.prs.slide_width = Inches(SLIDE_W)
        self.prs.slide_height = Inches(SLIDE_H)
        self.blank = self.prs.slide_layouts[6]
        self.footer = truncate(f"Model committee · {objective}", 120)

    def slide(self, title: str, subtitle: str | None = None) -> Slide:
        s = self.prs.slides.add_slide(self.blank)
        _rect(s, 0, 0, 0.18, SLIDE_H, NAVY)
        _textbox(
            s,
            MARGIN - 0.1,
            0.35,
            CONTENT_W,
            0.7,
            [Para(title, bold=True, color=NAVY)],
            max_pt=28,
            min_pt=18,
            anchor=MSO_ANCHOR.BOTTOM,
        )
        _rect(s, MARGIN, 1.1, 0.9, 0.05, NAVY)
        if subtitle:
            _textbox(
                s,
                MARGIN + 1.0,
                0.98,
                CONTENT_W - 1.0,
                0.32,
                [Para(subtitle, color=INK_3)],
                max_pt=12,
                min_pt=9,
                anchor=MSO_ANCHOR.MIDDLE,
            )
        n = len(self.prs.slides)
        _textbox(s, MARGIN - 0.1, 7.0, CONTENT_W - 1.0, 0.32, [Para(self.footer, color=INK_3)], max_pt=9, min_pt=8)
        _textbox(
            s,
            SLIDE_W - MARGIN - 1.0,
            7.0,
            1.1,
            0.32,
            [Para(str(n), color=INK_3)],
            max_pt=9,
            min_pt=8,
            align=PP_ALIGN.RIGHT,
        )
        return s

    def empty(self, s: Slide, message: str) -> None:
        _textbox(s, MARGIN - 0.1, CONTENT_TOP, CONTENT_W, 1.0, [Para(message, color=INK_3)], max_pt=18, min_pt=14)

    def bullets_slides(
        self,
        title: str,
        items: list[list[Para]],
        empty: str,
        subtitle: str | None = None,
        pt: float = 16,
        max_items: int = 6,
    ) -> None:
        if not items:
            self.empty(self.slide(title, subtitle), empty)
            return
        height = CONTENT_BOTTOM - CONTENT_TOP
        pages = _paginate(items, CONTENT_W, height, pt, max_items)
        for i, page in enumerate(pages):
            label = title if i == 0 else f"{title} (continued)"
            s = self.slide(label, subtitle)
            _textbox(
                s, MARGIN - 0.1, CONTENT_TOP, CONTENT_W, height, [p for it in page for p in it], max_pt=pt, min_pt=10
            )

    def table_slides(
        self,
        title: str,
        header: list[str],
        rows: list[list[str]],
        col_w: list[float],
        empty: str,
        subtitle: str | None = None,
        fills: list[list[tuple[RGBColor, RGBColor] | None]] | None = None,
        note: str | None = None,
    ) -> None:
        if not rows:
            self.empty(self.slide(title, subtitle), empty)
            return
        chunks = [range(i, min(i + ROWS_PER_TABLE, len(rows))) for i in range(0, len(rows), ROWS_PER_TABLE)]
        for n, idx in enumerate(chunks):
            label = title if n == 0 else f"{title} (continued)"
            s = self.slide(label, subtitle)
            top = CONTENT_TOP
            if note:
                _textbox(s, MARGIN - 0.1, top, CONTENT_W, 0.6, [Para(note, color=INK_2)], max_pt=13, min_pt=10)
                top += 0.65
            _table(s, top, header, [rows[i] for i in idx], col_w, [fills[i] for i in idx] if fills else None)


def _table(
    slide: Slide,
    top: float,
    header: list[str],
    rows: list[list[str]],
    col_w: list[float],
    fills: list[tuple[RGBColor, RGBColor] | None] | None,
) -> None:
    avail = CONTENT_BOTTOM - top
    n_rows = len(rows) + 1
    row_h = min(avail / n_rows, 0.75)
    scale = CONTENT_W / sum(col_w)
    widths = [w * scale for w in col_w]
    pt = 12 if len(header) <= 6 else 10
    line_h = pt * 1.2 / 72
    lines = max(int((row_h - 0.1) / line_h), 1)
    shape = slide.shapes.add_table(
        n_rows, len(header), Inches(MARGIN), Inches(top), Inches(CONTENT_W), Inches(row_h * n_rows)
    )
    tbl = shape.table
    tbl.first_row = True
    for j, w in enumerate(widths):
        tbl.columns[j].width = Emu(int(Inches(w)))
    for i in range(n_rows):
        tbl.rows[i].height = Emu(int(Inches(row_h)))
    for i, values in enumerate([header, *rows]):
        for j, value in enumerate(values):
            cell = tbl.cell(i, j)
            cell.margin_left = cell.margin_right = Inches(0.06)
            cell.margin_top = cell.margin_bottom = Inches(0.03)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            per_line = max(int((widths[j] - 0.12) / _char_w(pt, i == 0 or j == 0)), 4)
            text = truncate(value or "-", per_line * lines)
            cell.text = text
            run = cell.text_frame.paragraphs[0].runs[0]
            run.font.size = Pt(pt)
            run.font.bold = i == 0 or j == 0
            cell.fill.solid()
            if i == 0:
                cell.fill.fore_color.rgb = NAVY
                run.font.color.rgb = WHITE
            else:
                colors = fills[i - 1][j] if fills and fills[i - 1] else None
                cell.fill.fore_color.rgb = colors[0] if colors else (PANEL if i % 2 == 0 else WHITE)
                run.font.color.rgb = colors[1] if colors else INK


# ---------------------------------------------------------------- slides


def _title_slide(
    deck: _Deck, synthesis: Synthesis, members: list[MemberReport], config: RunConfig, names: dict[str, str]
) -> None:
    s = deck.prs.slides.add_slide(deck.blank)
    _rect(s, 0, 0, 4.2, SLIDE_H, NAVY)
    _textbox(s, 0.5, 0.6, 3.4, 0.5, [Para("MODEL COMMITTEE REPORT", bold=True, color=WHITE)], max_pt=12, min_pt=10)
    meta = [
        Para(report_date(members), bold=True, color=WHITE, space_after=10),
        Para(f"Depth: {config.depth.value.title()}", color=WHITE),
        Para(f"Synthesizer: {synthesis.synthesizer}", color=WHITE, space_after=14),
        Para("Members", bold=True, color=WHITE),
    ] + [Para(f"{names[m.key]} · {STATUS_LABEL[m.status]}", color=WHITE, space_after=3) for m in members]
    _textbox(s, 0.5, 3.6, 3.4, 3.5, meta, max_pt=14, min_pt=9)
    _textbox(
        s,
        4.8,
        1.1,
        7.9,
        2.9,
        [Para(synthesis.narrative.headline, bold=True, color=NAVY)],
        max_pt=40,
        min_pt=22,
        anchor=MSO_ANCHOR.BOTTOM,
    )
    _rect(s, 4.9, 4.2, 1.2, 0.06, NAVY)
    _textbox(
        s,
        4.8,
        4.45,
        7.9,
        2.5,
        [Para("Objective", bold=True, color=INK_3, size=0.8), Para(synthesis.objective, color=INK_2)],
        max_pt=18,
        min_pt=11,
    )


def _answer_slide(deck: _Deck, synthesis: Synthesis, grouped: dict[Classification, list[CanonicalClaim]]) -> None:
    n = synthesis.narrative
    s = deck.slide("Answer and recommendation")
    left_w = 8.4
    lead = plain(n.answer).strip().split("\n\n")[0]  # first paragraph; the full answer lives in the report
    _textbox(s, MARGIN - 0.1, CONTENT_TOP, left_w, 2.35, [Para(lead)], max_pt=20, min_pt=12)
    rec_top = CONTENT_TOP + 2.5
    _rect(s, MARGIN, rec_top, left_w - 0.1, 2.0, PANEL)
    _rect(s, MARGIN, rec_top, 0.08, 2.0, NAVY)
    _textbox(
        s,
        MARGIN + 0.15,
        rec_top + 0.08,
        left_w - 0.35,
        1.85,
        [
            Para("RECOMMENDATION", bold=True, color=NAVY, size=0.7),
            Para(plain(n.recommendation) or "No recommendation made.", color=INK if n.recommendation else INK_3),
        ],
        max_pt=18,
        min_pt=11,
    )
    _textbox(
        s,
        MARGIN - 0.1,
        rec_top + 2.1,
        left_w,
        0.85,
        [Para(f"Confidence: {n.confidence_note}", color=INK_2)],
        max_pt=14,
        min_pt=10,
    )
    x = MARGIN + left_w + 0.3
    w = SLIDE_W - MARGIN - x
    for i, cls in enumerate(CLASSIFICATION_ORDER):
        fill, fg = CLASS_COLORS[cls]
        y = CONTENT_TOP + i * 1.3
        _rect(s, x, y, w, 1.15, fill)
        _textbox(
            s,
            x + 0.1,
            y + 0.05,
            1.2,
            1.05,
            [Para(str(len(grouped[cls])), bold=True, color=fg)],
            max_pt=36,
            min_pt=24,
            anchor=MSO_ANCHOR.MIDDLE,
        )
        _textbox(
            s,
            x + 1.2,
            y + 0.05,
            w - 1.3,
            1.05,
            [Para(CLASSIFICATION_LABEL[cls], bold=True, color=fg)],
            max_pt=18,
            min_pt=12,
            anchor=MSO_ANCHOR.MIDDLE,
        )


def _claim_item(c: CanonicalClaim, detail: str) -> list[Para]:
    return [Para(f"• {c.id}  {c.statement}", space_after=2), Para(detail, size=0.75, color=INK_3, space_after=10)]


def _facts(c: CanonicalClaim, n_members: int) -> str:
    noun = "source" if c.independent_sources == 1 else "sources"
    return f"{c.support} of {n_members} support · {c.independent_sources} independent {noun}"


def _stances(c: CanonicalClaim, keys: list[str], names: dict[str, str]) -> str:
    parts = [f"{names.get(k, k)}: {STANCE_LABEL[c.positions[k].stance]}" for k in keys if k in c.positions]
    return " · ".join(parts)


def _disagreement_slides(deck: _Deck, claims: list[CanonicalClaim], keys: list[str], names: dict[str, str]) -> None:
    title = "Disagreements"
    sub = "One member supports, another rejects. Cause and recommended handling."
    if not claims:
        deck.empty(deck.slide(title, sub), "No member contradicted another.")
        return
    card_h = (CONTENT_BOTTOM - CONTENT_TOP - 0.2) / 2
    for page in range(0, len(claims), 2):
        s = deck.slide(title if page == 0 else f"{title} (continued)", sub)
        for i, c in enumerate(claims[page : page + 2]):
            y = CONTENT_TOP + i * (card_h + 0.2)
            fill, fg = CLASS_COLORS[Classification.disagreement]
            _rect(s, MARGIN, y, CONTENT_W, card_h, WHITE, RULE)
            _rect(s, MARGIN, y, 0.08, card_h, fg)
            dtype = c.disagreement_type
            handling = DISAGREEMENT_HANDLING.get(dtype) if dtype else None
            paras = [
                Para(f"{c.id}  {c.statement}", bold=True, space_after=6),
                Para(
                    f"Type: {dtype.value.title() if dtype else 'Not classified'}",
                    size=0.85,
                    color=fg,
                    bold=True,
                    space_after=3,
                ),
                Para(f"Cause: {c.disagreement_cause or 'Not stated by the synthesizer.'}", size=0.85, space_after=3),
            ]
            if handling:
                paras.append(Para(f"Handling: {handling}", size=0.85, space_after=3))
            paras.append(Para(_stances(c, keys, names), size=0.8, color=INK_3, space_after=0))
            _textbox(s, MARGIN + 0.15, y + 0.08, CONTENT_W - 0.25, card_h - 0.16, paras, max_pt=17, min_pt=10)


def _consensus_slides(deck: _Deck, synthesis: Synthesis, grouped: dict[Classification, list[CanonicalClaim]]) -> None:
    n_members = len(synthesis.matrix.members)
    rows, fills = [], []
    for cls, claims in grouped.items():
        for c in claims:
            thin = c.support >= 2 and c.independent_sources <= 1
            rows.append(
                [
                    c.id,
                    c.statement,
                    CLASSIFICATION_LABEL[cls],
                    f"{c.support}/{n_members} ({consensus_text(c)})",
                    f"{c.independent_sources}" + (" (thin)" if thin else ""),
                ]
            )
            fills.append([None, None, CLASS_COLORS[cls], None, (ALERT_BG, ALERT_FG) if thin else None])
    deck.table_slides(
        "Consensus vs evidence",
        ["ID", "Claim", "Class", "Model consensus", "Independent sources"],
        rows,
        [0.7, 6.4, 1.6, 1.7, 1.7],
        "No claims to compare.",
        fills=fills,
        note="Model agreement is not independent confirmation: members share training data and often find the "
        "same pages. 'Thin' marks claims two or more members support on one source domain or fewer.",
    )


def _decision_slides(deck: _Deck, synthesis: Synthesis) -> None:
    rows_in = synthesis.narrative.decision_matrix
    criteria: list[str] = []
    for r in rows_in:
        criteria += [k for k in r.scores if k not in criteria]
    header = ["Option", *criteria, "Verdict"]
    rows = [[r.option, *(r.scores.get(k, "-") for k in criteria), r.verdict or "-"] for r in rows_in]
    deck.table_slides(
        "Decision matrix", header, rows, [1.6] + [2.0] * len(criteria) + [1.5], "No decision matrix was produced."
    )


def _risks_slide(deck: _Deck, synthesis: Synthesis, validation: ValidationReport) -> None:
    n = synthesis.narrative
    risks = [Para(f"• {r}") for r in n.unresolved_risks] or [Para("None recorded.", color=INK_3)]
    actions = [Para(f"{i}. {a}") for i, a in enumerate(n.next_actions, 1)] or [Para("None recorded.", color=INK_3)]
    col_w = (CONTENT_W - 0.4) / 2
    height = CONTENT_BOTTOM - CONTENT_TOP - 0.5
    alert = None
    if validation.untraceable_citations:
        k = len(validation.untraceable_citations)
        alert = f"Validation: {k} synthesis citation{'s' if k != 1 else ''} not traceable to any member report."
        height -= 0.6
    # Long lists continue on another slide rather than shrinking below a legible size.
    risk_pages = _paginate([[p] for p in risks], col_w, height, 14, 8)
    action_pages = _paginate([[p] for p in actions], col_w, height, 14, 8)
    for i in range(max(len(risk_pages), len(action_pages))):
        s = deck.slide("Risks and next actions" if i == 0 else "Risks and next actions (continued)")
        for j, (head, pages) in enumerate((("Unresolved risks", risk_pages), ("Next actions", action_pages))):
            x = MARGIN - 0.1 + j * (col_w + 0.4)
            _textbox(s, x, CONTENT_TOP, col_w, 0.45, [Para(head, bold=True, color=NAVY)], max_pt=18, min_pt=14)
            if i < len(pages):
                _textbox(
                    s, x, CONTENT_TOP + 0.5, col_w, height, [p for it in pages[i] for p in it], max_pt=14, min_pt=10
                )
        if alert and i == 0:
            y = CONTENT_BOTTOM - 0.5
            _rect(s, MARGIN, y, CONTENT_W, 0.5, ALERT_BG)
            _textbox(
                s,
                MARGIN + 0.1,
                y,
                CONTENT_W - 0.2,
                0.5,
                [Para(alert, bold=True, color=ALERT_FG)],
                max_pt=13,
                min_pt=10,
                anchor=MSO_ANCHOR.MIDDLE,
            )


def _member_slide(deck: _Deck, m: MemberReport, name: str) -> None:
    s = deck.slide(f"Appendix: {name}", f"{m.spec} · {STATUS_LABEL[m.status]}")
    t = m.telemetry
    tele = [
        ("Status", STATUS_LABEL[m.status]),
        ("Duration", fmt_duration(t.duration_s if t else None)),
        ("Cost", fmt_cost(t.cost_usd if t else None)),
        ("Tokens", fmt_int(total_tokens(m))),
        ("Web searches", fmt_int(t.web_searches if t else None)),
        ("Confidence", fmt_confidence(m.findings.confidence if m.findings else None)),
    ]
    tile_w = (CONTENT_W - 0.1 * (len(tele) - 1)) / len(tele)
    failed = m.status is not MemberStatus.succeeded
    for i, (label, value) in enumerate(tele):
        x = MARGIN + i * (tile_w + 0.1)
        bad = failed and label == "Status"
        _rect(s, x, CONTENT_TOP, tile_w, 0.95, ALERT_BG if bad else PANEL)
        _textbox(
            s,
            x + 0.05,
            CONTENT_TOP + 0.05,
            tile_w - 0.1,
            0.85,
            [
                Para(label.upper(), bold=True, color=ALERT_FG if bad else INK_3, size=0.55, space_after=2),
                Para(value, bold=True, color=ALERT_FG if bad else INK),
            ],
            max_pt=20,
            min_pt=11,
        )
    top = CONTENT_TOP + 1.15
    body: list[Para] = []
    if m.error:
        body.append(Para(f"Error: {m.error}", bold=True, color=ALERT_FG, space_after=10))
    if m.parse_error:
        body.append(Para(f"Parse error: {m.parse_error}", color=ALERT_FG, space_after=10))
    if m.findings:
        body += [
            Para("Summary", bold=True, color=NAVY, size=0.8, space_after=2),
            Para(m.findings.summary, space_after=12),
            Para("Recommendation", bold=True, color=NAVY, size=0.8, space_after=2),
            Para(m.findings.recommendation or "None given.", space_after=12),
        ]
        if m.findings.counterargument:
            body += [
                Para("Strongest counterargument", bold=True, color=NAVY, size=0.8, space_after=2),
                Para(m.findings.counterargument, space_after=12),
            ]
    elif not m.error:
        body.append(Para("No structured findings were parsed from this report.", color=INK_3))
    _textbox(s, MARGIN - 0.1, top, CONTENT_W, CONTENT_BOTTOM - top, body, max_pt=18, min_pt=10)


def write_pptx(
    path: Path, *, synthesis: Synthesis, members: list[MemberReport], validation: ValidationReport, config: RunConfig
) -> None:
    keys = member_order(synthesis, members)
    names = member_names(members, keys)
    grouped = claims_by_class(synthesis)
    n_members = len(synthesis.matrix.members)
    deck = _Deck(synthesis.objective)

    _title_slide(deck, synthesis, members, config, names)
    _answer_slide(deck, synthesis, grouped)
    deck.bullets_slides(
        "Agreement",
        [_claim_item(c, _facts(c, n_members)) for c in grouped[Classification.agreement]],
        "No claims were supported by two or more members.",
        "Two or more members support; none rejects.",
    )
    _disagreement_slides(deck, grouped[Classification.disagreement], keys, names)
    deck.bullets_slides(
        "Unique findings",
        [
            _claim_item(
                c, "Raised only by " + ", ".join(names.get(k, k) for k in raised_by(c)) + " · " + _facts(c, n_members)
            )
            for c in grouped[Classification.unique]
        ],
        "Every claim was raised by more than one member.",
        "Raised by a single member: unconfirmed, not dismissed.",
    )
    deck.bullets_slides(
        "Contested claims",
        [_claim_item(c, _stances(c, keys, names)) for c in grouped[Classification.contested]],
        "No claims were left uncertain.",
        "Raised by several members; none firmly supports them.",
    )
    _consensus_slides(deck, synthesis, grouped)
    _decision_slides(deck, synthesis)
    _risks_slide(deck, synthesis, validation)
    for m in members:
        _member_slide(deck, m, names[m.key])
    deck.prs.core_properties.title = truncate(synthesis.narrative.headline, 250)
    deck.prs.save(Path(path))
