"""Provider adapters: spec parsing, registry, output parsing, error paths, timeouts, key gating.

Unit tests drive the CLI adapters with canned output in the real formats (tests/stubs/fake_cli.py), run
in-process through a fake run_cli so they spawn nothing; only the subprocess-helper tests and one
end-to-end test start real child processes. HTTP adapters go through httpx.MockTransport.
Live tests (`-m live`) make one tiny real call per backend.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import re
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import httpx
import pytest

from model_committee.providers import _subprocess, antigravity_cli, claude_cli, codex_cli
from model_committee.providers._subprocess import CliRun, run_cli
from model_committee.providers.anthropic_api import AnthropicApiProvider
from model_committee.providers.antigravity_cli import AntigravityCliProvider
from model_committee.providers.base import ProviderError, ProviderUnavailable
from model_committee.providers.claude_cli import ClaudeCliProvider
from model_committee.providers.codex_cli import CodexCliProvider
from model_committee.providers.ollama import OllamaProvider
from model_committee.providers.openai_compat import GoogleProvider, OpenAIProvider, OpenRouterProvider
from model_committee.providers.registry import (
    DEFAULT_LINEUP,
    PROVIDERS,
    build_provider,
    detect_available,
    parse_spec,
)

STUB = Path(__file__).parent / "stubs" / "fake_cli.py"
KEY_ENVS = ["OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"]
# The base interpreter, not the venv launcher (which would add a second process per spawn on Windows).
PYTHON = getattr(sys, "_base_executable", None) or sys.executable

_spec = importlib.util.spec_from_file_location("fake_cli", STUB)
fake_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_cli)


def stub(scenario: str) -> list[str]:
    return [PYTHON, str(STUB), scenario]


async def _inproc_run_cli(argv, *, stdin_text, cwd, timeout_s, env_extra=None) -> CliRun:
    """Stand-in for run_cli: plays the stub scenario named in argv without starting a process."""
    assert argv[:2] == [PYTHON, str(STUB)], argv
    scenario, args = argv[2], argv[3:]
    capture = os.environ.get("FAKE_CLI_CAPTURE")
    if capture:
        Path(capture).write_text(json.dumps({"argv": args, "stdin": stdin_text or "", "env_member": "1"}))
    out, err, code = io.StringIO(), io.StringIO(), 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            fake_cli.respond(scenario, args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return CliRun(returncode=code, stdout=out.getvalue(), stderr=err.getvalue(), duration_s=0.01)


@pytest.fixture(autouse=True)
def inproc_cli(request, monkeypatch):
    if "real_spawn" in request.fixturenames:
        return
    for module in (claude_cli, codex_cli, antigravity_cli):
        monkeypatch.setattr(module, "run_cli", _inproc_run_cli)


@pytest.fixture
def real_spawn():
    """Requesting this fixture opts a test out of the in-process fake: its CLI really runs."""


@pytest.fixture
def capture(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "capture.json"
    monkeypatch.setenv("FAKE_CLI_CAPTURE", str(path))
    return path


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for key in KEY_ENVS:
        monkeypatch.delenv(key, raising=False)


def pid_alive(pid: int) -> bool:
    """Without spawning anything (tasklist launches are slow here)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---------------------------------------------------------------- spec parsing + registry


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("claude-cli", ("claude-cli", None)),
        ("claude-cli:sonnet", ("claude-cli", "sonnet")),
        ("ollama:qwen2.5-coder:1.5b", ("ollama", "qwen2.5-coder:1.5b")),
        ("agy:gemini-3.8-flash-high", ("agy", "gemini-3.8-flash-high")),
        ("openrouter:anthropic/claude-x", ("openrouter", "anthropic/claude-x")),
        ("codex-cli:", ("codex-cli", None)),
    ],
)
def test_parse_spec_splits_on_first_colon(spec, expected):
    assert parse_spec(spec) == expected


def test_build_provider_round_trips_spec():
    p = build_provider("ollama:qwen2.5-coder:1.5b")
    assert isinstance(p, OllamaProvider)
    assert p.model == "qwen2.5-coder:1.5b"
    assert p.spec == "ollama:qwen2.5-coder:1.5b"
    assert build_provider("claude-cli").spec == "claude-cli"


def test_build_provider_unknown_name():
    with pytest.raises(ValueError, match="unknown provider"):
        build_provider("nope:model")


