"""MCP server (stdio): exposes the committee as tools to any MCP client (Claude Code, Codex, agy).

Runs take minutes, so `committee_run` starts the run as a background task in this server's event loop and
returns a run_id at once; the client polls `committee_status` and fetches `committee_result`. Runs live as
long as this server process does: if the client shuts the server down mid-run, the orchestrator records the
run as failed ("cancelled"). For runs that must outlive the client session, use the `committee run` CLI.

Nothing in this process may write to stdout except the MCP transport; providers capture child output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from model_committee import __version__
from model_committee.artifacts import new_run_id
from model_committee.guard import NestedCommitteeError, ensure_not_member
from model_committee.router import infer_deliverable, infer_depth
from model_committee.schemas import MAX_MEMBERS, MIN_MEMBERS, Deliverable, Depth, JoinPolicy, RunConfig

RUNS_DIR_ENV = "MODEL_COMMITTEE_RUNS_DIR"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
START_WAIT_S = 3.0  # committee_run waits this long so fast failures (unavailable provider) are reported inline
HIDDEN_ARTIFACT_KINDS = {"raw-output", "prompt"}

# Test hooks, resolved lazily like server.py: tests monkeypatch these module attributes.
run_committee: Callable[..., Any] | None = None
detect_available: Callable[..., Any] | None = None
provider_factory: Callable[[str], Any] | None = None

INSTRUCTIONS = (
    "Model Committee: 2-8 independent model agents research one objective in parallel; a synthesizer maps "
    "agreement, disagreement (with cause) and unique findings. Call committee_providers first, then "
    "committee_run with a pointed decision question and explicit criteria. Runs take minutes: poll "
    "committee_status every 30-60 s, then call committee_result. Model consensus is not source independence: "
    "report the independent source counts and validation warnings alongside the answer."
)

mcp = MCPServer(name="model-committee", version=__version__, instructions=INSTRUCTIONS)


class _RunHandle:
    def __init__(self, run_id: str, run_dir: Path, task: asyncio.Task):
        self.run_id = run_id
        self.run_dir = run_dir
        self.task = task
        self.error: str | None = None


_handles: dict[str, _RunHandle] = {}


# ---------------------------------------------------------------- helpers


def runs_root() -> Path:
    """MODEL_COMMITTEE_RUNS_DIR, else <repo>/runs (the checkout this package runs from), else ./runs."""
    env = os.environ.get(RUNS_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").is_file():
        return repo / "runs"
    return Path.cwd() / "runs"


def _run_committee() -> Callable[..., Any]:
    if run_committee is not None:
        return run_committee
    from model_committee.orchestrator import run_committee as rc

    return rc


def _detect_available() -> Callable[..., Any]:
    if detect_available is not None:
        return detect_available
    from model_committee.providers.registry import detect_available as da

    return da


def _default_lineup() -> list[str]:
    from model_committee.providers.registry import DEFAULT_LINEUP

    return list(DEFAULT_LINEUP)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_dir(run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id or ""):
        raise ToolError(f"invalid run_id {run_id!r}; use a run_id from committee_run or committee_list_runs")
    path = runs_root() / run_id
    if not path.is_dir() and run_id not in _handles:
        raise ToolError(f"unknown run_id {run_id!r} under {runs_root()}; call committee_list_runs to see runs")
    return path


def _effective_status(run_id: str, manifest: dict | None) -> str:
    status = (manifest or {}).get("status", "running")
    handle = _handles.get(run_id)
    if status == "running" and handle is not None and handle.task.done():
        return "failed"  # the task died without the orchestrator recording an outcome
    return status


def _tail_events(run_dir: Path, n: int) -> list[dict]:
    path = run_dir / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for raw in lines[-n:] if n > 0 else []:
        try:
            events.append(json.loads(raw))
        except ValueError:
            continue
    return events


def _members(manifest: dict) -> list[dict]:
    rows = []
    for m in manifest.get("members") or []:
        tel = m.get("telemetry") or {}
        rows.append({"key": m.get("key"), "spec": m.get("spec"), "status": m.get("status"),
                     "duration_s": tel.get("duration_s"), "error": m.get("error")})
    return rows


def _status_payload(run_id: str, run_dir: Path, *, events: int) -> dict[str, Any]:
    manifest = _read_json(run_dir / "run-manifest.json")
    handle = _handles.get(run_id)
    error = (manifest or {}).get("error") or (handle.error if handle else None)
    config = (manifest or {}).get("config") or {}
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": _effective_status(run_id, manifest),
        "objective": config.get("objective"),
        "created_at": (manifest or {}).get("created_at"),
        "finished_at": (manifest or {}).get("finished_at"),
        "error": error,
        "members": _members(manifest or {}),
        "timings": (manifest or {}).get("timings") or {},
        "last_events": _tail_events(run_dir, events),
    }


# ---------------------------------------------------------------- tools


@mcp.tool(annotations=ToolAnnotations(title="List committee providers", read_only_hint=True,
                                      open_world_hint=False))
async def committee_providers() -> dict[str, Any]:
    """List the model backends (Claude Code CLI, Codex CLI, agy, Ollama, API-key providers) and whether each
    is usable on this machine, with an example provider spec, plus the default lineup and synthesizer.
    Call this before committee_run to choose 2-8 available members."""
    rows = await _detect_available()()
    return {
        "providers": rows,
        "default_lineup": _default_lineup(),
        "default_synthesizer": RunConfig.model_fields["synthesizer"].default,
        "min_members": MIN_MEMBERS,
        "max_members": MAX_MEMBERS,
    }


@mcp.tool(annotations=ToolAnnotations(title="Start a model committee run", read_only_hint=False,
                                      destructive_hint=False, idempotent_hint=False, open_world_hint=True))
async def committee_run(
    objective: str,
    models: list[str] | None = None,
    depth: Literal["auto", "brief", "standard", "detailed"] = "auto",
    deliverable: Literal["auto", "none", "report", "spreadsheet", "deck", "all"] = "auto",
    criteria: list[str] | None = None,
    synthesizer: str = "claude-cli",
    join: Literal["all", "quorum"] = "all",
    min_members: int = MIN_MEMBERS,
    context_paths: list[str] | None = None,
    verify_sources: bool = False,
) -> dict[str, Any]:
    """Start a committee run in the background and return its run_id and run directory immediately.

    objective: a pointed decision question with numbers, timelines and trade-offs (vague in, vague out).
    models: 2-8 provider specs, e.g. ["claude-cli", "codex-cli", "agy:gemini-3.8-flash-high"]
      (default: the default lineup). criteria: decision criteria every member must address.
    depth: brief (~7 min/member cap), standard (~15), detailed (~30); auto infers from the objective.
    deliverable: none (synthesis.md only), report (HTML), spreadsheet (xlsx), deck (pptx) or all.
    join: all waits for every member; quorum synthesizes once min_members succeed.
    context_paths: local files (pdf, xlsx, text) every member receives.
    Then poll committee_status(run_id) every 30-60 s and call committee_result(run_id) when it finishes.
    """
    try:
        ensure_not_member()
    except NestedCommitteeError as exc:
        raise ToolError(str(exc)) from exc
    try:
        config = RunConfig(
            objective=objective.strip(),
            models=models or _default_lineup(),
            synthesizer=synthesizer,
            depth=Depth(depth) if depth != "auto" else infer_depth(objective),
            deliverable=Deliverable(deliverable) if deliverable != "auto" else infer_deliverable(objective),
            criteria=criteria or [],
            join=JoinPolicy(join),
            min_members=min_members,
            context_paths=context_paths or [],
            verify_sources=verify_sources,
        )
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ToolError(f"invalid committee configuration: {problems}") from exc
    for path in config.context_paths:
        if not Path(path).expanduser().is_file():
            raise ToolError(f"context file not found: {path}")

    root = runs_root()
    root.mkdir(parents=True, exist_ok=True)
    base = new_run_id(config.objective)
    run_id, n = base, 1
    while run_id in _handles or (root / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"

    runner = _run_committee()
    task = asyncio.create_task(
        runner(config, runs_root=root, on_event=None, provider_factory=provider_factory, run_id=run_id),
        name=f"committee-{run_id}",
    )
    handle = _RunHandle(run_id, root / run_id, task)
    _handles[run_id] = handle

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            handle.error = "cancelled"
        elif t.exception() is not None:
            exc = t.exception()
            handle.error = f"{type(exc).__name__}: {exc}"

    task.add_done_callback(_done)
    await asyncio.wait({task}, timeout=START_WAIT_S)

    payload = _status_payload(run_id, handle.run_dir, events=5)
    payload.update({
        "models": config.models,
        "synthesizer": config.synthesizer,
        "depth": config.depth.value,
        "deliverable": config.deliverable.value,
        "next": "poll committee_status(run_id) every 30-60 s; call committee_result(run_id) once status is "
                "succeeded or failed",
    })
    return payload


@mcp.tool(annotations=ToolAnnotations(title="Committee run status", read_only_hint=True, open_world_hint=False))
async def committee_status(run_id: str, events: int = 10) -> dict[str, Any]:
    """Status of a committee run: running | succeeded | failed, each member's status and duration, timings,
    and the last `events` progress events (member.started, member.finished, synthesis.started, ...)."""
    return _status_payload(run_id, _run_dir(run_id), events=max(0, min(events, 200)))


@mcp.tool(annotations=ToolAnnotations(title="Committee run result", read_only_hint=True, open_world_hint=False))
async def committee_result(run_id: str) -> dict[str, Any]:
    """Result of a finished committee run: the synthesis markdown (answer, recommendation, agreement,
    disagreement with type and cause, unique findings, decision matrix, risks, next actions), validation
    warnings and untraceable citations, and absolute paths of every artifact (report.html, committee.xlsx,
    committee-deck.pptx, member reports). Fails while the run is still running."""
    run_dir = _run_dir(run_id)
    manifest = _read_json(run_dir / "run-manifest.json")
    status = _effective_status(run_id, manifest)
    if status == "running":
        raise ToolError(f"run {run_id} is still running; poll committee_status and retry when it finishes")
    manifest = manifest or {}
    synthesis = _read_json(run_dir / "synthesis.json") or {}
    narrative = synthesis.get("narrative") or {}
    validation = _read_json(run_dir / "validation.json") or {}
    try:
        markdown = (run_dir / "synthesis.md").read_text(encoding="utf-8")
    except OSError:
        markdown = None
    handle = _handles.get(run_id)
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": status,
        "error": manifest.get("error") or (handle.error if handle else None),
        "headline": narrative.get("headline"),
        "recommendation": narrative.get("recommendation"),
        "members_succeeded": synthesis.get("members_succeeded", []),
        "members_missing": synthesis.get("members_missing", []),
        "members": _members(manifest),
        "synthesis_markdown": markdown,
        "validation_warnings": validation.get("warnings", []),
        "untraceable_citations": validation.get("untraceable_citations", []),
        "artifacts": [
            {"kind": a.get("kind"), "path": str(run_dir / a.get("path", ""))}
            for a in manifest.get("artifacts") or [] if a.get("kind") not in HIDDEN_ARTIFACT_KINDS
        ],
    }


@mcp.tool(annotations=ToolAnnotations(title="List committee runs", read_only_hint=True, open_world_hint=False))
async def committee_list_runs(limit: int = 20) -> dict[str, Any]:
    """Most recent committee runs (newest first) with run_id, objective, status and timestamps."""
    root = runs_root()
    rows: list[dict] = []
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir() or not RUN_ID_RE.fullmatch(child.name):
                continue
            manifest = _read_json(child / "run-manifest.json")
            if manifest is None:
                continue
            objective = (manifest.get("config") or {}).get("objective", "")
            rows.append({
                "run_id": child.name,
                "objective": objective if len(objective) <= 200 else objective[:197] + "...",
                "status": _effective_status(child.name, manifest),
                "created_at": manifest.get("created_at"),
                "finished_at": manifest.get("finished_at"),
            })
    rows.sort(key=lambda r: (r["created_at"] or "", r["run_id"]), reverse=True)
    return {"runs_root": str(root), "runs": rows[:max(1, min(limit, 500))]}


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request INFO lines flood the client's stderr log
    mcp.run("stdio")


if __name__ == "__main__":
    main()
