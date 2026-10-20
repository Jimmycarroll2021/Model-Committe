"""Run a committee in its own detached process, so it outlives whatever started it (web UI, MCP server).

The child is `python -m model_committee.cli exec --config <file> --run-id <id> --runs-dir <dir>`. It writes the
run directory exactly like `committee run`, so the web UI and MCP server only need to read files to follow it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from model_committee.artifacts import process_started
from model_committee.schemas import RunConfig

LAUNCH_DIR = ".launch"  # under runs_root: per-run config + stdout/stderr log of the detached process


def launch_detached(config: RunConfig, *, runs_root: Path, run_id: str) -> int:
    """Start the run and return the child's PID. The run directory appears once the child has started."""
    launch = Path(runs_root) / LAUNCH_DIR
    launch.mkdir(parents=True, exist_ok=True)
    config_path = launch / f"{run_id}.json"
    config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    log_path = launch / f"{run_id}.log"
    cmd = [sys.executable, "-m", "model_committee.cli", "exec", "--config", str(config_path),
           "--run-id", run_id, "--runs-dir", str(Path(runs_root).resolve())]
    if sys.platform != "win32":
        with open(log_path, "ab") as log:
            return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    close_fds=True, start_new_session=True).pid
    # Try to leave the parent's job object, so closing the server's job/console does not kill the run. A job
    # that forbids breakaway (the MCP Python SDK client and Claude Code's shell use KILL_ON_JOB_CLOSE jobs) does
    # not always make CreateProcess fail: the child can silently stay inside. So start it suspended, check, and
    # only let it run if it really is outside every job.
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
             | subprocess.CREATE_BREAKAWAY_FROM_JOB | _CREATE_SUSPENDED)
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, close_fds=True)
    except OSError:
        proc = None
    if proc is not None:
        if not _in_any_job(proc._handle) and _resume(proc._handle):
            return proc.pid
        proc.kill()  # still suspended, so nothing has run yet
        proc.wait()
    # Have WMI create the process instead: its parent is the WMI service, so it is outside our jobs. WMI can't
    # pass our environment or redirect output, so the child does both itself.
    return _launch_via_wmi(cmd, launch, run_id, log_path)


_CREATE_SUSPENDED = 0x00000004


def _in_any_job(process_handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    result = wintypes.BOOL()
    ok = ctypes.WinDLL("kernel32").IsProcessInJob(wintypes.HANDLE(process_handle), None, ctypes.byref(result))
    return bool(result.value) or not ok  # if we can't tell, assume the worst


def _resume(process_handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    return ntdll.NtResumeProcess(wintypes.HANDLE(process_handle)) == 0


def _launch_via_wmi(cmd: list[str], launch: Path, run_id: str, log_path: Path) -> int:
    env_path = launch / f"{run_id}.env.json"
    env_path.write_text(json.dumps(dict(os.environ)), encoding="utf-8")  # child reads then deletes it at once
    full = [*cmd, "--log", str(log_path), "--env-file", str(env_path)]
    script = (
        "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0};"
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
        "@{CommandLine=$env:MC_CMDLINE; CurrentDirectory=$env:MC_CWD; ProcessStartupInformation=$si};"
        "Write-Output \"$($r.ReturnValue) $($r.ProcessId)\""
    )
    env = {**os.environ, "MC_CMDLINE": subprocess.list2cmdline(full), "MC_CWD": str(Path.cwd())}
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                         capture_output=True, text=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
    parts = out.stdout.split()
    if out.returncode != 0 or len(parts) != 2 or parts[0] != "0":
        env_path.unlink(missing_ok=True)
        raise OSError(f"WMI process creation failed: {out.stdout.strip()} {out.stderr.strip()[-500:]}")
    return int(parts[1])


def wait_for_run_dir(runs_root: Path, run_id: str, pid: int, timeout_s: float = 30.0) -> bool:
    """True once the child has created its run directory; False if it exited or timed out first."""
    deadline = time.monotonic() + timeout_s
    manifest = Path(runs_root) / run_id / "run-manifest.json"
    while time.monotonic() < deadline:
        if manifest.exists():
            return True
        if process_started(pid) is None:
            return manifest.exists()
        time.sleep(0.2)
    return False


def launch_log(runs_root: Path, run_id: str) -> str:
    path = Path(runs_root) / LAUNCH_DIR / f"{run_id}.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


CANCEL_MARKER = "CANCELLED"


def cancel_detached(pid: int, run_dir: Path | None = None) -> bool:
    """Kill a detached run and its member CLIs. The killed orchestrator can't record this itself, so a marker
    file tells artifacts.reconcile_interrupted to report "cancelled" rather than "interrupted"."""
    if process_started(pid) is None:
        return False
    if run_dir is not None and Path(run_dir).is_dir():
        (Path(run_dir) / CANCEL_MARKER).write_text("cancelled by user\n", encoding="utf-8")
    if sys.platform == "win32":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0
    os.killpg(os.getpgid(pid), signal.SIGTERM)
    return True
