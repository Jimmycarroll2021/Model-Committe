"""Output skills: package a finished committee run as an HTML report, an xlsx workbook and a pptx deck.

Helpers shared by the renderers live here; the renderer modules are imported lazily by render_outputs()
so importing this package stays cheap and free of circular imports.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from pathlib import Path

from model_committee.schemas import (
    CanonicalClaim,
    Classification,
    Deliverable,
    MemberReport,
    MemberStatus,
    RunConfig,
    Synthesis,
    ValidationReport,
)

REPORT_FILE = "report.html"
SPREADSHEET_FILE = "committee.xlsx"
DECK_FILE = "committee-deck.pptx"

CLASSIFICATION_ORDER = [
    Classification.agreement,
    Classification.disagreement,
    Classification.unique,
    Classification.contested,
]

CLASSIFICATION_LABEL = {
    Classification.agreement: "Agreement",
    Classification.disagreement: "Disagreement",
    Classification.unique: "Unique",
    Classification.contested: "Contested",
}

STANCE_LABEL = {
    "support": "Supports",
    "reject": "Rejects",
    "uncertain": "Uncertain",
    "not_mentioned": "Not mentioned",
}

STATUS_LABEL = {
    MemberStatus.pending: "Pending",
    MemberStatus.running: "Running",
    MemberStatus.succeeded: "Succeeded",
    MemberStatus.failed: "Failed",
    MemberStatus.timed_out: "Timed out",
    MemberStatus.skipped: "Skipped",
}

_SAFE_SCHEMES = ("http://", "https://", "mailto:")


def render_outputs(
    deliverable: Deliverable,
    *,
    synthesis: Synthesis,
    members: list[MemberReport],
    validation: ValidationReport,
    config: RunConfig,
    out_dir: Path,
) -> list[tuple[str, str]]:
    """Write requested deliverables into out_dir. Returns [(filename relative to out_dir, kind)].

    none -> []; report -> report.html; spreadsheet -> committee.xlsx; deck -> committee-deck.pptx;
    all -> all three.
    """
    deliverable = Deliverable(deliverable)
    if deliverable is Deliverable.none:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {Deliverable.report, Deliverable.spreadsheet, Deliverable.deck}
    if deliverable is not Deliverable.all:
        wanted = {deliverable}

    written: list[tuple[str, str]] = []
    if Deliverable.report in wanted:
        from model_committee.outputs.report import render_html

        text = render_html(synthesis=synthesis, members=members, validation=validation, config=config)
        (out_dir / REPORT_FILE).write_text(text, encoding="utf-8")
        written.append((REPORT_FILE, "report"))
    if Deliverable.spreadsheet in wanted:
        from model_committee.outputs.spreadsheet import write_xlsx

        write_xlsx(out_dir / SPREADSHEET_FILE, synthesis=synthesis, members=members, validation=validation)
        written.append((SPREADSHEET_FILE, "spreadsheet"))
    if Deliverable.deck in wanted:
        from model_committee.outputs.deck import write_pptx

        write_pptx(out_dir / DECK_FILE, synthesis=synthesis, members=members, validation=validation, config=config)
        written.append((DECK_FILE, "deck"))
    return written


# ---------------------------------------------------------------- shared helpers


def effective_classification(claim: CanonicalClaim) -> Classification:
    """The classification synthesis assigned; recomputed from stances only if it is missing."""
    if claim.classification is not None:
        return claim.classification
    stances = [p.stance for p in claim.positions.values() if p.stance != "not_mentioned"]
    support, reject = stances.count("support"), stances.count("reject")
    if len(stances) <= 1:
        return Classification.unique
    if support and reject:
        return Classification.disagreement
    if support >= 2:
        return Classification.agreement
    return Classification.contested


def claims_by_class(synthesis: Synthesis) -> dict[Classification, list[CanonicalClaim]]:
    grouped: dict[Classification, list[CanonicalClaim]] = {c: [] for c in CLASSIFICATION_ORDER}
    for claim in synthesis.matrix.claims:
        grouped[effective_classification(claim)].append(claim)
    return grouped


def member_names(members: list[MemberReport], extra_keys: list[str] | None = None) -> dict[str, str]:
    """member key -> display name. Labels are disambiguated with the key when two members share one."""
    labels = [m.label or m.key for m in members]
    names = {
        m.key: (f"{label} ({m.key})" if labels.count(label) > 1 else label)
        for m, label in zip(members, labels, strict=True)
    }
    for key in extra_keys or []:
        names.setdefault(key, key)
    return names


def member_order(synthesis: Synthesis, members: list[MemberReport]) -> list[str]:
    """Every member key: report order first, then any matrix-only keys."""
    keys = [m.key for m in members]
    keys += [k for k in synthesis.matrix.members if k not in keys]
    return keys


def raised_by(claim: CanonicalClaim) -> list[str]:
    return [key for key, pos in claim.positions.items() if pos.stance != "not_mentioned"]


def stance_of(claim: CanonicalClaim, key: str) -> str | None:
    pos = claim.positions.get(key)
    return pos.stance if pos else None


def consensus_text(claim: CanonicalClaim) -> str:
    return f"{round(claim.model_consensus * 100)}%"


def safe_url(url: str | None) -> str | None:
    """Return url only if it uses an allow-listed scheme (blocks javascript:, data:, entity tricks)."""
    if not url:
        return None
    candidate = re.sub(r"[\x00-\x20]", "", html.unescape(url)).lower()
    return url.strip() if candidate.startswith(_SAFE_SCHEMES) else None


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def fmt_cost(cost: float | None) -> str:
    return "-" if cost is None else f"${cost:.2f}"


def fmt_int(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def fmt_confidence(value: float | None) -> str:
    return "-" if value is None else f"{round(value * 100)}%"


def total_tokens(member: MemberReport) -> int | None:
    t = member.telemetry
    if t is None or (t.input_tokens is None and t.output_tokens is None):
        return None
    return (t.input_tokens or 0) + (t.output_tokens or 0)


def report_date(members: list[MemberReport]) -> str:
    """Date the committee finished (latest member finish), falling back to today."""
    stamps = []
    for m in members:
        if m.finished_at:
            try:
                stamp = datetime.fromisoformat(m.finished_at)
            except ValueError:
                continue
            stamps.append(stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC))
    when = max(stamps) if stamps else datetime.now(UTC)
    return when.strftime("%d %B %Y").lstrip("0")


_MD_MARKUP = [
    (re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)"), r"\1"),  # [text](url) -> text
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), "• "),
]


def plain(text: str | None) -> str:
    """Strip common markdown markup for surfaces that can't render it (slides, cells)."""
    text = text or ""
    for rx, repl in _MD_MARKUP:
        text = rx.sub(repl, text)
    return text


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"
