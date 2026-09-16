# Architecture Decision Record

Every decision below is tagged with its epistemic status. **Do not treat an
`[INFERENCE]` as an `[EXTERNAL-FACT]`, and never silently upgrade a
`[BUSINESS]` into a technical assumption.**

| Tag | Meaning |
|---|---|
| `[EXTERNAL-FACT]` | Verified against a live external source on 2026-08-22. |
| `[STAKEHOLDER]` | Stated directly by the project owner. Authoritative; not negotiable by a coding agent. |
| `[DOC]` | Asserted in the original `Project-Doc.md`. May be right or wrong ,  check the verdict. |
| `[INFERENCE]` | Engineering judgement drawn from the facts above. Revisable with evidence. |
| `[BUSINESS]` | Still open. Needs a human. Do not invent an answer. |

---

## The problem, restated

`Project-Doc.md` describes a B2B "school partnership intelligence platform."
That is **not** what this system is for.

`[STAKEHOLDER]` Tensor School of CS & AI is a **single-campus** B.Tech college in
Bengaluru (Sattva Tech Park; degree conferred by S-VYASA Deemed University;
YC S21). Fees are **Rs 6,00,000/year all-in** (Rs 5L tuition + Rs 1L technology
fee) = **Rs 24,00,000 over four years**, plus hostel at Rs 9-11k/month. T-SAT
merit scholarships waive up to Rs 8L.

`[STAKEHOLDER]` The purpose of this system is **student recruitment**: find the
institutions whose class 11-12 students are the best candidates for that program,
rank them, and tell BD who to contact.

The unit of value is **not** "a school we can sell to." It is **"an institution
through which we can reach qualified, affluent PCM students."**

Consequences a coding agent must internalise:

- Total enrollment is nearly irrelevant. **Class-12 Science (PCM) headcount** is
  the volume metric.
- An institution without classes 11-12 has **zero** value. Hard filter, not a
  score penalty.
- `[INFERENCE]` Affordability is a **hard qualifier and it outranks headcount.**
  Rs 6L/yr requires a family already paying roughly Rs 1.5L+/yr in school fees.
  A 2,000-student PU college charging Rs 15k/yr is worth less than an
  800-student IB school, regardless of PCM counts.
- `[INFERENCE]` Willingness-to-pay is **not monotonic** but decays slowly at the
  top and collapses at the bottom. See ADR-011.

---

## ADR-001 - Discovery is a registry-import problem, not a crawling problem

**Verdict on `[DOC]`: rejected.** The doc's Scrapy/Playwright crawler tier is
unnecessary.

`[EXTERNAL-FACT]` CBSE publishes a per-school affiliation detail page, free and
structured, e.g.
<https://saras.cbse.gov.in/SARAS/AffiliatedList/AfflicationDetails/330801>.
Verified fields: institution name, affiliation number, state, district, full
postal address, PIN, **website**, year of foundation, **principal name +
qualifications + administrative/teaching experience**, school status (Middle /
Secondary / Senior Secondary), school type, affiliation validity period, and
**Trust/Society name**. ~29,000 schools. The disaffiliated/closed list is
published separately.

`[EXTERNAL-FACT]` CBSE's **Mandatory Public Disclosure (Appendix-IX)** is a
*statutory obligation* on every affiliated school to publish a standardised page
on its own website: school email and phone, **class-wise student strength**,
**fee structure**, staff counts, and trust/society registration certificates.
CBSE re-issued the revised proforma with a compliance deadline of 2026-02-15.

`[INFERENCE]` Together these mean per-institution web extraction has a **known,
standardised target at a known URL pattern**. That is a fetch-a-list-of-URLs
problem, not a crawl-frontier problem. `httpx` + `asyncio` + a per-domain
semaphore is sufficient. **Do not add Scrapy.**

## ADR-002 - Registry snapshot diffing replaces the news pipeline

**Verdict on `[DOC]`: rejected for v1.**

