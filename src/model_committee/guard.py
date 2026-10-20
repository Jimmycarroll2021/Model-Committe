"""Recursion guard: a committee member must never start a committee of its own.

Every member and synthesizer subprocess runs with MODEL_COMMITTEE_MEMBER=1 (see providers._subprocess).
A member that is itself an agent (Claude Code, Codex, agy) could otherwise reach this tool through a skill,
an MCP server or the CLI and fan out recursively. Every entry point that starts a run calls ensure_not_member().
"""

from __future__ import annotations

import os

MEMBER_ENV_VAR = "MODEL_COMMITTEE_MEMBER"


class NestedCommitteeError(RuntimeError):
    pass


def is_member_process() -> bool:
    return os.environ.get(MEMBER_ENV_VAR, "").strip() not in ("", "0")


def ensure_not_member() -> None:
    if is_member_process():
        raise NestedCommitteeError(
            f"refusing to start a committee: {MEMBER_ENV_VAR} is set, so this process is a committee member "
            "and members may not launch committees of their own"
        )
