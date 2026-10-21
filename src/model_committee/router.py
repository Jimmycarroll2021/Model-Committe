"""Intent and skill router: decide which skills a request activates and fill unset run options from the text."""

from __future__ import annotations

import re
from dataclasses import dataclass

from model_committee.schemas import Deliverable, Depth, Mode


@dataclass(frozen=True)
class Skill:
    name: str
    description: str


SKILLS: dict[str, Skill] = {
    "model-committee": Skill(
        "model-committee",
        "Run 2-8 independent model agents on one objective and synthesize agreement, disagreement and "
        "unique findings.",
    ),
    "report": Skill("report", "Package a synthesis as a standalone, printable HTML report."),
    "spreadsheet": Skill("spreadsheet", "Export the claim matrix, evidence and decision matrix to xlsx."),
    "deck": Skill("deck", "Render the decision narrative as a 16:9 board deck (pptx)."),
}

# Only phrasings that ask for an output or a depth count; topic words ("deep learning", "short-term", "migrate
# off Excel") must not change the run.
_ASK = r"\b(?:as|in|into|a|an|the|with|build|make|create|produce|give|return|prepare|export(?:ed)?(?: to)?)\s+"
_DELIVERABLE_HINTS: list[tuple[Deliverable, re.Pattern[str]]] = [
    (Deliverable.deck, re.compile(_ASK + r"(?:\w+[ -])?(?:deck|slides?|presentation|pptx)\b", re.I)),
    (Deliverable.spreadsheet, re.compile(_ASK + r"(?:\w+[ -])?(?:spreadsheet|xlsx|workbook)\b", re.I)),
]
_DEPTH_HINTS: list[tuple[Depth, re.Pattern[str]]] = [
    (Depth.detailed, re.compile(r"\b(in-depth|deep[ -]dive|thorough(ly)?|comprehensive(ly)?|detailed (analysis|"
                                r"report|review|research))\b", re.I)),
    (Depth.brief, re.compile(r"\b(brief(ly)?|quick (take|answer|look|summary)|tl;?dr|summary only)\b", re.I)),
]


# Option lines: "A", "A)", "A.", "(A) text", "A: text" at the start of a line.
_OPTION_LINE = re.compile(r"^[ \t]*\(?([A-H])(?:[).:]|\)?[ \t]*$|[).:]?[ \t]+\S)", re.MULTILINE)


def detect_options(objective: str) -> list[str]:
    """Letters of a lettered multiple-choice list in the objective (A, B, C...), or [] if there isn't one."""
    letters: list[str] = []
    for m in _OPTION_LINE.finditer(objective):
        letter = m.group(1)
        expected = chr(ord("A") + len(letters))
        if letter == expected:
            letters.append(letter)
        elif letter == "A":  # a new list started; keep the longer one
            letters = ["A"] if len(letters) < 2 else letters
    return letters if len(letters) >= 2 else []


def infer_mode(objective: str) -> Mode:
    """A lettered-option question carries everything needed: reason over it rather than research the web."""
    return Mode.reasoning if detect_options(objective) else Mode.research


def infer_deliverable(objective: str) -> Deliverable:
    hits = [d for d, rx in _DELIVERABLE_HINTS if rx.search(objective)]
    if len(hits) > 1:
        return Deliverable.all
    return hits[0] if hits else Deliverable.report


def infer_depth(objective: str) -> Depth:
    for depth, rx in _DEPTH_HINTS:
        if rx.search(objective):
            return depth
    return Depth.standard


def activated_skills(deliverable: Deliverable) -> list[str]:
    outputs = {
        Deliverable.none: [],
        Deliverable.report: ["report"],
        Deliverable.spreadsheet: ["spreadsheet"],
        Deliverable.deck: ["deck"],
        Deliverable.all: ["report", "spreadsheet", "deck"],
    }[deliverable]
    return ["model-committee", *outputs]
