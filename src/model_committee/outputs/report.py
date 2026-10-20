"""Standalone HTML report: one self-contained file, no external requests, print and dark-mode aware.

Every string that came from a model (claims, reports, notes, URLs) is untrusted and goes through esc(),
md(), or link() — never into the page raw.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from html.parser import HTMLParser

import markdown as md_lib

from model_committee.outputs import (
    CLASSIFICATION_LABEL,
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
    raised_by,
    report_date,
    safe_url,
    total_tokens,
)
from model_committee.schemas import (
    DISAGREEMENT_HANDLING,
    CanonicalClaim,
    Classification,
    Evidence,
    MemberReport,
    MemberStatus,
    RunConfig,
    Synthesis,
    ValidationReport,
)

# ---------------------------------------------------------------- escaping


def esc(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def link(url: str | None, text: str | None = None) -> str:
    """Anchor for an allow-listed URL; anything else is shown as inert text."""
    label = esc(text or url or "")
    href = safe_url(url)
    if not href:
        return label
    return f'<a href="{esc(href)}" rel="noopener noreferrer nofollow">{label}</a>'


_ALLOWED_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "b",
    "i",
    "code",
    "pre",
    "blockquote",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
    "hr",
    "br",
    "del",
    "sup",
    "sub",
}
_VOID_TAGS = {"hr", "br"}


class _Sanitizer(HTMLParser):
    """Allow-list sanitizer for markdown output. Disallowed markup is kept visible as escaped text, links
    are scheme-checked, images become links (so nothing remote is ever loaded), and every attribute
    other than a checked href is dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "img":
            self.out.append(link(a.get("src"), "[image] " + (a.get("alt") or a.get("src") or "")))
        elif tag not in _ALLOWED_TAGS:
            self.out.append(esc(self.get_starttag_text()))
        elif tag == "a":
            href = safe_url(a.get("href"))
            self.out.append(f'<a href="{esc(href)}" rel="noopener noreferrer nofollow">' if href else "<a>")
            self.stack.append(tag)
        else:
            self.out.append(f"<{tag}>")
            if tag not in _VOID_TAGS:
                self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS and self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _ALLOWED_TAGS or tag not in self.stack:
            if tag not in _VOID_TAGS and tag != "img":
                self.out.append(esc(f"</{tag}>"))
            return
        while self.stack:
            open_tag = self.stack.pop()
            self.out.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        self.out.append(esc(data))

    def result(self) -> str:
        self.close()
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.stack))


def md(text: str) -> str:
    """Render untrusted markdown to sanitized HTML (see _Sanitizer)."""
    rendered = md_lib.markdown(text or "", extensions=["tables", "fenced_code", "sane_lists"])
    sanitizer = _Sanitizer()
    sanitizer.feed(rendered)
    return sanitizer.result()


# ---------------------------------------------------------------- small components

_CHIP_CLASS = {
    Classification.agreement: "agree",
    Classification.disagreement: "disagree",
    Classification.unique: "unique",
    Classification.contested: "contested",
}

_STANCE_CLASS = {"support": "agree", "reject": "reject", "uncertain": "contested", "not_mentioned": "muted"}

_STATUS_CLASS = {
    MemberStatus.succeeded: "agree",
    MemberStatus.failed: "reject",
    MemberStatus.timed_out: "reject",
    MemberStatus.skipped: "muted",
    MemberStatus.pending: "muted",
    MemberStatus.running: "unique",
}


def chip(cls: Classification) -> str:
    return f'<span class="chip {_CHIP_CLASS[cls]}">{CLASSIFICATION_LABEL[cls]}</span>'


def stance_chip(stance: str | None) -> str:
    stance = stance or "not_mentioned"
    return f'<span class="chip sm {_STANCE_CLASS[stance]}">{STANCE_LABEL[stance]}</span>'


def status_chip(status: MemberStatus) -> str:
    return f'<span class="chip sm {_STATUS_CLASS[status]}">{STATUS_LABEL[status]}</span>'


def bullet_list(items: Iterable[str], empty: str) -> str:
    items = [i for i in items if i and i.strip()]
    if not items:
        return f'<p class="empty">{esc(empty)}</p>'
    return "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"


