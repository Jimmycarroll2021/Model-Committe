"""Control plane: configure the run, fan out members in parallel, apply the join policy, fan in, package outputs."""

from __future__ import annotations

import asyncio
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from model_committee.artifacts import RunStore, new_run_id, process_started
from model_committee.context import ContextBundle, load_context
from model_committee.depth import budget_for
from model_committee.guard import ensure_not_member
from model_committee.member import member_key, run_member
from model_committee.prompts import member_prompt
from model_committee.providers.base import Provider
from model_committee.router import activated_skills
from model_committee.schemas import (
    Deliverable,
    Event,
    JoinPolicy,
    MemberReport,
    MemberStatus,
    RunConfig,
    RunManifest,
    Synthesis,
    utcnow,
)
from model_committee.synthesis import align_claims, classify, render_markdown, write_narrative
from model_committee.validation import validate


class CommitteeError(RuntimeError):
    pass


@dataclass
class RunResult:
    run_dir: Path
    manifest: RunManifest
    synthesis: Synthesis | None


def _default_factory(spec: str) -> Provider:
    from model_committee.providers.registry import build_provider

    return build_provider(spec)


async def run_committee(
    config: RunConfig,
    *,
    runs_root: Path,
    on_event: Callable[[Event], None] | None = None,
    provider_factory: Callable[[str], Provider] | None = None,
    run_id: str | None = None,
) -> RunResult:
    ensure_not_member()  # single choke point for CLI, web UI, MCP and skill entry points
    factory = provider_factory or _default_factory
    manifest = RunManifest(run_id=run_id or new_run_id(config.objective), config=config,
                           owner_pid=os.getpid(), owner_started=process_started(os.getpid()))
    store = RunStore(Path(runs_root), manifest)

    def emit(event: Event) -> None:
        store.log_event(event)
        if on_event:
            on_event(event)

    t0 = time.monotonic()
    synthesis: Synthesis | None = None
    emit(Event(type="run.started", detail=f"skills: {', '.join(activated_skills(config.deliverable))}"))
    try:
        synthesis = await _execute(config, store, factory, emit)
        manifest.status = "succeeded"
    except asyncio.CancelledError:
        manifest.status, manifest.error = "failed", "cancelled"
        emit(Event(type="run.failed", status="failed", detail="cancelled"))
        raise
    except CommitteeError as exc:
        manifest.status, manifest.error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - recorded in the manifest, surfaced to the caller via status
        manifest.status, manifest.error = "failed", f"{type(exc).__name__}: {exc}"
        store.write_text("error.txt", traceback.format_exc(), kind="error")
    finally:
        manifest.finished_at = utcnow()
        manifest.timings["total_s"] = round(time.monotonic() - t0, 2)
        store.save_manifest()
    if manifest.status == "failed":
        emit(Event(type="run.failed", status="failed", detail=manifest.error))
    else:
        emit(Event(type="run.finished", status="succeeded", detail=str(store.dir)))
    return RunResult(run_dir=store.dir, manifest=manifest, synthesis=synthesis)


async def _execute(config: RunConfig, store: RunStore, factory, emit) -> Synthesis:
    budget = budget_for(config.depth)
    timings = store.manifest.timings
    providers = [factory(spec) for spec in config.models]
    synthesizer = factory(config.synthesizer)
    for p in [*providers, synthesizer]:
        ok, reason = await p.available()
        if not ok:
            raise CommitteeError(f"provider {p.spec} is not available: {reason}")
    if config.min_members > len(providers):
        raise CommitteeError(f"min_members={config.min_members} exceeds the {len(providers)} selected models")

    bundle = load_context(config.context_paths)
    for f in bundle.files:
        store.write_text(f"context/{f.name}.txt", f.text, kind="context")
    emit(Event(type="context.loaded", detail=f"{len(bundle.files)} file(s); flagged: {len(bundle.flagged)}"))

    prompt = member_prompt(config, budget, bundle)
    store.write_text("prompts/member.md", prompt, kind="prompt")
    keys = [member_key(i, p.spec) for i, p in enumerate(providers)]
    store.manifest.members = [
        MemberReport(key=k, spec=p.spec, label=p.label, vendor=p.vendor, status=MemberStatus.running,
                     context_files=bundle.names, started_at=utcnow())
        for k, p in zip(keys, providers, strict=True)
    ]  # every member is dispatched at once, so they are all running from here
    store.save_manifest()

    t = time.monotonic()
    reports = await _fan_out(config, store, providers, keys, prompt, bundle, emit,
                             timeout_s=config.member_timeout_s or budget.member_timeout_s)
    timings["fan_out_s"] = round(time.monotonic() - t, 2)
    store.manifest.members = reports
    store.save_manifest()

    ok = [r for r in reports if r.status is MemberStatus.succeeded]
    if len(ok) < config.min_members:
        raise CommitteeError(f"only {len(ok)} of {len(reports)} members reported; need {config.min_members}")
    store.write_json("evidence-index.json", _evidence_index(ok), kind="evidence-index")

    emit(Event(type="synthesis.started", detail=synthesizer.label))
    t = time.monotonic()
    workdir = store.sandbox("synthesizer")
    matrix, warnings = await align_claims(synthesizer, config.objective, ok, workdir=workdir,
                                          timeout_s=budget.synthesis_timeout_s)
    classify(matrix, ok)
    store.write_json("claim-matrix.json", matrix, kind="claim-matrix")
    narrative, w2 = await write_narrative(synthesizer, config, ok, matrix, granularity=budget.synthesis_granularity,
                                          workdir=workdir, timeout_s=budget.synthesis_timeout_s)
    synthesis = Synthesis(
        objective=config.objective, synthesizer=synthesizer.spec, narrative=narrative, matrix=matrix,
        members_succeeded=[r.key for r in ok], members_missing=[r.key for r in reports if r not in ok],
    )
    synthesis.markdown = render_markdown(synthesis, reports)
    store.write_text("synthesis.md", synthesis.markdown, kind="synthesis")
    store.write_json("synthesis.json", synthesis, kind="synthesis-data")
    timings["synthesis_s"] = round(time.monotonic() - t, 2)
    emit(Event(type="synthesis.finished", detail=f"{len(matrix.claims)} canonical claims"))

    t = time.monotonic()
    validation = await validate(synthesis, reports, bundle, fetch_sources=config.verify_sources)
    if synthesizer.name == "agy":
        warnings.append("synthesizer runs on agy, which cannot be made tool-free; prefer claude-cli or codex-cli")
    validation.warnings = warnings + w2 + validation.warnings
    timings["validation_s"] = round(time.monotonic() - t, 2)
    emit(Event(type="validation.finished", detail=f"{len(validation.warnings)} warning(s)"))

    t = time.monotonic()
    written = _render(config, synthesis, reports, validation, store)  # may append renderer-failure warnings
    timings["outputs_s"] = round(time.monotonic() - t, 2)
    store.write_json("validation.json", validation, kind="validation")
    emit(Event(type="outputs.finished", detail=", ".join(written) or "none requested"))
    return synthesis


