# Runbook

How to run the school intelligence tool. Written for whoever operates it after
handover, not for whoever wrote it.

---

## What this tool is

A searchable directory of CBSE schools in India for the Tensor School BD team.
For each school it holds what a salesperson needs to make contact ,  name,
principal, phone, email, website, address ,  plus context where it is published:
fee, student count, teacher count, board, trust.

**It is a directory, not a CRM and not a student database.** It never stores
anything about an individual student.

---

## One-time setup

```bash
# 1. Postgres (Docker). Port 5433, so it will not clash with another project.
docker compose up -d

# 2. Python 3.13 dependencies
uv sync --all-groups

# 3. Configuration
cp .env.example .env          # then edit DATABASE_URL if you changed the port

# 4. Create the tables
uv run alembic upgrade head

# 5. Seed the source registry (idempotent, safe to re-run)
uv run python -m school_intel.cli seed
```

Check it worked:

```bash
uv run python -m school_intel.cli --help
```

---

## Running the tool

Two processes. Both should be running during normal use.

```bash
make serve     # the web UI, http://127.0.0.1:8000
make worker    # drains the job queue - REQUIRED for "Get more details"
```

**If the worker is not running, the "Get more details" button appears to do
nothing.** It queues the work correctly, but nothing picks it up. This is the
single most likely support question.

---

## The data pipeline

Four stages. Each is safe to re-run and none of them destroys anything.

```
import  ->  fetch/enrich  ->  extract  ->  resolve  ->  score
```

### 1. Import the directory (fast, do this first)

```bash
uv run python -m school_intel.cli import cbse_saras
```

All 38 states, about 38 requests, a few minutes. Gets **name, principal,
address, website, district and class-12 status for every CBSE school in India**
,  roughly 33,000 schools. This is the cheap stage and it is where most of the
value comes from.

One state only, for testing:

```bash
uv run python -m school_intel.cli import cbse_saras --state KARNATAKA
```

### 2. Enrich (slow, this is the expensive stage)

Visits each school's own website for phone, email, fee, teacher and student
counts.

```bash
# 250 schools per state - the recommended national pass
uv run python -m school_intel.cli enrich --per-state 250

# one state, deeper
uv run python -m school_intel.cli enrich --state KARNATAKA
```

**Use `--per-state`, not `--limit`, for a national run.** `--limit` processes in
database order, which means it spends the entire budget on whichever state has
the lowest ids and the map still shows one state.

**Watch the log, not the database.** Progress is logged every 5 schools
(`45/6287 processed | fees 3 | contacts 21 | outcomes {...}`). Rows are written
in batches, so querying the database from another terminal can show no change
for a while even though the run is working ,  that is normal and not a hang.

Budget roughly **3–6 web requests per school**. 250 per state across 38 states
is about 30,000 requests and **roughly 7 hours** at ~15 schools a minute. Most
of that time is school websites being slow or dead, not our own rate limiting , 
we already fetch different schools back to back and only serialise per domain.

#### Run it on its own

**Give the enrichment the machine to itself.** It holds an OCR engine's models
in memory and keeps many sockets open, and a national pass runs for hours. Run
alongside other heavy work it can be killed by the operating system for memory
,  which is not a clean stop, though `mark-enriched` still recovers the progress.

If the machine is also being used for other things, run a smaller pass
(`--per-state 100`) more often rather than one long one.

#### Stopping part-way, and resuming later

**You can stop at any time.** Press Ctrl-C. Work is committed every 5 schools
and every page fetched is stored on disk, so nothing meaningful is lost.

```bash
# after stopping
uv run python -m school_intel.cli mark-enriched
```

That records which schools have been visited. The next run then skips them:

```bash
# tomorrow - the SAME command, it resumes rather than restarting
uv run python -m school_intel.cli enrich --per-state 250
```

A school is marked done **even if its website was dead**. That is deliberate:
retrying unreachable sites every run would spend the whole budget on them.

Schools are re-visited only after 180 days (`REFRESH_DAYS`), matching how often
fee structures actually change. To force a re-visit sooner, pass
`--redo-after-days`.

### 3, 4, 5. Extract, resolve, score

```bash
uv run python -m school_intel.cli extract --reextract
uv run python -m school_intel.cli resolve
uv run python -m school_intel.cli score
```

`extract --reextract` makes **zero web requests**. It re-reads pages already
saved on disk. If a parser is improved, this is how you apply the improvement to
data you already have ,  you do not re-crawl.

**Run `resolve` and `score` after any enrichment run, in that order.** They are
minutes on the full corpus and nothing shows in the UI until they have run.

