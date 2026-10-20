---
name: model-committee
description: This skill should be used when the user asks to "run a model committee", "ask several models", "ask multiple models", "get a second opinion from multiple models", "have Claude, Codex and Gemini research this", "multi-model research", "multi-model decision", or wants independent AI research agents to investigate one question in parallel and see where they agree, disagree and what only one of them found.
version: 0.1.0
---

# Model Committee

Run 2-8 independent model agents (Claude Code CLI, Codex CLI, Antigravity `agy`, Ollama, API-key models) on
one objective in parallel. Each member researches on its own and cites sources; a synthesizer aligns their
claims; code classifies each claim as agreement, disagreement, unique or contested and counts independent
source domains. Outputs land in `<runs-dir>/<run_id>/`: `synthesis.md`, `validation.json`,
`claim-matrix.json`, member reports, and optionally `report.html`, `committee.xlsx`, `committee-deck.pptx`.

Paths below use `REPO=${CLAUDE_PLUGIN_ROOT}` (the model-committee checkout). If that value still reads
literally as a `${...}` placeholder, the skill was not loaded as a plugin: locate the checkout (the
directory containing `pyproject.toml` with `name = "model-committee"`) or ask the user for it.

## Step 0: recursion guard (mandatory)

Run `echo "${MODEL_COMMITTEE_MEMBER:-}"` first. If it prints anything other than empty or `0`, this session
is itself a committee member. Do not start a committee: answer the question directly and say that nested
committees are disabled. The CLI and MCP server refuse in that case too; do not try to work around it.

## Step 1: frame the objective

Vague objectives produce vague committees. Restate the request as a pointed decision question with
numbers, timelines and trade-offs, plus 2-5 explicit decision criteria. Example: "Should a 4-person team
move a 5M-document search workload from Elasticsearch to Postgres pgvector within 6 months?" with criteria
`cost`, `ops burden`, `recall at 5M docs`, `migration risk`. Show the framed question and criteria to the user
in one or two lines and proceed unless it materially changes what they asked; ask only when the decision
itself is unclear. For prompt design and the analytical contract, see `references/run-design.md`.

## Step 2: choose lineup, depth and deliverable

Check which backends work here:

```bash
uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" committee providers
```

- **Lineup**: default `claude-cli codex-cli agy:gemini-3.8-flash-high` (one model per vendor maximizes
  independence). Swap in available backends when one is missing; keep at least 2 members, ideally from
  different vendors. Honour lineups the user names.
- **Depth**: `brief` unless the user asks for depth or the decision is high-stakes. `standard` and `detailed`
  cost more and take longer (per-member timeout caps: brief 7 min, standard 15 min, detailed 30 min, plus
  synthesis).
- **Deliverable**: `report` (HTML) by default; `deck` for slides/board, `spreadsheet` for xlsx, `all` when asked,
  `none` for synthesis only.
- Mention cost before `detailed` depth or more than 4 members: each member is a full agent run billed to
  the user's plans or API keys.

## Step 3: launch in the background

Runs take minutes. Start the run with the Bash tool using `run_in_background: true`, passing the objective
on stdin via a quoted heredoc so no shell characters in it are interpreted:

```bash
REPO="${CLAUDE_PLUGIN_ROOT}"
uv run --quiet --project "$REPO" committee run - \
  -m claude-cli -m codex-cli -m agy:gemini-3.8-flash-high \
  --depth brief --deliverable report \
  --criteria "cost" --criteria "ops burden" \
  --runs-dir "${MODEL_COMMITTEE_RUNS_DIR:-$REPO/runs}" --quiet <<'OBJECTIVE'
<framed decision question>
OBJECTIVE
```

Optional flags: `--context <file>` (repeatable; every member receives it), `--synthesizer <spec>`,
`--join quorum --min-members 2` (synthesize once 2 members succeed instead of waiting for stragglers),
`--verify-sources` (fetch cited URLs and check quotes). Full option list: `references/run-design.md`.

Tell the user the run has started, the lineup, and the expected duration. Then wait for the background task
to complete; do not poll in a loop. The final output line reads
`run <run_id>: <succeeded|failed> in <N>s -> <run_dir>`, followed by member statuses.

When the plugin's MCP tools are loaded (`committee_providers`, `committee_run`, `committee_status`,
`committee_result`, `committee_cancel`), they are an alternative to the CLI: `committee_run` reports
unavailable backends at once without launching, otherwise it starts the run as a separate process and
returns a run_id. The run keeps going if the session ends; poll `committee_status` every 30-60 s and call
`committee_result` once the status is not `running`. A run reported `failed` with an `interrupted` error
lost its process; rerun it. Use `committee_cancel` only when the user asks to stop a run.

## Step 4: read the results

If the run failed, report the error and member statuses from the output, then suggest the fix (swap an
unavailable provider, use `--join quorum`, raise `--timeout`). Otherwise read from `<run_dir>`:

1. `synthesis.md`: answer, recommendation, agreement, disagreement with type and cause, unique findings,
   decision matrix, risks, next actions.
2. `validation.json`: `warnings` and `untraceable_citations`.
3. `claim-matrix.json` when needed: per claim `classification`, `support`/`reject`, `model_consensus`,
   `independent_sources`, `source_domains`.

Read `members/<key>.md` only to explain a disagreement or check a unique finding.

## Step 5: report to the user

Keep it tight and in this order:

1. **Answer**: the headline and recommendation in 1-3 sentences.
2. **Agreement**: the claims most members support, each with its independent source count.
3. **Disagreements**: each with its type (factual, assumption, forecast, value, scope, evidence,
   calculation), the cause, and the suggested next step.
4. **Unique findings**: claims only one member raised; do not drop them, they may matter most.
5. **Caveat**: model consensus is not source independence. Flag any agreement resting on one source domain,
   members that made no web searches, and self-reported confidence.
6. **Warnings**: every item in `validation.json` warnings and any untraceable citations.
7. **Artifacts**: absolute paths to `synthesis.md`, `report.html` / `committee.xlsx` / `committee-deck.pptx`
   (whichever exist) and the run directory.

Do not present the committee's answer as verified fact; it is model-generated research built from web pages.

## Additional resources

- **`references/run-design.md`**: the analytical contract for consequential decisions, how criteria and
  context map to CLI flags, the disagreement taxonomy and next steps, and the full CLI option table.
