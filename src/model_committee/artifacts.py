"""Run directory: immutable member reports, derived artifacts, manifest with sha256 for every file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from model_committee.schemas import ArtifactRecord, Event, RunManifest


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
