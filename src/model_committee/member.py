"""One committee member: run the research agent in its own sandbox, parse its findings, persist immutably."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from model_committee.artifacts import RunStore, slugify
from model_committee.context import ContextBundle
from model_committee.parsing import parse_model, strip_json_block
from model_committee.prompts import repair_prompt
from model_committee.providers.base import Provider, ProviderError, ProviderUnavailable
from model_committee.schemas import Event, MemberFindings, MemberReport, MemberStatus, utcnow

REPAIR_TIMEOUT_S = 240  # one extra no-tools call if the report's JSON block is unusable
MIN_RETRY_BUDGET_S = 60  # don't start a retry with less research time than this left


def member_key(index: int, spec: str) -> str:
    return f"m{index + 1}-{slugify(spec, max_len=30)}"


async def run_member(
    *,
    key: str,
    provider: Provider,
    prompt: str,
    bundle: ContextBundle,
    store: RunStore,
    timeout_s: float,
    retries: int,
    emit: Callable[[Event], None],
) -> MemberReport:
    report = MemberReport(
        key=key, spec=provider.spec, label=provider.label, vendor=provider.vendor,
        status=MemberStatus.running, context_files=bundle.names, started_at=utcnow(),
    )
    emit(Event(type="member.started", member=key, status="running", detail=provider.label))
    workdir = store.sandbox(key)
    result = None
    # timeout_s is the member's whole research budget: retries share it rather than each getting a fresh one.
    deadline = time.monotonic() + timeout_s
    for attempt in range(1, retries + 2):
        report.attempts = attempt
        try:
            result = await provider.generate(prompt, workdir=workdir, timeout_s=deadline - time.monotonic(),
                                             research=True)
            break
        except TimeoutError:
            report.status, report.error = MemberStatus.timed_out, f"no report within {timeout_s:.0f}s"
            break
        except ProviderUnavailable as exc:
            report.status, report.error = MemberStatus.failed, str(exc)
            break
        except ProviderError as exc:
            report.status, report.error = MemberStatus.failed, str(exc)
            if attempt <= retries and deadline - time.monotonic() > MIN_RETRY_BUDGET_S:
                emit(Event(type="member.retry", member=key, status="running", detail=str(exc)[:300]))
                await asyncio.sleep(2 * attempt)
            else:
                break

    if result is None:
        report.finished_at = utcnow()
        emit(Event(type="member.finished", member=key, status=report.status.value, detail=report.error))
        return report

    report.telemetry = result.telemetry
    report.error = None
    store.write_text(f"raw/{key}.txt", result.raw or result.text, kind="raw-output")

    findings, parse_error = _parse(result.text)
    if findings is None:
        findings, parse_error = await _repair(provider, result.text, parse_error, workdir)

    report.findings = findings
    report.parse_error = parse_error
    report.report_markdown = strip_json_block(result.text)
    report.status = MemberStatus.succeeded
    report.finished_at = utcnow()

    store.write_text(f"members/{key}.md", _render_member_md(report), kind="member-report", immutable=True)
    store.write_json(f"members/{key}.json", report, kind="member-findings", immutable=True)
    detail = f"{len(findings.claims)} claims" if findings else f"report saved; findings unparsed ({parse_error})"
    emit(Event(type="member.finished", member=key, status=report.status.value, detail=detail))
    return report


def _parse(text: str) -> tuple[MemberFindings | None, str | None]:
    try:
        return parse_model(text, MemberFindings), None
    except ValueError as exc:
        return None, str(exc)


async def _repair(provider: Provider, text: str, error: str | None, workdir) -> tuple[MemberFindings | None, str]:
    """Ask the same model (no tools) to restate its own report as JSON, preserving attribution."""
    try:
        fixed = await provider.generate(
            repair_prompt(text, error or "missing"), workdir=workdir, timeout_s=REPAIR_TIMEOUT_S, research=False
        )
    except (ProviderError, TimeoutError) as exc:
        return None, f"{error}; repair failed: {exc}"
    findings, err2 = _parse(fixed.text)
    if findings is None:
        return None, f"{error}; repair failed: {err2}"
    return findings, f"repaired: original output had no valid JSON ({error})"


def _render_member_md(report: MemberReport) -> str:
    t = report.telemetry
    lines = [
        f"# Committee member report: {report.label}",
        "",
        f"- member: `{report.key}`  spec: `{report.spec}`",
        f"- started: {report.started_at}  finished: {report.finished_at}  attempts: {report.attempts}",
    ]
    if t:
        lines.append(f"- duration: {t.duration_s:.1f}s  web searches: {t.web_searches}  cost: {t.cost_usd}")
    lines.append(f"- context received: {', '.join(report.context_files) or 'none'}")
    if report.parse_error:
        lines.append(f"- structured findings: {report.parse_error}")
    lines += ["", "---", "", report.report_markdown]
    return "\n".join(lines) + "\n"