`[INFERENCE]` New affiliations, level upgrades (Secondary to Senior Secondary),
principal changes, address changes, and disaffiliations are all visible by
snapshotting the CBSE list monthly and diffing it. Deterministic, authoritative,
no LLM, no defamation risk, and it **works for all 29,000 schools** ,  whereas
news coverage exists for maybe the top 500.

`[STAKEHOLDER]` Reputation/controversy signals are **demoted to a later phase.**
Do not build a news ingestion pipeline, a signal-classification model, a severity
scale, news source-tiering, or a human review queue for signals in v1.

`[EXTERNAL-FACT]` A related idea was investigated and **rejected**: board-result
performance per school is not obtainable. CBSE discontinued school-wise merit
lists and rankings in 2017 and confirmed none for 2026, publishing only aggregate
region and school-type statistics. The only school-level result data is
self-published by schools, which is marketing-inflated *and* contains student
PII ,  see ADR-006.

## ADR-003 - Google Places / Maps cannot be a data source

`[EXTERNAL-FACT]` Google Maps Platform Service Specific Terms: `place_id` may be
stored indefinitely, Places latitude/longitude may be cached for at most 30
consecutive days, and **all other Places content must not be pre-fetched, cached,
or stored** ,  names, phone numbers, ratings, reviews, photos.

`[INFERENCE]` A permanent institution database built on Places is a terms
violation, not a cost trade-off. **Places is excluded from the source registry.**
It may only ever be used for a live, non-persisted UI lookup.

## ADR-004 - Geocoding is PIN-centroid only in v1

`[INFERENCE]` Given ADR-003, and since PIN code is present in every CBSE record,
street-level coordinates are not needed to answer "which institutions should BD
approach and in what order." PIN-code centroids are free, licence-clean, and
sufficient for state / district / metro / distance-band filtering and density
analysis.

Precise geocoding is deferred. If ever needed, use OSM/Nominatim (ODbL, with
attribution) or a geocoder granting storage rights. **Never Google.**

## ADR-005 - Legal-entity resolution uses CBSE's trust/society field

**Verdict on `[DOC]` 6.6: correct instinct, unimplementable mechanism.**

The doc requires "verified `legal_owning_entity` from registration docs."

`[EXTERNAL-FACT]` There is no national registry of trusts or societies in India.
MCA covers companies and Section-8 entities only (~3.67M, mirrored on
data.gov.in). Public charitable trusts register with state Charity Commissioners
or sub-registrars ,  decentralised and largely not machine-readable. Most Indian
private schools are trust- or society-run, so the doc's rule would merge almost
nothing.

`[INFERENCE]` But CBSE already publishes the Trust/Society name per school
(ADR-001), from a government source, for free. **That is the legal-entity axis.**
Normalised trust/society match = verified common ownership. Brand match without
trust match = `possible_franchise_network`. This turns 6.6 from a Phase-2
aspiration into a week-2 deliverable.

## ADR-006 - Institution-level data only. Never student-level. Hard boundary.

`[STAKEHOLDER]` The stated goal includes reaching individual 11th/12th students.
The **institution-level** half is what this system does. The student-level half is
out of scope by design.

`[EXTERNAL-FACT]` DPDP Act 2023 s.2(f) defines a child as **under 18** ,  most of
class 11-12. s.9(1) requires **verifiable parental consent** before processing any
child's personal data. s.9(3) imposes **absolute prohibitions** on tracking,
behavioural monitoring, and targeted advertising directed at children , 
prohibitions that cannot be unlocked by parental consent. Penalties reach
Rs 200 crore. An exemption pathway exists for educational institutions, but it
covers an institution processing its own students' data, not a third party
assembling a targeting list.

`[INFERENCE]` The operative risk is *accidental* ingestion, not deliberate: board
result PDFs, merit lists, toppers' lists and admission lists are ubiquitous on
school websites and trivially easy to scrape by accident. The exclusion is
therefore enforced as a **fetch-time denylist in the crawler**, not a downstream
filter. See `docs/COMPLIANCE.md`. This is not advisory; a change weakening it
must be rejected in review.

