"""Typed contracts shared by every stage of a committee run.

Data flows: RunConfig -> (fan-out) MemberReport x N -> ClaimMatrix -> Synthesis -> ValidationReport -> outputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MIN_MEMBERS = 2
MAX_MEMBERS = 8


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Depth(StrEnum):
    brief = "brief"
    standard = "standard"
    detailed = "detailed"


class JoinPolicy(StrEnum):
    all = "all"  # wait for every member; fail the run if fewer than min_members succeed
    quorum = "quorum"  # once min_members succeed, give stragglers quorum_grace_s then cancel them


class Deliverable(StrEnum):
    none = "none"  # synthesis.md + member reports only
    report = "report"  # standalone HTML report
    spreadsheet = "spreadsheet"  # xlsx claim matrix + evidence
    deck = "deck"  # pptx board deck
    all = "all"


# ---------------------------------------------------------------- run configuration


class RunConfig(BaseModel):
    objective: str = Field(min_length=3)
    models: list[str] = Field(description="Provider specs, e.g. 'claude-cli', 'agy:gemini-3.8-flash-high'")
    synthesizer: str = "claude-cli"
    depth: Depth = Depth.standard
    deliverable: Deliverable = Deliverable.report
    context_paths: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=list, description="Decision criteria members must address")
    join: JoinPolicy = JoinPolicy.all
    min_members: int = MIN_MEMBERS
    # quorum policy: how long stragglers get once min_members have succeeded
    quorum_grace_s: float = Field(default=120.0, ge=0)
    member_timeout_s: float | None = Field(default=None, gt=0)  # None -> depth default
    retries: int = Field(default=1, ge=0, le=3)
    allow_web: bool = True
    verify_sources: bool = False  # fetch cited URLs and check quoted passages

    @field_validator("models")
    @classmethod
    def _lineup_size(cls, v: list[str]) -> list[str]:
        if not MIN_MEMBERS <= len(v) <= MAX_MEMBERS:
            raise ValueError(f"a committee needs {MIN_MEMBERS}-{MAX_MEMBERS} members, got {len(v)}")
        return v

    @field_validator("min_members")
    @classmethod
    def _min_members(cls, v: int) -> int:
        if v < 1:
            raise ValueError("min_members must be >= 1")
        return v


# ---------------------------------------------------------------- member reports


class Evidence(BaseModel):
    source: str = Field(description="Title or description of the source")
    url: str | None = None
    quote: str | None = Field(default=None, description="Short verbatim passage supporting the claim")


class MemberClaim(BaseModel):
    id: str
    statement: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class MemberFindings(BaseModel):
    """Structured block every member must append to its prose report."""

    summary: str
    recommendation: str | None = None
    confidence: float = Field(ge=0, le=1)
    assumptions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    claims: list[MemberClaim] = Field(default_factory=list)
    counterargument: str | None = None
    would_change_mind: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_claim_ids(self) -> MemberFindings:
        """Models sometimes reuse ids; every claim must stay addressable or it would silently vanish."""
        seen: set[str] = set()
        for claim in self.claims:
            base, n = claim.id.strip() or "c", 1
            cid = base
            while cid in seen:
                n += 1
                cid = f"{base}-{n}"
            claim.id = cid
            seen.add(cid)
        return self


class ProviderTelemetry(BaseModel):
    provider: str
    model: str | None = None
    duration_s: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    web_searches: int | None = None
    tool_calls: int | None = None
    exit_code: int | None = None


class MemberStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    skipped = "skipped"  # quorum reached and member cancelled


class MemberReport(BaseModel):
    key: str = Field(description="Stable member id within the run, e.g. 'm1-claude-cli'")
    spec: str
    label: str
    vendor: str
    status: MemberStatus
    attempts: int = 0
    report_markdown: str = ""
    findings: MemberFindings | None = None
    parse_error: str | None = None
    error: str | None = None
    telemetry: ProviderTelemetry | None = None
    context_files: list[str] = Field(default_factory=list, description="Context the branch actually received")
    started_at: str | None = None
    finished_at: str | None = None


# ---------------------------------------------------------------- claim matrix


Stance = Literal["support", "reject", "uncertain", "not_mentioned"]


class DisagreementType(StrEnum):
    factual = "factual"
    assumption = "assumption"
    forecast = "forecast"
    value = "value"
    scope = "scope"
    evidence = "evidence"
    calculation = "calculation"


DISAGREEMENT_HANDLING: dict[DisagreementType, str] = {
    DisagreementType.factual: "Return to primary sources and verify dates, definitions and versions.",
    DisagreementType.assumption: "Expose the assumptions and rerun the scenarios.",
    DisagreementType.forecast: "Give ranges, drivers and leading indicators.",
    DisagreementType.value: "Ask the decision-maker to set weights or decision criteria.",
    DisagreementType.scope: "Normalize the objective and rerun the affected branches.",
    DisagreementType.evidence: "Compare source quality and independence.",
    DisagreementType.calculation: "Recompute deterministically in code or a spreadsheet.",
}


class Classification(StrEnum):
    agreement = "agreement"  # >=2 support, nobody rejects
    disagreement = "disagreement"  # at least one support and one reject
    unique = "unique"  # only one member raised it
    contested = "contested"  # raised by >=2, no support-vs-reject split, fewer than 2 supporters


class Position(BaseModel):
    stance: Stance
    claim_refs: list[str] = Field(default_factory=list, description="'<member_key>/<claim_id>' references")
    note: str | None = None


class CanonicalClaim(BaseModel):
    id: str
    statement: str
    positions: dict[str, Position] = Field(description="member_key -> position")
    disagreement_type: DisagreementType | None = None
    disagreement_cause: str | None = None
    # Filled deterministically by synthesis.classify(), never by the model:
    classification: Classification | None = None
    support: int = 0
    reject: int = 0
    uncertain: int = 0
    model_consensus: float = 0.0
    independent_sources: int = 0
    source_domains: list[str] = Field(default_factory=list)


class ClaimMatrix(BaseModel):
    members: list[str]
    claims: list[CanonicalClaim]


# ---------------------------------------------------------------- synthesis


class DecisionRow(BaseModel):
    option: str
    scores: dict[str, str] = Field(default_factory=dict, description="criterion -> assessment")
    verdict: str | None = None


class SynthesisNarrative(BaseModel):
    """What the synthesizer model writes. Agreement/disagreement/unique sections come from the matrix."""

    headline: str
    answer: str
    recommendation: str | None = None
    confidence_note: str
    decision_matrix: list[DecisionRow] = Field(default_factory=list)
    unresolved_risks: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    citations: list[Evidence] = Field(default_factory=list)


class Synthesis(BaseModel):
    objective: str
    synthesizer: str
    narrative: SynthesisNarrative
    matrix: ClaimMatrix
    members_succeeded: list[str]
    members_missing: list[str]
    markdown: str = ""


# ---------------------------------------------------------------- validation


class CitationCheck(BaseModel):
    url: str
    traced_to_members: list[str] = Field(default_factory=list)
    fetched: bool | None = None
    http_status: int | None = None
    quote_found: bool | None = None
    note: str | None = None


class ValidationReport(BaseModel):
    citations: list[CitationCheck] = Field(default_factory=list)
    untraceable_citations: list[str] = Field(default_factory=list, description="Synthesis URLs no member cited")
    unique_findings_preserved: bool = True
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- manifest + events


class ArtifactRecord(BaseModel):
    path: str
    sha256: str
    immutable: bool = False
    kind: str


class RunManifest(BaseModel):
    run_id: str
    created_at: str = Field(default_factory=utcnow)
    finished_at: str | None = None
    status: Literal["running", "succeeded", "failed"] = "running"
    config: RunConfig
    members: list[MemberReport] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    error: str | None = None
    timings: dict[str, float] = Field(default_factory=dict)


class Event(BaseModel):
    ts: str = Field(default_factory=utcnow)
    type: str  # run.started, member.started, member.finished, synthesis.started, ..., run.finished
    member: str | None = None
    status: str | None = None
    detail: str | None = None
