"""Post-synthesis checks: citation traceability (anti citation-laundering), optional source fetching, and
structural warnings (correlated consensus, missing members, unparsed reports, injection flags)."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
from urllib.parse import urlparse, urlunparse

import httpx

from model_committee.context import ContextBundle
from model_committee.schemas import (
    CitationCheck,
    Classification,
    MemberReport,
    MemberStatus,
    Synthesis,
    ValidationReport,
)

_URL_CANDIDATE = re.compile(r"https?://[^\s\]>\"'`<]+")
FETCH_TIMEOUT_S = 20
FETCH_CONCURRENCY = 6
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2_000_000


def find_urls(text: str) -> list[str]:
    """URLs in prose/markdown. Keeps balanced parentheses (Foo_(bar)) but drops a closing ')' of [t](url)."""
    out = []
    for raw in _URL_CANDIDATE.findall(text):
        url = raw.rstrip(".,;:!?*_")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1].rstrip(".,;:!?*_")
        out.append(url)
    return out


def normalize_url(url: str) -> str:
    """Comparison key: http/https, www., trailing slash, fragment and utm_* params don't distinguish sources."""
    try:
        p = urlparse(url.strip().rstrip(".,;:"))
    except ValueError:  # model-supplied URLs can be malformed (e.g. "http://[::1")
        return url.strip().lower()
    query = "&".join(q for q in p.query.split("&") if q and not q.startswith("utm_"))
    return urlunparse(("https", p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), "", query, ""))


async def _public_url_problem(url: str) -> str | None:
    """None if url is http(s) to a host that resolves only to public addresses; otherwise the reason (SSRF guard)."""
    try:
        p = urlparse(url)
        if p.scheme not in {"http", "https"} or not p.hostname:
            return "only http(s) URLs with a host are fetched"
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return "malformed URL"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, port)
    except OSError:
        return "host does not resolve"
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global:
            return f"refusing to fetch non-public address {addr}"
    return None


def _member_urls(members: list[MemberReport]) -> tuple[dict[str, set[str]], dict[str, str]]:
    """(normalized url -> member keys that cited it, normalized url -> an original spelling to fetch)."""
    index: dict[str, set[str]] = {}
    originals: dict[str, str] = {}
    for m in members:
        urls = set(find_urls(m.report_markdown))
        if m.findings:
            urls |= {e.url for c in m.findings.claims for e in c.evidence if e.url}
        for u in urls:
            norm = normalize_url(u)
            index.setdefault(norm, set()).add(m.key)
            originals.setdefault(norm, u.rstrip(".,;:"))
    return index, originals


def _quotes_by_url(members: list[MemberReport]) -> dict[str, list[str]]:
    quotes: dict[str, list[str]] = {}
    for m in members:
        if m.findings:
            for c in m.findings.claims:
                for e in c.evidence:
                    if e.url and e.quote:
                        quotes.setdefault(normalize_url(e.url), []).append(e.quote)
    return quotes


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip().lower()


async def _fetch(client: httpx.AsyncClient, url: str, quotes: list[str], sem: asyncio.Semaphore) -> CitationCheck:
    """Fetch a member-cited (untrusted) URL: public hosts only, every redirect hop re-checked, body capped."""
    check = CitationCheck(url=url)
    async with sem:
        target, body, ctype = url, b"", ""
        try:
            for _ in range(MAX_REDIRECTS + 1):
                if problem := await _public_url_problem(target):
                    check.fetched, check.note = False, problem
                    return check
                async with client.stream("GET", target) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        target = str(resp.url.join(resp.headers["location"]))
                        continue
                    check.http_status = resp.status_code
                    ctype = resp.headers.get("content-type", "")
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) >= MAX_BODY_BYTES:
                            break
                    break
            else:
                check.fetched, check.note = False, "too many redirects"
                return check
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            check.fetched, check.note = False, type(exc).__name__
            return check
    check.fetched = check.http_status is not None and check.http_status < 400
    if check.fetched and quotes and ("html" in ctype or "text" in ctype):
        body = _squash(body.decode("utf-8", errors="replace"))
        check.quote_found = any(_squash(q)[:120] in body for q in quotes)
        if not check.quote_found:
            check.note = "quoted passage not found verbatim on the page (may be paraphrased or rendered by JS)"
    return check


async def validate(
    synthesis: Synthesis, members: list[MemberReport], bundle: ContextBundle, *, fetch_sources: bool
) -> ValidationReport:
    report = ValidationReport()
    member_index, originals = _member_urls(members)

    cited = {c.url for c in synthesis.narrative.citations if c.url}
    cited |= set(find_urls(synthesis.narrative.answer))
    for url in sorted(cited):
        traced = sorted(member_index.get(normalize_url(url), set()))
        report.citations.append(CitationCheck(url=url, traced_to_members=traced))
        if not traced:
            report.untraceable_citations.append(url)
    if report.untraceable_citations:
        report.warnings.append(
            f"{len(report.untraceable_citations)} synthesis citation(s) do not appear in any member report "
            "(possible citation laundering or invented source)"
        )

    md = synthesis.markdown
    uniques = [c for c in synthesis.matrix.claims if c.classification == Classification.unique]
    report.unique_findings_preserved = all(c.statement in md for c in uniques)
    if not report.unique_findings_preserved:
        report.warnings.append("some unique findings are missing from the synthesis text")

    for c in synthesis.matrix.claims:
        if c.classification == Classification.agreement and c.independent_sources <= 1:
            report.warnings.append(
                f"{c.id}: agreement of {c.support} members rests on {c.independent_sources} independent source "
                "domain(s) - correlated-consensus risk"
            )
    for m in members:
        if m.status is not MemberStatus.succeeded:
            report.warnings.append(f"{m.key} did not report ({m.status.value}): {m.error or ''}".rstrip(": "))
        elif m.findings is None:
            report.warnings.append(f"{m.key}: structured findings could not be parsed; claims missing from matrix")
        elif m.parse_error:
            report.warnings.append(f"{m.key}: {m.parse_error}")
        elif m.telemetry and m.telemetry.web_searches == 0 and not m.telemetry.tool_calls:
            report.warnings.append(f"{m.key} made no web searches or tool calls - findings rest on model knowledge")
    for f in bundle.flagged:
        report.warnings.append(f"context file {f.name} contains possible prompt-injection text: {f.injection_flags}")

    if fetch_sources:
        quotes = _quotes_by_url(members)
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        headers = {"User-Agent": "model-committee/0.1 (+source verification)"}
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=False, headers=headers) as client:
            fetched = await asyncio.gather(
                *[_fetch(client, originals[n], quotes.get(n, []), sem) for n in sorted(member_index)]
            )
        by_url = {normalize_url(c.url): c for c in report.citations}
        for check in fetched:
            norm = normalize_url(check.url)
            check.traced_to_members = sorted(member_index.get(norm, set()))
            existing = by_url.get(norm)
            if existing:
                existing.fetched, existing.http_status = check.fetched, check.http_status
                existing.quote_found, existing.note = check.quote_found, check.note
            else:
                report.citations.append(check)
        dead = [c.url for c in report.citations if c.fetched is False]
        if dead:
            report.warnings.append(f"{len(dead)} cited source(s) could not be fetched")
    return report
