import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from model_committee import cli, launcher, mcp_server
from model_committee.artifacts import RUNS_DIR_ENV, process_started
from model_committee.guard import MEMBER_ENV_VAR
from model_committee.orchestrator import RunResult
from model_committee.providers._subprocess import MEMBER_ENV
from model_committee.schemas import RunConfig, RunManifest
from tests.fakes import NARRATIVE, FakeProvider, factory
from tests.test_orchestrator import ALIGNMENT, A, B, C


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(RUNS_DIR_ENV, str(tmp_path / "runs"))
    monkeypatch.delenv(MEMBER_ENV_VAR, raising=False)
    monkeypatch.setattr(mcp_server, "_handles", {})
    monkeypatch.setattr(mcp_server, "START_WAIT_S", 0.05)
    yield


def _fake_lineup() -> dict[str, FakeProvider]:
    return {"a": FakeProvider(research_reply=A), "b": FakeProvider(research_reply=B),
            "c": FakeProvider(research_reply=C), "s": FakeProvider(synth_replies=[ALIGNMENT, NARRATIVE])}


async def _wait_finished(run_id: str, timeout_s: float = 10.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status = await mcp_server.committee_status(run_id)
        if status["status"] != "running":
            return status
        assert asyncio.get_running_loop().time() < deadline, f"run still running: {status}"
        await asyncio.sleep(0.02)


def test_member_env_var_matches_what_providers_set():
    assert MEMBER_ENV == {MEMBER_ENV_VAR: "1"}


def test_tools_are_registered():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert names == {"committee_providers", "committee_run", "committee_status", "committee_result",
                     "committee_list_runs", "committee_cancel"}


async def test_run_status_result_list_with_fake_providers(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(mcp_server, "provider_factory", factory(_fake_lineup()))
    started = await mcp_server.committee_run("Which supplier should we choose?", models=["a", "b", "c"],
                                             synthesizer="s", depth="brief", deliverable="none",
                                             criteria=["cost"])
    run_id = started["run_id"]
    assert started["run_dir"] == str(tmp_path / "runs" / run_id)
    assert started["depth"] == "brief" and started["deliverable"] == "none"
    assert started["models"] == ["a", "b", "c"]

    status = await _wait_finished(run_id)
    assert status["status"] == "succeeded", status
    assert [m["status"] for m in status["members"]] == ["succeeded"] * 3
    assert status["last_events"][-1]["type"] == "run.finished"

    result = await mcp_server.committee_result(run_id)
    assert result["status"] == "succeeded"
    assert result["headline"] == "Choose A"
    assert result["synthesis_markdown"] and "Choose A" in result["synthesis_markdown"]
    assert len(result["members_succeeded"]) == 3
    paths = {Path(a["path"]).name for a in result["artifacts"]}
    assert {"synthesis.md", "validation.json", "claim-matrix.json"} <= paths
    assert all(Path(a["path"]).is_file() for a in result["artifacts"])
    assert not any(a["kind"] in mcp_server.HIDDEN_ARTIFACT_KINDS for a in result["artifacts"])
    assert isinstance(result["validation_warnings"], list)

    listed = await mcp_server.committee_list_runs(limit=5)
    assert listed["runs"][0]["run_id"] == run_id
    assert listed["runs"][0]["status"] == "succeeded"


async def test_result_refused_while_running_and_status_reports_progress(monkeypatch, tmp_path: Path):
    release = asyncio.Event()

    async def slow_runner(config: RunConfig, *, runs_root: Path, on_event, provider_factory, run_id: str):
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True)
        manifest = RunManifest(run_id=run_id, config=config)
        (run_dir / "run-manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
        (run_dir / "events.jsonl").write_text('{"type": "run.started"}\n', encoding="utf-8")
        await release.wait()
        manifest.status = "succeeded"
        (run_dir / "run-manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
        return RunResult(run_dir=run_dir, manifest=manifest, synthesis=None)

    monkeypatch.setattr(mcp_server, "run_committee", slow_runner)
    started = await mcp_server.committee_run("Is the slow run done?", models=["x", "y"], deliverable="none")
    run_id = started["run_id"]
    assert started["status"] == "running"
    assert started["depth"] == "standard"  # auto-inferred

    status = await mcp_server.committee_status(run_id)
    assert status["status"] == "running"
    assert status["last_events"] == [{"type": "run.started"}]
    with pytest.raises(ToolError, match="still running"):
        await mcp_server.committee_result(run_id)

    release.set()
    await mcp_server._handles[run_id].task
    assert (await mcp_server.committee_status(run_id))["status"] == "succeeded"


async def test_crashed_runner_reported_as_failed(monkeypatch):
    async def broken_runner(config, *, runs_root, on_event, provider_factory, run_id):
        raise RuntimeError("orchestrator exploded")

    monkeypatch.setattr(mcp_server, "run_committee", broken_runner)
    started = await mcp_server.committee_run("Will this crash?", models=["x", "y"], deliverable="none")
    assert started["status"] == "failed"
    assert "orchestrator exploded" in started["error"]
    result = await mcp_server.committee_result(started["run_id"])
    assert result["status"] == "failed" and result["synthesis_markdown"] is None


async def test_unavailable_provider_fails_fast_inline(monkeypatch):
    lineup = _fake_lineup()
    lineup["b"] = FakeProvider(research_reply=B, is_available=False)
    monkeypatch.setattr(mcp_server, "provider_factory", factory(lineup))
    monkeypatch.setattr(mcp_server, "START_WAIT_S", 5.0)
    started = await mcp_server.committee_run("Which supplier should we choose?", models=["a", "b"],
                                             synthesizer="s", deliverable="none")
    assert started["status"] == "failed"
    assert "not available" in started["error"]


async def test_recursion_guard_refuses_run(monkeypatch):
    monkeypatch.setenv(MEMBER_ENV_VAR, "1")
    calls = []
    monkeypatch.setattr(mcp_server, "run_committee", lambda *a, **k: calls.append(a))
    with pytest.raises(ToolError, match="committee member"):
        await mcp_server.committee_run("Should a member start a committee?", models=["x", "y"])
    assert calls == [] and mcp_server._handles == {}


def test_cli_run_refuses_inside_member(monkeypatch, capsys):
    monkeypatch.setenv(MEMBER_ENV_VAR, "1")
    assert cli.main(["run", "Should a member start a committee?", "-m", "a", "-m", "b"]) == 2
    assert "committee member" in capsys.readouterr().out


async def test_invalid_config_is_a_tool_error(monkeypatch):
    monkeypatch.setattr(mcp_server, "run_committee", lambda *a, **k: None)
    with pytest.raises(ToolError, match="2-8 members"):
        await mcp_server.committee_run("One member is not a committee", models=["a"])
    with pytest.raises(ToolError, match="context file not found"):
        await mcp_server.committee_run("Read this file", models=["a", "b"], context_paths=["nope/missing.pdf"])


@pytest.mark.parametrize("run_id", ["../etc", "", "no such run", "20990101-000000-missing"])
async def test_bad_run_id(run_id: str):
    with pytest.raises(ToolError, match="run_id"):
        await mcp_server.committee_status(run_id)
    with pytest.raises(ToolError, match="run_id"):
        await mcp_server.committee_result(run_id)


async def test_list_runs_empty_and_providers_hook(monkeypatch):
    assert (await mcp_server.committee_list_runs())["runs"] == []

    async def fake_detect():
        return [{"name": "claude-cli", "available": True}]

    monkeypatch.setattr(mcp_server, "detect_available", fake_detect)
    out = await mcp_server.committee_providers()
    assert out["providers"] == [{"name": "claude-cli", "available": True}]
    assert len(out["default_lineup"]) >= 2 and out["min_members"] == 2


def test_runs_root_defaults_to_repo(monkeypatch):
    monkeypatch.delenv(RUNS_DIR_ENV)
    repo = Path(mcp_server.__file__).resolve().parents[2]
    assert mcp_server.runs_root() == repo / "runs"


# ---------------------------------------------------------------- detached runs, reconcile, cancel


def _write_running_manifest(root: Path, run_id: str, *, owner_pid: int | None) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    config = RunConfig(objective="Was this run interrupted?", models=["a", "b"])
    manifest = RunManifest(run_id=run_id, config=config, owner_pid=owner_pid,
                           owner_started=process_started(owner_pid) if owner_pid else None)
    (run_dir / "run-manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    (run_dir / "events.jsonl").write_text('{"type": "run.started"}\n', encoding="utf-8")
    return run_dir


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def test_unavailable_providers_rejected_inline_without_launching(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    launched = []
    monkeypatch.setattr(mcp_server, "launch_detached", lambda *a, **k: launched.append(a))
    with pytest.raises(ToolError, match="not available, nothing was launched") as err:
        await mcp_server.committee_run("Which API should we use?", models=["openai:x", "anthropic:y"],
                                       deliverable="none")
    assert "openai:x" in str(err.value) and "anthropic:y" in str(err.value)
    assert launched == [] and mcp_server._handles == {}
    assert not (tmp_path / "runs" / launcher.LAUNCH_DIR).exists()


async def test_detached_child_dying_early_returns_launch_log(monkeypatch, tmp_path: Path):
    async def ok(config):
        return None

    def crashing_launch(config, *, runs_root: Path, run_id: str) -> int:
        launch = runs_root / launcher.LAUNCH_DIR
        launch.mkdir(parents=True, exist_ok=True)
        with open(launch / f"{run_id}.log", "wb") as log:
            proc = subprocess.Popen([sys.executable, "-c", "import sys; print('boom: no such provider'); sys.exit(3)"],
                                    stdout=log, stderr=subprocess.STDOUT)
        proc.wait()
        return proc.pid

    monkeypatch.setattr(mcp_server, "_preflight", ok)
    monkeypatch.setattr(mcp_server, "launch_detached", crashing_launch)
    with pytest.raises(ToolError, match="exited before starting") as err:
        await mcp_server.committee_run("Will the child crash?", models=["x", "y"], deliverable="none")
    assert "boom: no such provider" in str(err.value)


async def test_dead_owner_is_reconciled_to_interrupted(tmp_path: Path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    while process_started(proc.pid) is not None:  # Windows can report a just-exited process briefly
        time.sleep(0.05)
    root = tmp_path / "runs"
    run_dir = _write_running_manifest(root, "20990101-000000-orphan", owner_pid=proc.pid)

    status = await mcp_server.committee_status("20990101-000000-orphan")
    assert status["status"] == "failed"
    assert status["error"].startswith("interrupted")
    assert status["last_events"][-1]["type"] == "run.failed"
    assert '"status": "failed"' in (run_dir / "run-manifest.json").read_text(encoding="utf-8")
    listed = await mcp_server.committee_list_runs()
    assert listed["runs"][0]["status"] == "failed"
    result = await mcp_server.committee_result("20990101-000000-orphan")  # finished now, so no "still running"
    assert result["status"] == "failed" and result["error"].startswith("interrupted")


async def test_live_owner_stays_running(tmp_path: Path):
    _write_running_manifest(tmp_path / "runs", "20990101-000000-alive", owner_pid=os.getpid())
    assert (await mcp_server.committee_status("20990101-000000-alive"))["status"] == "running"


async def test_cancel_kills_detached_owner(tmp_path: Path):
    proc = _sleeper()
    try:
        run_dir = _write_running_manifest(tmp_path / "runs", "20990101-000000-cancel-me", owner_pid=proc.pid)
        out = await mcp_server.committee_cancel("20990101-000000-cancel-me")
        assert out["cancelled"] is True
        assert out["status"] == "failed" and out["error"] == "cancelled by user"
        assert proc.wait(timeout=10) is not None
        assert (run_dir / launcher.CANCEL_MARKER).exists()

        again = await mcp_server.committee_cancel("20990101-000000-cancel-me")
        assert again["cancelled"] is False and again["detail"] == "run already failed"
    finally:
        if proc.poll() is None:
            proc.kill()


async def test_cancel_refuses_to_kill_this_server(tmp_path: Path):
    _write_running_manifest(tmp_path / "runs", "20990101-000000-self", owner_pid=os.getpid())
    with pytest.raises(ToolError, match="no separate owner process"):
        await mcp_server.committee_cancel("20990101-000000-self")


async def test_cancel_in_process_run(monkeypatch):
    async def forever(config, *, runs_root: Path, on_event, provider_factory, run_id: str):
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run-manifest.json").write_text(RunManifest(run_id=run_id, config=config).model_dump_json(),
                                                   encoding="utf-8")
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp_server, "run_committee", forever)
    started = await mcp_server.committee_run("Cancel me please", models=["x", "y"], deliverable="none")
    out = await mcp_server.committee_cancel(started["run_id"])
    assert out == {"run_id": started["run_id"], "cancelled": True, "status": "failed",
                   "error": "cancelled by user"}


@pytest.mark.live
async def test_live_detached_ollama_run_finishes(tmp_path: Path):
    """Real detached launch (no hooks): local Ollama members + synthesizer. Free but slow."""
    model = "ollama:qwen2.5-coder:1.5b"
    started = await mcp_server.committee_run("Is SQLite a reasonable default database for a single-user app?",
                                             models=[model, model], synthesizer=model, depth="brief",
                                             deliverable="none")
    run_id, pid = started["run_id"], started["pid"]
    assert pid and pid != os.getpid()
    assert started["status"] == "running"
    status = await _wait_finished(run_id, timeout_s=900)
    assert status["status"] == "succeeded", (status, launcher.launch_log(tmp_path / "runs", run_id)[-2000:])
    manifest = RunManifest.model_validate_json((tmp_path / "runs" / run_id / "run-manifest.json").read_text("utf-8"))
    # Owned by the detached process, not this one (on Windows a venv python.exe shim may sit in between, so the
    # recorded owner can be the launched pid's child).
    assert manifest.owner_pid not in (None, os.getpid())
    result = await mcp_server.committee_result(run_id)
    assert result["synthesis_markdown"]


async def test_launch_failure_is_a_tool_error(monkeypatch):
    async def ok(config):
        return None

    def refused(config, *, runs_root, run_id):
        raise OSError("WMI process creation failed: 2")

    monkeypatch.setattr(mcp_server, "_preflight", ok)
    monkeypatch.setattr(mcp_server, "launch_detached", refused)
    with pytest.raises(ToolError, match="could not start the committee process: WMI"):
        await mcp_server.committee_run("Can we launch?", models=["x", "y"], deliverable="none")