def _render(config: RunConfig, synthesis: Synthesis, reports: list[MemberReport], validation, store: RunStore):
    """Each output skill runs independently: one failing renderer must not sink the run or the others."""
    from model_committee.outputs import render_outputs

    wanted = [config.deliverable] if config.deliverable is not Deliverable.all else [
        Deliverable.report, Deliverable.spreadsheet, Deliverable.deck]
    written: list[str] = []
    for deliverable in wanted:
        try:
            produced = render_outputs(deliverable, synthesis=synthesis, members=reports, validation=validation,
                                      config=config, out_dir=store.dir)
        except Exception as exc:  # noqa: BLE001 - reported as a validation warning
            validation.warnings.append(f"{deliverable.value} output failed: {type(exc).__name__}: {exc}")
            continue
        for rel, kind in produced:
            store.register(rel, kind=kind)
            written.append(rel)
    return written


async def _fan_out(config: RunConfig, store: RunStore, providers: list[Provider], keys: list[str], prompt: str,
                   bundle: ContextBundle, emit, *, timeout_s: float) -> list[MemberReport]:
    """Run every member concurrently. Members never see each other's output (independence)."""
    tasks = {
        asyncio.create_task(
            run_member(key=k, provider=p, prompt=prompt, bundle=bundle, store=store, timeout_s=timeout_s,
                       retries=config.retries, emit=emit),
            name=k,
        ): k
        for k, p in zip(keys, providers, strict=True)
    }
    results: dict[str, MemberReport] = {}
    pending = set(tasks)
    grace_deadline: float | None = None
    try:
        while pending:
            wait_for = None if grace_deadline is None else max(grace_deadline - time.monotonic(), 0)
            done, pending = await asyncio.wait(pending, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                results[tasks[task]] = _task_report(task, tasks[task], store)
                _sync_member(store, results[tasks[task]])
            succeeded = sum(r.status is MemberStatus.succeeded for r in results.values())
            if config.join is JoinPolicy.quorum and pending and grace_deadline is None \
                    and succeeded >= config.min_members:
                grace_deadline = time.monotonic() + config.quorum_grace_s
                emit(Event(type="join.quorum", detail=f"{succeeded} reported; {config.quorum_grace_s:.0f}s grace"))
            if grace_deadline is not None and pending and time.monotonic() >= grace_deadline:
                break
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    for task in pending:
        key = tasks[task]
        report = next(m for m in store.manifest.members if m.key == key)
        report.status, report.error, report.finished_at = MemberStatus.skipped, "cancelled after quorum", utcnow()
        results[key] = report
        emit(Event(type="member.finished", member=key, status="skipped", detail=report.error))
    return [results[k] for k in keys]


def _task_report(task: asyncio.Task, key: str, store: RunStore) -> MemberReport:
    exc = task.exception()
    if exc is None:
        return task.result()
    base = next(m for m in store.manifest.members if m.key == key)
    return base.model_copy(update={"status": MemberStatus.failed, "error": f"{type(exc).__name__}: {exc}",
                                   "finished_at": utcnow()})


def _sync_member(store: RunStore, report: MemberReport) -> None:
    store.manifest.members = [report if m.key == report.key else m for m in store.manifest.members]
    store.save_manifest()


def _evidence_index(members: list[MemberReport]) -> list[dict]:
    return [
        {"member": m.key, "claim": c.id, "statement": c.statement, "source": e.source, "url": e.url, "quote": e.quote}
        for m in members if m.findings for c in m.findings.claims for e in c.evidence
    ]
