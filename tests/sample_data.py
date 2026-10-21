"""Realistic committee-run fixture shared by output, server and synthesis tests.

    from tests.sample_data import make_sample
    sample = make_sample()                     # 3 members, every classification present
    sample = make_sample(failed_member=True)   # m3 failed: excluded from the matrix, shown as failed
    sample = make_sample(empty=True)           # no claims, no decision matrix, no risks/actions
    sample = make_sample(mcq=True)             # lettered options, reasoning mode, 2-1 vote (B: m1, m2; C: m3)

Returns {"config": RunConfig, "synthesis": Synthesis, "members": list[MemberReport],
"validation": ValidationReport}. Classification counts are derived from the stances so they stay
consistent with each other whatever the flags.
"""

from __future__ import annotations

from urllib.parse import urlparse

from model_committee.schemas import (
    CanonicalClaim,
    CitationCheck,
    ClaimMatrix,
    Classification,
    DecisionRow,
    Deliverable,
    Depth,
    DisagreementType,
    Evidence,
    MemberClaim,
    MemberFindings,
    MemberReport,
    MemberStatus,
    Mode,
    Position,
    ProviderTelemetry,
    RunConfig,
    Synthesis,
    SynthesisNarrative,
    ValidationReport,
)

OBJECTIVE = (
    "Which third-party logistics (3PL) provider should an Australian D2C apparel brand shipping "
    "~12,000 orders/month choose for 2027: Northwind Logistics, Contoso Freight or Fabrikam Fulfilment?"
)

MCQ_OBJECTIVE = "\n".join(
    [
        "Which 3PL provider should an Australian D2C apparel brand shipping ~12,000 orders/month choose for 2027?",
        "A) Northwind Logistics",
        "B) Contoso Freight",
        "C) Fabrikam Fulfilment",
        "D) Keep fulfilment in-house",
    ]
)

CRITERIA = ["Cost per order", "Metro delivery SLA", "Contract flexibility", "Operational risk"]

M1, M2, M3 = "m1-claude-cli", "m2-codex-cli", "m3-agy-gemini"
MCQ_PICKS = {M1: "B", M2: "B", M3: "C"}

UNTRACEABLE_URL = "https://www.3pl-rankings.example.com/australia-2026"

# -- sources (url -> (title, quote))
SRC = {
    "contoso_rates": (
        "https://www.contosofreight.com.au/pricing/ecommerce-2027",
        "Contoso Freight 2027 e-commerce rate card",
        "Pick, pack and dispatch from $3.10 per order for volumes above 10,000 orders per month.",
    ),
    "northwind_rates": (
        "https://northwind-logistics.com.au/rates",
        "Northwind Logistics published rates",
        "Standard fulfilment fee $3.85 per order; volume discounts negotiated individually.",
    ),
    "fabrikam_rates": (
        "https://fabrikam.com.au/fulfilment/pricing",
        "Fabrikam Fulfilment pricing sheet",
        "From $3.60 per order including standard packaging.",
    ),
    "sla_survey": (
        "https://www.choice.com.au/shopping/online-shopping/delivery-times-2026",
        "CHOICE delivery-time survey 2026",
        "All three providers delivered 96%+ of Sydney and Melbourne metro parcels within two business days.",
    ),
    "northwind_press": (
        "https://northwind-logistics.com.au/news/brisbane-dc",
        "Northwind press release: Brisbane distribution centre",
        "Our new Brisbane distribution centre will be operational in the second quarter of 2027.",
    ),
    "council_da": (
        "https://developmenti.brisbane.qld.gov.au/Home/ApplicationDetailsView/A005912345",
        "Brisbane City Council development application A005912345",
        "Status: Information request issued. Decision pending.",
    ),
    "fabrikam_contract": (
        "https://fabrikam.com.au/legal/fulfilment-terms",
        "Fabrikam Fulfilment standard terms",
        "Clause 7.2: the Customer commits to a minimum of 9,000 orders per month for the first 12 months; "
        "shortfalls are invoiced at $1.20 per order.",
    ),
    "contoso_incident": (
        "https://status.contosofreight.com.au/incidents/2025-11-28",
        "Contoso Freight incident report, 28 Nov 2025",
        "Warehouse management system outage at the Erskine Park site; dispatch resumed after 71 hours.",
    ),
    "afr_growth": (
        "https://www.afr.com/companies/retail/online-apparel-growth-2026",
        "AFR: Online apparel sales growth 2026",
        "Online apparel sales grew 24% year on year in the 12 months to June 2026.",
    ),
    "northwind_carbon": (
        "https://northwind-logistics.com.au/sustainability",
        "Northwind sustainability statement",
        "All deliveries are carbon neutral through our offset partner programme.",
    ),
}


