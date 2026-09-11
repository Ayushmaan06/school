<!-- dgc-policy-v11 -->
> **New here?** Read `AGENTS.md`, then `docs/DECISIONS.md`. Project context is at
> the bottom of this file. The dual-graph retrieval policy below is unchanged.

# Dual-Graph Context Policy

This project uses a local dual-graph MCP server for efficient context retrieval.

## MANDATORY: Adaptive graph_continue rule

**Call ``graph_continue`` ONLY when you do NOT already know the relevant files.**

### Call ``graph_continue`` when:
- This is the first message of a new task / conversation
- The task shifts to a completely different area of the codebase
- You need files you haven't read yet in this session

### SKIP ``graph_continue`` when:
- You already identified the relevant files earlier in this conversation
- You are doing follow-up work on files already read (verify, refactor, test, docs, cleanup, commit)
- The task is pure text (writing a commit message, summarising, explaining)

**If skipping, go directly to ``graph_read`` on the already-known ``file::symbol``.**

## When you DO call graph_continue

1. **If ``graph_continue`` returns ``needs_project=true``**: call ``graph_scan`` with ``pwd``. Do NOT ask the user.

2. **If ``graph_continue`` returns ``skip=true``**: fewer than 5 files  -  read only specifically named files.

3. **Read ``recommended_files``** using ``graph_read``.
   - Always use ``file::symbol`` notation (e.g. ``src/auth.ts::handleLogin``)  -  never read whole files.
   - ``recommended_files`` entries that already contain ``::`` must be passed verbatim.

4. **Obey confidence caps:**
   - ``confidence=high`` -> Stop. Do NOT grep or explore further.
   - ``confidence=medium`` -> ``fallback_rg`` at most ``max_supplementary_greps`` times, then ``graph_read`` at most ``max_supplementary_files`` more symbols. Stop.
   - ``confidence=low`` -> same as medium. Stop.

## Session State (compact, update after every turn)

Maintain a short JSON block in your working memory. Update it after each turn:

``````json
{
  "files_identified": ["path/to/file.py"],
  "symbols_changed": ["module::function"],
  "fix_applied": true,
  "features_added": ["description"],
  "open_issues": ["one-line note"]
}
``````

Use this state  -  not prose summaries  -  to remember what's been done across turns.

## Token Usage

A ``token-counter`` MCP is available for tracking live token usage.

- Before reading a large file: ``count_tokens({text: "<content>"})`` to check cost first.
- To show running session cost: ``get_session_stats()``
- To log completed task: ``log_usage({input_tokens: N, output_tokens: N, description: "task"})``

## Rules

- Do NOT use ``rg``, ``grep``, or bash file exploration before calling ``graph_continue`` (when required).
- Do NOT do broad/recursive exploration at any confidence level.
- ``max_supplementary_greps`` and ``max_supplementary_files`` are hard caps  -  never exceed them.
- Do NOT call ``graph_continue`` more than once per turn.
- Always use ``file::symbol`` notation with ``graph_read``  -  never bare filenames.
- After edits, call ``graph_register_edit`` with changed files using ``file::symbol`` notation.

## Context Store

Whenever you make a decision, identify a task, note a next step, fact, or blocker during a conversation, append it to ``.dual-graph/context-store.json``.

**Entry format:**
``````json
{"type": "decision|task|next|fact|blocker", "content": "one sentence max 15 words", "tags": ["topic"], "files": ["relevant/file.ts"], "date": "YYYY-MM-DD"}
``````

**To append:** Read the file -> add the new entry to the array -> Write it back -> call ``graph_register_edit`` on ``.dual-graph/context-store.json``.

**Rules:**
- Only log things worth remembering across sessions (not every minor detail)
- ``content`` must be under 15 words
- ``files`` lists the files this decision/task relates to (can be empty)
- Log immediately when the item arises  -  not at session end

## Session End

When the user signals they are done (e.g. "bye", "done", "wrap up", "end session"), proactively update ``CONTEXT.md`` in the project root with:
- **Current Task**: one sentence on what was being worked on
- **Key Decisions**: bullet list, max 3 items
- **Next Steps**: bullet list, max 3 items

