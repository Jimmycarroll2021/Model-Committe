"""OpenAI Codex CLI (`codex exec`) as a committee member or synthesizer.

Isolation: --ignore-user-config skips ~/.codex/config.toml (its MCP servers, notify hook and sandbox
override) while auth still comes from CODEX_HOME. The user's configured default model is read from that
file and passed explicitly with -m, so "default" still means "whatever codex is configured to use".
Web search is the built-in `web_search` tool (live for research, disabled for synthesis).
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import ClassVar

from model_committee.providers._subprocess import CliProvider, run_cli, tail
from model_committee.providers.base import ProviderError, ProviderResult
from model_committee.schemas import ProviderTelemetry

NON_TOOL_ITEMS = {"agent_message", "reasoning", "error", "todo_list"}
# code_mode_host must stay on: this codex routes tools (web search included) through code-mode `exec`, and
# with the host disabled code mode fails closed and the model silently answers without searching.
DISABLED_FEATURES = (
    "hooks", "shell_tool", "unified_exec", "apps", "plugins", "multi_agent",
    "browser_use", "browser_use_external", "computer_use", "image_generation", "view_image", "memories",
)


def user_default_model() -> str | None:
    """The `model` key from the user's codex config, if any."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        with open(home / "config.toml", "rb") as fh:
            model = tomllib.load(fh).get("model")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return model if isinstance(model, str) and model else None


class CodexCliProvider(CliProvider):
    name: ClassVar[str] = "codex-cli"
    vendor: ClassVar[str] = "openai"
    binary_name: ClassVar[str] = "codex"

    def resolved_model(self) -> str | None:
        return self.model or self.extra.get("default_model") or user_default_model()

    def build_args(self, research: bool, last_message_file: Path) -> list[str]:
        # Values are bare words on purpose: codex is an npm .cmd shim, and an unparseable TOML value is
        # taken as a literal string, so `web_search=live` needs no quotes for cmd.exe to mangle.
        args = [
            "exec", "--skip-git-repo-check", "--ignore-user-config",
            # Web search only: no shell / code execution (read-only mode still permits reads outside cwd,
            # e.g. other members' outputs), no account connectors, plugins, subagents, browser or memories.
            *(arg for feature in DISABLED_FEATURES for arg in ("--disable", feature)),
            "--sandbox", "read-only", "--ephemeral", "--json", "--color", "never",
            "-c", f"web_search={'live' if research else 'disabled'}",
            "-o", str(last_message_file),
        ]
        model = self.resolved_model()
        if model:
            args += ["-m", model]
        return [*args, "-"]

    async def generate(self, prompt: str, *, workdir: Path, timeout_s: float, research: bool) -> ProviderResult:
        with tempfile.TemporaryDirectory(prefix="committee-codex-") as tmp:
            last = Path(tmp) / "last-message.txt"
            run = await run_cli(
                [*self.command(), *self.build_args(research, last)],
                stdin_text=prompt, cwd=workdir, timeout_s=timeout_s,
            )
            final = last.read_text(encoding="utf-8", errors="replace") if last.exists() else ""
        return parse_events(run.stdout, run.stderr, run.returncode, run.duration_s, final, self.resolved_model())


def parse_events(
    stdout: str, stderr: str, returncode: int, duration_s: float, final_message: str, model: str | None
) -> ProviderResult:
    """Parse `codex exec --json` JSONL events; prefer the -o last-message file for the answer text."""
    input_tokens = output_tokens = searches = tool_calls = 0
    saw_usage = False
    messages: list[str] = []
    failures: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "turn.completed":
            usage = event.get("usage") or {}
            saw_usage = True
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0) + int(usage.get("reasoning_output_tokens") or 0)
        elif kind == "turn.failed":
            failures.append(str((event.get("error") or {}).get("message") or event))
        elif kind == "error":
            failures.append(str(event.get("message") or event))
        elif kind == "item.completed":
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "agent_message" and item.get("text"):
                messages.append(item["text"])
            elif item_type == "web_search":
                searches += 1
                tool_calls += 1
            elif item_type not in NON_TOOL_ITEMS:
                tool_calls += 1

    text = (final_message or (messages[-1] if messages else "")).strip()
    if returncode != 0 or failures or not text:
        why = "; ".join(failures) or tail(stderr) or "no output"
        raise ProviderError(f"codex exec failed (exit {returncode}): {tail(why, 800)}")
    telemetry = ProviderTelemetry(
        provider="codex-cli",
        model=model,
        duration_s=duration_s,
        input_tokens=input_tokens if saw_usage else None,
        output_tokens=output_tokens if saw_usage else None,
        web_searches=searches,
        tool_calls=tool_calls,
        exit_code=returncode,
    )
    return ProviderResult(text=text, telemetry=telemetry, raw=stdout)