def _ev(key: str) -> Evidence:
    url, title, quote = SRC[key]
    return Evidence(source=title, url=url, quote=quote)


def _claim(cid: str, statement: str, confidence: float, *sources: str, assumptions=()) -> MemberClaim:
    return MemberClaim(
        id=cid,
        statement=statement,
        confidence=confidence,
        evidence=[_ev(s) for s in sources],
        assumptions=list(assumptions),
    )


# ---------------------------------------------------------------- member reports


def _member_claims() -> dict[str, list[MemberClaim]]:
    return {
        M1: [
            _claim("c1", "Contoso Freight is the cheapest per order at our volume (~$3.10).", 0.85, "contoso_rates"),
            _claim("c2", "All three providers meet a two-business-day metro SLA.", 0.8, "sla_survey"),
            _claim(
                "c3",
                "Northwind's Brisbane distribution centre will open by Q2 2027.",
                0.6,
                "northwind_press",
            ),
            _claim(
                "c4",
                "Switching costs are unlikely to be recovered within 9 months at flat volume.",
                0.65,
                "contoso_rates",
                "northwind_rates",
                assumptions=["Order volume stays flat at 12,000/month through 2027."],
            ),
            _claim(
                "c5",
                "Northwind's carbon-neutral claim relies on offsets with no named certifier.",
                0.4,
                "northwind_carbon",
            ),
        ],
        M2: [
            _claim(
                "c1",
                "Contoso has the lowest fulfilment fee above 10k orders/month.",
                0.9,
                "contoso_rates",
                "fabrikam_rates",
            ),
            _claim("c2", "Metro two-day delivery performance is equivalent across providers.", 0.75, "sla_survey"),
            _claim(
                "c3",
                "The Brisbane DC timeline is doubtful: the development approval is still pending.",
                0.7,
                "council_da",
            ),
            _claim(
                "c4",
                "Moving to Contoso pays back within 9 months if volume grows ~25% a year.",
                0.7,
                "contoso_rates",
                "afr_growth",
                assumptions=["Order volume grows 25% year on year, in line with category growth."],
            ),
            _claim(
                "c5", "Contoso had a 71-hour warehouse outage during the November 2025 peak.", 0.85, "contoso_incident"
            ),
            _claim(
                "c6", "Northwind's carbon-neutral delivery is not independently certified.", 0.45, "northwind_carbon"
            ),
        ],
        M3: [
            _claim("c1", "Contoso is cheapest per order; Fabrikam is second.", 0.8, "contoso_rates", "fabrikam_rates"),
            _claim(
                "c2", "Delivery SLAs are broadly comparable, though regional performance varies.", 0.55, "sla_survey"
            ),
            _claim(
                "c3", "Payback on switching to Contoso is under 9 months given category growth.", 0.65, "afr_growth"
            ),
            _claim(
                "c4",
                "Fabrikam requires a 12-month minimum of 9,000 orders/month with a shortfall penalty.",
                0.9,
                "fabrikam_contract",
            ),
        ],
    }