The compliant motion is also the better product: the institution is the consent
gateway. Secure the seminar slot or counselling session, and students opt in
themselves.

## ADR-007 - Institution universe, tiered by value density

`[STAKEHOLDER]` Recruitment is mostly online with some physical sessions. Expect
Bengaluru and Hyderabad, South India broadly, national metros and tier-1 cities,
exploratory tier-2, occasional North/East requests. **Target national metros for
v1; the architecture must be capable of going truly national without rework.**

`[INFERENCE]` Ranked by value per institution, not by count:

| Tier | Set | Est. size | Why |
|---|---|---|---|
| **A** | IB + Cambridge schools in India | ~800 | `[EXTERNAL-FACT]` ~245-270 IB World Schools, ~550-600 Cambridge IGCSE. Proven ability to pay Rs 5-12L/yr school fees. Rs 24L is cheap vs a Rs 1cr+ overseas degree. Tiny list, highest conversion value. |
| **B** | Metro CBSE + CISCE **Senior Secondary** with Science stream, fee >= ~Rs 1.5L/yr | ~3-6k after fee filter | The volume core. From CBSE SARAS + CISCE. |
| **C** | Premium integrated PU / coaching campuses (Deeksha, BASE, premium Narayana / Sri Chaitanya, metro Allen / FIITJEE) | ~500-1,500 | Highest PCM density anywhere, and centralised enough for HQ deals. |
| **D** | Mass-market PU / junior colleges, non-metro mid-fee CBSE | ~25k+ | **Schema-ready, not ingested in v1.** Mostly below the affordability floor. |

`[EXTERNAL-FACT]` Tier D is large and real ,  Karnataka alone has 2,084 PU
colleges, Telangana ~500 board-affiliated junior colleges ,  and in Karnataka,
Maharashtra, Telangana, AP and TN classes 11-12 are frequently a *separate
institution* appearing in no school registry. The data model therefore uses
`institution_type` rather than assuming "school", so Tier D can be switched on
later as a config and importer change, **never a schema migration**.

## ADR-008 - Chain intelligence survives, re-pointed at coaching/PU groups

**Verdict on `[DOC]` 8: right feature, wrong target.**

`[INFERENCE]` For student recruitment a K-12 chain relationship confers little
leverage ,  campus access is negotiated per principal. A **coaching/PU chain**
relationship confers enormous leverage.

`[EXTERNAL-FACT]` Narayana operates 950+ institutions across 250+ cities in 23
states. Deeksha Vedantu has 50 campuses across 3 states. These groups publish
first-party campus directories (e.g. `narayanagroup.com/centers`,
`baseedu.in/centers.php`).

`[INFERENCE]` So group resolution for Tier C is an **authoritative first-party
import**, not fuzzy brand matching ,  easier and more accurate than 6.6's
machinery. `[STAKEHOLDER]` Both BD motions are in scope: per-institution seminar
slots and chain-HQ deals.

## ADR-009 - Three-layer data model; canonical layer is derived and rebuildable

**Verdict on `[DOC]` 4: rejected. Two internal contradictions and one fatal gap.**

Contradictions in the original: 6.4 mandates board as many-to-one while 4 keeps
`schools.board` as a scalar column; 6.14 says missing data must reduce confidence
rather than a sub-score, while 7 puts "10% Data Confidence" *inside* the score.

The fatal gap: `school_sources` records *that* a URL was fetched, never *what it
said*. Without stored raw bytes, improving an extraction prompt requires
re-crawling everything ,  contradicting the stated requirement to improve
extraction without rebuilding from scratch.

`[INFERENCE]` Three layers:

1. **Raw** (immutable, append-only) ,  `fetches`, `raw_documents`, content-addressed.
2. **Observations** (append-only claims) ,  every extracted value with source,
   fetch, `evidence_span`, extractor version, confidence.
3. **Canonical** (derived, disposable) ,  `institutions` et al., produced by a
   deterministic resolver.

