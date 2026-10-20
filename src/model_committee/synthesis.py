"""Fan-in: align member claims (model), classify them (code), write the narrative (model), render synthesis.md.

The model never decides what counts as agreement, disagreement or a unique finding. It only maps member claims onto
canonical claims; classification and counts are computed here, and every member claim is guaranteed a place in the
matrix, so the narrative stage cannot silently drop a minority finding.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from model_committee.parsing import parse_model
from model_committee.prompts import alignment_prompt, narrative_prompt
from model_committee.providers.base import Provider, ProviderError
from model_committee.schemas import (
    DISAGREEMENT_HANDLING,
    CanonicalClaim,
    ClaimMatrix,
    Classification,
    MemberReport,
    Position,
    RunConfig,
    Synthesis,
    SynthesisNarrative,
)

ATTEMPTS = 2


class SynthesisError(RuntimeError):
    pass


# ---------------------------------------------------------------- model calls


async def _ask(provider: Provider, prompt: str, model_cls, *, workdir: Path, timeout_s: float):
    """Call the synthesizer and parse into model_cls, feeding the parse error back once."""
    last_error = ""
    for _ in range(ATTEMPTS):
        text = prompt if not last_error else (
            f"{prompt}\n\nYour previous reply could not be used: {last_error}\nReply again with ONLY the JSON block."
        )
        result = await provider.generate(text, workdir=workdir, timeout_s=timeout_s, research=False)
        try:
            return parse_model(result.text, model_cls), result
        except ValueError as exc:
            last_error = str(exc)
    raise SynthesisError(f"synthesizer output unusable after {ATTEMPTS} attempts: {last_error}")


class _Alignment(ClaimMatrix):
    members: list[str] = []


async def align_claims(
    provider: Provider, objective: str, members: list[MemberReport], *, workdir: Path, timeout_s: float
) -> tuple[ClaimMatrix, list[str]]:
    """Returns (matrix, warnings). Falls back to one claim per member claim if the aligner fails."""
    warnings: list[str] = []
    with_claims = [m for m in members if m.findings and m.findings.claims]
    if not with_claims:
        return ClaimMatrix(members=[m.key for m in members], claims=[]), ["no member produced structured claims"]
    try:
        aligned, _ = await _ask(provider, alignment_prompt(objective, members), _Alignment,
                                workdir=workdir, timeout_s=timeout_s)
        claims = aligned.claims
    except (SynthesisError, ProviderError, TimeoutError) as exc:
        warnings.append(f"claim alignment failed ({exc}); every member claim is listed separately")
        claims = []
    matrix = ClaimMatrix(members=[m.key for m in members], claims=claims)
    warnings += complete_matrix(matrix, members)
    return matrix, warnings


async def write_narrative(
    provider: Provider, config: RunConfig, members: list[MemberReport], matrix: ClaimMatrix, *,
    granularity: str, workdir: Path, timeout_s: float,
) -> tuple[SynthesisNarrative, list[str]]:
    matrix_json = json.dumps(_matrix_digest(matrix), indent=1, ensure_ascii=False)
    try:
        narrative, _ = await _ask(provider, narrative_prompt(config, members, matrix_json, granularity),
                                  SynthesisNarrative, workdir=workdir, timeout_s=timeout_s)
        return narrative, []
    except (SynthesisError, ProviderError, TimeoutError) as exc:
        recs = [f"- **{m.key}**: {m.findings.recommendation}"
                for m in members if m.findings and m.findings.recommendation]
        fallback = SynthesisNarrative(
            headline="Synthesis narrative unavailable - see the claim matrix and member reports",
            answer="The synthesizer failed to produce a narrative. Member recommendations:\n\n" + "\n".join(recs),
            confidence_note="No synthesized confidence; read the member reports directly.",
        )
        return fallback, [f"narrative synthesis failed: {exc}"]


# ---------------------------------------------------------------- deterministic stages


def complete_matrix(matrix: ClaimMatrix, members: list[MemberReport]) -> list[str]:
    """Repair the aligner's output in place: valid member keys only, a position for every member, and every
    member claim referenced somewhere (unreferenced ones become their own canonical claim)."""
    warnings: list[str] = []
    keys = [m.key for m in members]
    valid_refs = {f"{m.key}/{c.id}" for m in members if m.findings for c in m.findings.claims}
    # Members without structured claims (unparsed reports) are aligned from prose, so they can't carry refs.
    has_claims = {m.key for m in members if m.findings and m.findings.claims}
    for i, claim in enumerate(matrix.claims, start=1):
        claim.id = f"k{i}"
        unknown = set(claim.positions) - set(keys)
        if unknown:
            warnings.append(f"aligner referenced unknown members {sorted(unknown)} on {claim.id}; ignored")
        claim.positions = {k: claim.positions.get(k, Position(stance="not_mentioned")) for k in keys}
        for k, pos in claim.positions.items():
            refs = [r if "/" in r else f"{k}/{r}" for r in pos.claim_refs]  # tolerate bare "c1"
            pos.claim_refs = [r for r in refs if r in valid_refs and r.startswith(f"{k}/")]
            if pos.stance in ("support", "reject") and not pos.claim_refs and k in has_claims:
                # A firm position must trace to something the member actually claimed, otherwise it could be
                # counted here and again when that member's own claim is restored below.
                warnings.append(f"{claim.id}: {k} {pos.stance} had no valid claim reference; set to uncertain")
                pos.stance = "uncertain"
    invented = [c for c in matrix.claims if all(p.stance == "not_mentioned" for p in c.positions.values())]
    if invented:
        warnings.append(f"dropped {len(invented)} aligner claim(s) that no member holds")
        matrix.claims = [c for c in matrix.claims if c not in invented]
        for i, claim in enumerate(matrix.claims, start=1):
            claim.id = f"k{i}"

    referenced = {r for c in matrix.claims for p in c.positions.values() for r in p.claim_refs}
    for m in members:
        if not m.findings:
            continue
        for c in m.findings.claims:
            ref = f"{m.key}/{c.id}"
            if ref in referenced:
                continue
            positions = {k: Position(stance="not_mentioned") for k in keys}
            positions[m.key] = Position(stance="support", claim_refs=[ref])
            matrix.claims.append(CanonicalClaim(id=f"k{len(matrix.claims) + 1}", statement=c.statement,
                                                positions=positions))
            warnings.append(f"aligner dropped {ref}; restored as its own claim")
    return warnings


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return None
    return host.removeprefix("www.") or None


def classify(matrix: ClaimMatrix, members: list[MemberReport]) -> ClaimMatrix:
    evidence = {f"{m.key}/{c.id}": c.evidence for m in members if m.findings for c in m.findings.claims}
    n = max(len(matrix.members), 1)
    for claim in matrix.claims:
        stances = [p.stance for p in claim.positions.values()]
        claim.support = stances.count("support")
        claim.reject = stances.count("reject")
        claim.uncertain = stances.count("uncertain")
        mentioned = n - stances.count("not_mentioned")
        if claim.support and claim.reject:
            claim.classification = Classification.disagreement
        elif mentioned <= 1:
            claim.classification = Classification.unique
        elif claim.support >= 2:
            claim.classification = Classification.agreement
        else:
            claim.classification = Classification.contested
        if claim.classification is not Classification.disagreement:
            claim.disagreement_type = None
            claim.disagreement_cause = None
        claim.model_consensus = round(claim.support / n, 3)
        domains = {
            d
            for p in claim.positions.values() if p.stance == "support"
            for ref in p.claim_refs for e in evidence.get(ref, [])
            if (d := _domain(e.url))
        }
        claim.source_domains = sorted(domains)
        claim.independent_sources = len(domains)
    return matrix


def _matrix_digest(matrix: ClaimMatrix) -> list[dict]:
    return [
        {"id": c.id, "statement": c.statement, "classification": c.classification,
         "stances": {k: p.stance for k, p in c.positions.items()},
         "model_consensus": c.model_consensus, "independent_sources": c.independent_sources,
         "disagreement_type": c.disagreement_type, "disagreement_cause": c.disagreement_cause}
        for c in matrix.claims
    ]


# ---------------------------------------------------------------- markdown


def _stances(claim: CanonicalClaim) -> str:
    return ", ".join(f"{k}: {p.stance}" for k, p in claim.positions.items() if p.stance != "not_mentioned")


def render_markdown(synth: Synthesis, members: list[MemberReport]) -> str:
    n = synth.narrative
    by_class = {c: [x for x in synth.matrix.claims if x.classification == c] for c in Classification}
    out = [f"# {n.headline}", "", f"**Objective:** {synth.objective}", "",
           f"Synthesizer: `{synth.synthesizer}` | members reporting: {len(synth.members_succeeded)}"
           + (f" | missing: {', '.join(synth.members_missing)}" if synth.members_missing else ""), "",
           "## Answer", "", n.answer, ""]
    if n.recommendation:
        out += ["## Recommendation", "", n.recommendation, ""]
    out += ["## Confidence", "", n.confidence_note, ""]

    out += ["## Where the members agree", ""]
    agreed = by_class[Classification.agreement]
    out += [f"- {c.statement} _(consensus {c.model_consensus:.0%}, {c.independent_sources} independent source "
            f"domain{'s' if c.independent_sources != 1 else ''})_" for c in agreed] or ["- none"]
    out += ["", "## Where they disagree, and why", ""]
    for c in by_class[Classification.disagreement]:
        kind = c.disagreement_type.value if c.disagreement_type else "unclassified"
        out.append(f"- **{c.statement}** — {kind} disagreement ({_stances(c)}).")
        if c.disagreement_cause:
            out.append(f"  - Cause: {c.disagreement_cause}")
        if c.disagreement_type:
            out.append(f"  - Handling: {DISAGREEMENT_HANDLING[c.disagreement_type]}")
    if not by_class[Classification.disagreement]:
        out.append("- none")
    out += ["", "## Unique findings (raised by one member only)", ""]
    for c in by_class[Classification.unique]:
        who = next((k for k, p in c.positions.items() if p.stance != "not_mentioned"), "?")
        out.append(f"- {c.statement} _(only {who})_")
    if not by_class[Classification.unique]:
        out.append("- none")
    if by_class[Classification.contested]:
        out += ["", "## Contested / unresolved", ""]
        out += [f"- {c.statement} ({_stances(c)})" for c in by_class[Classification.contested]]

    if n.decision_matrix:
        criteria = sorted({k for row in n.decision_matrix for k in row.scores})
        out += ["", "## Decision matrix", "", "| Option | " + " | ".join(criteria) + " | Verdict |",
                "|---" * (len(criteria) + 2) + "|"]
        for row in n.decision_matrix:
            cells = [row.scores.get(k, "") for k in criteria]
            out.append("| " + " | ".join([row.option, *cells, row.verdict or ""]).replace("\n", " ") + " |")
    if n.unresolved_risks:
        out += ["", "## Unresolved risks", ""] + [f"- {r}" for r in n.unresolved_risks]
    if n.next_actions:
        out += ["", "## Next verification actions", ""] + [f"- {a}" for a in n.next_actions]

    out += ["", "## Member positions", ""]
    for m in members:
        if m.findings:
            out.append(f"- **{m.key}** ({m.label}, confidence {m.findings.confidence:.0%}): "
                       f"{m.findings.recommendation or m.findings.summary}")
        else:
            out.append(f"- **{m.key}** ({m.label}): {m.status.value} {m.error or m.parse_error or ''}".rstrip())
    if n.citations:
        out += ["", "## Sources cited in the synthesis", ""]
        out += [f"{i}. {c.source} {c.url or ''}".rstrip() for i, c in enumerate(n.citations, start=1)]
    out += ["", "_Model consensus is not proof: members may share training data or sources. Check independent "
            "source counts and the member reports before acting on a material decision._", ""]
    return "\n".join(out)