def table(head: list[str], rows: list[list[str]], cls: str = "") -> str:
    """Rows are pre-rendered HTML cells (callers escape)."""
    thead = "".join(f'<th scope="col">{h}</th>' for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (
        f'<div class="scroll"><table class="{cls}"><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def section(sid: str, title: str, body: str, *, count: int | None = None, lede: str = "") -> str:
    badge = f' <span class="count">{count}</span>' if count is not None else ""
    lede_html = f'<p class="lede">{lede}</p>' if lede else ""
    return f'<section id="{sid}"><h2>{esc(title)}{badge}</h2>{lede_html}{body}</section>'


def sources_line(claim: CanonicalClaim) -> str:
    n = claim.independent_sources
    domains = ", ".join(esc(d) for d in claim.source_domains)
    noun = "independent source" if n == 1 else "independent sources"
    return f"{n} {noun}" + (f" ({domains})" if domains else "")


# ---------------------------------------------------------------- sections


def _header(synthesis: Synthesis, members: list[MemberReport], config: RunConfig, names: dict[str, str]) -> str:
    member_items = "".join(
        f"<li>{esc(names[m.key])} <code>{esc(m.spec)}</code> {status_chip(m.status)}</li>" for m in members
    )
    ok = sum(m.status is MemberStatus.succeeded for m in members)
    return f"""
<header class="masthead">
  <p class="kicker">Model committee report</p>
  <h1>{esc(synthesis.narrative.headline)}</h1>
  <p class="objective"><span class="label">Objective</span> {esc(synthesis.objective)}</p>
  <dl class="meta">
    <div><dt>Date</dt><dd>{esc(report_date(members))}</dd></div>
    <div><dt>Depth</dt><dd>{esc(config.depth.value.title())}</dd></div>
    <div><dt>Synthesizer</dt><dd><code>{esc(synthesis.synthesizer)}</code></dd></div>
    <div><dt>Members</dt><dd>{ok} of {len(members)} succeeded</dd></div>
  </dl>
  <ul class="members">{member_items}</ul>
</header>"""


def _answer(synthesis: Synthesis, grouped: dict[Classification, list[CanonicalClaim]]) -> str:
    n = synthesis.narrative
    rec = (
        f'<div class="callout"><p class="label">Recommendation</p><div class="md">{md(n.recommendation)}</div></div>'
        if n.recommendation
        else '<div class="callout muted"><p class="label">Recommendation</p><p>No recommendation made.</p></div>'
    )
    stats = "".join(f'<div class="stat"><span class="num">{len(grouped[c])}</span>{chip(c)}</div>' for c in grouped)
    body = (
        f'<div class="answer md">{md(n.answer)}</div>{rec}'
        f'<p class="confidence"><span class="label">Confidence</span> {esc(n.confidence_note)}</p>'
        f'<div class="stats" aria-label="Claim counts by classification">{stats}</div>'
    )
    return section("answer", "Answer", body)


def _claim_card(claim: CanonicalClaim, cls: Classification, extra: str = "") -> str:
    return (
        f'<article class="claim {_CHIP_CLASS[cls]}">'
        f'<div class="claim-head">{chip(cls)}<span class="cid">{esc(claim.id)}</span></div>'
        f'<p class="statement">{esc(claim.statement)}</p>'
        f'<p class="facts">{claim.support} support · {claim.reject} reject · {claim.uncertain} uncertain · '
        f"model consensus {consensus_text(claim)} · {sources_line(claim)}</p>{extra}</article>"
    )


def _stance_table(claim: CanonicalClaim, keys: list[str], names: dict[str, str]) -> str:
    rows = []
    for key in keys:
        pos = claim.positions.get(key)
        if pos is None:
            continue
        refs = ", ".join(esc(r) for r in pos.claim_refs)
        rows.append([esc(names.get(key, key)), stance_chip(pos.stance), esc(pos.note or ""), f"<code>{refs}</code>"])
    if not rows:
        return ""
    return table(["Member", "Stance", "Note", "Claim refs"], rows, "stances")


def _agreement(claims: list[CanonicalClaim]) -> str:
    body = "".join(_claim_card(c, Classification.agreement) for c in claims)
    return section(
        "agreement",
        "Agreement",
        body or '<p class="empty">No claims were supported by two or more members.</p>',
        count=len(claims),
        lede="Claims at least two members support and none rejects.",
    )


def _disagreement(claims: list[CanonicalClaim], keys: list[str], names: dict[str, str]) -> str:
    cards = []
    for c in claims:
        dtype = c.disagreement_type
        handling = DISAGREEMENT_HANDLING.get(dtype) if dtype else None
        extra = '<dl class="why">'
        extra += f"<div><dt>Type</dt><dd>{esc(dtype.value.title()) if dtype else 'Not classified'}</dd></div>"
        extra += f"<div><dt>Cause</dt><dd>{esc(c.disagreement_cause or 'Not stated by the synthesizer.')}</dd></div>"
        if handling:
            extra += f"<div><dt>Recommended handling</dt><dd>{esc(handling)}</dd></div>"
        extra += "</dl>" + _stance_table(c, keys, names)
        cards.append(_claim_card(c, Classification.disagreement, extra))
    return section(
        "disagreement",
        "Disagreement",
        "".join(cards) or '<p class="empty">No member contradicted another.</p>',
        count=len(claims),
        lede="Claims at least one member supports and another rejects, with the likely cause.",
    )


def _unique(claims: list[CanonicalClaim], names: dict[str, str]) -> str:
    cards = []
    for c in claims:
        who = ", ".join(esc(names.get(k, k)) for k in raised_by(c)) or "Unknown member"
        notes = "".join(
            f'<p class="note">{esc(p.note)}</p>' for p in c.positions.values() if p.note and p.stance != "not_mentioned"
        )
        cards.append(
            _claim_card(c, Classification.unique, f'<p class="raised">Raised only by <strong>{who}</strong></p>{notes}')
        )
    return section(
        "unique",
        "Unique findings",
        "".join(cards) or '<p class="empty">Every claim was raised by more than one member.</p>',
        count=len(claims),
        lede="Raised by a single member. Not confirmed, but not to be dropped either.",
    )


def _contested(claims: list[CanonicalClaim], keys: list[str], names: dict[str, str]) -> str:
    body = "".join(_claim_card(c, Classification.contested, _stance_table(c, keys, names)) for c in claims)
    return section(
        "contested",
        "Contested",
        body or '<p class="empty">No claims were left uncertain.</p>',
        count=len(claims),
        lede="Raised by several members, but none firmly supports them.",
    )


def _consensus_vs_evidence(synthesis: Synthesis, grouped: dict[Classification, list[CanonicalClaim]]) -> str:
    n_members = len(synthesis.matrix.members)
    rows = []
    for cls, claims in grouped.items():
        for c in claims:
            thin = c.support >= 2 and c.independent_sources <= 1
            flag = ' <span class="chip sm disagree">Thin evidence</span>' if thin else ""
            rows.append(
                [
                    f"<code>{esc(c.id)}</code>",
                    esc(c.statement),
                    chip(cls),
                    f"{c.support} of {n_members} ({consensus_text(c)})",
                    f"{c.independent_sources}{flag}",
                    esc(", ".join(c.source_domains)) or "-",
                ]
            )
    lede = (
        "Model consensus counts members who support a claim. Independent sources counts distinct cited "
        "domains. Several models agreeing is <strong>not</strong> independent confirmation: models share "
        "training data and often find the same pages, so high consensus resting on one source is flagged."
    )
    body = (
        table(["Claim", "Statement", "Class", "Model consensus", "Independent sources", "Domains"], rows, "cve")
        if rows
        else '<p class="empty">No claims to compare.</p>'
    )
    return section("consensus", "Consensus vs evidence", body, lede=lede)


def _decision_matrix(synthesis: Synthesis) -> str:
    rows_in = synthesis.narrative.decision_matrix
    if not rows_in:
        return section("decision", "Decision matrix", '<p class="empty">No decision matrix was produced.</p>')
    criteria: list[str] = []
    for r in rows_in:
        criteria += [k for k in r.scores if k not in criteria]
    rows = [
        [f"<strong>{esc(r.option)}</strong>"] + [esc(r.scores.get(k, "-")) for k in criteria] + [esc(r.verdict or "-")]
        for r in rows_in
    ]
    head = ["Option"] + [esc(k) for k in criteria] + ["Verdict"]
    return section("decision", "Decision matrix", table(head, rows, "decision"))


def _risks_actions(synthesis: Synthesis) -> str:
    n = synthesis.narrative
    body = (
        '<div class="two-col">'
        f"<div><h3>Unresolved risks</h3>{bullet_list(n.unresolved_risks, 'None recorded.')}</div>"
        f"<div><h3>Next actions</h3>{bullet_list(n.next_actions, 'None recorded.')}</div>"
        "</div>"
    )
    return section("risks", "Risks and next actions", body)


def _evidence_item(e: Evidence, untraceable: set[str]) -> str:
    flag = ' <span class="chip sm reject">Untraceable</span>' if e.url and e.url in untraceable else ""
    quote = f"<blockquote>{esc(e.quote)}</blockquote>" if e.quote else ""
    target = link(e.url, e.source) if e.url else esc(e.source)
    url_text = f' <span class="url">{esc(e.url)}</span>' if e.url else ""
    return f"<li>{target}{flag}{url_text}{quote}</li>"


def _validation(synthesis: Synthesis, validation: ValidationReport, names: dict[str, str]) -> str:
    parts = []
    untraceable = set(validation.untraceable_citations)
    if validation.untraceable_citations:
        items = "".join(f"<li><code>{esc(u)}</code></li>" for u in validation.untraceable_citations)
        parts.append(
            '<div class="alert" role="alert"><p class="label">Untraceable citations</p>'
            "<p>The synthesis cites these URLs but no member report does. Treat any claim resting on them as "
            f"unverified.</p><ul>{items}</ul></div>"
        )
    if not validation.unique_findings_preserved:
        parts.append(
            '<div class="alert" role="alert"><p class="label">Unique findings dropped</p>'
            "<p>At least one unique member finding is missing from the synthesis narrative.</p></div>"
        )
    parts.append("<h3>Warnings</h3>" + bullet_list(validation.warnings, "No validation warnings."))
    if validation.citations:
        rows = []
        for c in validation.citations:
            traced = ", ".join(esc(names.get(k, k)) for k in c.traced_to_members) or '<strong class="bad">none</strong>'
            fetched = "-" if c.fetched is None else ("yes" if c.fetched else "no")
            if c.http_status:
                fetched += f" ({c.http_status})"
            quote = (
                "-"
                if c.quote_found is None
                else ("found" if c.quote_found else '<strong class="bad">not found</strong>')
            )
            rows.append([link(c.url), traced, fetched, quote, esc(c.note or "")])
        parts.append(
            "<h3>Citation checks</h3>" + table(["URL", "Traced to", "Fetched", "Quote", "Note"], rows, "checks")
        )
    cites = synthesis.narrative.citations
    if cites:
        parts.append(
            '<h3>Sources cited by the synthesis</h3><ol class="sources">'
            + "".join(_evidence_item(e, untraceable) for e in cites)
            + "</ol>"
        )
    return section("validation", "Validation", "".join(parts))


def _member_card(m: MemberReport, name: str) -> str:
    t = m.telemetry
    tele = [
        ("Status", status_chip(m.status)),
        ("Spec", f"<code>{esc(m.spec)}</code>"),
        ("Vendor", esc(m.vendor)),
        ("Attempts", esc(m.attempts)),
        ("Duration", esc(fmt_duration(t.duration_s if t else None))),
        ("Cost", esc(fmt_cost(t.cost_usd if t else None))),
        ("Tokens", esc(fmt_int(total_tokens(m)))),
        ("Web searches", esc(fmt_int(t.web_searches if t else None))),
    ]
    dl = '<dl class="tele">' + "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in tele) + "</dl>"
    body = [dl]
    if m.error:
        body.append(f'<div class="alert"><p class="label">Error</p><pre>{esc(m.error)}</pre></div>')
    if m.parse_error:
        body.append(f'<p class="warn"><span class="label">Parse error</span> {esc(m.parse_error)}</p>')
    f = m.findings
    if f:
        body.append(f'<p><span class="label">Summary</span> {esc(f.summary)}</p>')
        body.append(f'<p><span class="label">Recommendation</span> {esc(f.recommendation or "None given.")}</p>')
        body.append(f'<p><span class="label">Confidence</span> {esc(fmt_confidence(f.confidence))}</p>')
    elif m.status is MemberStatus.succeeded:
        body.append('<p class="empty">No structured findings were parsed from this report.</p>')
    if m.context_files:
        body.append(
            '<p><span class="label">Context received</span> '
            + ", ".join(f"<code>{esc(c)}</code>" for c in m.context_files)
            + "</p>"
        )
    if m.report_markdown.strip():
        body.append(
            f'<details><summary>Full report</summary><div class="prose">{md(m.report_markdown)}</div></details>'
        )
    cls = "failed" if m.status in (MemberStatus.failed, MemberStatus.timed_out) else ""
    return f'<article class="member {cls}" id="member-{esc(m.key)}"><h3>{esc(name)}</h3>{"".join(body)}</article>'


def _appendix(members: list[MemberReport], names: dict[str, str]) -> str:
    body = "".join(_member_card(m, names[m.key]) for m in members) or '<p class="empty">No members.</p>'
    return section(
        "members",
        "Member appendix",
        body,
        lede="Each member researched independently. Reports are reproduced as written.",
    )


# ---------------------------------------------------------------- page


def render_html(
    *, synthesis: Synthesis, members: list[MemberReport], validation: ValidationReport, config: RunConfig
) -> str:
    keys = member_order(synthesis, members)
    names = member_names(members, keys)
    grouped = claims_by_class(synthesis)
    toc = [
        ("answer", "Answer"),
        ("agreement", "Agreement"),
        ("disagreement", "Disagreement"),
        ("unique", "Unique"),
        ("contested", "Contested"),
        ("consensus", "Consensus vs evidence"),
        ("decision", "Decision matrix"),
        ("risks", "Risks & actions"),
        ("validation", "Validation"),
        ("members", "Members"),
    ]
    nav = '<nav aria-label="Sections">' + "".join(f'<a href="#{i}">{t}</a>' for i, t in toc) + "</nav>"
    body = "".join(
        [
            _header(synthesis, members, config, names),
            nav,
            "<main>",
            _answer(synthesis, grouped),
            _agreement(grouped[Classification.agreement]),
            _disagreement(grouped[Classification.disagreement], keys, names),
            _unique(grouped[Classification.unique], names),
            _contested(grouped[Classification.contested], keys, names),
            _consensus_vs_evidence(synthesis, grouped),
            _decision_matrix(synthesis),
            _risks_actions(synthesis),
            _validation(synthesis, validation, names),
            _appendix(members, names),
            "</main>",
            "<footer>Generated by model-committee. Member reports are model output and may contain errors; "
            "check the cited sources before acting.</footer>",
        ]
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(synthesis.narrative.headline)} · Committee report</title>\n"
        f'<style>{CSS}</style>\n</head>\n<body>\n<div class="page">{body}</div>\n</body>\n</html>\n'
    )


CSS = """
:root{
  --bg:#f7f7f5;--paper:#ffffff;--ink:#1c1d1f;--ink-2:#4a4d52;--ink-3:#73777e;--rule:#e2e2de;--rule-2:#ececea;
  --accent:#1f3a5f;
  --agree-bg:#e3f1e7;--agree-fg:#1d5e33;--agree-bd:#9ccaa9;
  --disagree-bg:#fbeadb;--disagree-fg:#8a3b0c;--disagree-bd:#e7b38c;
  --unique-bg:#e2ecf8;--unique-fg:#1f4a7c;--unique-bd:#a5c0e2;
  --contested-bg:#ececec;--contested-fg:#4b4f55;--contested-bd:#c4c6ca;
  --alert-bg:#fdecec;--alert-fg:#8f1d1d;--alert-bd:#e9a9a9;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#141517;--paper:#1c1d20;--ink:#e8e8e6;--ink-2:#b9bbbf;--ink-3:#8d9096;--rule:#33353a;--rule-2:#2a2c30;
  --accent:#9fb9dc;
  --agree-bg:#183323;--agree-fg:#9fd9b1;--agree-bd:#2f6a44;
  --disagree-bg:#3a2414;--disagree-fg:#f2b98d;--disagree-bd:#7a4520;
  --unique-bg:#172a42;--unique-fg:#a9c6ee;--unique-bd:#2f5584;
  --contested-bg:#2a2b2e;--contested-fg:#c3c5c9;--contested-bd:#4a4c51;
  --alert-bg:#3b1a1a;--alert-fg:#f3b0b0;--alert-bd:#7c2f2f;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.page{max-width:980px;margin:0 auto;padding:32px 20px 48px;background:var(--paper);min-height:100vh}
h1,h2,h3{line-height:1.25;margin:0 0 .5em;font-weight:650;letter-spacing:-.01em}
h1{font-size:1.9rem}
h2{font-size:1.3rem;border-bottom:2px solid var(--ink);padding-bottom:.3em;margin-top:0}
h3{font-size:1.02rem}
p{margin:.4em 0 .8em}
a{color:var(--accent);overflow-wrap:anywhere}
code{font:0.86em ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:0.86em ui-monospace,Consolas,monospace;margin:.3em 0}
section{margin:0 0 40px;scroll-margin-top:52px}
.kicker{text-transform:uppercase;letter-spacing:.12em;font-size:.75rem;color:var(--ink-3);margin:0 0 .4em;
  font-weight:600}
.label{display:inline-block;text-transform:uppercase;letter-spacing:.08em;font-size:.7rem;font-weight:700;
  color:var(--ink-3);margin-right:.4em}
.masthead{border-bottom:1px solid var(--rule);padding-bottom:20px;margin-bottom:12px}
.objective{color:var(--ink-2);font-size:1.02rem}
.meta{display:flex;flex-wrap:wrap;gap:8px 28px;margin:14px 0 10px}
.meta div{min-width:0}
.meta dt{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3);font-weight:700}
.meta dd{margin:0}
.members{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:.92rem}
.members li{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
nav{position:sticky;top:0;z-index:2;display:flex;gap:4px 14px;flex-wrap:wrap;padding:10px 0;margin-bottom:24px;
  background:var(--paper);border-bottom:1px solid var(--rule);font-size:.85rem}
nav a{text-decoration:none;color:var(--ink-2)}
nav a:hover{color:var(--accent);text-decoration:underline}
.count{display:inline-block;min-width:1.6em;padding:0 .45em;margin-left:.3em;border-radius:999px;
  background:var(--rule-2);
  color:var(--ink-2);font-size:.8rem;text-align:center;vertical-align:middle;font-weight:600}
.lede{color:var(--ink-2);font-size:.93rem;margin-top:-.2em}
.answer{font-size:1.1rem}
.callout{border-left:4px solid var(--accent);background:var(--rule-2);padding:10px 16px;margin:12px 0;
  border-radius:0 6px 6px 0}
.callout p{margin:.2em 0}
.callout.muted{border-left-color:var(--ink-3)}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
.stat{display:flex;align-items:center;gap:8px;border:1px solid var(--rule);border-radius:8px;padding:6px 12px}
.stat .num{font-size:1.35rem;font-weight:700;font-variant-numeric:tabular-nums}
.chip{display:inline-block;padding:1px 9px;border-radius:999px;font-size:.78rem;font-weight:650;
  border:1px solid;white-space:nowrap}
.chip.sm{font-size:.72rem;padding:0 7px}
.agree{--c-bg:var(--agree-bg);--c-fg:var(--agree-fg);--c-bd:var(--agree-bd)}
.disagree{--c-bg:var(--disagree-bg);--c-fg:var(--disagree-fg);--c-bd:var(--disagree-bd)}
.unique{--c-bg:var(--unique-bg);--c-fg:var(--unique-fg);--c-bd:var(--unique-bd)}
.reject{--c-bg:var(--alert-bg);--c-fg:var(--alert-fg);--c-bd:var(--alert-bd)}
.contested,.muted{--c-bg:var(--contested-bg);--c-fg:var(--contested-fg);--c-bd:var(--contested-bd)}
.chip{background:var(--c-bg);color:var(--c-fg);border-color:var(--c-bd)}
.claim{border:1px solid var(--rule);border-left:4px solid var(--c-bd);border-radius:6px;padding:12px 16px;
  margin:0 0 12px;break-inside:avoid}
.claim-head{display:flex;align-items:center;gap:10px}
.cid{font:600 .8rem ui-monospace,Consolas,monospace;color:var(--ink-3)}
.statement{font-size:1.02rem;font-weight:550;margin:.5em 0 .3em}
.facts{font-size:.82rem;color:var(--ink-3);margin:0 0 .4em}
.raised{font-size:.9rem;margin:.3em 0}
.note{font-size:.9rem;color:var(--ink-2);margin:.2em 0}
dl.why{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:10px 0;font-size:.92rem}
dl.why div{display:contents}
dl.why dt{font-weight:650;color:var(--ink-2)}
dl.why dd{margin:0}
.scroll{overflow-x:auto;margin:8px 0 12px;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{text-align:left;vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--rule)}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);
  border-bottom:2px solid var(--ink-3);white-space:nowrap}
table.cve td:nth-child(2){min-width:16em}
table.cve td:last-child{overflow-wrap:anywhere;min-width:9em}
th{white-space:normal}
table.decision td{min-width:9em}
table.checks td:first-child{max-width:22em;overflow-wrap:anywhere}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:28px}
ul,ol{padding-left:1.3em;margin:.3em 0 .8em}
li{margin:.25em 0}
.alert{background:var(--alert-bg);color:var(--alert-fg);border:1px solid var(--alert-bd);border-radius:6px;
  padding:10px 16px;margin:0 0 16px}
.alert .label{color:var(--alert-fg)}
.alert p,.alert ul{margin:.25em 0}
.bad{color:var(--alert-fg)}
.warn{color:var(--disagree-fg)}
.empty{color:var(--ink-3);font-style:italic}
.sources li{margin-bottom:.6em}
.url{display:block;font-size:.78rem;color:var(--ink-3);overflow-wrap:anywhere}
blockquote{margin:.3em 0;padding:.1em .9em;border-left:3px solid var(--rule);color:var(--ink-2);font-size:.9rem}
.member{border:1px solid var(--rule);border-radius:8px;padding:14px 18px;margin:0 0 16px}
.member.failed{border-color:var(--alert-bd);border-left:4px solid var(--alert-bd)}
dl.tele{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px 16px;margin:6px 0 12px}
dl.tele dt{font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3);font-weight:700}
dl.tele dd{margin:0;font-variant-numeric:tabular-nums}
details{border-top:1px solid var(--rule);margin-top:10px;padding-top:8px}
summary{cursor:pointer;font-weight:600;color:var(--accent)}
.prose{padding:6px 0 0;font-size:.93rem}
.prose h1{font-size:1.25rem}.prose h2{font-size:1.1rem;border:0;padding:0;margin-top:1em}.prose h3{font-size:1rem}
.prose pre{background:var(--rule-2);padding:8px 10px;border-radius:4px}
footer{margin-top:32px;padding-top:12px;border-top:1px solid var(--rule);font-size:.8rem;color:var(--ink-3)}
@media (max-width:640px){
  body{font-size:14.5px}
  .page{padding:20px 16px 36px}
  h1{font-size:1.45rem}
  .two-col{grid-template-columns:1fr;gap:8px}
  dl.why{grid-template-columns:1fr}
  dl.why dd{margin-bottom:6px}
  nav{position:static}
  .claim,.member{padding:10px 12px}
  table.stances th:nth-child(4),table.stances td:nth-child(4){display:none}
}
@media print{
  :root{--bg:#fff;--paper:#fff;--ink:#000;--ink-2:#222;--ink-3:#555;--rule:#ccc;--rule-2:#f1f1f1;--accent:#000}
  body{font-size:10.5pt;background:#fff}
  .page{max-width:none;padding:0}
  nav{display:none}
  section{break-inside:auto}
  h2{break-after:avoid}
  .chip,.alert,.callout,.claim,.count{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  details > summary{display:none}
  details::details-content{content-visibility:visible;display:block}
  details{border:0}
  .scroll{overflow:visible}
  a{color:#000;text-decoration:underline}
}
"""