REPORT_MD = {
    M1: """# 3PL provider assessment

## Summary
Contoso Freight offers the **lowest per-order cost** at our volume, but the business case for switching
depends heavily on order growth. At flat volume the switching cost is not recovered within 9 months.

## Cost comparison
| Provider | Fee per order | Source |
|---|---|---|
| Contoso Freight | $3.10 | [rate card](https://www.contosofreight.com.au/pricing/ecommerce-2027) |
| Fabrikam Fulfilment | $3.60 | [pricing sheet](https://fabrikam.com.au/fulfilment/pricing) |
| Northwind Logistics | $3.85 | [published rates](https://northwind-logistics.com.au/rates) |

## Delivery performance
All three meet a two-business-day metro SLA according to the 2026 CHOICE survey.

## Expansion plans
Northwind says its Brisbane DC will be operational in Q2 2027, which would cut Queensland transit times.

## Caveats
- Northwind's carbon-neutral claim relies on offsets with no named certifier.
- Integration effort with our storefront was not costed.
""",
    M2: """# Recommendation: Contoso Freight, with a peak-season contingency

Contoso has the lowest fulfilment fee above 10,000 orders/month (<https://www.contosofreight.com.au/pricing/ecommerce-2027>).
If order volume tracks category growth (~25%/yr), payback on switching is under 9 months.

### Risks
1. **Peak reliability.** Contoso suffered a 71-hour outage at Erskine Park on 28 Nov 2025.
2. Northwind's Brisbane DC is unlikely to open on schedule: the council development application is
   still awaiting a decision.

> Northwind's carbon-neutral claim could not be verified against an independent certifier.

```text
payback_months = switching_cost / (monthly_orders * fee_saving)
```
""",
    M3: """## Findings

* Contoso is cheapest per order; Fabrikam is second.
* SLAs are broadly comparable in metro areas; regional performance varies.
* Fabrikam's standard terms (clause 7.2) impose a **12-month minimum of 9,000 orders/month**, with
  shortfalls invoiced at $1.20/order.
* Category growth (24% YoY) makes a sub-9-month payback on switching plausible.

Overall: prefer Contoso, negotiate peak-season guarantees.
""",
}


def _members(failed_member: bool) -> list[MemberReport]:
    claims = _member_claims()
    base = [
        dict(
            key=M1,
            spec="claude-cli",
            label="Claude (CLI)",
            vendor="anthropic",
            duration=412.6,
            tin=48_210,
            tout=9_844,
            cost=1.84,
            searches=14,
            conf=0.7,
            summary="Contoso is cheapest, but switching only pays back quickly if volume grows.",
            rec="Shortlist Contoso; model payback under flat and growth scenarios before committing.",
        ),
        dict(
            key=M2,
            spec="codex-cli",
            label="Codex (CLI)",
            vendor="openai",
            duration=365.2,
            tin=61_002,
            tout=7_311,
            cost=None,
            searches=9,
            conf=0.75,
            summary="Contoso is the cost leader; its Nov 2025 outage is the main risk to manage.",
            rec="Choose Contoso Freight with contractual peak-season guarantees.",
        ),
        dict(
            key=M3,
            spec="agy:gemini-3.8-flash-high",
            label="Gemini 3.8 Flash (agy)",
            vendor="google",
            duration=288.9,
            tin=35_400,
            tout=5_120,
            cost=0.21,
            searches=6,
            conf=0.65,
            summary="Contoso is cheapest; Fabrikam's minimum-volume clause is a hidden cost.",
            rec="Prefer Contoso; avoid Fabrikam's minimum-volume commitment.",
        ),
    ]
    reports = []
    for i, b in enumerate(base):
        failed = failed_member and b["key"] == M3
        telemetry = ProviderTelemetry(
            provider=b["spec"].split(":")[0],
            model=b["spec"].split(":")[1] if ":" in b["spec"] else None,
            duration_s=31.4 if failed else b["duration"],
            input_tokens=None if failed else b["tin"],
            output_tokens=None if failed else b["tout"],
            cost_usd=None if failed else b["cost"],
            web_searches=None if failed else b["searches"],
            exit_code=1 if failed else 0,
        )
        common = dict(
            key=b["key"],
            spec=b["spec"],
            label=b["label"],
            vendor=b["vendor"],
            telemetry=telemetry,
            context_files=["brief.md"],
            started_at=f"2026-10-20T09:0{i}:00+00:00",
        )
        if failed:
            reports.append(
                MemberReport(
                    **common,
                    status=MemberStatus.failed,
                    attempts=2,
                    error="agy exited with code 1: quota exhausted for model gemini-3.8-flash-high (retry after 3600s)",
                    finished_at="2026-10-20T09:03:31+00:00",
                )
            )
            continue
        reports.append(
            MemberReport(
                **common,
                status=MemberStatus.succeeded,
                attempts=1,
                report_markdown=REPORT_MD[b["key"]],
                finished_at=f"2026-10-20T09:1{i}:00+00:00",
                findings=MemberFindings(
                    summary=b["summary"],
                    recommendation=b["rec"],
                    confidence=b["conf"],
                    assumptions=["Order volume of ~12,000/month", "Metro-heavy delivery mix (80% Sydney/Melbourne)"],
                    missing_information=["Negotiated (not list) rates", "Integration cost with the storefront"],
                    claims=claims[b["key"]],
                    counterargument="Northwind's Brisbane DC could make it the best option for Queensland customers.",
                    would_change_mind=["A firm Northwind price below $3.30/order"],
                ),
            )
        )
    return reports