The canonical layer is 100% reproducible from raw + observations + human
overrides. `make rebuild` must regenerate it identically given the same inputs.
This is what makes prompt, model, priority, and scoring improvements safe.
See `docs/DATA-MODEL.md`.

## ADR-010 - Fit and Confidence are two numbers, never blended

**Verdict on `[DOC]` 7: rejected.** Blending data confidence into the opportunity
score makes "small institution" indistinguishable from "unknown institution" , 
which the doc's own 6.14 correctly forbids.

`[INFERENCE]` Emit **Fit** (0-100, deterministic, fully explainable) and
**Confidence** (0-100, completeness x source authority x freshness) as separate
values, plus **flags** that are never score points. Unknown components are
**excluded from the Fit denominator and renormalised**, so an institution with
unknown fees is not scored as though it were cheap. See `docs/SCORING.md`.

## ADR-011 - Affordability is a band function, and it is the largest component

`[INFERENCE]` Derived from the Rs 6L/yr `[STAKEHOLDER]` figure. Annual school fee
is the strongest available proxy for ability to fund the program, and
`[EXTERNAL-FACT]` it is obtainable free from the Mandatory Public Disclosure page
(ADR-001) ,  a field `Project-Doc.md` never mentions at all.

The curve collapses at the bottom (a family paying Rs 15k/yr cannot fund Rs 6L/yr)
and decays only mildly at the top (very high-fee families weigh overseas study,
but Rs 24L still reads as good value). Exact bands in `docs/SCORING.md`.

## ADR-012 - Boring stack. No Celery, Redis, Scrapy, OpenSearch, or Playwright in v1

**Verdict on `[DOC]` 3: over-engineered by roughly 5x.**

`[INFERENCE]` The workload is ~5k-20k institutions in v1 (~40-60k at full
national scope), refreshed monthly, with a one-time enrichment pass of ~100k HTTP
fetches. That fits one small VM.

| Doc proposed | Use instead | Why |
|---|---|---|
| Celery + Redis | Postgres `jobs` table with `FOR UPDATE SKIP LOCKED` | ~40 lines, one fewer service, durable, inspectable in SQL |
| Scrapy | `httpx` + `asyncio` + per-domain semaphore | Target URLs are known (ADR-001); no frontier needed |
| Playwright by default | Deferred | Add only when measured static-fetch failure rate justifies it |
| OpenSearch / Elasticsearch | Postgres `tsvector` + `pg_trgm` | Tens of thousands of rows. Postgres is not the bottleneck |
| React + Next.js | FastAPI + Jinja + HTMX, CSV/XLSX export | `[STAKEHOLDER]` small BD team whose real output is an exportable ranked list |

`[INFERENCE]` Total: one Postgres, one Python process, object storage, cron. Each
rejected component has a documented promotion trigger in
`docs/ARCHITECTURE.md`. **Do not pre-emptively add any of them.**

## ADR-013 - Extraction is deterministic-first. LLM is an optional fallback, off by default.

**Supersedes the original LLM-first decision.** `[STAKEHOLDER]` The prototype must
run at zero external cost, with no API dependency, and iterate fast.

### The reasoning, corrected

The cost argument does not hold and should not be cited: `[EXTERNAL-FACT]` Haiku
4.5 is $1/$5 per MTok with a 50% Batch discount, so a 1,000-institution prototype
is roughly **$10**. Money was never the constraint.

The two arguments that do hold, both `[STAKEHOLDER]`:

1. **Iteration speed.** During development, extraction is re-run dozens of times
   while parsers and field mappings are tuned. Deterministic parsing is
   milliseconds per document; LLM inference is seconds, and the Batch API can take
   hours. That is the difference between a tight feedback loop and a blocked
   afternoon.
2. **Zero dependency.** No API key means the pipeline runs offline, on any
   contributor's machine, with no billing setup and no rate limits.

### Why deterministic extraction actually works here

