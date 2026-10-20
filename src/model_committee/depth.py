"""Depth -> budget levers. Brief runs are cheap and fast; detailed runs research and cross-check more."""

from __future__ import annotations

from dataclasses import dataclass

from model_committee.schemas import Depth


@dataclass(frozen=True)
class DepthBudget:
    report_words: int
    max_sources: int
    max_claims: int
    member_timeout_s: float
    synthesis_timeout_s: float
    search_guidance: str
    synthesis_granularity: str


BUDGETS: dict[Depth, DepthBudget] = {
    Depth.brief: DepthBudget(
        report_words=350,
        max_sources=3,
        max_claims=5,
        member_timeout_s=420,
        synthesis_timeout_s=420,
        search_guidance="Run a small number of targeted searches; open only the most authoritative sources.",
        synthesis_granularity="main findings only",
    ),
    Depth.standard: DepthBudget(
        report_words=900,
        max_sources=6,
        max_claims=8,
        member_timeout_s=900,
        synthesis_timeout_s=600,
        search_guidance="Search from several angles and read primary sources before concluding.",
        synthesis_granularity="claim- and assumption-level",
    ),
    Depth.detailed: DepthBudget(
        report_words=1800,
        max_sources=12,
        max_claims=12,
        member_timeout_s=1800,
        synthesis_timeout_s=900,
        search_guidance=(
            "Research broadly: multiple query formulations, primary sources, and at least one deliberate "
            "search for disconfirming evidence. Cross-check every quantitative claim."
        ),
        synthesis_granularity="claim-, assumption- and evidence-level",
    ),
}


def budget_for(depth: Depth) -> DepthBudget:
    return BUDGETS[depth]
