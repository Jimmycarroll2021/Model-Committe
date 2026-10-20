"""Run directory: immutable member reports, derived artifacts, manifest with sha256 for every file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from model_committee.schemas import ArtifactRecord, Event, MemberStatus, RunManifest

RUNS_DIR_ENV = "MODEL_COMMITTEE_RUNS_DIR"


def default_runs_dir() -> Path:
    """One stable place for runs no matter where `committee` is launched from, so the CLI, web UI and MCP server
    all see the same history: $MODEL_COMMITTEE_RUNS_DIR, else <checkout>/runs for a source/editable install,
    else ~/.model-committee/runs."""
    env = os.environ.get(RUNS_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").is_file():
        return repo / "runs"
    return Path.home() / ".model-committee" / "runs"


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "run"


def new_run_id(objective: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(objective)}"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RunStore:
    """Owns one run directory. All writes go through here so the manifest stays complete."""

    def __init__(self, root: Path, manifest: RunManifest):
        self.dir = root / manifest.run_id
        self.dir.mkdir(parents=True, exist_ok=False)
        (self.dir / "sandboxes").mkdir()
        self.manifest = manifest
        self._lock = threading.Lock()
        self.save_manifest()

    # -- writes

    def write_text(self, rel: str, content: str, *, kind: str, immutable: bool = False) -> Path:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not os.access(path, os.W_OK):
            raise PermissionError(f"refusing to overwrite immutable artifact {rel}")
        path.write_text(content, encoding="utf-8")
        return self._record(path, rel, kind=kind, immutable=immutable)

    def write_json(self, rel: str, data: BaseModel | dict | list, *, kind: str, immutable: bool = False) -> Path:
        if isinstance(data, BaseModel):
            text = data.model_dump_json(indent=2)
        else:
            text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        return self.write_text(rel, text, kind=kind, immutable=immutable)

    def register(self, rel: str, *, kind: str) -> Path:
        """Record a file some other component wrote (e.g. an xlsx/pptx renderer)."""
        return self._record(self.dir / rel, rel, kind=kind, immutable=False)

    def _record(self, path: Path, rel: str, *, kind: str, immutable: bool) -> Path:
        if immutable:
            os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        rec = ArtifactRecord(path=rel.replace("\\", "/"), sha256=sha256_file(path), immutable=immutable, kind=kind)
        with self._lock:
            self.manifest.artifacts = [a for a in self.manifest.artifacts if a.path != rec.path] + [rec]
        self.save_manifest()
        return path

    def sandbox(self, member_key: str) -> Path:
        path = self.dir / "sandboxes" / member_key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_event(self, event: Event) -> None:
        with self._lock, open(self.dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")

    def save_manifest(self) -> None:
        """Atomic replace, so concurrent readers (web UI, MCP status) never see a half-written manifest."""
        with self._lock:
            text = self.manifest.model_dump_json(indent=2)
            target = self.dir / "run-manifest.json"
            tmp = self.dir / ".run-manifest.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            for attempt in range(5):  # Windows: a reader may hold the file open; retry briefly (<0.5s total)
                try:
                    os.replace(tmp, target)
                    return
                except PermissionError:
                    time.sleep(0.03 * (attempt + 1))
            # Still locked: keep the previous manifest; the next save carries this state too.


def process_started(pid: int) -> float | None:
    """Creation time (epoch seconds) of a live process, or None if it is not running. Spawns nothing."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime  # 100 ns since 1601-01-01
            return ticks / 1e7 - 11_644_473_600
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        return Path(f"/proc/{pid}").stat().st_ctime
    except OSError:
        return 0.0  # alive, creation time unknown


LEGACY_STALE_S = 2 * 3600  # longer than the slowest depth budget (30 min members + synthesis)


def owner_alive(manifest: RunManifest, run_dir: Path | None = None) -> bool:
    if manifest.owner_pid is None:
        # Manifest from before owner tracking: judge by activity instead.
        events = run_dir / "events.jsonl" if run_dir else None
        return not (events and events.exists() and time.time() - events.stat().st_mtime > LEGACY_STALE_S)
    started = process_started(manifest.owner_pid)
    if started is None:
        return False
    # A different process that reused the PID started after (or well before) the recorded owner.
    return manifest.owner_started is None or not started or abs(started - manifest.owner_started) < 2.0


def reconcile_interrupted(run_dir: Path) -> RunManifest:
    """Load a manifest; if it claims "running" but its owner process is gone, record it as interrupted."""
    path = run_dir / "run-manifest.json"
    manifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if manifest.status == "running" and not owner_alive(manifest, run_dir):
        manifest.status = "failed"
        manifest.error = ("cancelled by user" if (run_dir / "CANCELLED").exists()
                          else "interrupted: the process running this committee exited before it finished")
        manifest.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        for member in manifest.members:
            if member.status.value in ("pending", "running"):
                member.status = MemberStatus.failed
                member.error = manifest.error.split(":")[0]
        tmp = run_dir / ".run-manifest.json.reconcile"
        tmp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.replace(tmp, path)
            with open(run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(Event(type="run.failed", status="failed", detail=manifest.error).model_dump_json() + "\n")
        except OSError:
            tmp.unlink(missing_ok=True)  # another reader got there first; the in-memory answer is still right
    return manifest


def verify_run(run_dir: Path) -> list[str]:
    """Re-hash every artifact in the manifest. Returns a list of problems (empty == intact)."""
    manifest = RunManifest.model_validate_json((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    for art in manifest.artifacts:
        path = run_dir / art.path
        if not path.exists():
            problems.append(f"missing: {art.path}")
        elif sha256_file(path) != art.sha256:
            problems.append(f"modified: {art.path}")
        elif art.immutable and os.access(path, os.W_OK):
            problems.append(f"immutable artifact is writable: {art.path}")
    return problems