`[EXTERNAL-FACT]` The main enrichment target is a **standardised statutory form**,
not prose. CBSE Appendix-IX has numbered rows with fixed labels (ADR-001). Forms
are precisely the case where label-and-table parsing beats a language model:
cheaper, faster, and *more* reproducible, since the same input always yields the
same output.

### Rejected: BERT or any local transformer

`[INFERENCE]` The suggestion was considered and rejected. It is the worst of the
three options:

- Token-classification BERT needs **labelled training data** ,  several hundred
  hand-annotated pages. Producing that is strictly more work than writing the
  parser it would replace.
- An off-the-shelf SQuAD-style QA model is unreliable on tables and numerals.
  WordPiece tokenisation mangles Indian number formats (`1,85,000`, `1.85 lakh`),
  which is exactly the field that matters most.
- It is not free in time either: ~2GB of `torch` + `transformers`, a model
  download, and CPU inference that is slower than regex by orders of magnitude.

Deterministic parsing wins on accuracy **and** speed here, with none of the setup.

### The three-tier extractor

| Tier | Method | Handles | Default |
|---|---|---|---|
| 1 | Label + table parser (`selectolax` for HTML, `pdfplumber` for PDF) | Appendix-IX proforma rows: fees, class-wise strength, contacts, principal | **on** |
| 2 | Pattern extractors | Emails, phones, Indian numerals, stream keywords, free-layout fee lines | **on** |
| 3 | LLM fallback (`claude-haiku-4-5`, strict structured output) | Only documents where tiers 1-2 found nothing for a decision-critical field | **off** (`EXTRACTION_LLM_ENABLED=false`) |

### Why this is a config change, not an architecture change

`[INFERENCE]` The `observations` table already records `extractor` per row
(ADR-009). `parser:mpd_table_v1`, `regex:fee_v1`, and `llm:haiku-4.5/mpd_v1` are
peers writing to the same schema. So:

- Tiers can be swapped, added, or A/B compared over the same stored
  `raw_documents` with no migration.
- The eval harness (ADR-015) measures them against each other on identical inputs.
- If deterministic coverage turns out to be inadequate on a field, flipping
  `EXTRACTION_LLM_ENABLED=true` for that field is a one-line change, not a rewrite.

This is the payoff for storing raw bytes. **ADR-015 becomes more important, not
less** ,  deterministic coverage is now an unknown to be measured rather than
assumed.

### The honest trade-off

`[INFERENCE]` Rule-based extraction will have **lower coverage** than an LLM on
messy, non-conforming pages. Expect roughly **40-60% fee coverage** rather than
50-75%. Missing values are flagged `fee_unverified` and reduce Confidence, which
is exactly the behaviour ADR-010 already specifies. Two fields are genuinely hard
deterministically and may stay sparse: **fees in non-tabular prose layouts**, and
**`counsellor_name`**, which is not part of the Appendix-IX proforma at all.

### Unchanged boundaries

Extending `[DOC]` 5.4, which was correct: search, filtering, ranking, scoring,
geography, conflict resolution, and entity merging are deterministic code. That
was always true and is now true of extraction as well. Every observation ,  parser,
regex, or LLM ,  must still carry an `evidence_span`. See `AGENTS.md`.

## ADR-014 - Entity resolution is deterministic; no embeddings

`[INFERENCE]` UDISE code and CBSE affiliation number are stable national unique
identifiers, so within-registry duplication is near zero and the only real
problem is cross-registry linkage. Deterministic blocking on (PIN, normalised
name, phone, website domain) with a scored mid-band routed to human review
handles this at v1 scale. **Do not build an embedding pipeline.**
`Project-Doc.md` 5.2's mention of embeddings is noted and deliberately not
implemented.

## ADR-015 - Evaluation is a first-class deliverable

**Verdict on `[DOC]`: missing entirely.** For a data-quality system this was the
largest omission ,  "improve the prompts" is unfalsifiable without a measurement
harness.