# ---------------------------------------------------------------- claim matrix


def _pos(stance: str, ref: str | None = None, note: str | None = None) -> Position:
    return Position(stance=stance, claim_refs=[ref] if ref else [], note=note)


def _canonical() -> list[CanonicalClaim]:
    return [
        CanonicalClaim(
            id="K1",
            statement="Contoso Freight has the lowest per-order fulfilment cost at ~12,000 orders/month.",
            positions={
                M1: _pos("support", f"{M1}/c1"),
                M2: _pos("support", f"{M2}/c1"),
                M3: _pos("support", f"{M3}/c1"),
            },
        ),
        CanonicalClaim(
            id="K2",
            statement="All three providers meet a two-business-day delivery SLA in Sydney and Melbourne.",
            positions={
                M1: _pos("support", f"{M1}/c2"),
                M2: _pos("support", f"{M2}/c2"),
                M3: _pos("uncertain", f"{M3}/c2", "Comparable in metro areas; regional performance varies."),
            },
        ),
        CanonicalClaim(
            id="K3",
            statement="Northwind's Brisbane distribution centre will be operational by Q2 2027.",
            positions={
                M1: _pos("support", f"{M1}/c3", "Relies on the operator's press release."),
                M2: _pos("reject", f"{M2}/c3", "Council development application still pending."),
                M3: _pos("not_mentioned"),
            },
            disagreement_type=DisagreementType.forecast,
            disagreement_cause=(
                "Claude relies on Northwind's own press release; Codex cites the council record showing the "
                "development application is still awaiting a decision."
            ),
        ),
        CanonicalClaim(
            id="K4",
            statement="Switching to Contoso pays back its switching cost within 9 months.",
            positions={
                M1: _pos("reject", f"{M1}/c4", "Assumes flat volume."),
                M2: _pos("support", f"{M2}/c4", "Assumes 25% annual growth."),
                M3: _pos("support", f"{M3}/c3", "Assumes category growth of 24%."),
            },
            disagreement_type=DisagreementType.assumption,
            disagreement_cause=(
                "Members assume different order growth: flat volume (Claude) versus ~25% a year (Codex, Gemini)."
            ),
        ),
        CanonicalClaim(
            id="K5",
            statement="Contoso had a 71-hour warehouse outage during the November 2025 peak.",
            positions={M2: _pos("support", f"{M2}/c5"), M1: _pos("not_mentioned"), M3: _pos("not_mentioned")},
        ),
        CanonicalClaim(
            id="K6",
            statement="Fabrikam requires a 12-month minimum of 9,000 orders/month with a $1.20/order shortfall fee.",
            positions={M3: _pos("support", f"{M3}/c4"), M1: _pos("not_mentioned"), M2: _pos("not_mentioned")},
        ),
        CanonicalClaim(
            id="K7",
            statement="Northwind's carbon-neutral delivery claim is independently certified.",
            positions={
                M1: _pos("uncertain", f"{M1}/c5", "No certifier named."),
                M2: _pos("uncertain", f"{M2}/c6", "Could not verify."),
                M3: _pos("not_mentioned"),
            },
        ),
    ]


