"""Claude Code CLI (`claude -p`) as a committee member or synthesizer.

Isolation: --safe-mode skips the user's CLAUDE.md, hooks, plugins, skills and MCP servers while keeping
the normal (OAuth) login; --strict-mcp-config with no --mcp-config loads zero MCP servers. (--bare also
skips hooks but only accepts ANTHROPIC_API_KEY auth, so it is not used.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from model_committee.providers._subprocess import CliProvider, run_cli, tail
from model_committee.providers.base import ProviderError, ProviderResult
from model_committee.schemas import ProviderTelemetry

# Web only: context is inlined in the prompt, so members never need the filesystem. File tools would let a
# prompt-injected page exfiltrate local files via WebFetch, or let a member read other members' outputs.
RESEARCH_TOOLS = ["WebSearch", "WebFetch"]
DENIED_TOOLS = [
    "Read", "Glob", "Grep", "LS", "NotebookRead",  # filesystem reads
    "Bash", "PowerShell", "Edit", "MultiEdit", "Write", "NotebookEdit",  # exec / writes
    "Task", "Agent",  # subagents
]
WEB_TOOLS = {"WebSearch", "WebFetch"}  # counted together as web operations


class ClaudeCliProvider(CliProvider):
    name: ClassVar[str] = "claude-cli"
    vendor: ClassVar[str] = "anthropic"
    binary_name: ClassVar[str] = "claude"
    known_models: ClassVar[list[str]] = ["opus", "sonnet", "haiku", "fable"]

    def build_args(self, research: bool) -> list[str]:
        args = [
            "-p", "--output-format", "stream-json", "--verbose",
            "--safe-mode", "--strict-mcp-config", "--no-session-persistence",
            "--permission-mode", "dontAsk",
            "--disallowedTools", ",".join(DENIED_TOOLS),
        ]
        if research:
            args += ["--tools", ",".join(RESEARCH_TOOLS), "--allowedTools", ",".join(RESEARCH_TOOLS)]
        else:
            args += ["--tools", ""]
        if self.model:
            args += ["--model", self.model]
        return args

    async def generate(self, prompt: str, *, workdir: Path, timeout_s: float, research: bool) -> ProviderResult:
        run = await run_cli(
            [*self.command(), *self.build_args(research)], stdin_text=prompt, cwd=workdir, timeout_s=timeout_s
        )
        return parse_stream(run.stdout, run.stderr, run.returncode, run.duration_s, self.model)


def parse_stream(stdout: str, stderr: str, returncode: int, duration_s: float, model: str | None) -> ProviderResult:
    """Parse `--output-format stream-json --verbose` JSONL: tool_use blocks + the final `result` event."""
    result: dict | None = None
    tool_names: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "server_tool_use"):
                    tool_names.append(str(block.get("name")))
        elif event.get("type") == "result":
            result = event

    if result is None:
        raise ProviderError(f"claude exited {returncode} without a result event; stderr: {tail(stderr)}")
    text = (result.get("result") or "").strip()
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        detail = text or result.get("errors") or result.get("subtype")
        raise ProviderError(f"claude reported an error ({result.get('subtype')}): {tail(str(detail), 800)}")
    if not text:
        raise ProviderError(f"claude returned an empty result (exit {returncode}); stderr: {tail(stderr)}")

    usage = result.get("usage") or {}
    server = usage.get("server_tool_use") or {}
    input_tokens = sum(
        int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    searches = sum(1 for n in tool_names if n in WEB_TOOLS)
    server_web = int(server.get("web_search_requests") or 0) + int(server.get("web_fetch_requests") or 0)
    telemetry = ProviderTelemetry(
        provider="claude-cli",
        model=model or _model_used(result),
        duration_s=duration_s,
        input_tokens=input_tokens or None,
        output_tokens=usage.get("output_tokens"),
        cost_usd=result.get("total_cost_usd"),
        web_searches=max(searches, server_web),
        # num_turns - 1 is a floor in case a tool call ever goes missing from the stream.
        tool_calls=max(len(tool_names), int(result.get("num_turns") or 1) - 1),
        exit_code=returncode,
    )
    return ProviderResult(text=text, telemetry=telemetry, raw=stdout)


def _model_used(result: dict) -> str | None:
    models = list((result.get("modelUsage") or {}).keys())
    return models[0] if models else None