Keep ``CONTEXT.md`` under 20 lines total. Do NOT summarize the full conversation  -  only what's needed to resume next session.

---

# Project: Tensor School Intelligence

## What we are building

An internal tool that ranks Indian institutions by how good a channel they are for
recruiting **class 11-12 PCM students** into **Tensor School of CS & AI**'s B.Tech
program (Bengaluru, Rs 6,00,000/year all-in = Rs 24L over four years), and tells
the BD team who to contact at each one.

**It is not a B2B school-partnership CRM. It is not a student database.** If a
change only makes sense under one of those readings, it is the wrong change.

## Documentation map

| File | Contains |
|---|---|
| `AGENTS.md` | **Start here.** Hard rules, AI boundaries, what not to add, testing expectations, common wrong assumptions |
| `docs/DECISIONS.md` | ADRs with fact/inference/stakeholder/business labels. Why the architecture is what it is |
| `docs/ARCHITECTURE.md` | Pipeline stages, stage contracts, repo layout, rejected components and their promotion triggers |
| `docs/DATA-MODEL.md` | Full DDL. Three layers. The rebuild contract |
| `docs/SOURCES.md` | Every source, its fields, cadence, legal footing, and the per-field conflict-resolution table |
| `docs/SCORING.md` | Hard gate, Fit components and bands, Confidence, flags, NL query rules |
| `docs/ENTITY-RESOLUTION.md` | Normalisation, linkage, blocking, match thresholds, group resolution |
| `docs/COMPLIANCE.md` | DPDP boundary, the PII denylist, robots and rate limits, ToS positions |
| `docs/IMPLEMENTATION-PLAN.md` | Dependency-ordered tasks with goals, edge cases, tests, and DoD |
| `Project-Doc.md` | **Historical only.** An earlier, materially different understanding. Where it conflicts with `docs/`, `docs/` wins |

## The ten hard rules

Full versions in `AGENTS.md`. Summarised so they are unmissable:

1. Never store student-level data. Denylist runs before the HTTP request. DPDP s.9.
2. Never use Google Places or Maps as a data source. ToS forbids storage.
3. A user request never triggers a fetch or an LLM call.
4. Never delete from `observations`, `fetches`, or `raw_documents`.
5. Never auto-merge an entity pair in the 0.65-0.90 ambiguity band.
6. Never write a guessed value. Missing beats wrong.
7. Never blend Confidence into Fit.
8. Never hardcode a scoring weight or band in Python - `score/scoring.yaml`.
9. Never let an LLM emit SQL, choose an ordering, or influence a score.
10. Never skip the structure-drift guard on registry parsers.

## Stack

Python 3.13 + `uv` · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 (`pg_trgm`) ·
`httpx` + `asyncio` · `selectolax` + `pdfplumber` for deterministic extraction ·
Jinja + HTMX · Postgres-as-queue · cron.

**No model inference and no API key in the default pipeline** (ADR-013). An
optional Claude Haiku 4.5 fallback tier exists behind
`EXTRACTION_LLM_ENABLED=false`.

**Deliberately absent:** Scrapy, Playwright, Celery, Redis, OpenSearch,
React/Next.js, embeddings, a news pipeline. Each was considered and rejected with a
measurable promotion trigger in `docs/ARCHITECTURE.md`. Do not add them
speculatively.

## Current status

Greenfield. Architecture settled and documented; **no implementation yet.**
Start at `docs/IMPLEMENTATION-PLAN.md` task **M0-0** — a half-day manual recon
spike that answers the two questions the architecture rests on (is SARAS
importable, and do schools actually publish parseable fees) and produces the
fixtures M1 and M2 need. Code starts at M0-1.

**Import is national; enrichment is campaign-scoped** (ADR-017). The registry
list is ~38 POSTs and nearly free, so the directory covers every metro from day
one; the expensive MPD enrichment follows `enrichment_priority` — Karnataka,
Tamil Nadu, Maharashtra, Telangana first, then tier-1 metros.

**Fee data:** ~30% of fee documents are scanned images. ADR-016 adds local OCR
(`rapidocr-onnxruntime`, no system binary, no API key) with a wrong-value guard,
because a misread digit crosses affordability bands and hard rule 6 says missing
beats wrong.

