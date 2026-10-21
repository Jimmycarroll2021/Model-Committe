from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from model_committee import server
from model_committee.schemas import ArtifactRecord, Event, MemberReport, MemberStatus, RunConfig, RunManifest

MODELS = ["claude-cli", "codex-cli", "agy:gemini-3.8-flash-high"]


def _write_run(root: Path, run_id: str, config: RunConfig, *, status: str = "succeeded") -> list[Event]:
    """Lay down a finished run the way the orchestrator would."""
    run_dir = root / run_id
    (run_dir / "members").mkdir(parents=True)
    (run_dir / "report.html").write_text("<h1>Report</h1><script>alert(1)</script>", encoding="utf-8")
    (run_dir / "synthesis.md").write_text("# Answer\n\nYes.", encoding="utf-8")
    (run_dir / "members" / "m1-claude-cli.md").write_text("member report", encoding="utf-8")
    (run_dir / "secret.txt").write_text("not an artifact", encoding="utf-8")
    members = [
        MemberReport(key=f"m{i + 1}-{spec.split(':')[0]}", spec=spec, label=spec, vendor="v",
                     status=MemberStatus.succeeded)
        for i, spec in enumerate(config.models)
    ]
    manifest = RunManifest(
        run_id=run_id,
        status=status,
        config=config,
        members=members,
        artifacts=[
            ArtifactRecord(path="report.html", sha256="x", kind="report"),
            ArtifactRecord(path="synthesis.md", sha256="x", kind="synthesis"),
            ArtifactRecord(path="members/m1-claude-cli.md", sha256="x", kind="member_report", immutable=True),
        ],
    )
    events = [Event(type="run.started")]
    for m in members:
        events.append(Event(type="member.started", member=m.key))
    for m in members:
        events.append(Event(type="member.finished", member=m.key, status="succeeded"))
    events += [Event(type="synthesis.started"), Event(type="synthesis.finished"),
               Event(type="outputs.finished"),
               Event(type="run.finished" if status == "succeeded" else "run.failed", status=status)]
    (run_dir / "events.jsonl").write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf-8")
    (run_dir / "run-manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return events


def _write_events(run_dir: Path, events: list[Event]) -> None:
    lines = "".join(e.model_dump_json() + "\n" for e in events)
    (run_dir / "events.jsonl").write_text(lines, encoding="utf-8")


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    async def fake_run_committee(config, *, runs_root, on_event=None, provider_factory=None, run_id=None):
        seen.append({"config": config, "run_id": run_id, "provider_factory": provider_factory})
        await asyncio.sleep(0.05)
        _write_run(Path(runs_root), run_id, config)
        return None

    monkeypatch.setattr(server, "run_committee", fake_run_committee)
    return seen


@pytest.fixture
def client(runs_root: Path, calls: list[dict]):
    with TestClient(server.create_app(runs_root)) as c:
        yield c


def _sse_events(client: TestClient, run_id: str, headers: dict | None = None) -> list[dict]:
    out = []
    with client.stream("GET", f"/api/runs/{run_id}/events", headers=headers or {}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


def test_index_serves_ui(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Model Committee" in resp.text
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200


def test_providers_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    async def fake_detect():
        return [{"name": "claude-cli", "vendor": "anthropic", "available": True, "reason": "ok",
                 "example_spec": "claude-cli", "models": []}]

    monkeypatch.setattr(server, "detect_available", fake_detect)
    monkeypatch.setattr(server, "DEFAULT_LINEUP", ["claude-cli", "codex-cli"])
    body = client.get("/api/providers").json()
    assert body["providers"][0]["name"] == "claude-cli"
    assert body["default_lineup"] == ["claude-cli", "codex-cli"]
    assert body["default_synthesizer"] == "claude-cli"
    assert (body["min_members"], body["max_members"]) == (2, 8)


@pytest.mark.parametrize("n", [1, 9])
def test_post_rejects_bad_lineup_size(client: TestClient, calls: list[dict], n: int):
    resp = client.post("/api/runs", json={"objective": "Compare things", "models": ["claude-cli"] * n})
    assert resp.status_code == 422
    assert calls == []


def test_post_rejects_non_json_body(client: TestClient, calls: list[dict]):
    body = json.dumps({"objective": "Compare things", "models": MODELS})
    resp = client.post("/api/runs", content=body, headers={"content-type": "text/plain"})
    assert resp.status_code == 422
    assert calls == []


def test_post_rejects_foreign_origin_and_host(client: TestClient, calls: list[dict]):
    payload = {"objective": "Compare things", "models": MODELS}
    resp = client.post("/api/runs", json=payload, headers={"origin": "https://evil.example"})
    assert resp.status_code == 403
    assert client.get("/api/runs", headers={"host": "evil.example"}).status_code == 403
    assert calls == []


def _wait_for_status(client: TestClient, run_id: str, want: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        if resp.status_code == 200 and resp.json()["status"] == want:
            return resp.json()
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached {want}")


def test_post_happy_path_runs_to_completion(client: TestClient, calls: list[dict]):
    resp = client.post("/api/runs", json={"objective": "Which database for a small SaaS?", "models": MODELS,
                                          "depth": "brief", "deliverable": "all"},
                       headers={"origin": "http://127.0.0.1:8765"})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert run_id.endswith("which-database-for-a-small-saas")
    manifest = _wait_for_status(client, run_id, "succeeded")
    assert calls[0]["run_id"] == run_id
    assert calls[0]["config"].depth == "brief"
    assert [m["spec"] for m in manifest["members"]] == MODELS
    events = _sse_events(client, run_id)
    assert events[-1]["type"] == "run.finished"


def test_same_objective_twice_gets_distinct_run_ids(client: TestClient):
    payload = {"objective": "Same question", "models": MODELS}
    a = client.post("/api/runs", json=payload).json()["run_id"]
    b = client.post("/api/runs", json=payload).json()["run_id"]
    assert a != b
    _wait_for_status(client, a, "succeeded")
    _wait_for_status(client, b, "succeeded")


def test_failed_launch_reports_error(runs_root: Path, monkeypatch: pytest.MonkeyPatch):
    async def boom(config, **kwargs):
        raise ValueError("unknown provider 'nope'")

    monkeypatch.setattr(server, "run_committee", boom)
    with TestClient(server.create_app(runs_root)) as c:
        payload = {"objective": "Anything", "models": ["nope", "nope"]}
        run_id = c.post("/api/runs", json=payload).json()["run_id"]
        manifest = _wait_for_status(c, run_id, "failed")
        assert "unknown provider" in manifest["error"]
        events = _sse_events(c, run_id)
        assert events[-1]["type"] == "run.failed"
        assert "unknown provider" in events[-1]["detail"]


def test_cancel_running_run(runs_root: Path, monkeypatch: pytest.MonkeyPatch):
    async def slow(config, *, runs_root, run_id, **kwargs):
        (Path(runs_root) / run_id).mkdir(parents=True)
        await asyncio.sleep(30)

    monkeypatch.setattr(server, "run_committee", slow)
    with TestClient(server.create_app(runs_root)) as c:
        run_id = c.post("/api/runs", json={"objective": "Slow one", "models": MODELS}).json()["run_id"]
        assert c.post(f"/api/runs/{run_id}/cancel").json()["cancelled"] is True
        _wait_for_status(c, run_id, "failed")
        events = _sse_events(c, run_id)
        assert len(events) == 1
        assert events[0]["type"] == "run.failed" and events[0]["status"] == "cancelled"
        assert c.post(f"/api/runs/{run_id}/cancel").json()["cancelled"] is False
    assert TestClient(server.create_app(runs_root)).post("/api/runs/unknown-run/cancel").status_code == 404


def test_list_and_get_runs(client: TestClient, runs_root: Path):
    cfg = RunConfig(objective="Older question", models=MODELS[:2])
    _write_run(runs_root, "20260101-000000-older-question", cfg)
    newer = RunConfig(objective="Newer question", models=MODELS)
    _write_run(runs_root, "20260102-000000-newer-question", newer, status="failed")
    (runs_root / "not-a-run").mkdir()
    rows = client.get("/api/runs").json()
    assert [r["run_id"] for r in rows] == ["20260102-000000-newer-question", "20260101-000000-older-question"]
    assert rows[0]["objective"] == "Newer question"
    assert rows[0]["status"] == "failed"
    assert [m["status"] for m in rows[1]["members"]] == ["succeeded", "succeeded"]
    got = client.get("/api/runs/20260101-000000-older-question").json()
    assert got["config"]["objective"] == "Older question"
    assert client.get("/api/runs/20990101-000000-missing").status_code == 404


def test_sse_replays_finished_run_in_order_and_terminates(client: TestClient, runs_root: Path):
    cfg = RunConfig(objective="Replay me", models=MODELS)
    written = _write_run(runs_root, "20260103-000000-replay-me", cfg)
    events = _sse_events(client, "20260103-000000-replay-me")
    assert [e["type"] for e in events] == [e.type for e in written]
    assert [e.get("member") for e in events] == [e.member for e in written]
    # Last-Event-ID resumes after the given event.
    resumed = _sse_events(client, "20260103-000000-replay-me", headers={"last-event-id": "3"})
    assert [e["type"] for e in resumed] == [e.type for e in written[3:]]


def test_sse_follows_live_file(client: TestClient, runs_root: Path):
    cfg = RunConfig(objective="Live one", models=MODELS[:2])
    run_dir = runs_root / "20260104-000000-live-one"
    run_dir.mkdir(parents=True)
    manifest = RunManifest(run_id=run_dir.name, config=cfg)
    (run_dir / "run-manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(Event(type="run.started").model_dump_json() + "\n", encoding="utf-8")

    import threading

    def writer():
        time.sleep(0.4)
        with open(run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(Event(type="member.started", member="m1").model_dump_json() + "\n")
            fh.write(Event(type="run.finished", status="succeeded").model_dump_json() + "\n")

    t = threading.Thread(target=writer)
    t.start()
    events = _sse_events(client, run_dir.name)
    t.join()
    assert [e["type"] for e in events] == ["run.started", "member.started", "run.finished"]


def test_sse_closes_for_finished_run_without_terminal_event(client: TestClient, runs_root: Path):
    cfg = RunConfig(objective="Truncated", models=MODELS[:2])
    run_dir = runs_root / "20260105-000000-truncated"
    run_dir.mkdir(parents=True)
    manifest = RunManifest(run_id=run_dir.name, config=cfg, status="failed", error="crashed")
    (run_dir / "run-manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(Event(type="run.started").model_dump_json() + "\n", encoding="utf-8")
    events = _sse_events(client, run_dir.name)
    assert [e["type"] for e in events] == ["run.started", "run.failed"]
    assert events[-1]["detail"] == "crashed"


@pytest.fixture
def finished_run(runs_root: Path) -> str:
    run_id = "20260106-000000-files"
    _write_run(runs_root, run_id, RunConfig(objective="Files", models=MODELS))
    return run_id


def test_listed_artifact_served_with_csp(client: TestClient, finished_run: str):
    resp = client.get(f"/api/runs/{finished_run}/files/report.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "style-src 'unsafe-inline'" in csp and "img-src data:" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in resp.headers

    md = client.get(f"/api/runs/{finished_run}/files/synthesis.md")
    assert md.headers["content-type"].startswith("text/markdown")
    member = client.get(f"/api/runs/{finished_run}/files/members/m1-claude-cli.md")
    assert member.status_code == 200 and member.text == "member report"
    assert "default-src 'none'" in member.headers["content-security-policy"]


def test_download_sets_attachment(client: TestClient, finished_run: str):
    resp = client.get(f"/api/runs/{finished_run}/files/report.html?download=1")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")
    assert "report.html" in resp.headers["content-disposition"]


@pytest.mark.parametrize(
    "path",
    [
        "secret.txt",
        "run-manifest.json",
        "../secret.txt",
        "members/../secret.txt",
        "%2e%2e/%2e%2e/pyproject.toml",
        "..%2F..%2Fpyproject.toml",
        "members%2F..%2F..%2Fsecret.txt",
        "%2E%2E%5Csecret.txt",
    ],
)
def test_unlisted_or_traversal_paths_rejected(client: TestClient, finished_run: str, path: str):
    resp = client.get(f"/api/runs/{finished_run}/files/{path}")
    assert resp.status_code == 404
    assert "not an artifact" not in resp.text


def test_listed_path_escaping_run_dir_rejected(client: TestClient, runs_root: Path, finished_run: str):
    """Even if a manifest were tampered with to list '../x', the file must stay inside the run dir."""
    (runs_root / "outside.txt").write_text("outside", encoding="utf-8")
    manifest_path = runs_root / finished_run / "run-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["artifacts"].append({"path": "../outside.txt", "sha256": "x", "kind": "evil"})
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    from urllib.parse import quote

    resp = client.get(f"/api/runs/{finished_run}/files/{quote('../outside.txt', safe='')}")
    assert resp.status_code == 404
    assert "outside" not in resp.text


@pytest.mark.parametrize("run_id", ["..", "..%2F..", "bad.id", "-leading", "a" * 200, "%2e%2e"])
def test_bad_run_ids_rejected(client: TestClient, run_id: str):
    for url in (f"/api/runs/{run_id}", f"/api/runs/{run_id}/events", f"/api/runs/{run_id}/files/report.html"):
        assert client.get(url).status_code in (404, 422), url


# ---------------------------------------------------------------- recursion guard


def test_post_refused_inside_committee_member(client: TestClient, calls: list[dict],
                                              monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL_COMMITTEE_MEMBER", "1")
    resp = client.post("/api/runs", json={"objective": "Nested run", "models": MODELS})
    assert resp.status_code == 409
    assert "MODEL_COMMITTEE_MEMBER" in resp.json()["detail"]
    assert calls == []


# ---------------------------------------------------------------- context allow-list + upload


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(server.CONTEXT_DIR_ENV, raising=False)
    return tmp_path / "context-inbox"  # default: <runs_root>/../context-inbox


def _post_with_context(client: TestClient, paths: list[str]):
    return client.post("/api/runs", json={"objective": "With context", "models": MODELS, "context_paths": paths})


@pytest.mark.parametrize("bad", ["ABS_OUTSIDE", "../secret.txt", "sub/../../secret.txt", "missing.md"])
def test_context_paths_outside_inbox_rejected(client: TestClient, calls: list[dict], inbox: Path,
                                              tmp_path: Path, bad: str):
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    resp = _post_with_context(client, [str(secret) if bad == "ABS_OUTSIDE" else bad])
    assert resp.status_code == 422
    assert "context_paths" in json.dumps(resp.json())
    assert calls == []


def test_context_path_inside_inbox_accepted_and_resolved(client: TestClient, calls: list[dict], inbox: Path):
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "brief.md").write_text("context", encoding="utf-8")
    resp = _post_with_context(client, ["brief.md"])
    assert resp.status_code == 202
    _wait_for_status(client, resp.json()["run_id"], "succeeded")
    assert calls[0]["config"].context_paths == [str((inbox / "brief.md").resolve())]


def test_context_dir_env_override(runs_root: Path, tmp_path: Path, calls: list[dict],
                                  monkeypatch: pytest.MonkeyPatch):
    custom = tmp_path / "elsewhere"
    custom.mkdir()
    (custom / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv(server.CONTEXT_DIR_ENV, str(custom))
    with TestClient(server.create_app(runs_root)) as c:
        assert _post_with_context(c, [str(custom / "a.txt")]).status_code == 202
        default_inbox = tmp_path / "context-inbox"
        default_inbox.mkdir()
        (default_inbox / "b.txt").write_text("x", encoding="utf-8")
        assert _post_with_context(c, [str(default_inbox / "b.txt")]).status_code == 422


def test_upload_context_then_run(client: TestClient, calls: list[dict], inbox: Path):
    resp = client.post("/api/context", params={"filename": "../../Q3 notes.md"}, content=b"# notes",
                       headers={"content-type": "application/octet-stream"})
    assert resp.status_code == 201
    body = resp.json()
    stored = inbox / body["path"]
    assert stored.read_bytes() == b"# notes"
    assert stored.resolve().parent == inbox.resolve()
    assert body["path"].endswith("Q3-notes.md") and body["name"] == "Q3 notes.md"
    run = _post_with_context(client, [body["path"]])
    assert run.status_code == 202


def test_upload_context_rejects_bad_files(client: TestClient, inbox: Path, monkeypatch: pytest.MonkeyPatch):
    def up(name: str, data: bytes):
        return client.post("/api/context", params={"filename": name}, content=data)

    assert up("tool.exe", b"MZ").status_code == 415
    assert up("..", b"x").status_code == 415
    assert up("empty.md", b"").status_code == 422
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 10)
    assert up("big.txt", b"x" * 11).status_code == 413
    assert not inbox.exists() or not any(inbox.iterdir())


# ---------------------------------------------------------------- access token + bind policy


def test_token_required_on_api_routes(runs_root: Path, calls: list[dict]):
    app = server.create_app(runs_root, token="s3cret")
    with TestClient(app) as c:
        assert c.get("/").status_code == 200  # the UI shell itself carries no data
        assert c.get("/api/runs").status_code == 401
        assert c.get("/api/runs", headers={"authorization": "Bearer nope"}).status_code == 401
        payload = {"objective": "Token run", "models": MODELS}
        assert c.post("/api/runs", json=payload).status_code == 401
        assert c.post("/api/context", params={"filename": "a.md"}, content=b"x").status_code == 401
        assert calls == []
        ok = c.get("/api/runs", headers={"authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert "httponly" in ok.headers["set-cookie"].lower()
        assert "samesite=strict" in ok.headers["set-cookie"].lower()
    with TestClient(app, cookies={server.AUTH_COOKIE: "s3cret"}) as c:
        assert c.get("/api/runs").status_code == 200  # cookie path: EventSource + artifact links
    with TestClient(app, cookies={server.AUTH_COOKIE: "wrong"}) as c:
        assert c.get("/api/runs").status_code == 401


def test_remote_bind_requires_allow_remote_and_token(runs_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(server.TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit):
        server.build_app("0.0.0.0", runs_root)
    with pytest.raises(SystemExit):
        server.build_app("192.168.1.20", runs_root, allow_remote=True)  # no token
    monkeypatch.setenv(server.TOKEN_ENV, "s3cret")
    with pytest.raises(SystemExit):
        server.build_app("0.0.0.0", runs_root)  # token but no allow_remote
    with pytest.raises(ValueError):
        server.create_app(runs_root, extra_hosts={"192.168.1.20"})

    app = server.build_app("0.0.0.0", runs_root, allow_remote=True)
    with TestClient(app) as c:
        lan = {"host": "192.168.1.20:8765"}
        assert c.get("/api/runs", headers=lan).status_code == 401
        # Spoofing Host: localhost does not bypass the token either.
        assert c.get("/api/runs", headers={"host": "localhost"}).status_code == 401
        assert c.get("/api/runs", headers={**lan, "authorization": "Bearer s3cret"}).status_code == 200


def test_loopback_bind_is_open_but_honours_token(runs_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(server.TOKEN_ENV, raising=False)
    with TestClient(server.build_app("127.0.0.1", runs_root)) as c:
        assert c.get("/api/runs").status_code == 200
    monkeypatch.setenv(server.TOKEN_ENV, "s3cret")
    with TestClient(server.build_app("localhost", runs_root)) as c:
        assert c.get("/api/runs").status_code == 401


def test_serve_accepts_allow_remote_kwarg():
    import inspect

    assert inspect.signature(server.serve).parameters["allow_remote"].default is False
    assert server.is_loopback("127.0.0.1") and server.is_loopback("::1") and server.is_loopback("localhost")
    assert not server.is_loopback("0.0.0.0") and not server.is_loopback("192.168.1.20")


# ---------------------------------------------------------------- detached runs + interrupted reconciliation


def _dead_pid() -> int:
    """A PID with no live process (cheaper and steadier than spawning one and waiting for it to exit)."""
    from model_committee.artifacts import process_started

    pid = 4_000_000
    while process_started(pid) is not None:
        pid -= 4
    return pid


def _wait_status(client: TestClient, run_id: str, done, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}")
        if resp.status_code == 200:
            body = resp.json()
            if done(body):
                return body
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} never finished: {body.get('status')} {body.get('error')}")


def test_real_detached_launch_survives_outside_server(runs_root: Path, monkeypatch: pytest.MonkeyPatch):
    import os

    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODEL_COMMITTEE_MEMBER"):
        monkeypatch.delenv(var, raising=False)
    assert server.run_committee is None  # default path: no in-process hook
    with TestClient(server.create_app(runs_root)) as c:
        payload = {"objective": "Detached launch check", "models": ["openai:x", "anthropic:y"]}
        resp = c.post("/api/runs", json=payload)
        assert resp.status_code == 202, resp.text
        run_id, pid = resp.json()["run_id"], resp.json()["pid"]
        assert pid != os.getpid()
        manifest = _wait_status(c, run_id, lambda m: m["status"] != "running", timeout=90)
        # The run's owner is the child (on Windows the venv launcher may sit between, so not always == pid).
        assert manifest["owner_pid"] not in (None, os.getpid())
        assert manifest["status"] == "failed"
        assert "not available" in manifest["error"]
        events = _sse_events(c, run_id)
        assert events[0]["type"] == "run.started" and events[-1]["type"] == "run.failed"


def test_detached_launch_failure_returns_log_tail(runs_root: Path, monkeypatch: pytest.MonkeyPatch):
    from model_committee import launcher

    def fake_launch(config, *, runs_root, run_id):
        log = Path(runs_root) / launcher.LAUNCH_DIR / f"{run_id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Traceback ...\nModuleNotFoundError: no module named 'boom'\n", encoding="utf-8")
        return _dead_pid()

    monkeypatch.setattr(launcher, "launch_detached", fake_launch)
    with TestClient(server.create_app(runs_root)) as c:
        resp = c.post("/api/runs", json={"objective": "Broken launch", "models": MODELS})
        assert resp.status_code == 500
        assert "no module named 'boom'" in resp.json()["detail"]


def _running_manifest(runs_root: Path, run_id: str, owner_pid: int, owner_started: float | None) -> Path:
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    members = [MemberReport(key="m1-claude-cli", spec="claude-cli", label="c", vendor="v",
                            status=MemberStatus.running)]
    manifest = RunManifest(run_id=run_id, config=RunConfig(objective="Orphaned run", models=MODELS[:2]),
                           members=members, owner_pid=owner_pid, owner_started=owner_started)
    (run_dir / "run-manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    _write_events(run_dir, [Event(type="run.started"), Event(type="member.started", member="m1-claude-cli")])
    return run_dir


def test_dead_owner_reported_as_interrupted(client: TestClient, runs_root: Path):
    run_id = "20261021-000000-orphaned"
    _running_manifest(runs_root, run_id, _dead_pid(), 1.0)
    got = client.get(f"/api/runs/{run_id}").json()
    assert got["status"] == "failed"
    assert got["error"].startswith("interrupted")
    assert got["members"][0]["status"] == "failed"
    row = next(r for r in client.get("/api/runs").json() if r["run_id"] == run_id)
    assert row["status"] == "failed" and row["error"].startswith("interrupted")
    events = _sse_events(client, run_id)
    assert [e["type"] for e in events] == ["run.started", "member.started", "run.failed"]
    assert events[-1]["detail"].startswith("interrupted")


def test_sse_closes_when_owner_dies_mid_stream(client: TestClient, runs_root: Path):
    """A stream opened while the run looks alive must still end once its process is gone."""
    run_id = "20261021-000001-dies-mid-stream"
    _running_manifest(runs_root, run_id, _dead_pid(), 1.0)
    events = _sse_events(client, run_id)
    assert events[-1]["type"] == "run.failed"


def test_cancel_on_disk_run_kills_owner(client: TestClient, runs_root: Path):
    import subprocess
    import sys

    from model_committee.artifacts import process_started

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        deadline = time.monotonic() + 10
        while process_started(proc.pid) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        run_id = "20261021-000002-cancel-me"
        _running_manifest(runs_root, run_id, proc.pid, process_started(proc.pid))
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "running"
        resp = client.post(f"/api/runs/{run_id}/cancel")
        assert resp.status_code == 200 and resp.json()["cancelled"] is True
        proc.wait(timeout=10)
        got = client.get(f"/api/runs/{run_id}").json()
        assert got["status"] == "failed" and got["error"] == "cancelled by user"
        assert _sse_events(client, run_id)[-1]["detail"] == "cancelled by user"
        assert client.post(f"/api/runs/{run_id}/cancel").json()["cancelled"] is False
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_refuses_to_kill_the_server_itself(client: TestClient, runs_root: Path):
    import os

    from model_committee.artifacts import process_started

    run_id = "20261021-000003-own-pid"
    _running_manifest(runs_root, run_id, os.getpid(), process_started(os.getpid()))
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 409
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "running"


# ---------------------------------------------------------------- mode + answer artifacts


@pytest.mark.parametrize("mode", ["auto", "research", "reasoning"])
def test_post_passes_mode_through(client: TestClient, calls: list[dict], mode: str):
    resp = client.post("/api/runs", json={"objective": "Pick one\nA. x\nB. y", "models": MODELS, "mode": mode})
    assert resp.status_code == 202
    _wait_for_status(client, resp.json()["run_id"], "succeeded")
    assert calls[-1]["config"].mode == mode


def test_post_rejects_unknown_mode(client: TestClient, calls: list[dict]):
    resp = client.post("/api/runs", json={"objective": "Pick one", "models": MODELS, "mode": "vibes"})
    assert resp.status_code == 422
    assert calls == []


def test_member_findings_and_synthesis_json_reach_the_ui(client: TestClient, runs_root: Path):
    """The UI reads members[].findings from the manifest and the tally from synthesis.json."""
    from model_committee.schemas import MemberFindings

    run_id = "20261021-120000-mcq"
    cfg = RunConfig(objective="Which?\nA. one\nB. two", models=MODELS[:2], mode="reasoning")
    _write_run(runs_root, run_id, cfg)
    manifest_path = runs_root / run_id / "run-manifest.json"
    manifest = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    manifest.members[0].findings = MemberFindings(summary="s", answer="B", confidence=0.89)
    synthesis = {"answer_tally": {"B": [m.key for m in manifest.members]}, "mode": "reasoning",
                 "members_succeeded": [m.key for m in manifest.members], "narrative": {"headline": "B"}}
    (runs_root / run_id / "synthesis.json").write_text(json.dumps(synthesis), encoding="utf-8")
    manifest.artifacts.append(ArtifactRecord(path="synthesis.json", sha256="x", kind="synthesis-data"))
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")

    got = client.get(f"/api/runs/{run_id}").json()
    assert got["config"]["mode"] == "reasoning"
    assert got["members"][0]["findings"]["answer"] == "B"
    assert got["members"][0]["findings"]["confidence"] == 0.89
    resp = client.get(f"/api/runs/{run_id}/files/synthesis.json")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/json")
    assert resp.json()["answer_tally"] == {"B": [m.key for m in manifest.members]}


def test_ui_assets_are_revalidated_not_heuristically_cached(tmp_path):
    from fastapi.testclient import TestClient

    from model_committee.server import create_app

    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    for path in ("/", "/static/app.js", "/static/app.css"):
        assert client.get(path).headers.get("cache-control") == "no-cache", path


def test_index_versions_asset_urls_by_content(tmp_path):
    import re as _re

    from fastapi.testclient import TestClient

    from model_committee.server import create_app

    page = TestClient(create_app(tmp_path), base_url="http://127.0.0.1").get("/").text
    assert _re.search(r'/static/app\.js\?v=[0-9a-f]{10}"', page)
    assert _re.search(r'/static/app\.css\?v=[0-9a-f]{10}"', page)
