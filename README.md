# Model Committee

Ask one question, get it researched independently by two to eight AI models in parallel, then get a
synthesis that shows where they agree, where they disagree (and why), and what only one of them found.
Each member is a full research agent that searches the web and cites sources; the synthesis is
classified from an explicit claim matrix, not a vote. Results can be packaged as an HTML report, a
spreadsheet or a slide deck.

## How it works

```text
                        ┌── member A (claude-cli) ── research ── report A ──┐
objective + context ──> ├── member B (codex-cli)  ── research ── report B ──┼──> claim matrix ──> synthesis ──> report / xlsx / deck
                        └── member C (agy:gemini) ── research ── report C ──┘   (agreement, disagreement + cause,
                              (parallel, isolated, read-only tools)               unique findings, uncertainty)
```

1. Every member gets the same objective, criteria and context, and researches it on its own.
2. Each full report is saved read-only and hashed.
3. A synthesizer model aligns members' claims into canonical claims with per-member stances.
4. Code (not the model) classifies each claim as agreement, disagreement, unique or contested, and counts
   independent source domains next to model consensus.
5. Citations in the synthesis are checked against what members actually cited.

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- At least two usable model backends, from any of:
  - Claude Code CLI (`claude`), logged in.
  - OpenAI Codex CLI (`codex`), logged in.
  - Antigravity CLI (`agy`), logged in.
  - Ollama, running locally.
  - API keys in the environment: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
    `OPENROUTER_API_KEY`.

## Install

```bash
uv sync
```

## Quickstart

```bash
uv run committee providers                       # show which backends are usable here
uv run committee run "Which vector DB should we use for 5M docs?" \
    -m claude-cli -m codex-cli -m agy:gemini-3.8-flash-high \
    --depth brief --deliverable all --criteria cost --criteria ops-burden
```

## Usage

```bash
uv run committee providers                       # show which backends are usable here
uv run committee run "Which vector DB should we use for 5M docs?" \
    -m claude-cli -m codex-cli -m agy:gemini-3.8-flash-high \
    --depth brief --deliverable all --criteria cost --criteria ops-burden \
    [--context spec.pdf] [--synthesizer claude-cli] [--join quorum --min-members 2] [--verify-sources] [--runs-dir runs]
uv run committee run - < question.txt            # objective from stdin
uv run committee verify runs/<run_id>            # re-hash artifacts, confirm member reports untouched
uv run committee serve --port 8765               # local web UI at http://127.0.0.1:8765
uv run committee mcp                             # MCP server on stdio (see "Use as a skill / harness")
```

| Option | Meaning |
|---|---|
| `-m <spec>` | A committee member (repeat 2-8 times). See provider specs below. |
| `--depth` | `brief`, `standard` or `detailed`. Default `auto`: inferred from phrasing such as "quick take" or "thorough analysis", else `standard`. |
| `--deliverable` | `none`, `report`, `spreadsheet`, `deck` or `all`. Default `auto`: "build a deck" / "export a spreadsheet" in the objective pick those, else `report`. |
| `--criteria` | A decision criterion every member must address (repeatable). |
| `--context` | A file every member receives (repeatable). Text is extracted and snapshotted. |
| `--synthesizer` | Provider spec for the synthesizer (default `claude-cli`). |
| `--join` | `all` (wait for every member) or `quorum` (once `--min-members` succeed, stragglers get `--quorum-grace` seconds, default 120, then are cancelled). |
| `--min-members` | Minimum successful members for a valid run (default 2). |
| `--verify-sources` | Fetch cited URLs (public hosts only, redirects re-checked, 2 MB cap) and check the quoted passages are present. |
| `--timeout` | Per-member research budget in seconds (default by depth). Retries share it. |
| `--retries` | Retries after a provider error, 0-3 (default 1). Timeouts are not retried. |
| `--no-web` | Members may not browse; they work from context and their own knowledge. |
| `--runs-dir` | Where run directories are written (default `runs`). |

### Provider specs