def test_registry_contents_and_default_lineup():
    assert set(PROVIDERS) == {
        "claude-cli", "codex-cli", "agy", "ollama", "openai", "openrouter", "google", "anthropic"
    }
    assert DEFAULT_LINEUP == ["claude-cli", "codex-cli", "agy:gemini-3.8-flash-high"]
    for spec in DEFAULT_LINEUP:
        build_provider(spec)


async def test_detect_available_shape(monkeypatch):
    monkeypatch.setattr(_subprocess.shutil, "which", lambda _name: None)

    async def unreachable(_self):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(OllamaProvider, "installed_models", unreachable)
    rows = await detect_available()
    assert [r["name"] for r in rows] == list(PROVIDERS)
    for r in rows:
        assert set(r) == {"name", "vendor", "available", "reason", "example_spec", "models"}
        assert r["available"] is False and r["reason"]
    by_name = {r["name"]: r for r in rows}
    assert "not on PATH" in by_name["claude-cli"]["reason"]
    assert by_name["openai"]["reason"] == "OPENAI_API_KEY is not set"
    assert "gemini-3.8-flash-high" in by_name["agy"]["models"]


# ---------------------------------------------------------------- vendor labels


@pytest.mark.parametrize(
    ("model", "vendor"),
    [
        ("gemini-3.8-flash-high", "google"),
        (None, "google"),
        ("claude-sonnet-4-6", "anthropic"),
        ("claude-opus-4-6-thinking", "anthropic"),
        ("gpt-oss-120b-medium", "open-source"),
    ],
)
def test_agy_vendor_follows_model(model, vendor):
    p = AntigravityCliProvider(model=model)
    assert p.vendor == vendor
    assert p.label.startswith(f"{vendor} / ")


def test_openrouter_vendor_from_model_prefix():
    assert OpenRouterProvider(model="anthropic/claude-x").vendor == "anthropic"
    assert OpenRouterProvider(model="meta-llama/llama-4").vendor == "open-source"
    assert OpenRouterProvider(model="someorg/thing").vendor == "someorg"
    assert ClaudeCliProvider.vendor == "anthropic"  # ClassVar default untouched


# ---------------------------------------------------------------- subprocess helper


async def test_real_subprocess_end_to_end(tmp_path, capture, real_spawn):
    """Binary override -> run_cli -> child process -> parse, with a long, shell-hostile prompt on stdin."""
    prompt = "start " + "x" * 60_000 + ' "quotes" %PATH% ^ & | < > end'
    p = ClaudeCliProvider(extra={"binary": stub("claude_ok")})
    r = await p.generate(prompt, workdir=tmp_path, timeout_s=60, research=True)
    assert r.text.startswith("Python 3.15.0") and r.telemetry.exit_code == 0
    seen = json.loads(capture.read_text(encoding="utf-8"))
    assert seen["stdin"] == prompt
    assert seen["env_member"] == "1"
    assert "--safe-mode" in seen["argv"]


async def test_run_cli_timeout_raises_and_kills(tmp_path):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await run_cli([PYTHON, "-c", "import time; time.sleep(60)"], stdin_text="", cwd=tmp_path, timeout_s=1)
    assert time.monotonic() - started < 30  # would be 60 if the kill did not happen


async def test_run_cli_kill_takes_the_whole_tree(tmp_path, capture):
    """Grandchildren die too. Cancellation shares the timeout's kill path; we cancel only once the
    grandchild provably exists, so a slow process launch cannot make the test racy."""
    task = asyncio.create_task(run_cli(stub("sleep"), stdin_text="", cwd=tmp_path, timeout_s=120))
    pid_file = Path(str(capture) + ".pid")
    deadline = time.monotonic() + 60
    while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    grandchild = int(pid_file.read_text())
    assert pid_alive(grandchild)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(40):
        if not pid_alive(grandchild):
            break
        await asyncio.sleep(0.05)
    assert not pid_alive(grandchild), "grandchild survived the kill"


async def test_settle_readers_does_not_wait_on_inherited_pipes():
    """A pipe still held open by a detached grandchild must not block us past the grace period."""
    done = asyncio.create_task(asyncio.sleep(0))
    stuck = asyncio.create_task(asyncio.sleep(3600))
    started = time.monotonic()
    await _subprocess.settle_readers([done, stuck], 0.2)
    assert time.monotonic() - started < 2
    assert done.done() and not done.cancelled()
    await asyncio.sleep(0)
    assert stuck.cancelled()


