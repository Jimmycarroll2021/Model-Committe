"""`committee` command line: providers | run | verify | serve | mcp."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from model_committee.router import infer_deliverable, infer_depth
from model_committee.schemas import MAX_MEMBERS, MIN_MEMBERS, Deliverable, Depth, Event, JoinPolicy, Mode, RunConfig

DEFAULT_RUNS_DIR = None  # -> artifacts.default_runs_dir()


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _print_event(event: Event) -> None:
    who = f"[{event.member}] " if event.member else ""
    status = f"{event.status} " if event.status else ""
    detail = (event.detail or "").replace("\n", " ")
    _out(f"{event.ts[11:19]} {event.type:<20} {who}{status}{detail[:160]}")


async def _cmd_providers(_args) -> int:
    from model_committee.providers.registry import DEFAULT_LINEUP, detect_available

    rows = await detect_available()
    for r in rows:
        mark = "yes" if r["available"] else "no "
        _out(f"{mark}  {r['name']:<14} {r['vendor']:<12} e.g. {r['example_spec']:<32} {r['reason']}")
    _out(f"\ndefault lineup: {' '.join(DEFAULT_LINEUP)}")
    return 0


async def _cmd_run(args) -> int:
    from model_committee.guard import NestedCommitteeError, ensure_not_member
    from model_committee.orchestrator import run_committee
    from model_committee.providers.registry import DEFAULT_LINEUP

    try:
        ensure_not_member()
    except NestedCommitteeError as exc:
        _out(f"error: {exc}")
        return 2
    objective = args.objective
    if objective == "-":
        objective = sys.stdin.read().strip()
    config = RunConfig(
        objective=objective,
        models=args.model or list(DEFAULT_LINEUP),
        synthesizer=args.synthesizer,
        depth=Depth(args.depth) if args.depth != "auto" else infer_depth(objective),
        mode=Mode(args.mode),
        deliverable=Deliverable(args.deliverable) if args.deliverable != "auto" else infer_deliverable(objective),
        context_paths=args.context or [],
        criteria=args.criteria or [],
        join=JoinPolicy(args.join),
        min_members=args.min_members,
        quorum_grace_s=args.quorum_grace,
        member_timeout_s=args.timeout,
        retries=args.retries,
        allow_web=not args.no_web,
        verify_sources=args.verify_sources,
    )
    _out(f"committee: {len(config.models)} members ({', '.join(config.models)}), synthesizer {config.synthesizer}, "
         f"depth {config.depth.value}, deliverable {config.deliverable.value}")
    result = await run_committee(config, runs_root=_runs_dir(args), on_event=None if args.quiet else _print_event)
    m = result.manifest
    _out()
    _out(f"run {m.run_id}: {m.status} in {m.timings.get('total_s', 0):.0f}s -> {result.run_dir}")
    for member in m.members:
        t = member.telemetry
        extra = f"{t.duration_s:.0f}s searches={t.web_searches} cost={t.cost_usd}" if t else (member.error or "")
        _out(f"  {member.key:<28} {member.status.value:<10} {extra}")
    if m.error:
        _out(f"error: {m.error}")
    if result.synthesis:
        _out()
        _out(f"# {result.synthesis.narrative.headline}")
        if result.synthesis.narrative.recommendation:
            _out(f"Recommendation: {result.synthesis.narrative.recommendation}")
        _out(f"\nartifacts: {', '.join(a.path for a in m.artifacts if a.kind not in {'raw-output', 'prompt'})}")
    if args.json:
        _out(m.model_dump_json(indent=2))
    return 0 if m.status == "succeeded" else 1


async def _cmd_verify(args) -> int:
    from model_committee.artifacts import verify_run

    problems = verify_run(Path(args.run_dir))
    for p in problems:
        _out(f"FAIL {p}")
    if not problems:
        manifest = json.loads((Path(args.run_dir) / "run-manifest.json").read_text(encoding="utf-8"))
        _out(f"OK {len(manifest['artifacts'])} artifacts intact")
    return 1 if problems else 0


def _cmd_serve(args) -> int:
    from model_committee.server import serve

    _out(f"runs: {_runs_dir(args)}")
    serve(host=args.host, port=args.port, runs_root=_runs_dir(args), allow_remote=args.allow_remote)
    return 0


async def _cmd_exec(args) -> int:
    """Internal: body of a detached run started by the web UI or MCP server (see launcher.py)."""
    if args.log:  # WMI-launched: no inherited stdout/stderr, so point both at the launch log ourselves
        fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        sys.stdout = open(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)  # noqa: SIM115
        sys.stderr = open(2, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)  # noqa: SIM115
    if args.env_file:  # WMI-launched: restore the launcher's environment, then remove the copy from disk
        env_path = Path(args.env_file)
        os.environ.clear()
        os.environ.update(json.loads(env_path.read_text(encoding="utf-8")))
        env_path.unlink(missing_ok=True)
    from model_committee.orchestrator import run_committee

    config = RunConfig.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
    result = await run_committee(config, runs_root=_runs_dir(args), run_id=args.run_id)
    return 0 if result.manifest.status == "succeeded" else 1


def _runs_dir(args) -> Path:
    from model_committee.artifacts import default_runs_dir

    return Path(args.runs_dir) if args.runs_dir else default_runs_dir()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="committee", description="Multi-model research committee")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("providers", help="list model backends and whether they are usable here")

    run = sub.add_parser("run", help="run a committee on an objective")
    run.add_argument("objective", help="the question or decision ('-' reads stdin)")
    run.add_argument("-m", "--model", action="append",
                     help=f"member provider spec, repeat {MIN_MEMBERS}-{MAX_MEMBERS} times (default: default lineup)")
    run.add_argument("--synthesizer", default="claude-cli", help="provider spec for the synthesizer")
    run.add_argument("--depth", default="auto", choices=["auto", *[d.value for d in Depth]])
    run.add_argument("--mode", default="auto", choices=[m.value for m in Mode],
                     help="research: members search the web; reasoning: self-contained question, no browsing")
    run.add_argument("--deliverable", default="auto", choices=["auto", *[d.value for d in Deliverable]])
    run.add_argument("--context", action="append", help="context file (pdf, xlsx, text); repeatable")
    run.add_argument("--criteria", action="append", help="decision criterion; repeatable")
    run.add_argument("--join", default="all", choices=[j.value for j in JoinPolicy])
    run.add_argument("--min-members", type=int, default=MIN_MEMBERS)
    run.add_argument("--quorum-grace", type=float, default=120.0, help="seconds stragglers get after quorum")
    run.add_argument("--timeout", type=float, default=None, help="per-member timeout seconds (default: by depth)")
    run.add_argument("--retries", type=int, default=1)
    run.add_argument("--no-web", action="store_true", help="members may not browse")
    run.add_argument("--verify-sources", action="store_true", help="fetch cited URLs and check quoted passages")
    run.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--json", action="store_true", help="print the final manifest as JSON")

    verify = sub.add_parser("verify", help="re-hash a run's artifacts")
    verify.add_argument("run_dir")

    serve = sub.add_parser("serve", help="start the local web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    serve.add_argument("--allow-remote", action="store_true",
                       help="allow a non-loopback --host (needs MODEL_COMMITTEE_TOKEN, sent as a bearer token)")

    exe = sub.add_parser("exec", help=argparse.SUPPRESS)  # internal: detached run body (launcher.py)
    exe.add_argument("--config", required=True)
    exe.add_argument("--run-id", required=True)
    exe.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    exe.add_argument("--log", default=None)
    exe.add_argument("--env-file", default=None)

    sub.add_parser("mcp", help="start the MCP server on stdio (runs dir: $MODEL_COMMITTEE_RUNS_DIR or <repo>/runs)")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "mcp":
        from model_committee.mcp_server import main as mcp_main

        mcp_main()
        return 0
    handler = {"providers": _cmd_providers, "run": _cmd_run, "verify": _cmd_verify, "exec": _cmd_exec}[args.command]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        _out("interrupted")
        return 130
    except ValueError as exc:  # config validation
        _out(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