`[INFERENCE]` ~200 hand-verified institutions in `eval_gold`, per-field precision
and recall, run on every extraction-prompt or model change. A prompt change that
does not report its eval delta is not reviewable. See
`docs/IMPLEMENTATION-PLAN.md` task M2-4.

## ADR-016 - OCR the scanned fee documents, locally, with a wrong-value guard

`[BUSINESS decided 2026-09-05]` `[VERIFIED M0-0]` 30% of resolved fee documents
(13 of 44 sampled) are scanned images with no text layer. `pdfplumber` returns
0-1 characters. No label parser, no regex and **no text LLM** reads an image, so
tier 3 does not rescue this either ,  the gap is not an extraction-quality problem
but an input-modality one. Without OCR, fee coverage caps at roughly 30%; with
it, roughly 45%.

**Decision: add local OCR as tier 1b**, between the table parser and the pattern
extractors, applied **only** to a PDF that (a) came from a fee-structure link or
a fee-labelled row, and (b) yields under ~50 characters of extractable text.

`rapidocr-onnxruntime`, not Tesseract. It installs from PyPI with no system
binary, which keeps `uv sync` sufficient to run the pipeline ,  the property
ADR-012 and ADR-013 both protect. It does not pull PyTorch the way `easyocr`
does. Tesseract is the fallback if accuracy proves inadequate, and that costs a
documented system dependency.

**This does not weaken ADR-013.** OCR is deterministic, local, reproducible over
stored bytes, and needs no API key. `cli extract --reextract` still makes zero
fetches and zero API calls. `observations.extractor` records `ocr:rapidocr_v1`.

**`[VERIFIED 2026-09-05]` Validated on a real scanned fee PDF** before this ADR
was accepted (`vvi.edu.in/.../Fee-Structure-for-2024-25.pdf`, 0 chars of text
layer). Rendered at 200 dpi, `rapidocr` returned 61 boxes including:

```
SL.NO  CLASS    FIRSTWEEKOF MAY2024  AUGUST2024  NOVEMBER2024  FEBRUARY2025  TOTAL
1      ITOIV    80500                14300       14300         14300         123400
2      VTOX     85000                11900       11900         11900         120700
3      XITOXII  55500                27700       27700         27700         138600
```

Class 11-12 = Rs 1,38,600/yr, correctly recovered. Three parser requirements fall
out of that sample and are not optional:

1. **OCR drops spaces.** `XITOXII`, `MAY2024`, `VAGDEVIVILASSCHOOL`. Label
   matching against `labels.yaml` must normalise whitespace out of **both** sides
   before comparing, or every label misses.
2. **Take the TOTAL column, not the first figure.** Indian school fees are
   published as instalments: `55500 + 27700 x 3 = 138600`. Reading the first
   number yields Rs 55,500 ,  a different affordability band and a wrong value.
   Where a total is absent, sum the instalment columns and record the arithmetic
   in `evidence_span`.
3. **OCR mangles stylised text but not tabular digits.** `Principal` came back as
   `Pingipa!` while every fee figure was exact. Trust OCR for the numeric grid;
   never source `principal_name` from it.

**The wrong-value guard is the load-bearing part of this ADR.** A digit
misread moves an institution across affordability bands: `74080 -> 7408` is four
bands, and hard rule 6 says a missing value beats a wrong one. So an OCR-derived
fee:

1. Sets the `fee_ocr` flag and is **always** visible as such in the UI and export.
2. Carries a reduced `observations.confidence`, and loses to any text-layer or
   HTML fee observation in `resolve/conflict.py`.
3. Must parse as a clean integer in a plausible range; anything else emits
   nothing rather than a repair attempt.
4. **Is routed to `review_queue` as `ocr_fee_unverified` when it lands within 10%
   of an affordability band boundary**, because that is exactly where a one-digit
   error changes the ranking.
5. Is measured separately in `make eval` (ADR-015). If OCR precision on the gold
   set is below the deterministic tiers by a wide margin, this ADR gets reversed
   ,  the eval harness is what decides that, not opinion.