async def test_run_cli_refuses_metacharacters_through_cmd_shims(tmp_path):
    with pytest.raises(ProviderError, match="cmd.exe shim"):
        await run_cli(["C:/x/codex.cmd", "-c", 'web_search="live"'], stdin_text="", cwd=tmp_path, timeout_s=5)


async def test_missing_binary_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(_subprocess.shutil, "which", lambda _name: None)
    p = ClaudeCliProvider()
    assert (await p.available())[0] is False
    with pytest.raises(ProviderUnavailable):
        await p.generate("hi", workdir=tmp_path, timeout_s=5, research=True)


# ---------------------------------------------------------------- claude-cli


async def test_claude_research_parses_result_and_counts_searches(tmp_path, capture):
    p = ClaudeCliProvider(model="sonnet", extra={"binary": stub("claude_ok")})
    r = await p.generate("What is the latest Python?", workdir=tmp_path, timeout_s=30, research=True)
    assert r.text.startswith("Python 3.15.0")
    t = r.telemetry
    assert (t.provider, t.model, t.exit_code) == ("claude-cli", "sonnet", 0)
    assert t.web_searches == 3 and t.tool_calls == 3  # WebSearch x2 + WebFetch
    assert t.input_tokens == 2010 and t.output_tokens == 55 and t.cost_usd == pytest.approx(0.0225)
    assert '"type": "result"' in r.raw

    argv = json.loads(capture.read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert {"--safe-mode", "--strict-mcp-config", "--no-session-persistence"} <= set(argv)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "What is the latest Python?" not in " ".join(argv)  # prompt goes over stdin


async def test_claude_synthesizer_mode_has_no_tools(tmp_path, capture):
    p = ClaudeCliProvider(extra={"binary": stub("claude_ok")})
    r = await p.generate("merge these", workdir=tmp_path, timeout_s=30, research=False)
    assert r.telemetry.model == "claude-opus-5-5"  # falls back to modelUsage when no model was given
    argv = json.loads(capture.read_text())["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv and "--model" not in argv


@pytest.mark.parametrize("research", [True, False])
def test_claude_never_gets_filesystem_or_exec_tools(research):
    """Members must not read outside their sandbox (other members' raw outputs, local secrets)."""
    argv = ClaudeCliProvider().build_args(research)
    enabled = argv[argv.index("--tools") + 1].split(",")
    denied = set(argv[argv.index("--disallowedTools") + 1].split(","))
    for tool in ("Read", "Glob", "Grep", "LS", "NotebookRead", "Bash", "PowerShell", "Write", "Edit", "Task"):
        assert tool not in enabled
        assert tool in denied
    if research:
        assert enabled == ["WebSearch", "WebFetch"]
        assert "Read" not in argv[argv.index("--allowedTools") + 1]


AGY_TOOLS_2026_10_20 = (
    "ask_custom_permission ask_permission ask_question browser_click_element browser_drag_pixel_to_pixel "
    "browser_get_dom browser_get_network_request browser_input browser_list_network_requests browser_mouse_down "
    "browser_mouse_up browser_move_mouse browser_press_key browser_refresh_page browser_resize_window "
    "browser_scroll browser_scroll_dom browser_select_option browser_subagent call_mcp_tool "
    "capture_browser_console_logs capture_browser_screenshot click_browser_pixel command_status define_subagent "
    "delete_knowledge execute_browser_javascript find_by_name finish generate_image grep_search invoke_subagent "
    "list_browser_pages list_dir list_permissions list_plugin_accounts list_resources manage_inbox "
    "manage_subagents manage_task multi_replace_file_content notebook_edit notebook_execution open_browser_url "
    "read_browser_page read_resource read_url_content replace_file_content run_command run_workflow schedule "
    "search_marketplace search_web sed_file send_command_input send_message view_file read_file wait "
    "wait_5_seconds write_to_file"
).split()


def test_agy_deny_hook_leaves_only_web_tools():
    allowed = [t for t in AGY_TOOLS_2026_10_20 if not re.fullmatch(antigravity_cli.DENY_FOR_RESEARCH, t)]
    assert sorted(allowed) == ["ask_question", "finish", "read_url_content", "search_web", "wait", "wait_5_seconds"]


async def test_agy_slash_prompt_is_not_expanded(tmp_path, capture):
    p = AntigravityCliProvider(extra={"binary": stub("agy_ok")})
    await p.generate("/plugin install evil", workdir=tmp_path, timeout_s=60, research=True)
    content = json.loads(json.loads(capture.read_text())["stdin"])["message"]["content"]
    assert not content.lstrip().startswith("/")


async def test_claude_error_result_raises(tmp_path):
    p = ClaudeCliProvider(extra={"binary": stub("claude_error")})
    with pytest.raises(ProviderError, match="529 overloaded"):
        await p.generate("x", workdir=tmp_path, timeout_s=30, research=True)


async def test_claude_garbage_output_raises_with_stderr(tmp_path):
    p = ClaudeCliProvider(extra={"binary": stub("garbage")})
    with pytest.raises(ProviderError, match="boom: something broke"):
        await p.generate("x", workdir=tmp_path, timeout_s=30, research=True)


# ---------------------------------------------------------------- codex-cli


async def test_codex_research_reads_last_message_and_usage(tmp_path, capture):
    p = CodexCliProvider(model="gpt-test", extra={"binary": stub("codex_ok")})
    r = await p.generate("latest python?", workdir=tmp_path, timeout_s=30, research=True)
    assert r.text == "Python 3.15.0 ([python.org](https://www.python.org/downloads/))."
    t = r.telemetry
    assert (t.provider, t.model) == ("codex-cli", "gpt-test")
    assert t.web_searches == 2 and t.tool_calls == 2
    assert t.input_tokens == 40677 and t.output_tokens == 172
    argv = json.loads(capture.read_text())["argv"]
    assert argv[0] == "exec" and argv[-1] == "-"
    assert {"--skip-git-repo-check", "--ignore-user-config", "--ephemeral", "--json"} <= set(argv)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "web_search=live" in argv
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "hooks"} <= disabled
    assert "code_mode_host" not in disabled  # web search runs through code mode; disabling it kills search
    assert argv[argv.index("-m") + 1] == "gpt-test"
    assert not list(tmp_path.glob("*last-message*"))  # -o file lives outside the member workdir


async def test_codex_synthesizer_disables_web_search_and_uses_config_model(tmp_path, capture, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-from-config"\n[mcp_servers.x]\nurl = "u"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    p = CodexCliProvider(extra={"binary": stub("codex_ok")})
    r = await p.generate("merge", workdir=tmp_path, timeout_s=30, research=False)
    argv = json.loads(capture.read_text())["argv"]
    assert "web_search=disabled" in argv
    assert argv[argv.index("-m") + 1] == "gpt-from-config"
    assert r.telemetry.model == "gpt-from-config"


async def test_codex_turn_failed_raises(tmp_path):
    p = CodexCliProvider(model="m", extra={"binary": stub("codex_fail")})
    with pytest.raises(ProviderError, match="stream disconnected"):
        await p.generate("x", workdir=tmp_path, timeout_s=30, research=True)


# ---------------------------------------------------------------- agy


async def test_agy_research_stream_json(tmp_path, capture):
    p = AntigravityCliProvider(model="gemini-3.8-flash-low", extra={"binary": stub("agy_ok")})
    r = await p.generate("latest python?", workdir=tmp_path, timeout_s=300, research=True)
    assert r.text == "Python 3.15.0"
    t = r.telemetry
    assert t.web_searches == 2 and t.tool_calls == 2  # search_web + read_url_content
    assert t.input_tokens == 23672 and t.output_tokens == 100
    seen = json.loads(capture.read_text())
    msg = json.loads(seen["stdin"])
    assert msg == {"event": "user", "message": {"role": "user", "content": "latest python?"}}
    argv = seen["argv"]
    assert argv[0] == "-p=" and argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--mode") + 1] == "plan" and "--sandbox" in argv
    assert "--disable-slash-commands" not in argv  # agy: it turns --mode plan into a no-op
    hooks = json.loads((tmp_path / ".agents" / "hooks.json").read_text())
    group = hooks["committee-tool-guard"]["PreToolUse"][0]
    assert group["matcher"] == antigravity_cli.DENY_FOR_RESEARCH
    assert json.loads((tmp_path / ".agents" / "deny.json").read_text())["decision"] == "deny"
    assert argv[argv.index("--print-timeout") + 1] == "295s"
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-low"


async def test_agy_synthesizer_prepends_no_tools_note(tmp_path, capture):
    p = AntigravityCliProvider(extra={"binary": stub("agy_ok")})
    r = await p.generate("merge", workdir=tmp_path, timeout_s=60, research=False)
    content = json.loads(json.loads(capture.read_text())["stdin"])["message"]["content"]
    assert content.startswith("Do not call any tools") and content.endswith("merge")
    hooks = json.loads((tmp_path / ".agents" / "hooks.json").read_text())
    assert hooks["committee-tool-guard"]["PreToolUse"][0]["matcher"] == ".*"  # synthesizer: no tools at all
    assert r.telemetry.model == "gemini-3.8-flash-low"  # from the init event


async def test_agy_status_error_but_completed_answer_is_accepted(tmp_path):
    p = AntigravityCliProvider(model="gemini-3.8-flash-low", extra={"binary": stub("agy_recovered")})
    r = await p.generate("x", workdir=tmp_path, timeout_s=60, research=True)
    assert r.text == "ALPHA OMEGA"


async def test_agy_real_error_raises(tmp_path):
    p = AntigravityCliProvider(model="gemini-3.8-flash-low", extra={"binary": stub("agy_fail")})
    with pytest.raises(ProviderError, match="No capacity|503"):
        await p.generate("x", workdir=tmp_path, timeout_s=60, research=True)


# ---------------------------------------------------------------- ollama


def ollama_transport(seen: list[httpx.Request], chat: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:1.5b"}]})
        return httpx.Response(status, json=chat or {})

    return httpx.MockTransport(handler)


async def test_ollama_generate_and_research_note(tmp_path):
    seen: list[httpx.Request] = []
    chat = {"message": {"role": "assistant", "content": "hello"}, "prompt_eval_count": 12, "eval_count": 3}
    p = OllamaProvider(model="qwen2.5-coder:1.5b", extra={"transport": ollama_transport(seen, chat)})
    assert (await p.available())[0] is True
    r = await p.generate("question", workdir=tmp_path, timeout_s=10, research=True)
    assert r.text == "hello"
    assert (r.telemetry.input_tokens, r.telemetry.output_tokens, r.telemetry.web_searches) == (12, 3, 0)
    body = json.loads(seen[-1].content)
    assert body["stream"] is False and body["model"] == "qwen2.5-coder:1.5b"
    assert "no web browsing" in body["messages"][0]["content"]
    assert body["options"]["num_ctx"] >= 8192


async def test_ollama_missing_model_and_http_error(tmp_path):
    seen: list[httpx.Request] = []
    p = OllamaProvider(model="llama9", extra={"transport": ollama_transport(seen)})
    ok, reason = await p.available()
    assert ok is False and "not pulled" in reason
    bad = OllamaProvider(model="qwen2.5-coder:1.5b",
                         extra={"transport": ollama_transport(seen, {"error": "model crashed"}, status=500)})
    with pytest.raises(ProviderError, match="HTTP 500"):
        await bad.generate("q", workdir=tmp_path, timeout_s=10, research=False)


async def test_ollama_unreachable(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    p = OllamaProvider(model="x", extra={"transport": httpx.MockTransport(refuse)})
    assert p.host == "http://127.0.0.1:9"
    ok, reason = await p.available()
    assert ok is False and "not reachable" in reason


async def test_http_timeout_raises_timeout_error(tmp_path):
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    p = OllamaProvider(model="m", extra={"transport": httpx.MockTransport(slow)})
    with pytest.raises(TimeoutError):
        await p.generate("q", workdir=tmp_path, timeout_s=0.3, research=False)


# ---------------------------------------------------------------- OpenAI-compatible


@pytest.mark.parametrize(
    ("cls", "env", "url"),
    [
        (OpenAIProvider, "OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions"),
        (OpenRouterProvider, "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions"),
        (GoogleProvider, "GEMINI_API_KEY",
         "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ],
)
async def test_openai_compat_key_gating_and_request(cls, env, url, tmp_path, monkeypatch):
    p = cls(model="org/some-model")
    ok, reason = await p.available()
    assert ok is False and reason == f"{env} is not set"
    with pytest.raises(ProviderUnavailable):
        await p.generate("q", workdir=tmp_path, timeout_s=5, research=False)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "model": "org/some-model",
            "choices": [{"message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 4},
        })

    monkeypatch.setenv(env, "sk-test-secret")
    p = cls(model="org/some-model", extra={"transport": httpx.MockTransport(handler)})
    assert (await p.available())[0] is True
    r = await p.generate("q", workdir=tmp_path, timeout_s=5, research=True)
    assert r.text == "answer" and r.telemetry.input_tokens == 20 and r.telemetry.output_tokens == 4
    assert str(seen[0].url) == url
    assert seen[0].headers["authorization"] == "Bearer sk-test-secret"
    body = json.loads(seen[0].content)
    assert "no web browsing" in body["messages"][0]["content"]
    assert "sk-test-secret" not in r.raw


async def test_openai_compat_http_error_does_not_leak_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    transport = httpx.MockTransport(lambda _r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    p = OpenAIProvider(model="m", extra={"transport": transport})
    with pytest.raises(ProviderError) as info:
        await p.generate("q", workdir=tmp_path, timeout_s=5, research=False)
    assert "401" in str(info.value) and "sk-test-secret" not in str(info.value)


async def test_openai_compat_requires_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    ok, reason = await OpenAIProvider().available()
    assert ok is False and "name a model" in reason


# ---------------------------------------------------------------- anthropic


def anthropic_message(content: list[dict], stop: str = "end_turn", searches: int = 0) -> dict:
    return {
        "model": "claude-opus-5-5", "stop_reason": stop, "content": content,
        "usage": {"input_tokens": 100, "output_tokens": 50, "server_tool_use": {"web_search_requests": searches}},
    }


async def test_anthropic_key_gating(tmp_path):
    p = AnthropicApiProvider()
    assert await p.available() == (False, "ANTHROPIC_API_KEY is not set")
    with pytest.raises(ProviderUnavailable):
        await p.generate("q", workdir=tmp_path, timeout_s=5, research=True)


async def test_anthropic_research_with_pause_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    tool_use = {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "python"}}
    replies = [
        anthropic_message([{"type": "text", "text": "Searching. "}, tool_use], stop="pause_turn", searches=1),
        anthropic_message([{"type": "text", "text": "Python 3.15.0 is latest."}], searches=1),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=replies[len(seen) - 1])

    p = AnthropicApiProvider(extra={"transport": httpx.MockTransport(handler)})
    r = await p.generate("latest python?", workdir=tmp_path, timeout_s=10, research=True)
    assert r.text == "Searching. Python 3.15.0 is latest."
    assert r.telemetry.web_searches == 2 and r.telemetry.tool_calls == 1
    assert r.telemetry.input_tokens == 200 and r.telemetry.output_tokens == 100

    first = json.loads(seen[0].content)
    assert seen[0].headers["x-api-key"] == "sk-ant-test"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    assert seen[0].headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert [t["type"] for t in first["tools"]] == ["web_search_20260209", "web_fetch_20260209"]
    second = json.loads(seen[1].content)
    assert [m["role"] for m in second["messages"]] == ["user", "assistant"]  # resumed, no extra user turn
    assert second["messages"][1]["content"][-1]["type"] == "server_tool_use"


async def test_anthropic_synth_mode_no_tools_and_older_model_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=anthropic_message([{"type": "text", "text": "ok"}]))

    transport = httpx.MockTransport(handler)
    await AnthropicApiProvider(extra={"transport": transport}).generate(
        "q", workdir=tmp_path, timeout_s=5, research=False)
    assert "tools" not in json.loads(seen[-1].content)
    await AnthropicApiProvider(model="claude-haiku-5-5", extra={"transport": transport}).generate(
        "q", workdir=tmp_path, timeout_s=5, research=True)
    body = json.loads(seen[-1].content)
    assert body["tools"][0]["type"] == "web_search_20250305"
    assert "fallbacks" not in body and "anthropic-beta" not in seen[-1].headers


async def test_anthropic_refusal_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    reply = anthropic_message([], stop="refusal")
    reply["stop_details"] = {"type": "refusal", "category": "cyber"}
    p = AnthropicApiProvider(extra={"transport": httpx.MockTransport(lambda _r: httpx.Response(200, json=reply))})
    with pytest.raises(ProviderError, match="refusal, category=cyber"):
        await p.generate("q", workdir=tmp_path, timeout_s=5, research=False)


# ---------------------------------------------------------------- live (opt-in: -m live)

LIVE_SPECS = ["claude-cli", "codex-cli", "agy:gemini-3.8-flash-low", "ollama:qwen2.5-coder:1.5b"]


@pytest.mark.live
@pytest.mark.parametrize("spec", LIVE_SPECS)
async def test_live_tiny_call(spec, tmp_path, real_spawn):
    p = build_provider(spec)
    ok, reason = await p.available()
    assert ok, reason
    r = await p.generate(
        "Reply with exactly the word PONG and nothing else.", workdir=tmp_path, timeout_s=240, research=False
    )
    assert "PONG" in r.text.upper()
    assert r.telemetry.duration_s > 0
    assert r.raw
