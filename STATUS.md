# Status — 2026-09-06

Where the build is, what to do next, and what is deliberately not done.
For how to *operate* the tool, see `RUNBOOK.md`.

---

## Short answer: is the UI all that's left?

**No — the UI is already built and working.** Map, state pages, city search,
CSV export and the "Get more details" button all run today.

What actually remains is three small things and a pile of optional work:

| Remaining | Size | Why it matters |
|---|---|---|
| Finish the enrichment run | ~6 hours of fetching | Fills phone/email nationally |
| Look at the UI with national data | ~1 hour | It has only ever been seen with one state's data |
| Tests for the API layer | ~2 hours | Newest code, currently untested |
| XLSX export alongside CSV | ~1 hour | Plan asked for both; only CSV exists |

Everything else on the original plan is optional for this handover.

---

## What is built and working

| Piece | State |
|---|---|
| Postgres schema, 19 tables, migrations | done, up/down verified |
| Job queue + worker | done |
| CBSE registry importer (all 38 states) | done, live-verified |
| Fetch layer: robots, PII denylist, rate limits, CAPTCHA breaker | done |
| Extraction: registry, disclosure pages, PDFs, OCR for scans | done |
| Normalisation: names, cities, phones, money | done |
| Resolver + rebuild contract | done, 4 properties tested |
| PIN → metro geography | done |
| Scoring (Fit + Confidence) | done |
| India map, state pages, city search | done |
| CSV export | done |
| "Get more details" → job queue → worker | done |
| JustDial link-out (recognised cities only) | done |
| RUNBOOK.md | done |
| ~400 tests | passing |

---

## Data in the database right now

```
33,636 schools          all 38 states
32,369 with a website
33,132 principal names
 2,081 enriched          8 states - see below
```

Enriched means we visited the school's own website for phone, email, fee,
teachers and student numbers.

| State | Enriched |
|---|---|
| Karnataka | 585 |
| Maharashtra | 395 |
| Madhya Pradesh | 272 |
| Andhra Pradesh | 228 |
| Delhi | 66 |
| Tamil Nadu | 60 |
| Manipur | 48 |
| Gujarat | 45 |

**5,704 schools still to enrich** out of a 6,287 target (250 per state).

### Measured hit rates, on schools we actually visited

| Field | Rate |
|---|---|
| Phone | 81% |
| Email | 64% |
| Pincode | 36% |
| Teachers | 18% |
| Students (mostly estimated) | 17% |
| Fee | 16% |

Fee and student counts are low because **most schools do not publish them**,
not because extraction is failing. That was measured, not assumed.

---

## Tomorrow, in order

```bash
# 0. Database up
docker compose up -d

# 1. Turn already-fetched pages into data. NO fetching, safe to re-run.
uv run python -m school_intel.cli extract --reextract
uv run python -m school_intel.cli resolve
uv run python -m school_intel.cli score

# 2. Look at it
uv run python -m school_intel.cli serve     # http://127.0.0.1:8000

# 3. Resume enrichment - ON ITS OWN, nothing else heavy running
uv run python -m school_intel.cli enrich --per-state 250

# 4. If it stops for any reason, record progress then re-run the same command
uv run python -m school_intel.cli mark-enriched
```

Step 3 takes about 6 hours for the remaining 5,704 schools. It resumes from
where it stopped, so it can be run in whatever blocks of time are available.

After enrichment finishes, run step 1 again to pull the new data through.

---

## Known limitations, stated plainly

**Fee coverage is ~16% of visited schools.** Measured over 449 schools, then
confirmed at scale. The chain is homepage → disclosure page → linked fee
document, and it breaks at every hop. About 30% of the fee documents that *are*
found are scanned images; OCR recovers a fifth of those.

**Student counts are usually estimated.** Most schools publish a teacher count
but not a student count, so students are approximated as `teachers × 25` and
marked: amber `~450 estimated` in the UI, a separate `Students estimated?`
column in the CSV. Never presented as a published figure.

**Class-wise strength is behind another link.** 15 of 17 disclosure pages link
out to a separate "Student Strength Details" page rather than showing numbers.
Following it is a small change to the discovery step and would improve student
coverage; it was skipped deliberately to prioritise contacts.

**The CBSE registry serves a CAPTCHA under sustained load.** Detected and
handled — the tool stops rather than hammering, and never stores the CAPTCHA
page as if it were data. It caps how fast the registry can be re-crawled.

**The eval gold set has 11 rows, not the ~200 the plan wants.** It has already
caught three real bugs, but no accuracy percentage should be quoted from it.
Expanding it is human work: read a school's disclosure page and record what it
actually says.

**The API layer has no tests.** `api/queries.py` and `api/app.py` are the
newest code and the least covered. Everything behind them is well tested.

---

## Deliberately not done

Each was considered and set aside for this handover, not forgotten:

* **XLSX export** — CSV works and opens in Excel; XLSX adds number formatting
* **Other boards** (CISCE, IB, Cambridge) — more schools, none of them CBSE
* **UDISE+ import** — would give authoritative enrollment for every school in
  India, but its endpoints need a browser devtools session to capture first
* **Entity matching across registries** — only needed once a second registry exists
* **Change signals, natural-language search** — genuinely optional for v1
* **Scraping JustDial** — decided against; we link out instead. Their markup is
  built from build-hash class names that change on every deploy, so a scraper
  would break silently after handover

---

## Things that will bite whoever picks this up

1. **The worker must be running** or "Get more details" looks broken. It queues
   correctly and nothing consumes it.
2. **Use `--per-state`, not `--limit`** for a national run, or the whole budget
   goes to one state.
3. **Watch the log, not the database**, during enrichment. Rows commit in
   batches, so a quiet database is normal and not a hang.
4. **Run enrichment on its own.** It was killed by the OS for memory when run
   alongside other heavy jobs. `mark-enriched` recovers the progress.
5. **Every knob is config, not code** — see the table at the end of
   `RUNBOOK.md`. Those lists grow every time a new website layout appears.
