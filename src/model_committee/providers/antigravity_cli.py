"""Antigravity CLI (`agy`) as a committee member or synthesizer: Gemini, plus the Claude and GPT-OSS
models agy also serves.

The prompt goes over stdin as one stream-json NDJSON message (`{"event":"user","message":{...}}`), so
prompt length is not bound by the Windows command line. `--mode plan --sandbox` keeps the agent read-only.

agy has no flag to remove tools, and the user's settings may allow file reads anywhere
(`allowNonWorkspaceAccess`, `read_file(*)`). So every run writes a workspace PreToolUse hook into
`<workdir>/.agents/hooks.json` that hard-denies every non-web tool (file, shell, browser, MCP, subagent)
for members and every tool for the synthesizer. A hook that errors also blocks the tool, so it fails closed.
Gap: a tool added by a future agy release that matches none of the deny patterns would be allowed.
(--disable-slash-commands is not used: agy warns it makes --mode plan a no-op.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

from model_committee.providers._subprocess import CliProvider, run_cli, tail
from model_committee.providers.base import ProviderError, ProviderResult
from model_committee.schemas import ProviderTelemetry

KNOWN_MODELS = [
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
]
WEB_SEARCH_TOOLS = {"search_web", "read_url_content"}  # counted together as web operations
NO_TOOLS_NOTE = (
    "Do not call any tools (no web search, no file or browser access). Answer only from the text "
    "supplied below.\n\n"
)
# Tool-name regex for the deny hook. Allowed for members: search_web, read_url_content, finish, wait*,
# ask_question. Everything agy exposed on 2026-10-20 that touches files, shell, browser, MCP or
# subagents matches (see tests).
DENY_FOR_RESEARCH = (
    ".*(file|command|run_|list_|find_|grep_|sed_|call_|send_|execute_|open_|click_|capture_|define_|invoke_|"
    "notebook|manage_|delete_|schedule|generate_|browser|subagent|knowledge|mcp|marketplace|resource|view_|"
    "write|replace|dir|workflow|task|inbox|plugin|permission).*"
)
DENY_ALL = ".*"
DENY_DECISION = {"decision": "deny", "reason": "Committee members may only use web search and URL reading."}
# Seconds handed back to us before our own hard kill, so agy can emit its result event on --print-timeout.
PRINT_TIMEOUT_MARGIN_S = 5


def vendor_for_model(model: str | None) -> str:
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt-oss", "llama", "qwen", "mistral", "deepseek")):
        return "open-source"
    if m.startswith("gpt"):
        return "openai"
    return "google"


class AntigravityCliProvider(CliProvider):
    name: ClassVar[str] = "agy"
    binary_name: ClassVar[str] = "agy"
    known_models: ClassVar[list[str]] = KNOWN_MODELS

    @property
    def vendor(self) -> str:  # type: ignore[override]  # depends on the model agy is routing to
        return vendor_for_model(self.model)

    def build_args(self, timeout_s: float) -> list[str]:
        print_timeout = max(int(timeout_s) - PRINT_TIMEOUT_MARGIN_S, 1)
        args = [
            "-p=", "--input-format", "stream-json", "--output-format", "stream-json",
            "--mode", "plan", "--sandbox",
            "--print-timeout", f"{print_timeout}s",
        ]
        if self.model:
            args += ["--model", self.model]
        return args

    async def generate(self, prompt: str, *, workdir: Path, timeout_s: float, research: bool) -> ProviderResult:
        write_tool_guard(workdir, research)
        text = prompt if research else NO_TOOLS_NOTE + prompt
        if text.lstrip().startswith("/"):  # never let a prompt be expanded as a slash command
            text = "Request:\n" + text
        message = json.dumps({"event": "user", "message": {"role": "user", "content": text}}) + "\n"
        run = await run_cli(
            [*self.command(), *self.build_args(timeout_s)], stdin_text=message, cwd=workdir, timeout_s=timeout_s
        )
        return parse_stream(run.stdout, run.stderr, run.returncode, run.duration_s, self.model)


def write_tool_guard(workdir: Path, research: bool) -> None:
    """Write the deny hook agy loads from the workspace's .agents/ directory."""
    agents = workdir / ".agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "deny.json").write_text(json.dumps(DENY_DECISION), encoding="utf-8")
    show = "type deny.json" if sys.platform == "win32" else "cat deny.json"  # runs with cwd = .agents
    hooks = {
        "committee-tool-guard": {
            "PreToolUse": [
                {
                    "matcher": DENY_FOR_RESEARCH if research else DENY_ALL,
                    "hooks": [{"type": "command", "command": show, "timeout": 10}],
                }
            ]
        }
    }
    (agents / "hooks.json").write_text(json.dumps(hooks, indent=2), encoding="utf-8")


def parse_stream(stdout: str, stderr: str, returncode: int, duration_s: float, model: str | None) -> ProviderResult:
    """Parse agy stream-json: `init`, `step_update`* and a final `result` event.

    agy reports status ERROR when transient API errors (e.g. 503 capacity) were retried even if the final
    agent_response step then completed, so success is judged on the last step, not only the status.
    """
    result: dict | None = None
    init_model: str | None = None
    last_step: dict | None = None
    searches = tool_calls = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("event")
        if kind == "init":
            init_model = (event.get("init") or {}).get("model")
        elif kind == "step_update":
            step = event.get("step_update") or {}
            if step.get("state") != "DONE":
                continue
            last_step = step
            if step.get("step_type") == "tool":
                tool_calls += 1
                if step.get("tool_name") in WEB_SEARCH_TOOLS:
                    searches += 1
        elif kind == "result":
            result = event.get("result") or {}

    if result is None:
        raise ProviderError(f"agy exited {returncode} without a result event; stderr: {tail(stderr)}")
    text = (result.get("response") or "").strip()
    finished = bool(last_step) and last_step.get("step_type") == "agent_response"
    ok = result.get("status") == "SUCCESS" or (finished and returncode == 0)
    if not ok or not text:
        why = result.get("error") or tail(stderr) or f"status {result.get('status')}"
        raise ProviderError(f"agy failed (exit {returncode}, status {result.get('status')}): {tail(str(why), 800)}")

    usage = result.get("usage") or {}
    output = usage.get("output_tokens")
    if output is not None:
        output = int(output) + int(usage.get("thinking_tokens") or 0)
    telemetry = ProviderTelemetry(
        provider="agy",
        model=model or init_model,
        duration_s=duration_s,
        input_tokens=usage.get("input_tokens"),
        output_tokens=output,
        web_searches=searches,
        tool_calls=tool_calls,
        exit_code=returncode,
    )
    return ProviderResult(text=text, telemetry=telemetry, raw=stdout)