def _classify(claim: CanonicalClaim, members: list[MemberReport], present: list[str]) -> CanonicalClaim:
    """Fill the deterministic fields the way synthesis.classify() does."""
    stances = [p.stance for k, p in claim.positions.items() if k in present]
    raised = [s for s in stances if s != "not_mentioned"]
    support, reject, uncertain = raised.count("support"), raised.count("reject"), raised.count("uncertain")
    if len(raised) <= 1:
        cls = Classification.unique
    elif support and reject:
        cls = Classification.disagreement
    elif support >= 2:
        cls = Classification.agreement
    else:
        cls = Classification.contested

    by_key = {m.key: m for m in members}
    domains: set[str] = set()
    for pos in claim.positions.values():
        for ref in pos.claim_refs:
            key, _, cid = ref.partition("/")
            findings = by_key[key].findings if key in by_key else None
            for mc in findings.claims if findings else []:
                if mc.id == cid:
                    domains.update(urlparse(e.url).hostname.removeprefix("www.") for e in mc.evidence if e.url)

    update = dict(
        classification=cls,
        support=support,
        reject=reject,
        uncertain=uncertain,
        model_consensus=round(support / len(present), 3) if present else 0.0,
        independent_sources=len(domains),
        source_domains=sorted(domains),
    )
    if cls is not Classification.disagreement:
        update.update(disagreement_type=None, disagreement_cause=None)
    return claim.model_copy(update=update)


# ---------------------------------------------------------------- narrative + validation


def _narrative(empty: bool) -> SynthesisNarrative:
    if empty:
        return SynthesisNarrative(
            headline="No actionable consensus: members returned no comparable claims",
            answer="The committee did not produce claims that could be aligned, so no recommendation is made.",
            recommendation=None,
            confidence_note="Low: no claims were extracted from member reports.",
        )
    return SynthesisNarrative(
        headline="Choose Contoso Freight, but negotiate peak-season guarantees first",
        answer=(
            "Contoso Freight is the lowest-cost provider at ~12,000 orders/month (all members agree) and meets "
            "the metro two-day SLA. The case for switching quickly depends on order growth, and Contoso's "
            "November 2025 outage is a reliability risk that only one member surfaced."
        ),
        recommendation=(
            "Shortlist Contoso Freight and negotiate contractual peak-season dispatch guarantees; model "
            "payback under flat and 25%-growth scenarios before signing. Avoid Fabrikam's minimum-volume terms."
        ),
        confidence_note=(
            "Moderate. Cost evidence comes from published rate cards (not negotiated rates); payback depends on "
            "an unresolved growth assumption."
        ),
        decision_matrix=[
            DecisionRow(
                option="Contoso Freight",
                scores={
                    "Cost per order": "$3.10 (lowest)",
                    "Metro delivery SLA": "Meets 2-day",
                    "Contract flexibility": "Standard 12-month term",
                    "Operational risk": "71h outage Nov 2025",
                },
                verdict="Preferred",
            ),
            DecisionRow(
                option="Fabrikam Fulfilment",
                scores={
                    "Cost per order": "$3.60",
                    "Metro delivery SLA": "Meets 2-day",
                    "Contract flexibility": "9k/month minimum, shortfall fee",
                    "Operational risk": "No incidents found",
                },
                verdict="Fallback",
            ),
            DecisionRow(
                option="Northwind Logistics",
                scores={
                    "Cost per order": "$3.85",
                    "Metro delivery SLA": "Meets 2-day",
                    "Contract flexibility": "Negotiable",
                    "Operational risk": "Brisbane DC timeline uncertain",
                },
                verdict="Not recommended",
            ),
        ],
        unresolved_risks=[
            "Order growth assumption (flat vs 25%/yr) decides whether switching pays back within 9 months.",
            "Contoso peak-season reliability after the November 2025 outage.",
            "Negotiated rates may differ materially from published rate cards.",
        ],
        next_actions=[
            "Request negotiated quotes from Contoso and Fabrikam for 12k and 15k orders/month.",
            "Ask Contoso for its post-incident report and peak-season capacity plan.",
            "Rebuild the payback model with flat, 10% and 25% growth scenarios.",
        ],
        citations=[
            _ev("contoso_rates"),
            _ev("sla_survey"),
            _ev("contoso_incident"),
            _ev("fabrikam_contract"),
            Evidence(
                source="3PL rankings Australia 2026",
                url=UNTRACEABLE_URL,
                quote="Contoso ranked first for value among national 3PLs.",
            ),
        ],
    )


