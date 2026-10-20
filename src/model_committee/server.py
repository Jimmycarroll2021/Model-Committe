"""Local web UI: start committee runs, stream their progress over SSE, browse past runs and artifacts.

Binds to loopback by default. Binding anywhere else needs allow_remote=True AND an access token
(MODEL_COMMITTEE_TOKEN); with a token set, every /api route requires `Authorization: Bearer <token>` (or the
HttpOnly cookie the server sets after a successful bearer request, so EventSource and artifact links work).
Context files may only come from one allow-listed inbox directory. Everything a run produces is
model-generated, so artifacts are only served when the manifest lists them, with a restrictive CSP.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from model_committee.artifacts import new_run_id
from model_committee.schemas import MAX_MEMBERS, MIN_MEMBERS, Event, RunConfig

STATIC_DIR = Path(__file__).parent / "static"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
TERMINAL_EVENTS = {"run.finished", "run.failed"}
POLL_INTERVAL_S = 0.2
KEEPALIVE_S = 15.0

ARTIFACT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'"
)
UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".csv": "text/csv; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}
TOKEN_ENV = "MODEL_COMMITTEE_TOKEN"
CONTEXT_DIR_ENV = "MODEL_COMMITTEE_CONTEXT_DIR"
AUTH_COOKIE = "mc_token"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_SUFFIXES = {".pdf", ".xlsx", ".xlsm", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
                   ".xml", ".html", ".htm", ".py", ".js", ".ts", ".sql", ".log", ".rst", ".ini", ".toml"}

# Resolved lazily so this module imports while the orchestrator/registry are still being built.
# Tests monkeypatch these module attributes.
run_committee: Callable[..., Any] | None = None
detect_available: Callable[..., Any] | None = None
DEFAULT_LINEUP: list[str] | None = None


def _run_committee() -> Callable[..., Any]:
    if run_committee is not None:
        return run_committee
    from model_committee.orchestrator import run_committee as rc

    return rc


def _detect_available() -> Callable[..., Any]:
    if detect_available is not None:
        return detect_available
    from model_committee.providers.registry import detect_available as da

    return da


def _default_lineup() -> list[str]:
    if DEFAULT_LINEUP is not None:
        return list(DEFAULT_LINEUP)
    from model_committee.providers.registry import DEFAULT_LINEUP as lineup

    return list(lineup)


class _RunHandle:
    """A run started by this server process."""

    def __init__(self, run_id: str, task: asyncio.Task):
        self.run_id = run_id
        self.task = task
        self.error: str | None = None
        self.cancelled = False


def _sse(data: dict, event_id: int | None = None) -> bytes:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _hostname(netloc: str) -> str:
    netloc = netloc.split("/", 1)[0].lower()
    if netloc.startswith("["):
        return netloc.split("]", 1)[0] + "]"
    return netloc.rsplit(":", 1)[0]


def is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_context_dir(runs_root: Path) -> Path:
    env = os.environ.get(CONTEXT_DIR_ENV, "").strip()
    return Path(env) if env else Path(runs_root).resolve().parent / "context-inbox"


def create_app(
    runs_root: Path,
    *,
    provider_factory: Callable[[str], Any] | None = None,
    extra_hosts: set[str] | None = None,
    token: str | None = None,
    context_dir: Path | None = None,
) -> FastAPI:
    """extra_hosts: hostnames besides localhost the UI may be reached by (remote binds only; needs a token).
    token: when set, every /api route requires it. context_dir: the only place context files may come from.
    """
    runs_root = Path(runs_root)
    if extra_hosts and not token:
        raise ValueError(f"serving to non-loopback hosts requires an access token ({TOKEN_ENV})")
    allowed = LOCAL_HOSTS | {h.lower() for h in extra_hosts or ()}
    inbox = Path(context_dir) if context_dir is not None else default_context_dir(runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Model Committee", docs_url=None, redoc_url=None, openapi_url=None)
    handles: dict[str, _RunHandle] = {}
    app.state.runs_root = runs_root
    app.state.handles = handles

    @app.middleware("http")
    async def _local_only(request: Request, call_next):
        # DNS-rebinding / cross-site guard: this server launches model CLIs, so only same-host callers.
        # "*" (wildcard bind with a token) skips the host checks; the token is the control there.
        any_host = "*" in allowed
        if not any_host and _hostname(request.headers.get("host", "")) not in allowed:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if not any_host and request.method not in ("GET", "HEAD") and origin is not None:
            if _hostname(re.sub(r"^[a-z][a-z0-9+.-]*://", "", origin.lower())) not in allowed:
                return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        if not token or not request.url.path.startswith("/api"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        via_header = bool(bearer) and hmac.compare_digest(bearer.encode(), token.encode())
        cookie = request.cookies.get(AUTH_COOKIE, "")
        if not via_header and not (cookie and hmac.compare_digest(cookie.encode(), token.encode())):
            return JSONResponse({"detail": "missing or invalid access token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        response = await call_next(request)
        if via_header and cookie != token:
            # EventSource and plain links cannot send headers; let them authenticate with a same-site cookie.
            response.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="strict", path="/api")
        return response

    def run_dir(run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        return runs_root / run_id

    def load_manifest(run_id: str) -> dict:
        manifest = _read_json(run_dir(run_id) / "run-manifest.json")
        if manifest is None:
            handle = handles.get(run_id)
            if handle is not None:
                # Started here but no manifest yet (just launched, or failed before writing one).
                status = "failed" if handle.error or handle.cancelled else "running"
                return {"run_id": run_id, "status": status, "error": handle.error, "members": [],
                        "artifacts": [], "config": None}
            raise HTTPException(404, "run not found")
        return manifest

    def effective_status(run_id: str, manifest: dict) -> str:
        status = manifest.get("status", "running")
        handle = handles.get(run_id)
        if status == "running" and handle is not None and handle.task.done():
            return "cancelled" if handle.cancelled else "failed"
        return status

    # ------------------------------------------------------------------ UI

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Content-Security-Policy": UI_CSP})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------------------ API

    @app.get("/api/providers")
    async def providers() -> dict:
        try:
            found = await _detect_available()()
        except ImportError as exc:
            raise HTTPException(503, f"provider registry unavailable: {exc}") from exc
        return {
            "providers": found,
            "default_lineup": _default_lineup(),
            "default_synthesizer": RunConfig.model_fields["synthesizer"].default,
            "min_members": MIN_MEMBERS,
            "max_members": MAX_MEMBERS,
        }

    def resolve_context(paths: list[str]) -> list[str]:
        """Only files inside the inbox may become context (they get copied into a servable run dir)."""
        root = inbox.resolve()
        out, problems = [], []
        for raw in paths:
            candidate = Path(raw).expanduser()
            target = (candidate if candidate.is_absolute() else root / candidate).resolve()
            if not target.is_relative_to(root):
                problems.append(f"{raw}: context files must be inside {root}")
            elif not target.is_file():
                problems.append(f"{raw}: not found in {root}")
            else:
                out.append(str(target))
        if problems:
            raise HTTPException(422, [{"loc": ["body", "context_paths"], "msg": p, "type": "value_error"}
                                      for p in problems])
        return out

    @app.post("/api/context", status_code=201)
    async def upload_context(request: Request, filename: str) -> dict:
        """Raw-body upload (no multipart dependency): POST /api/context?filename=notes.pdf with the bytes."""
        original = Path(filename.replace("\\", "/")).name
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", original).strip(".-")[:80]
        suffix = Path(safe).suffix.lower()
        if not safe or suffix not in UPLOAD_SUFFIXES:
            raise HTTPException(415, f"unsupported file type; allowed: {', '.join(sorted(UPLOAD_SUFFIXES))}")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"file larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        if not data:
            raise HTTPException(422, "empty file")
        inbox.mkdir(parents=True, exist_ok=True)
        stored = f"{uuid.uuid4().hex[:8]}-{safe}"
        (inbox / stored).write_bytes(bytes(data))
        return {"path": stored, "name": original, "bytes": len(data)}

    @app.post("/api/runs", status_code=202)
    async def start_run(config: RunConfig) -> dict:
        from model_committee.guard import NestedCommitteeError, ensure_not_member

        try:
            ensure_not_member()
        except NestedCommitteeError as exc:
            raise HTTPException(409, str(exc)) from exc
        if config.context_paths:
            inbox.mkdir(parents=True, exist_ok=True)
            config = config.model_copy(update={"context_paths": resolve_context(config.context_paths)})
        try:
            runner = _run_committee()
        except ImportError as exc:
            raise HTTPException(503, f"orchestrator unavailable: {exc}") from exc
        base = new_run_id(config.objective)
        run_id, n = base, 1
        while run_id in handles or (runs_root / run_id).exists():
            n += 1
            run_id = f"{base}-{n}"

        async def _go() -> None:
            await runner(config, runs_root=runs_root, on_event=None, provider_factory=provider_factory,
                         run_id=run_id)

        task = asyncio.create_task(_go(), name=f"committee-{run_id}")
        handle = _RunHandle(run_id, task)
        handles[run_id] = handle

        def _done(t: asyncio.Task) -> None:
            if t.cancelled():
                handle.cancelled = True
            elif t.exception() is not None:
                exc = t.exception()
                handle.error = f"{type(exc).__name__}: {exc}"

        task.add_done_callback(_done)
        return {"run_id": run_id}

    @app.get("/api/runs")
    async def list_runs() -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        for child in runs_root.iterdir():
            if not child.is_dir() or not RUN_ID_RE.fullmatch(child.name):
                continue
            manifest = _read_json(child / "run-manifest.json")
            if manifest is None:
                continue
            seen.add(child.name)
            config = manifest.get("config") or {}
            rows.append({
                "run_id": child.name,
                "objective": config.get("objective", ""),
                "status": effective_status(child.name, manifest),
                "created_at": manifest.get("created_at"),
                "finished_at": manifest.get("finished_at"),
                "members": [
                    {"key": m.get("key"), "spec": m.get("spec"), "status": m.get("status")}
                    for m in manifest.get("members") or []
                ],
            })
        for run_id in handles:
            if run_id not in seen:
                rows.append({"run_id": run_id, "objective": "", "created_at": None, "finished_at": None,
                             "status": load_manifest(run_id)["status"], "members": []})
        rows.sort(key=lambda r: (r["created_at"] or "9999", r["run_id"]), reverse=True)
        return rows

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        manifest = load_manifest(run_id)
        manifest["status"] = effective_status(run_id, manifest)
        handle = handles.get(run_id)
        if handle is not None and handle.error and not manifest.get("error"):
            manifest["error"] = handle.error
        return manifest

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict:
        run_dir(run_id)
        handle = handles.get(run_id)
        if handle is None:
            raise HTTPException(404, "run not started by this server")
        if handle.task.done():
            return {"run_id": run_id, "cancelled": False, "detail": "run already finished"}
        handle.cancelled = True
        handle.task.cancel()
        return {"run_id": run_id, "cancelled": True}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> StreamingResponse:
        directory = run_dir(run_id)
        if not directory.exists() and run_id not in handles:
            raise HTTPException(404, "run not found")
        try:
            skip = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            skip = 0
        return StreamingResponse(
            _tail_events(directory, run_id, skip, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _tail_events(directory: Path, run_id: str, skip: int, request: Request) -> AsyncIterator[bytes]:
        """Replay events.jsonl, then follow it until a terminal event (or the run stops without one)."""
        path = directory / "events.jsonl"
        offset, count, buf = 0, 0, b""
        idle = 0.0
        while True:
            chunk = b""
            if path.exists():
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                offset += len(chunk)
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                if not raw.strip():
                    continue
                count += 1
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if count > skip:
                    yield _sse(event, count)
                if event.get("type") in TERMINAL_EVENTS:
                    return
            if chunk:
                idle = 0.0
                continue
            if _finished_without_terminal(directory, run_id):
                # Drain anything written between our read and the check, then close with a synthetic end.
                if path.exists() and path.stat().st_size > offset:
                    continue
                yield _sse(_synthetic_end(run_id), count + 1)
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(POLL_INTERVAL_S)
            idle += POLL_INTERVAL_S
            if idle >= KEEPALIVE_S:
                idle = 0.0
                yield b": keepalive\n\n"

    def _finished_without_terminal(directory: Path, run_id: str) -> bool:
        handle = handles.get(run_id)
        if handle is not None:
            return handle.task.done()
        manifest = _read_json(directory / "run-manifest.json")
        return manifest is not None and manifest.get("status", "running") != "running"

    def _synthetic_end(run_id: str) -> dict:
        handle = handles.get(run_id)
        manifest = _read_json(run_dir(run_id) / "run-manifest.json") or {}
        if handle is not None and handle.cancelled:
            return Event(type="run.failed", status="cancelled", detail="cancelled by user").model_dump()
        if handle is not None and handle.error:
            return Event(type="run.failed", status="failed", detail=handle.error).model_dump()
        status = manifest.get("status", "failed")
        kind = "run.finished" if status == "succeeded" else "run.failed"
        return Event(type=kind, status=status, detail=manifest.get("error")).model_dump()

    @app.get("/api/runs/{run_id}/files/{path:path}")
    async def run_file(run_id: str, path: str, download: bool = False) -> FileResponse:
        directory = run_dir(run_id)
        manifest = load_manifest(run_id)
        listed = {a.get("path") for a in manifest.get("artifacts") or []}
        if path not in listed or "\\" in path or path.startswith("/"):
            raise HTTPException(404, "file not found")
        root = directory.resolve()
        target = (directory / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "file not found")
        media_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        headers = {"Content-Security-Policy": ARTIFACT_CSP, "X-Content-Type-Options": "nosniff",
                   "Cache-Control": "no-store"}
        return FileResponse(
            target,
            media_type=media_type,
            headers=headers,
            filename=target.name if download else None,
            content_disposition_type="attachment" if download else "inline",
        )

    return app


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    runs_root: Path | str = "runs",
    *,
    allow_remote: bool = False,
) -> None:
    import uvicorn

    app = build_app(host, runs_root, allow_remote=allow_remote)
    if not is_loopback(host):
        print(f"Remote access enabled. Open http://{host}:{port}/#token=<your {TOKEN_ENV}>")
    uvicorn.run(app, host=host, port=port)


def build_app(host: str, runs_root: Path | str, *, allow_remote: bool = False) -> FastAPI:
    """Apply the bind policy: loopback is open (token optional); anything else needs allow_remote + a token."""
    token = os.environ.get(TOKEN_ENV, "").strip() or None
    extra: set[str] = set()
    if not is_loopback(host):
        if not allow_remote or not token:
            raise SystemExit(
                f"refusing to bind {host}: the web UI launches model CLIs with your logins. Non-loopback binds "
                f"need --allow-remote and an access token in {TOKEN_ENV}."
            )
        bare = host.strip("[]")
        extra = {"*"} if bare in ("0.0.0.0", "::") else {bare}
    return create_app(Path(runs_root), extra_hosts=extra, token=token)
