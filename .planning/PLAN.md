# Model Committee — build plan

Source spec: an architecture write-up of a commercial "model council" feature, de-branded and rebuilt here
as an open implementation (docs/ARCHITECTURE.md).

## Goal
One objective in, 2-8 independent model-research agents fan out in parallel, each returns a full cited
report; a synthesizer fans in and produces agreement / disagreement (+ cause) / unique findings /
uncertainty, then output skills package the result (HTML report, xlsx, pptx deck).

## Riskiest assumptions (checked 2026-10-20)
| Assumption | Evidence |
|---|---|
| We can reach 3 frontier vendors without API keys | `claude -p` OK, `codex exec` OK, `agy -p` OK (Gemini 3.8 Flash + GPT-OSS-120B). `gemini` CLI is blocked (IneligibleTierError) so Google goes via `agy`. |
| Members can research the web headlessly | `agy --mode plan` web-searched and cited python.org; claude `WebSearch`/`WebFetch`; codex web search to verify in adapter. |
| No env API keys | Confirmed. HTTP adapters (OpenAI-compatible, Anthropic) are built but only key-gated. |

## Architecture (maps to the spec)
| Spec stage | Module |
|---|---|
| Intent + skill router | `router.py` (deliverable/depth inference, skill registry) |
| Run configuration | `schemas.RunConfig`, `depth.py` |
| Context preparation, injection scanning | `context.py` |
| Parallel dispatcher, join policy, retries, timeouts | `orchestrator.py` |
| Council member agent, isolated sandbox dir, immutable report | `member.py`, `artifacts.py` |
| Provider data plane | `providers/*` |
| Claim alignment (LLM) + deterministic classification | `synthesis.py` |
| Citation traceability / optional source fetch | `validation.py` |
| Output skills | `outputs/*` |
| Interfaces | `cli.py` (`committee`), `server.py` + `static/` (local web UI) |

Key design decisions:
- **Classification is deterministic.** The synthesizer model only aligns member claims into canonical
  claims with per-member stances. Agreement / disagreement / unique / contested are computed in code, so
  a polished narrative cannot silently drop a minority finding (spec: "synthesizer bottleneck").
- **Model consensus is reported separately from source independence** (distinct cited domains).
- **Member reports are immutable** (read-only + sha256 in manifest; `committee verify <run>`).
- **Members are isolated**: own sandbox cwd, read-only tool permissions, no write/exec tools; untrusted
  context is fenced and pre-scanned for prompt-injection patterns.
- **Context asymmetry is recorded**: each member report lists the context it actually received.

## Delegation
- Me: schemas, depth, artifacts, prompts, parsing, member, orchestrator, synthesis, validation, router, context, cli.
- Agent A: providers (claude-cli, codex-cli, agy, ollama, openai-compatible, anthropic) + tests.
- Agent B: output skills (HTML report, xlsx, pptx) + tests.
- Agent C: web UI (FastAPI + SSE + static page) + tests.
- Agent D: de-branded docs/ARCHITECTURE.md + README.
- QA: independent code review agent, then live end-to-end runs (3 vendors).

## Definition of done
- `uv run pytest` green, `uv run ruff check` clean.
- Live run with claude-cli + codex-cli + agy:gemini produces member reports, claim matrix, synthesis,
  report.html, xlsx, pptx; `committee verify` passes.
- Web UI starts a run and streams progress (browser-checked).
- No occurrence of the source vendor's name in the repo (enforced by a test).
- Pushed to github.com/Jimmycarroll2021/Model-Committe.