## ADR-017 - Import nationally, enrich by campaign

`[BUSINESS decided 2026-09-05]` `[STAKEHOLDER]` BD expects to reach beyond the
southern corridor into national metros and tier-1/tier-2 cities opportunistically,
and wants the tool to answer questions about them.

**Decision: decouple import scope from enrichment scope.** They have completely
different costs and there is no reason to tie them together:

| Stage | Scope | Cost |
|---|---|---|
| Registry import (S1) | **National ,  all states** | ~38 POSTs. The list already carries name, affiliation no, district, level, principal, address and website (`docs/SOURCES.md` S1) |
| Detail + MPD enrichment | **Campaign geographies only** | 3-6 fetches per institution. This is the entire cost of the pipeline |

So the directory is national from day one ,  a BD question about Hyderabad or
Delhi NCR is answerable immediately, with contactable principal and website , 
while the expensive per-institution enrichment follows the campaign. v1 enrichment
priority: **Karnataka, Tamil Nadu, Maharashtra, Telangana**, then tier-1 metros.

Telangana is in the first wave deliberately: Hyderabad is the home market of the
Sri Chaitanya and Narayana groups, which ADR-008 already identifies as the chain
axis, and M0-0 found seven Sri Chaitanya campuses in the Bengaluru sample alone
sharing one URL template.

`enrichment_priority` is a config table keyed on state and metro, not code, and
it reuses the `campaign` machinery `docs/SCORING.md` already defines. Widening it
is a config edit.

**This is also why S8 (Serper) stays out.** The question "how do we find schools
in other cities" has already been answered by the registry: ADR-001 holds
nationally, not just regionally. A search API would be a more expensive, less
complete, less legally clean route to a list we already have in full.

---

## What `Project-Doc.md` got right and we keep unchanged

Carry these forward verbatim; they are good and were arrived at carefully:

- **5.4 ,  the AI boundary.** AI extracts and classifies; deterministic code does
  search, filter, sort, aggregate, geography, and scoring. Correct, and extended
  in ADR-013.
- **6.3 ,  city is never a safe join key alone.** Always `city + state`.
  Gurgaon/Gurugram-class renames fragment data silently.
- **6.7 ,  quantities are ranges, not points**, and a >20% disagreement between
  sources is itself signal, to be surfaced with attribution rather than averaged.
  Applied here to fees as well as enrollment.
- **6.8 ,  leadership churns annually**; use `effective_from`/`verified_at` and
  flag anything older than ~12 months as stale rather than presenting it as fact.
- **6.10 ,  never auto-merge above the ambiguity threshold.** Route to review.
  Auto-merging is cheap to build and expensive to undo once BD has contacted the
  wrong branch.
- **6.11 ,  explicit per-field source priority**, not "last scrape wins", and
  retain the losing value. Implemented literally in `docs/SOURCES.md`.
- **6.12 ,  never collect student-level data**, and rate-limit per-domain rather
  than only globally, because a small school's shared hosting will fall over.
  Hardened into ADR-006.
- **6.13 ,  natural language is a translation layer onto the structured filters**,
  never a second search path.
- **6.14 ,  show the score breakdown**; missing data reduces confidence rather
  than silently zeroing a sub-score. Implemented properly in ADR-010.

---

## Still open - `[BUSINESS]`, do not invent answers

1. **Which BD motion leads.** `[STAKEHOLDER]` "they are new to it... they will
   definitely try the first two." Both are built; which one gets prioritised
   affects only ranking weights, which are config.
2. **T-SAT merit targeting.** Up to Rs 8L is waivable on performance, implying a
   merit axis alongside affluence. But ADR-002 established that school-level
   result data is unobtainable. Whether to pursue a merit proxy at all is open.
3. **Named reviewer** for the merge/conflict review queue. The queue is built;
   who works it is a staffing decision.
4. **Campaign geography weights** beyond the Bengaluru-anchored default.
5. **CRM destination**, if any. Export is CSV/XLSX until told otherwise.
