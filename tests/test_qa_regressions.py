"""Regression tests for findings from the independent QA review."""

import json
from pathlib import Path

import pytest

from model_committee.orchestrator import run_committee
from model_committee.parsing import parse_model, strip_json_block
from model_committee.schemas import (
    CanonicalClaim,
    ClaimMatrix,
    Classification,
    Deliverable,
    Depth,
    MemberFindings,
    MemberReport,
    MemberStatus,
    Position,
    RunConfig,
)
from model_committee.synthesis import classify, complete_matrix
from model_committee.validation import _fetch, _public_url_problem, find_urls, normalize_url
from tests.fakes import NARRATIVE, FakeProvider, claim, factory, member_reply


def test_duplicate_claim_ids_are_renamed_not_dropped():
    f = MemberFindings(summary="s", confidence=0.5, claims=[
        {"id": "c1", "statement": "first", "confidence": 0.5},
        {"id": "c1", "statement": "SECOND (minority)", "confidence": 0.5},
    ])
    assert [c.id for c in f.claims] == ["c1", "c1-2"]
    member = MemberReport(key="a", spec="a", label="a", vendor="t", status=MemberStatus.succeeded, findings=f)
    matrix = ClaimMatrix(members=["a"], claims=[])
    complete_matrix(matrix, [member])
    assert [c.statement for c in matrix.claims] == ["first", "SECOND (minority)"]


def test_parse_skips_trailing_unrelated_json_object():
    text = member_reply([claim("c1", "s")]) + '\nNote: config was {"debug": true}'
    assert parse_model(text, MemberFindings).claims[0].statement == "s"


def test_earlier_code_fence_does_not_pair_with_json_fence():
    text = "## Analysis\n```python\nprint(1)\n```\nMore prose.\n" + member_reply([claim("c1", "s")], prose="")
    prose = strip_json_block(text)
    assert '"claims"' not in prose and prose.endswith("More prose.")
    assert parse_model(text, MemberFindings).claims[0].id == "c1"


@pytest.mark.parametrize("kwargs", [{"retries": -1}, {"retries": 9}, {"member_timeout_s": -5}, {"quorum_grace_s": -1}])
def test_run_config_rejects_out_of_range_numbers(kwargs):
    with pytest.raises(ValueError):
        RunConfig(objective="question?", models=["a", "b"], **kwargs)


def test_refless_firm_stance_is_downgraded():
    f = MemberFindings(summary="s", confidence=0.5, claims=[{"id": "c1", "statement": "x", "confidence": 0.5}])
    members = [MemberReport(key=k, spec=k, label=k, vendor="t", status=MemberStatus.succeeded, findings=f)
               for k in ("a", "b")]
    matrix = ClaimMatrix(members=["a", "b"], claims=[CanonicalClaim(id="k", statement="x", positions={
        "a": Position(stance="support", claim_refs=["a/c1"]), "b": Position(stance="reject")})])
    warnings = complete_matrix(matrix, members)
    classify(matrix, members)
    assert matrix.claims[0].positions["b"].stance == "uncertain"
    assert any("no valid claim reference" in w for w in warnings)


def test_find_urls_and_normalisation():
    text = "See [docs](https://www.x.com/a/) and https://en.wikipedia.org/wiki/Foo_(bar). Also (https://y.org/z)."
    assert find_urls(text) == ["https://www.x.com/a/", "https://en.wikipedia.org/wiki/Foo_(bar)", "https://y.org/z"]
    assert normalize_url("http://www.X.com/a/?utm_source=q#frag") == normalize_url("https://x.com/a")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/admin", "http://169.254.169.254/latest/meta-data",
                                 "http://localhost/x", "file:///etc/passwd", "http://10.0.0.5/"])
async def test_ssrf_guard_refuses_non_public_targets(url):
    assert await _public_url_problem(url) is not None


async def test_fetch_does_not_follow_redirect_into_private_network():
    import asyncio

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/internal-admin"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        check = await _fetch(client, "https://93.184.215.14/redir", ["secret"], asyncio.Semaphore(1))
    assert check.fetched is False and "non-public" in check.note


async def test_one_failing_renderer_does_not_sink_the_run(tmp_path: Path, monkeypatch):
    import model_committee.outputs as outputs

    real = outputs.render_outputs

    def flaky(deliverable, **kw):
        if deliverable is Deliverable.deck:
            raise RuntimeError("pptx exploded")
        return real(deliverable, **kw)

    monkeypatch.setattr(outputs, "render_outputs", flaky)
    providers = {
        "a": FakeProvider(research_reply=member_reply([claim("c1", "A is cheaper", "https://a.example/p")])),
        "b": FakeProvider(research_reply=member_reply([claim("c1", "A costs less", "https://b.example/p")])),
        "s": FakeProvider(synth_replies=[json.dumps({"claims": []}), NARRATIVE]),
    }
    cfg = RunConfig(objective="Which supplier?", models=["a", "b"], synthesizer="s", depth=Depth.brief,
                    deliverable=Deliverable.all)
    result = await run_committee(cfg, runs_root=tmp_path, provider_factory=factory(providers))
    assert result.manifest.status == "succeeded"
    paths = {a.path for a in result.manifest.artifacts}
    assert {"report.html", "committee.xlsx"} <= paths and "committee-deck.pptx" not in paths
    validation = json.loads((result.run_dir / "validation.json").read_text(encoding="utf-8"))
    assert any("deck output failed" in w for w in validation["warnings"])
    assert all(c.classification is Classification.unique for c in result.synthesis.matrix.claims)


def test_unparsed_member_dissent_is_not_erased():
    f = MemberFindings(summary="s", confidence=0.5,
                       claims=[{"id": "c1", "statement": "X is better", "confidence": 0.5}])
    members = [MemberReport(key=k, spec=k, label=k, vendor="t", status=MemberStatus.succeeded, findings=f)
               for k in ("m1", "m2")]
    members.append(MemberReport(key="m3", spec="m3", label="m3", vendor="t", status=MemberStatus.succeeded,
                                report_markdown="I think Y is better."))
    matrix = ClaimMatrix(members=["m1", "m2", "m3"], claims=[CanonicalClaim(id="k", statement="X is better", positions={
        "m1": Position(stance="support", claim_refs=["c1"]),  # bare ref, tolerated
        "m2": Position(stance="support", claim_refs=["m2/c1"]),
        "m3": Position(stance="reject")})])
    complete_matrix(matrix, members)
    classify(matrix, members)
    assert len(matrix.claims) == 1
    assert matrix.claims[0].classification is Classification.disagreement
    assert matrix.claims[0].positions["m1"].claim_refs == ["m1/c1"]


async def test_malformed_urls_do_not_crash_validation():
    assert normalize_url("http://[::1") == "http://[::1"
    assert await _public_url_problem("http://example.com:abc/x") == "malformed URL"


@pytest.mark.parametrize("objective", ["Should we migrate off Excel?", "Best deep learning framework?",
                                       "What are the short-term risks?"])
def test_router_ignores_topic_words(objective):
    from model_committee.router import infer_deliverable, infer_depth

    assert infer_deliverable(objective) is Deliverable.report
    assert infer_depth(objective) is Depth.standard