| Spec | Backend | Notes |
|---|---|---|
| `claude-cli[:model]` | Claude Code CLI | Anthropic. Uses your logged-in session. |
| `codex-cli[:model]` | OpenAI Codex CLI | OpenAI. Uses your logged-in session. |
| `agy:<model>` | Antigravity CLI | Models: `gemini-3.8-flash-high`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-low`, `gpt-oss-120b-medium`, `claude-sonnet-4-6`. |
| `ollama:<model>` | Local Ollama | Any pulled model. |
| `openai:<model>` | OpenAI API | Requires `OPENAI_API_KEY`. |
| `anthropic:<model>` | Anthropic API | Requires `ANTHROPIC_API_KEY`. |
| `google:<model>` | Gemini API | Requires `GEMINI_API_KEY`. |
| `openrouter:<vendor/model>` | OpenRouter | Requires `OPENROUTER_API_KEY`. |

### Depth

| Depth | Report length | Sources / member | Claims / member | Member timeout | Synthesis |
|---|---|---|---|---|---|
| `brief` | ~350 words | 3 | 5 | 7 min | Main findings |
| `standard` | ~900 words | 6 | 8 | 15 min | Claim- and assumption-level |
| `detailed` | ~1800 words | 12 | 12 | 30 min | Claim-, assumption- and evidence-level, with a search for disconfirming evidence |

## Outputs

Each run writes `runs/<run_id>/`:

- `synthesis.md` / `synthesis.json`: the answer, recommendation, decision matrix, agreement,
  disagreement (with type and cause), unique findings, risks and next actions.
- `members/<member>.md` and `.json`: every member's full report and structured findings (read-only).
- `claim-matrix.json`: canonical claims, each member's stance, classification, model consensus and
  independent source count.
- `evidence-index.json`: every cited source by member and claim.
- `validation.json`: citation traceability and optional source-fetch results.
- `run-manifest.json`, `events.jsonl`: config, member status and telemetry, sha256 of every artifact.
- `report.html`, `committee.xlsx`, `committee-deck.pptx`: depending on `--deliverable`.

## Example (real run, 2026-10-20)

`committee run "Should a 5-person startup storing about 5 million 1536-dimension embeddings use PostgreSQL
with pgvector or a dedicated vector database such as Qdrant?" -m claude-cli -m codex-cli -m
agy:gemini-3.8-flash-high --depth brief --deliverable all --verify-sources`

- 3/3 members succeeded in parallel (86 s, 70 s, 354 s; 5, 4 and 19 web operations); whole run 514 s.
- 12 canonical claims: 5 agreements, 3 assumption-driven disagreements, 4 unique findings.
- The split was explained, not voted away: Claude and Codex assumed Postgres is already in the stack and
  latency needs are moderate; Gemini assumed a strict sub-50 ms p95 and an unquantized pgvector index.
  The synthesis gave a conditional recommendation and kept Gemini's unique finding that an unquantized
  HNSW index needs 64 GB+ RAM.
- Validation flagged three unanimous claims that rested on a single source domain (correlated consensus),
  fetched all 9 cited sources and found 6 of the quoted passages verbatim.

## Reading the synthesis

- **Model consensus is not truth.** Three members agreeing may mean three models read the same article.
  Check the independent source count next to each claim.
- **Unique findings matter.** A claim only one member raised is shown, not voted away. It may be the most
  important line in the run.
- **Read the disagreement type.** Factual, assumption, forecast, value, scope, evidence and calculation
  disagreements each need a different next step; the synthesis suggests one.
- **Confidence numbers are self-reported**, not calibrated. The member's assumptions and "what would change
  my mind" are more informative.
- For material decisions, open the cited sources and the member reports.

## Cost

Each member is a full agent run with its own web research, and the synthesizer is another model call.
Cost scales with members x depth: a `brief` run with three members is modest, a `detailed` run with eight
members is expensive and slow. CLI backends bill against your logged-in plan; API backends bill your key.

## Use as a skill / harness

The repo is also a Claude Code plugin (a `model-committee` skill, a `/committee` command and an MCP server)
and a stdio MCP server any MCP-capable agent can call. All of them need `uv` on `PATH`.

### Claude Code plugin

```text
/plugin marketplace add Jimmycarroll2021/Model-Committe
/plugin install model-committee@model-committee
```

On Claude Code 2.1.275 or later, one step does both:
`/plugin install model-committee --marketplace Jimmycarroll2021/Model-Committe`. To try a local checkout
without installing: `claude --plugin-dir /path/to/model-committee`.

Then ask for it in plain words ("run a model committee on ...", "get a second opinion from multiple
models on ...") or use `/committee <decision question>`. The skill frames the objective as a decision question
with criteria, checks `committee providers`, launches the run in the background and reports the answer,
agreement, disagreements with cause, unique findings, warnings and artifact paths. The plugin also starts
the MCP server below, so its tools are available too.

Runs go to `$MODEL_COMMITTEE_RUNS_DIR`, else `runs/` inside the checkout the plugin runs from (else
`~/.model-committee/runs`). An installed plugin lives in Claude Code's plugin cache, which plugin updates
replace, so set `MODEL_COMMITTEE_RUNS_DIR` to keep runs.

### MCP server

`committee mcp` starts an MCP server on stdio with six tools:

| Tool | Does |
|---|---|
| `committee_providers` | Lists backends and whether each is usable here, plus the default lineup. |
| `committee_run` | Checks the configuration and that every chosen backend is usable (errors come back at once, nothing is launched), then starts the run as a separate process (objective, models, depth, deliverable, criteria, synthesizer, join, min_members, context_paths, verify_sources) and returns `run_id` and the run directory within a few seconds. |
| `committee_status` | Run status, member statuses and durations, last progress events. |
| `committee_result` | When finished: synthesis markdown, validation warnings, untraceable citations, artifact paths. |
| `committee_cancel` | Stops a running run and the member CLIs it started. |
| `committee_list_runs` | Recent runs, newest first. |

Each run executes in its own detached process, so it keeps going when the MCP server or the client session
exits; any later server (or the CLI, or the web UI) can follow it from the run directory. A run whose
process dies before finishing (killed, machine restarted) is reported as `failed` with an `interrupted`
error instead of staying `running`. On Windows this also holds for clients that put their MCP servers in a
kill-on-close Job Object that forbids breakaway (the MCP Python SDK's stdio client, Claude Code's shell): the
launcher detects that the child is still inside a job and starts it through WMI instead, outside the job.

Register it with each client (replace `/path/to/model-committee` with your checkout):

```bash
# Claude Code (add -s user to make it available in every project)
claude mcp add model-committee -- uv run --quiet --project /path/to/model-committee committee mcp

