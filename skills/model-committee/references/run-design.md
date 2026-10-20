# Committee run design

Source: `docs/ARCHITECTURE.md` ("Recommended run design", "Disagreement taxonomy", "What agreement means").

## The analytical contract

For a consequential supplier, architecture, product or investment decision, give the committee an explicit
contract:

```text
Run a model committee with one leading model from Anthropic, OpenAI and Google.
Use detailed depth.

Decision: [state the exact decision]
Context: [attach files and list constraints]
Criteria: [cost, capability, implementation time, security, lock-in]
Time horizon: [period]

Require each member to:
1. Research independently.
2. State assumptions and missing information.
3. Cite primary sources where possible.
4. Give a recommendation, confidence level and strongest counterargument.
5. Identify what evidence would change its recommendation.

In the synthesis:
- Separate factual, assumption-based and values-based disagreements.
- Show unique findings even if only one member found them.
- Distinguish model consensus from independent source support.
- Recalculate quantitative claims deterministically.
- End with a decision matrix, unresolved risks and next verification actions.

Return the synthesis, every member report, and a board-ready deck.
```

With the CLI, items 1-5 are built into every member prompt, so the objective only needs the decision,
constraints and time horizon. Map the rest to flags:

| Contract element | Flag |
|---|---|
| Criteria | `--criteria <c>` (repeat per criterion) |
| Attachments | `--context <file>` (repeat; pdf, xlsx, text) |
| Depth | `--depth brief\|standard\|detailed` |
| Deck / report / spreadsheet | `--deliverable deck\|report\|spreadsheet\|all\|none` |
| One model per vendor | `-m claude-cli -m codex-cli -m agy:gemini-3.8-flash-high` |

## Writing the objective

- Ask a decision, not a topic: "Should we X or Y by <date> given <constraint>?" beats "Tell me about X".
- Include numbers (volumes, budgets, team size), the time horizon and the options being compared.
- Put constraints in the objective or a context file; members only know what they are given.
- One decision per run. Split compound questions into separate runs.

## Disagreement taxonomy and next steps

| Type | Next step |
|---|---|
| factual | Return to primary sources and verify dates, definitions and versions. |
| assumption | Expose the assumptions and rerun the scenarios. |
| forecast | Give ranges, drivers and leading indicators. |
| value | Ask the decision-maker to set weights or decision criteria. |
| scope | Normalize the objective and rerun the affected branches. |
| evidence | Compare source quality and independence. |
| calculation | Recompute deterministically in code or a spreadsheet. |

## Reading agreement correctly

- Classifications come from code, not the synthesizer: agreement = 2+ support and nobody rejects;
  disagreement = at least one support and one reject; unique = raised by one member; contested = raised by
  2+ but nobody firmly supports it.
- Three members agreeing may mean three models read the same article. Compare `model_consensus` with
  `independent_sources` in `claim-matrix.json`; a validation warning flags agreement resting on one domain.
- Confidence numbers are self-reported, not calibrated. Assumptions and "what would change my mind" are more
  informative.

## CLI options (`committee run`)

| Option | Meaning |
|---|---|
| `objective` | The question; `-` reads it from stdin. |
| `-m <spec>` | Member provider spec, repeat 2-8 times. Default lineup when omitted. |
| `--depth` | `auto` (default; inferred from wording), `brief`, `standard`, `detailed`. |
| `--deliverable` | `auto` (default), `none`, `report`, `spreadsheet`, `deck`, `all`. |
| `--criteria` | Decision criterion every member must address (repeatable). |
| `--context` | File every member receives (repeatable). |
| `--synthesizer` | Provider spec for the synthesizer (default `claude-cli`). |
| `--join` | `all` (wait for every member) or `quorum`. |
| `--min-members` | Minimum successful members (default 2). |
| `--quorum-grace` | Seconds stragglers get after quorum (default 120). |
| `--timeout` | Per-member timeout in seconds (default by depth: 7/15/30 min). |
| `--retries` | Retries per member (default 1). |
| `--no-web` | Members may not browse. |
| `--verify-sources` | Fetch cited URLs and check quoted passages. |
| `--runs-dir` | Where run directories are written. |
| `--quiet` | Suppress progress events; print only the summary. |
| `--json` | Also print the final manifest as JSON. |

Provider specs: `claude-cli[:model]`, `codex-cli[:model]`, `agy:<model>` (`gemini-3.8-flash-high|medium|low`,
`gpt-oss-120b-medium`, `claude-sonnet-4-6`), `ollama:<model>`, `openai:<model>`, `anthropic:<model>`,
`google:<model>`, `openrouter:<vendor/model>`. API specs need the matching key in the environment.

Other commands: `committee providers` (availability), `committee verify <run_dir>` (re-hash artifacts),
`committee serve` (local web UI), `committee mcp` (MCP server on stdio).