**`extract --reextract` is a parser-change operation, not a per-run one.** The
enrich stage already extracts what it fetches, so a normal pass does not need
it. It is incremental ,  a document already read by the current version of the
extractors is skipped ,  so running it anyway is cheap, but the reason to run it
is that a parser changed.

Bumping an extractor's version string (`EXTRACTOR = "regex:mpd_patterns_v1"` ->
`_v2`) is what makes its stored documents eligible again. `PIPELINE_VERSION` is
derived from those strings, so there is no second place to update. Use
`--full` only for a parser edited without its version being bumped.

---

## Where the numbers come from

| Field | Source | Typical coverage |
|---|---|---|
| Name, principal, address, website, district | CBSE registry list | near-complete |
| Phone, email | School website + disclosure page | ~40% of enriched |
| Counsellor | Disclosure page | sparse |
| Fee | Disclosure page's linked fee document | ~10% of enriched |
| Teacher count | Disclosure page | ~25% of enriched |
| Student count | Published, else **estimated from teachers** | see below |
| Board, trust, founding year | CBSE registry | good |

### Student counts are sometimes estimated

Most schools publish a teacher count but not a student count. Where that
happens the tool estimates students as **teachers x 25** and marks the value:

* the UI shows **`~450 estimated`** in amber, with a tooltip
* the CSV has a separate **`Students estimated?`** column
* the number is never presented as something the school stated

If someone asks "is this figure real?", the answer is in that column.

---

## Exporting for the sales team

From any state or city page, click **Download CSV**, or:

```
http://127.0.0.1:8000/export.csv?state=karnataka
http://127.0.0.1:8000/export.csv?city=mysuru
```

Opens directly in Excel. **Unknown values are blank cells, never `0`** ,  a zero
gets sorted, summed and averaged by whoever opens the sheet, which silently
turns "we don't know" into "this school has no students".

---

## Things that will happen, and what they mean

**"The CBSE site stopped returning data."**
It serves a CAPTCHA after sustained traffic. The tool detects this, stops
requesting that domain, and records why ,  it does not store the CAPTCHA page as
if it were school data. Wait a few hours and re-run; the rate limit is already
set low (`rate_limit_rps: 0.1` for `cbse_saras` in
`school_intel/sources/seed.yaml`).

**"An import aborted saying the row count collapsed."**
A safety guard: if a source suddenly returns less than half its previous row
count, the tool refuses rather than concluding thousands of schools closed. It
is usually the website changing its HTML. Investigate before overriding.

**"A state shows schools but no phone numbers."**
That state has been imported but not enriched. The map's **Enriched** column
shows this. Use "Get more details", or run `enrich --state <NAME>`.

**"The enrichment run just disappeared."**
Usually the operating system killed it for memory ,  see "Run it on its own".
Nothing is lost: run `mark-enriched`, then re-run the same enrich command and it
continues from where it stopped.

**"City search found nothing and offered no outside link."**
Deliberate. The JustDial link is only offered for places we hold schools for,
because their URL silently redirects unknown cities to Mumbai ,  offering it
blindly would show the wrong city's schools.

---

## What this tool deliberately does not do

* **No student-level data, ever.** URLs that look like results, merit lists,
  toppers or admission lists are blocked before the request is made. This is a
  legal boundary (DPDP Act 2023), not a preference.
* **No Google Places / Maps.** Their terms forbid storing the data.
* **No scraping of JustDial or similar directories.** The tool links out to
  them instead. Their pages are built with class names that change on every
  deploy, so a scraper would break silently.
* **A click never starts a web request.** Buttons queue work; the worker does
  it. That is why the UI stays fast and why rate limits hold.

---

## Health check

```bash
uv run python -m school_intel.cli --help        # commands available
docker compose ps                               # database up
uv run pytest -q                                # ~400 tests
```

Quick data check:

```bash
curl -s http://127.0.0.1:8000/api/states | head
```

---

## Where to change things

| Want to change | Edit |
|---|---|
| Scoring weights, fee bands, geography | `school_intel/score/scoring.yaml` |
| Which label means which field | `school_intel/extract/mpd/labels.yaml` |
| City renames, abbreviations, titles | `school_intel/extract/normalize_data/` |
| PIN-to-metro mapping | `school_intel/sources/metro_areas.yaml` |
| Map bubble positions | `school_intel/api/state_coords.yaml` |
| Rate limits, refresh cadence | `school_intel/sources/seed.yaml` |

All of these are configuration. **None of them requires a code change**, which
is deliberate: the lists grow every time a new website layout turns up.

Deeper background is in `docs/` ,  start with `AGENTS.md`, then
`docs/DECISIONS.md` for why the architecture is the way it is.