def _validation(empty: bool) -> ValidationReport:
    if empty:
        return ValidationReport(warnings=["No claims were extracted; nothing to validate."])
    return ValidationReport(
        citations=[
            CitationCheck(
                url=SRC["contoso_rates"][0],
                traced_to_members=[M1, M2, M3],
                fetched=True,
                http_status=200,
                quote_found=True,
            ),
            CitationCheck(
                url=SRC["sla_survey"][0],
                traced_to_members=[M1, M2, M3],
                fetched=True,
                http_status=200,
                quote_found=True,
            ),
            CitationCheck(
                url=SRC["contoso_incident"][0],
                traced_to_members=[M2],
                fetched=True,
                http_status=200,
                quote_found=False,
                note="Quoted duration not found on page.",
            ),
            CitationCheck(
                url=SRC["fabrikam_contract"][0], traced_to_members=[M3], fetched=False, note="Fetch blocked (403)."
            ),
            CitationCheck(url=UNTRACEABLE_URL, traced_to_members=[], note="Not cited by any member."),
        ],
        untraceable_citations=[UNTRACEABLE_URL],
        unique_findings_preserved=True,
        warnings=["1 synthesis citation is not traceable to any member report."],
    )


def _tally(members: list[MemberReport]) -> dict[str, list[str]]:
    """Option -> member keys, most votes first (same ordering as synthesis.tally_answers)."""
    tally: dict[str, list[str]] = {}
    for m in members:
        if m.findings and m.findings.answer:
            tally.setdefault(m.findings.answer, []).append(m.key)
    return dict(sorted(tally.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def make_sample(*, failed_member: bool = False, empty: bool = False, mcq: bool = False) -> dict:
    """Build a consistent committee run. See module docstring for the flags."""
    members = _members(failed_member)
    present = [m.key for m in members if m.status is MemberStatus.succeeded]
    missing = [m.key for m in members if m.status is not MemberStatus.succeeded]
    objective = MCQ_OBJECTIVE if mcq else OBJECTIVE
    if mcq:
        for m in members:
            if m.findings:
                m.findings.answer = MCQ_PICKS[m.key]

    claims: list[CanonicalClaim] = []
    if not empty:
        for claim in _canonical():
            positions = {k: p for k, p in claim.positions.items() if k in present}
            if any(p.stance != "not_mentioned" for p in positions.values()):
                claims.append(_classify(claim.model_copy(update={"positions": positions}), members, present))

    config = RunConfig(
        objective=objective,
        mode=Mode.reasoning if mcq else Mode.research,
        models=[m.spec for m in members],
        synthesizer="claude-cli",
        depth=Depth.standard,
        deliverable=Deliverable.all,
        criteria=list(CRITERIA),
        context_paths=["brief.md"],
    )
    synthesis = Synthesis(
        objective=objective,
        mode="reasoning" if mcq else "research",
        answer_tally=_tally(members) if mcq else {},
        synthesizer="claude-cli",
        narrative=_narrative(empty),
        matrix=ClaimMatrix(members=present, claims=claims),
        members_succeeded=present,
        members_missing=missing,
        markdown="# Synthesis\n\n" + _narrative(empty).answer,
    )
    return {"config": config, "synthesis": synthesis, "members": members, "validation": _validation(empty)}
