"""Detached runs and interrupted-run detection (runs must not die with, or outlive the truth of, their launcher)."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from model_committee.artifacts import (
    RunStore,
    default_runs_dir,
    process_started,
    reconcile_interrupted,
)
from model_committee.launcher import cancel_detached, launch_detached, launch_log, wait_for_run_dir
from model_committee.schemas import Event, MemberReport, MemberStatus, RunConfig, RunManifest


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _running_store(tmp_path: Path, **owner) -> RunStore:
    cfg = RunConfig(objective="question?", models=["a", "b"])
    manifest = RunManifest(run_id="r1", config=cfg, **owner,
                           members=[MemberReport(key="m1", spec="a", label="a", vendor="t",
                                                 status=MemberStatus.running)])
    store = RunStore(tmp_path, manifest)
    store.log_event(Event(type="run.started"))
    return store


def test_process_started_for_self_and_dead_pid():
    assert process_started(os.getpid()) is not None
    assert process_started(_dead_pid()) is None


def test_run_with_dead_owner_is_reported_interrupted(tmp_path: Path):
    store = _running_store(tmp_path, owner_pid=_dead_pid(), owner_started=1.0)
    manifest = reconcile_interrupted(store.dir)
    assert manifest.status == "failed" and manifest.error.startswith("interrupted")
    assert manifest.members[0].status is MemberStatus.failed
    # persisted, with a terminal event so live followers stop
    assert reconcile_interrupted(store.dir).status == "failed"
    assert '"run.failed"' in (store.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]


def test_run_with_live_owner_stays_running(tmp_path: Path):
    store = _running_store(tmp_path, owner_pid=os.getpid(), owner_started=process_started(os.getpid()))
    assert reconcile_interrupted(store.dir).status == "running"


def test_reused_pid_is_not_mistaken_for_owner(tmp_path: Path):
    store = _running_store(tmp_path, owner_pid=os.getpid(), owner_started=process_started(os.getpid()) - 3600)
    assert reconcile_interrupted(store.dir).status == "failed"


def test_legacy_manifest_without_owner_goes_stale(tmp_path: Path):
    store = _running_store(tmp_path)
    assert reconcile_interrupted(store.dir).status == "running"  # recent activity: give it the benefit
    old = time.time() - 3 * 3600
    os.utime(store.dir / "events.jsonl", (old, old))
    assert reconcile_interrupted(store.dir).status == "failed"


def test_cancel_marker_reports_cancelled(tmp_path: Path):
    store = _running_store(tmp_path, owner_pid=_dead_pid(), owner_started=1.0)
    (store.dir / "CANCELLED").write_text("x", encoding="utf-8")
    assert reconcile_interrupted(store.dir).error == "cancelled by user"


def test_default_runs_dir_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MODEL_COMMITTEE_RUNS_DIR", str(tmp_path / "r"))
    assert default_runs_dir() == (tmp_path / "r").resolve()
    monkeypatch.delenv("MODEL_COMMITTEE_RUNS_DIR")
    assert default_runs_dir().name == "runs"


def test_detached_run_records_its_own_owner_and_finishes(tmp_path: Path):
    """Real child process. Key-gated providers with no key fail fast, so this costs nothing."""
    env_keys = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
    assert not any(os.environ.get(k) for k in env_keys)
    cfg = RunConfig(objective="Which supplier?", models=["openai:x", "anthropic:y"], synthesizer="anthropic:y")
    pid = launch_detached(cfg, runs_root=tmp_path, run_id="detached-1")
    assert pid != os.getpid()
    assert wait_for_run_dir(tmp_path, "detached-1", pid, timeout_s=120), launch_log(tmp_path, "detached-1")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and process_started(pid) is not None:
        time.sleep(0.5)
    manifest = reconcile_interrupted(tmp_path / "detached-1")
    # The venv's python.exe may be a launcher that starts the real interpreter, so the run records its own PID.
    assert manifest.owner_pid not in (None, os.getpid())
    assert manifest.status == "failed" and "not available" in manifest.error
    assert cancel_detached(manifest.owner_pid, tmp_path / "detached-1") is False  # already gone


@pytest.mark.skipif(sys.platform != "win32", reason="WMI fallback is Windows-only")
def test_wmi_launch_runs_outside_parent_and_cleans_up_env(tmp_path: Path):
    """The fallback used when the parent's job object forbids breakaway (MCP SDK client, Claude Code shell)."""
    from model_committee.launcher import LAUNCH_DIR, _launch_via_wmi

    cfg = RunConfig(objective="Which supplier?", models=["openai:x", "anthropic:y"], synthesizer="anthropic:y")
    launch = tmp_path / LAUNCH_DIR
    launch.mkdir()
    (launch / "wmi-1.json").write_text(cfg.model_dump_json(), encoding="utf-8")
    cmd = [sys.executable, "-m", "model_committee.cli", "exec", "--config", str(launch / "wmi-1.json"),
           "--run-id", "wmi-1", "--runs-dir", str(tmp_path)]
    pid = _launch_via_wmi(cmd, launch, "wmi-1", launch / "wmi-1.log")
    assert wait_for_run_dir(tmp_path, "wmi-1", pid, timeout_s=120), launch_log(tmp_path, "wmi-1")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and reconcile_interrupted(tmp_path / "wmi-1").status == "running":
        time.sleep(0.5)
    manifest = reconcile_interrupted(tmp_path / "wmi-1")
    assert manifest.status == "failed" and "not available" in manifest.error
    assert not (launch / "wmi-1.env.json").exists()  # secrets copy removed by the child


def test_interrupted_run_ends_at_last_activity_not_discovery(tmp_path: Path):
    store = _running_store(tmp_path, owner_pid=_dead_pid(), owner_started=1.0)
    store.log_event(Event(ts="2026-01-01T00:05:00+00:00", type="member.finished", member="m1"))
    assert reconcile_interrupted(store.dir).finished_at == "2026-01-01T00:05:00+00:00"
