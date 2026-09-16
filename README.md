# Tensor School Intelligence

Internal tool that finds and ranks the Indian institutions worth approaching to
recruit **class 11-12 PCM students** into Tensor School of CS & AI's B.Tech
program, and tells the BD team who to contact at each one.

The output that matters: *"here are the 40 institutions to approach next, ranked,
with a named contact and the reason for each"* ,  exported to a spreadsheet in
under a minute, instead of hours of manual Googling.

## What it does

1. **Imports** authoritative registries ,  CBSE SARAS, CISCE, IB, Cambridge,
   UDISE+, and coaching-group campus directories. No general crawling.
2. **Enriches** each institution from its own statutory CBSE Mandatory Public
   Disclosure page, which is where fee structure, class-wise student strength, and
   contact details legally have to be published.
3. **Resolves** everything deterministically into one canonical record per
   institution, keeping every source claim and its evidence.
4. **Scores** each one on **Fit** (affordability, PCM-12 volume, curriculum,
   geography, reachability, group leverage) and, separately, **Confidence** (how
   much of that is actually known).
5. **Serves** a filterable ranked list, a per-institution profile that can explain
   every value it shows, and a CSV/XLSX export.

## What it deliberately does not do

- **No student data.** Ever. Institution-level only ,  see `docs/COMPLIANCE.md`.
  This is a legal boundary under India's DPDP Act 2023, enforced as a fetch-time
  denylist rather than a downstream filter.
- **No general web crawling.** Registry imports plus one known page per
  institution, capped at six fetches each.
- **No news monitoring or reputation scoring** in v1. Signals come from diffing
  authoritative registry snapshots month over month, which is deterministic and
  covers every school rather than just the famous ones.
- **No Google Places.** Its terms forbid storing the data.

## Stack

Python 3.13 · FastAPI · PostgreSQL 16 · `httpx` · Claude Haiku 4.5 (Batch API) ·
Jinja + HTMX. One database, one worker process, cron. Runs on a single small VM.

## Quick start

```bash
uv sync
cp .env.example .env        # set DATABASE_URL and ANTHROPIC_API_KEY
alembic upgrade head

python -m school_intel.cli import cbse_saras --state KA
python -m school_intel.cli worker            # processes fetch + extract jobs
python -m school_intel.cli resolve
python -m school_intel.cli score
python -m school_intel.cli serve
```

## Documentation

Read in this order:

| File | Why |
|---|---|
| [`docs/School-Intelligence-PRD.md`](docs/School-Intelligence-PRD.md) | **Non-technical start here.** Features, what users can do, expectations, rollout, tech stack |
| [`AGENTS.md`](AGENTS.md) | Hard rules and common wrong assumptions. Read before your first edit |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why the architecture is this shape, with every claim labelled verified / inferred / stakeholder / open |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Pipeline, stage contracts, repo layout |
| [`docs/DATA-MODEL.md`](docs/DATA-MODEL.md) | Schema and the rebuild contract |
| [`docs/SOURCES.md`](docs/SOURCES.md) | Every source and the conflict-resolution rules |
| [`docs/SCORING.md`](docs/SCORING.md) | Fit and Confidence, in full |
| [`docs/ENTITY-RESOLUTION.md`](docs/ENTITY-RESOLUTION.md) | Matching, merging, chains |
| [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) | DPDP, robots, ToS. Normative |
| [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) | What to build, in order |

`Project-Doc.md` is retained as historical context. It describes an earlier and
materially different reading of the problem; where it conflicts with `docs/`,
`docs/` wins.

## Design principles

- **A wrong answer is worse than no answer.** Every field carries its source and
  the verbatim evidence span behind it. `/institutions/{id}/why` exists precisely
  so a BD user can check.
- **The canonical table is a cache, not the truth.** Raw bytes and source claims
  are append-only; `make rebuild` regenerates everything downstream. Improving an
  extraction prompt costs nothing and re-crawls nothing.
- **AI extracts; code decides.** Search, filtering, ranking, scoring, matching,
  and signals are all deterministic and auditable.
- **Boring infrastructure.** Every component that was considered and rejected has
  a written, measurable trigger for reconsidering it.

## Status

Greenfield. Architecture settled and documented; implementation starts at
`docs/IMPLEMENTATION-PLAN.md` task M0-1.
