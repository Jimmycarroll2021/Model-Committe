"""Run a model CLI as a child process: prompt on stdin, hard timeout that kills the whole process tree."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from model_committee.providers.base import Provider, ProviderError, ProviderUnavailable

IS_WINDOWS = sys.platform == "win32"
CREATE_SUSPENDED = 0x00000004
MEMBER_ENV = {"MODEL_COMMITTEE_MEMBER": "1"}
# After the main process exits, how long to wait for pipe EOF. Grandchildren (MCP servers, daemons) can
# inherit stdout and hold it open forever; we keep whatever was read and move on.
PIPE_GRACE_S = 5.0
# cmd.exe re-parses the command line of .cmd/.bat shims (npm installs), so these characters in an argument
# could be mangled or interpreted. Prompt text always goes via stdin; flags must stay clean.
_CMD_UNSAFE = set('%^&|<>!"\r\n')


@dataclass
class CliRun:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float


def resolve_command(binary: str | list[str]) -> list[str] | None:
    """Turn a binary name (or an explicit argv prefix, used by test stubs) into an argv prefix."""
    if isinstance(binary, list):
        return list(binary) if binary else None
    path = shutil.which(binary)
    return [path] if path else None


class CliProvider(Provider):
    """A provider backed by a local CLI. `extra["binary"]` overrides the executable (str or argv prefix)."""

    binary_name: ClassVar[str] = ""

    def command(self) -> list[str]:
        cmd = resolve_command(self.extra.get("binary") or self.binary_name)
        if cmd is None:
            raise ProviderUnavailable(f"`{self.binary_name}` is not on PATH")
        return cmd

    async def available(self) -> tuple[bool, str]:
        try:
            cmd = self.command()
        except ProviderUnavailable as exc:
            return False, str(exc)
        return True, f"found {cmd[-1] if len(cmd) > 1 else cmd[0]}"


def tail(text: str, limit: int = 1500) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _check_shim_args(argv: list[str]) -> None:
    if not argv[0].lower().endswith((".cmd", ".bat")):
        return
    for arg in argv[1:]:
        if _CMD_UNSAFE & set(arg):
            raise ProviderError(f"refusing to pass {arg!r} through a cmd.exe shim ({argv[0]})")


class _WinJob:
    """A Windows job object holding the child, so the whole tree dies in one call without spawning taskkill.

    The child is created suspended, assigned, then resumed, so nothing it spawns can escape the job.
    No KILL_ON_JOB_CLOSE: on a normal exit we leave detached helpers (e.g. a shared daemon the CLI started)
    alone; we only kill on timeout.
    """

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SUSPEND_RESUME = 0x0800

    def __init__(self, pid: int):
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)
        self.assigned = self.resumed = False
        access = self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE | self._PROCESS_SUSPEND_RESUME
        proc = k32.OpenProcess(access, False, pid)
        if not proc:
            return
        try:
            if self.handle:
                self.assigned = bool(k32.AssignProcessToJobObject(self.handle, proc))
        finally:  # always resume, job or not; a child left suspended would hang until our timeout
            self.resumed = ntdll.NtResumeProcess(proc) == 0
            k32.CloseHandle(proc)

    def terminate(self) -> bool:
        return bool(self.assigned and self._k32.TerminateJobObject(self.handle, 1))

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


async def kill_tree(pid: int, job: _WinJob | None = None) -> None:
    """Kill a process and all its descendants (npm shims -> node -> CLI -> MCP servers)."""
    if IS_WINDOWS:
        if job is not None and job.terminate():
            return
        proc = await asyncio.create_subprocess_exec(
            "taskkill", "/T", "/F", "/PID", str(pid),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await proc.wait()
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)


async def _drain(stream: asyncio.StreamReader, sink: bytearray) -> None:
    while chunk := await stream.read(65536):
        sink.extend(chunk)


async def settle_readers(readers: list[asyncio.Task], grace_s: float) -> None:
    """Give pipe readers grace_s to hit EOF after the process exited. A detached grandchild (MCP server,
    daemon) can inherit stdout and hold it open forever; then we keep what was read and move on."""
    _, pending = await asyncio.wait(readers, timeout=grace_s)
    for r in pending:
        r.cancel()


async def run_cli(
    argv: list[str],
    *,
    stdin_text: str | None,
    cwd: Path,
    timeout_s: float,
    env_extra: dict[str, str] | None = None,
) -> CliRun:
    """Run argv to completion. Raises TimeoutError (after killing the tree) if it outlives timeout_s."""
    _check_shim_args(argv)
    env = {**os.environ, **MEMBER_ENV, **(env_extra or {})}
    kwargs: dict = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED} if IS_WINDOWS else {"start_new_session": True}
    )
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            **kwargs,
        )
    except FileNotFoundError as exc:
        raise ProviderUnavailable(f"cannot launch {argv[0]}: {exc}") from exc

    job = _WinJob(proc.pid) if IS_WINDOWS else None
    if job is not None and not job.resumed:
        proc.kill()
        job.close()
        raise ProviderError(f"could not start {argv[0]} (failed to resume the suspended process)")
    out, err = bytearray(), bytearray()
    readers = [asyncio.create_task(_drain(proc.stdout, out)), asyncio.create_task(_drain(proc.stderr, err))]

    async def _feed_and_wait() -> int:
        if stdin_text is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                proc.stdin.close()
        return await proc.wait()

    try:
        try:
            returncode = await asyncio.wait_for(_feed_and_wait(), timeout=timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            await kill_tree(proc.pid, job)
            with contextlib.suppress(Exception):  # reap briefly; the tree is already dead or dying
                await asyncio.wait_for(proc.wait(), timeout=3)
            for r in readers:
                r.cancel()
            raise
    finally:
        if job is not None:
            job.close()
    await settle_readers(readers, PIPE_GRACE_S)
    return CliRun(
        returncode=returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        duration_s=round(time.monotonic() - started, 2),
    )