# Codex CLI
codex mcp add model-committee -- uv run --quiet --project /path/to/model-committee committee mcp

# Antigravity CLI (flags go before the name; -- lets the args start with '-')
agy mcp add model-committee -- uv run --quiet --project /path/to/model-committee committee mcp
```

Or in `~/.codex/config.toml`:

```toml
[mcp_servers.model-committee]
command = "uv"
args = ["run", "--quiet", "--project", "/path/to/model-committee", "committee", "mcp"]
startup_timeout_sec = 30.0

[mcp_servers.model-committee.env]
MODEL_COMMITTEE_RUNS_DIR = "/path/to/committee-runs"
```

### Recursion guard

Every member and synthesizer process runs with `MODEL_COMMITTEE_MEMBER=1`. When that variable is set, the
orchestrator itself refuses to start (so `committee run`, the web UI, the MCP `committee_run` tool and the
skill all refuse), so a member
that is itself an agent (Claude Code, Codex, agy) with this plugin or server installed cannot fan out
recursively.

## Development

```bash
uv run pytest             # unit tests (no network, no model calls)
uv run pytest -m live     # end-to-end tests against real CLIs/APIs (slow, costs credits)
uv run ruff check
```

## Security notes

- Members get web search and web fetch only: no file reads, writes or shell. Each runs in its own working
  directory, so a member cannot read local files or other members' reports (verified with a canary file
  for claude-cli, codex-cli and agy). The synthesizer gets no tools. claude-cli enforces this with
  `--tools`/`--disallowedTools`, codex-cli by disabling its shell/connector features in a read-only sandbox,
  and agy through a per-run workspace PreToolUse hook that denies every non-web tool. This is tool-level
  isolation, not a VM.
- Context files are scanned for prompt-injection patterns and fenced as data between per-run random
  markers, so file text cannot forge the end of its fence.
- The web UI binds to loopback. A non-loopback `--host` is refused unless you pass `--allow-remote` and set
  `MODEL_COMMITTEE_TOKEN`; every API call then needs `Authorization: Bearer <token>` (open the UI as
  `http://host:port/#token=<token>`). Requests with a foreign Host or Origin are rejected.
- Web UI runs only accept context files from the inbox (`$MODEL_COMMITTEE_CONTEXT_DIR`, default
  `context-inbox/` next to the runs dir); the UI uploads attachments there.
- `--verify-sources` only fetches public addresses (no localhost, private ranges or cloud metadata), re-checks
  every redirect hop and caps bodies at 2 MB. Known gap: DNS rebinding between the check and the fetch.
- Treat member output as untrusted: it is model-generated text built from arbitrary web pages.
- API keys are read from the environment and never written into prompts or run artifacts.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full architecture, design decisions and known
gaps.
