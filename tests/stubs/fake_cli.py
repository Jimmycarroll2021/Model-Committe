"""Stand-in for the model CLIs in unit tests: `python fake_cli.py <scenario> [cli args...]`.

Most tests call `respond()` in-process through a fake run_cli; only the subprocess-helper tests spawn it.

Emits canned output shaped like what the real claude / codex / agy CLIs print (captured 2026-10-20).
If FAKE_CLI_CAPTURE is set, writes {"argv", "stdin", "env_member"} there as JSON before responding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def out(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def claude_ok() -> None:
    out({"type": "system", "subtype": "init", "tools": ["WebSearch", "WebFetch"], "mcp_servers": []})
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "latest python"}},
    ]}})
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://www.python.org/downloads/"}},
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "python 3.15 release"}},
    ]}})
    out({
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
        "result": "Python 3.15.0 is the latest. Source: https://www.python.org/downloads/",
        "total_cost_usd": 0.0225,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1800, "cache_read_input_tokens": 200,
                  "output_tokens": 55, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}},
        "modelUsage": {"claude-opus-5-5": {"inputTokens": 10}},
    })


def claude_error() -> None:
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
         "errors": ["API Error: 529 overloaded"]})


def codex_ok(argv: list[str]) -> None:
    out({"type": "thread.started", "thread_id": "t1"})
    out({"type": "turn.started"})
    out({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Checking python.org."}})
    out({"type": "item.completed", "item": {"id": "i1", "type": "web_search", "query": "latest python",
                                            "action": {"type": "search", "query": "latest python"}}})
    out({"type": "item.completed", "item": {"id": "i2", "type": "web_search", "query": "",
                                            "action": {"type": "open_page"}}})
    final = "Python 3.15.0 ([python.org](https://www.python.org/downloads/))."
    out({"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": final}})
    out({"type": "turn.completed", "usage": {"input_tokens": 40677, "cached_input_tokens": 23040,
                                             "output_tokens": 146, "reasoning_output_tokens": 26}})
    if "-o" in argv:
        with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as fh:
            fh.write(final)


def codex_fail() -> None:
    out({"type": "thread.started", "thread_id": "t1"})
    out({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
    print("ERROR: stream disconnected", file=sys.stderr)
    sys.exit(1)


def _agy_step(i: int, step_type: str, state: str = "DONE", **kw) -> None:
    out({"event": "step_update", "step_update": {"conversation_id": "c1", "step_index": i, "state": state,
                                                 "step_type": step_type, **kw}})


def agy_ok() -> None:
    out({"event": "init", "conversation_id": "c1", "init": {"model": "gemini-3.8-flash-low", "tools": []}})
    _agy_step(0, "user_input")
    _agy_step(1, "tool", "ACTIVE", tool_name="search_web")
    _agy_step(1, "tool", tool_name="search_web")
    _agy_step(2, "tool", tool_name="read_url_content")
    _agy_step(3, "agent_response", "ACTIVE", text_delta="Python 3.15.0")
    _agy_step(3, "agent_response", text_delta="\n")
    out({"event": "result", "result": {
        "conversation_id": "c1", "status": "SUCCESS", "response": "Python 3.15.0\n", "duration_seconds": 50.2,
        "num_turns": 1, "usage": {"input_tokens": 23672, "output_tokens": 92, "thinking_tokens": 8,
                                  "cache_read_tokens": 34288, "total_tokens": 23764}}})


def agy_recovered() -> None:
    """Transient 503s retried, then the answer completed: agy still reports status ERROR."""
    out({"event": "init", "conversation_id": "c1", "init": {"model": "gemini-3.8-flash-low"}})
    _agy_step(0, "user_input")
    _agy_step(1, "error_message")
    _agy_step(2, "agent_response", text_delta="ALPHA OMEGA\n")
    out({"event": "result", "result": {
        "status": "ERROR", "response": "ALPHA OMEGA\n",
        "error": "API error (attempt 4): UNAVAILABLE (code 503): No capacity available",
        "usage": {"input_tokens": 17434, "output_tokens": 13, "thinking_tokens": 0}}})


def agy_fail() -> None:
    out({"event": "init", "conversation_id": "c1", "init": {"model": "gemini-3.8-flash-low"}})
    _agy_step(0, "user_input")
    _agy_step(1, "error_message")
    out({"event": "result", "result": {"status": "ERROR", "response": "",
                                       "error": "API error (attempt 4): UNAVAILABLE (code 503)"}})
    sys.exit(1)


def sleep_with_child(capture: str | None) -> None:
    """Spawn a grandchild, record its pid, then hang: the timeout must kill both."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    if capture:
        with open(capture + ".pid", "w") as fh:
            fh.write(str(child.pid))
    time.sleep(120)


def garbage() -> None:
    print("not json at all")
    print("boom: something broke", file=sys.stderr)
    sys.exit(3)


CANNED = {
    "claude_ok": claude_ok,
    "claude_error": claude_error,
    "garbage": garbage,
    "codex_fail": codex_fail,
    "agy_ok": agy_ok,
    "agy_recovered": agy_recovered,
    "agy_fail": agy_fail,
}


def respond(scenario: str, argv: list[str]) -> None:
    """Print a canned scenario. Also used in-process by the tests (no child process needed)."""
    if scenario == "codex_ok":
        codex_ok(argv)
    elif scenario in CANNED:
        CANNED[scenario]()
    else:
        sys.exit(f"unknown scenario {scenario}")


def main() -> None:
    scenario, argv = sys.argv[1], sys.argv[2:]
    stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
    capture = os.environ.get("FAKE_CLI_CAPTURE")
    if capture:
        with open(capture, "w", encoding="utf-8") as fh:
            json.dump({"argv": argv, "stdin": stdin, "env_member": os.environ.get("MODEL_COMMITTEE_MEMBER")}, fh)
    if scenario == "sleep":
        sleep_with_child(capture)
    else:
        respond(scenario, argv)


if __name__ == "__main__":
    main()
