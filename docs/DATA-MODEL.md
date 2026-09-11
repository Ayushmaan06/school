# Data Model

PostgreSQL 16. Three layers (ADR-009). The DDL below is the specification —
implement it in Alembic migrations, do not deviate without an ADR.

**The one rule:** layers 1 and 2 are append-only and are the truth. Layer 3 is a
derived cache that `make rebuild` can drop and regenerate from layers 1 and 2 plus
`overrides`. Any change that breaks that property is wrong.

---

## Layer 0 — Source registry

Drives fetch cadence, conflict resolution, and compliance. Seeded from
`docs/SOURCES.md`; not user-editable at runtime.

```sql
CREATE TABLE source_registry (
  id              text PRIMARY KEY,     -- 'cbse_saras','cbse_mpd','cisce','ib','cambridge','udise','group_directory','serper'
  kind            text NOT NULL,        -- 'registry'|'institution_site'|'group_site'|'search_api'
  authority_tier  smallint NOT NULL,    -- 1=govt/statutory 2=board/official 3=institution self-published 4=third party
  robots_ok       boolean,
  tos_note        text,
  refresh_days    integer NOT NULL,
  rate_limit_rps  numeric NOT NULL DEFAULT 1.0,
  enabled         boolean NOT NULL DEFAULT true
);
```

`authority_tier` is the default tie-break in conflict resolution. Per-field
overrides to that default live in `docs/SOURCES.md` and are implemented in
`resolve/conflict.py`.

---

## Layer 1 — Raw. Immutable, append-only.

```sql
CREATE TABLE fetches (
  id            bigserial PRIMARY KEY,
  source_id     text NOT NULL REFERENCES source_registry(id),
  url           text NOT NULL,
  requested_at  timestamptz NOT NULL DEFAULT now(),
  http_status   integer,
  content_type  text,
  error         text,                  -- NULL on success
  content_hash  text,                  -- sha256 of body; NULL on failure
  bytes         integer,
  elapsed_ms    integer,
  robots_allowed boolean NOT NULL,
  denylist_hit  text                   -- non-NULL means blocked by ADR-006 gate
);
CREATE INDEX ON fetches (url, requested_at DESC);
CREATE INDEX ON fetches (source_id, requested_at DESC);
CREATE INDEX ON fetches (content_hash);

CREATE TABLE raw_documents (
  content_hash  text PRIMARY KEY,       -- sha256; content-addressed
  storage_path  text NOT NULL,          -- object-store key, e.g. raw/9f/2a/9f2a...c1
  media_type    text NOT NULL,
  bytes         integer NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now()
);
```

Content addressing gives free dedup: re-fetching an unchanged page writes a
`fetches` row (proving freshness) but no new document. It also makes
"has this page changed?" a hash comparison.

**Never delete from these tables.** Retention pruning, if ever needed, is a
separate reviewed decision — see `docs/COMPLIANCE.md`.

---

## Layer 2 — Observations. Append-only claims.

The most important table in the system. One row = one source asserting one field
value about one entity at one point in time.

```sql
CREATE TABLE observations (
  id              bigserial PRIMARY KEY,

  -- subject
  entity_type     text   NOT NULL,      -- 'institution'|'group'|'person'
  entity_key      text   NOT NULL,      -- natural key BEFORE resolution, e.g. 'cbse:330801','udise:29010100108'
  institution_id  bigint REFERENCES institutions(id),  -- set by resolver; NULL until linked
  group_id        bigint REFERENCES groups(id),

  -- claim
  field           text   NOT NULL,      -- see canonical field vocabulary below
  value_text      text,
  value_num       numeric,
  value_json      jsonb,
  unit            text,                 -- 'INR','INR_per_year','count', NULL

  -- provenance  (all of these are mandatory in practice)
  source_id       text   NOT NULL REFERENCES source_registry(id),
  fetch_id        bigint REFERENCES fetches(id),
  content_hash    text   REFERENCES raw_documents(content_hash),
  evidence_span   text,                 -- verbatim quote supporting the claim
  observed_at     timestamptz NOT NULL, -- when the SOURCE says it was true; else fetch time
  extracted_at    timestamptz NOT NULL DEFAULT now(),
  extractor       text   NOT NULL,      -- 'parser:cbse_saras_v3' | 'llm:haiku-4.5/mpd_v2'
  confidence      numeric NOT NULL CHECK (confidence >= 0 AND confidence <= 1),

  superseded_by   bigint REFERENCES observations(id)
);
CREATE INDEX ON observations (entity_key, field);
CREATE INDEX ON observations (institution_id, field) WHERE institution_id IS NOT NULL;
CREATE INDEX ON observations (content_hash, extractor);
CREATE UNIQUE INDEX ON observations (content_hash, extractor, entity_key, field)
  WHERE superseded_by IS NULL;
```

Notes for implementers:

- `evidence_span` is **mandatory for every LLM extraction** and should be the
  literal source text. It powers `/institutions/{id}/why`. An extraction with no
  evidence span is a bug.
- `observed_at` vs `extracted_at` matters. A UDISE 2023-24 enrollment figure read
  in 2026 has `observed_at` in 2024. Freshness logic uses `observed_at`.
- `extractor` embeds the prompt/parser version. Re-running an improved prompt
  produces **new rows**, does not update old ones, and lets you A/B two prompt
  versions over the same `raw_documents`.
- The partial unique index makes re-extraction idempotent: same document + same
  extractor + same field = one live row.

### Canonical field vocabulary

Use exactly these `field` strings. Adding one requires updating
`resolve/conflict.py` and `docs/SOURCES.md` in the same change.

```
name  legal_entity_name  institution_type
address  pincode  city  district  state  metro_area
grade_low  grade_high  has_class_12  streams  medium  gender  residential
board  board_status  board_valid_from  board_valid_to
fee_annual_inr  fee_year  fee_component_breakdown
total_enrollment  enrollment_year  class_12_total  pcm_12_count  pcm_12_count_year
website  email  phone
principal_name  principal_title  counsellor_name  counsellor_title
management_type  year_founded  status
group_name  group_campus_count
```

---

## Layer 3 — Canonical. Derived, disposable.

```sql
CREATE TABLE institutions (
  id                    bigserial PRIMARY KEY,

  -- stable external identifiers (the backbone of entity resolution, ADR-014)
  udise_code            text UNIQUE,
  cbse_affiliation_no   text UNIQUE,
  cisce_code            text UNIQUE,
  ib_school_code        text UNIQUE,
  cambridge_centre_no   text UNIQUE,

  canonical_name        text NOT NULL,
  institution_type      text NOT NULL,   -- 'school'|'pu_junior_college'|'integrated_coaching_campus'

  -- geography
  address               text,
  pincode               text,
  city                  text,
  district              text,
  state                 text,
  metro_area            text,            -- 'Bengaluru','Delhi NCR','MMR','Hyderabad', ...
  lat                   double precision,
  lon                   double precision,
  geo_precision         text,            -- 'pin_centroid'|'district_centroid'|'exact'

  -- offering  (the hard gate lives here)
  grade_low             smallint,
  grade_high            smallint,
  has_class_12          boolean,
  streams               text[],          -- 'PCM','PCB','PCMB','Commerce','Arts'
  medium                text[],
  gender                text,            -- 'coed'|'boys'|'girls'
  residential           text,            -- 'day'|'residential'|'both'

  -- commercial  (ranges, per Project-Doc 6.7, applied to fees too)
  fee_annual_inr_min    integer,
  fee_annual_inr_max    integer,
  -- Single sortable/scoreable fee value. Generated, so sorting and the
  -- affordability component can never disagree about what "the fee" is.
  -- Both known -> midpoint; only one known -> that one; neither -> NULL.
  fee_annual_inr_mid    integer GENERATED ALWAYS AS (
                          COALESCE((fee_annual_inr_min + fee_annual_inr_max) / 2,
                                   fee_annual_inr_min,
                                   fee_annual_inr_max)) STORED,
  fee_year              smallint,
  total_enrollment      integer,
  enrollment_year       smallint,
  class_12_total        integer,
  pcm_12_count          integer,
  pcm_12_count_year     smallint,
  pcm_12_is_estimated   boolean NOT NULL DEFAULT false,

  -- contact
  website               text,
  email                 text,
  phone                 text,

  -- org
  group_id              bigint REFERENCES groups(id),
  legal_entity_name     text,            -- trust/society, from CBSE (ADR-005)
  legal_entity_norm     text,            -- normalised, used for group matching
  management_type       text,

  -- lifecycle
  status                text NOT NULL DEFAULT 'active',  -- 'active'|'closed'|'disaffiliated'
  year_founded          smallint,
  first_seen_at         timestamptz,
  last_resolved_at      timestamptz,

  search_tsv            tsvector
);
CREATE INDEX ON institutions (state, city);
CREATE INDEX ON institutions (pincode);
CREATE INDEX ON institutions (metro_area) WHERE metro_area IS NOT NULL;
CREATE INDEX ON institutions (group_id) WHERE group_id IS NOT NULL;
CREATE INDEX ON institutions (legal_entity_norm) WHERE legal_entity_norm IS NOT NULL;
CREATE INDEX ON institutions USING gin (search_tsv);
CREATE INDEX ON institutions USING gin (canonical_name gin_trgm_ops);
CREATE INDEX ON institutions (has_class_12) WHERE has_class_12 = true;
-- Sort keys. `[STAKEHOLDER]`-required: fee and student-count sorting.
-- NULLS LAST in the index matches the mandatory query-side NULLS LAST, so the
-- planner can serve a sorted page from the index instead of sorting the table.
CREATE INDEX ON institutions (fee_annual_inr_mid DESC NULLS LAST, id);
CREATE INDEX ON institutions (fee_annual_inr_min DESC NULLS LAST, id);
CREATE INDEX ON institutions (pcm_12_count      DESC NULLS LAST, id);
CREATE INDEX ON institutions (class_12_total    DESC NULLS LAST, id);
CREATE INDEX ON institutions (total_enrollment  DESC NULLS LAST, id);
```

**No score column.** Scores are versioned rows elsewhere — this was a
`Project-Doc.md` 4 vs 6.14 contradiction (ADR-010).

```sql
-- Fixes the Project-Doc 6.4 contradiction: board is many-to-one, not a scalar.
CREATE TABLE institution_boards (
  institution_id bigint NOT NULL REFERENCES institutions(id) ON DELETE CASCADE,
  board          text   NOT NULL,   -- 'CBSE','CISCE_ICSE','CISCE_ISC','IB_DP','IB_MYP',
                                    -- 'CAIE_IGCSE','CAIE_ALEVEL','STATE_KA','STATE_MH', ...
  status         text   NOT NULL,   -- 'active'|'expired'|'provisional'
  valid_from     date,
  valid_to       date,
  PRIMARY KEY (institution_id, board)
);
```

A school can hold CBSE and CAIE_IGCSE simultaneously; both rows exist. Board
filters are `EXISTS` subqueries, never equality on a column.

```sql
CREATE TABLE groups (
  id                bigserial PRIMARY KEY,
  canonical_name    text NOT NULL,
  group_type        text NOT NULL,   -- 'verified_single_owner'|'possible_franchise_network'|'coaching_chain'
  legal_entity_name text,
  legal_entity_norm text,
  website           text,
  hq_city           text,
  hq_state          text,
  campus_count      integer,
  qualifying_campus_count integer,   -- campuses that pass the hard gate; drives chain leverage
  states_present    text[],
  evidence          text NOT NULL    -- how we concluded this group exists. Mandatory.
);
```

`group_type` is never guessed. `verified_single_owner` requires matching
`legal_entity_norm` (ADR-005). `coaching_chain` requires a first-party campus
directory import (ADR-008). Everything else is
`possible_franchise_network` and must render visibly distinct in the UI.

```sql
CREATE TABLE people (
  id              bigserial PRIMARY KEY,
  full_name       text NOT NULL,
  normalized_name text NOT NULL
);
CREATE INDEX ON people (normalized_name);

-- Fixes Project-Doc's people.school_id problem: a trust CEO belongs to a group,
-- not a school, and principals move between institutions over time.
CREATE TABLE roles (
  id               bigserial PRIMARY KEY,
  person_id        bigint NOT NULL REFERENCES people(id),
  institution_id   bigint REFERENCES institutions(id) ON DELETE CASCADE,
  group_id         bigint REFERENCES groups(id) ON DELETE CASCADE,
  title_raw        text NOT NULL,     -- keep the scraped title verbatim (6.8)
  title_normalized text NOT NULL,     -- 'principal'|'vice_principal'|'career_counsellor'
                                      -- |'academic_coordinator'|'director'|'trustee'|'other'
  effective_from   date,
  effective_to     date,
  verified_at      timestamptz,
  CHECK (institution_id IS NOT NULL OR group_id IS NOT NULL)
);
CREATE INDEX ON roles (institution_id, title_normalized);
```

`career_counsellor` and `academic_coordinator` are as important as `principal`
here — they are usually the actual gatekeeper for a campus session.

```sql
CREATE TABLE scores (
  institution_id bigint NOT NULL REFERENCES institutions(id) ON DELETE CASCADE,
  model_version  text   NOT NULL,   -- 'fit_v1'
  campaign       text   NOT NULL DEFAULT 'default',
  fit            numeric NOT NULL CHECK (fit >= 0 AND fit <= 100),
  confidence     numeric NOT NULL CHECK (confidence >= 0 AND confidence <= 100),
  components     jsonb  NOT NULL,   -- per-component raw value, points, max, reason
  flags          text[] NOT NULL DEFAULT '{}',
  computed_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (institution_id, model_version, campaign, computed_at)
);
CREATE INDEX ON scores (model_version, campaign, fit DESC);
```

History is retained by design (`Project-Doc.md` 6.14). The UI reads the latest
row per `(institution, model_version, campaign)`; a 60->90 jump is explainable by
diffing two `components` blobs.

```sql
CREATE TABLE signals (
  id                bigserial PRIMARY KEY,
  institution_id    bigint REFERENCES institutions(id) ON DELETE CASCADE,
  group_id          bigint REFERENCES groups(id) ON DELETE CASCADE,
  signal_type       text NOT NULL,   -- 'new_affiliation'|'level_upgrade'|'principal_change'
                                     -- |'address_change'|'disaffiliation'|'new_campus'
  detected_at       timestamptz NOT NULL DEFAULT now(),
  event_date        date,
  before_json       jsonb,
  after_json        jsonb,
  source_id         text NOT NULL REFERENCES source_registry(id),
  evidence_fetch_id bigint REFERENCES fetches(id),
  is_active         boolean NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX ON signals (institution_id, signal_type, event_date)
  WHERE institution_id IS NOT NULL;
```

No `severity`, no `confidence`, no human review gate — because every v1 signal is
a deterministic diff of two authoritative snapshots (ADR-002). If the news
pipeline is ever revived, it gets its own table rather than polluting this one.

```sql
CREATE TABLE registry_snapshots (
  id           bigserial PRIMARY KEY,
  source_id    text NOT NULL REFERENCES source_registry(id),
  taken_at     timestamptz NOT NULL DEFAULT now(),
  row_count    integer NOT NULL,
  content_hash text NOT NULL REFERENCES raw_documents(content_hash)
);
```

Stage 8 diffs the two most recent snapshots for a source. **Guard:** if
`row_count` is less than 50% of the previous snapshot, abort and alert — do not
emit thousands of spurious `disaffiliation` signals. See ARCHITECTURE.md,
"Source structure drift."

---

## Operational tables

```sql
CREATE TABLE jobs (
  id           bigserial PRIMARY KEY,
  kind         text NOT NULL,        -- 'fetch'|'extract'|'resolve'|'score'|'snapshot'
  payload      jsonb NOT NULL,
  state        text NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|dead
  attempts     smallint NOT NULL DEFAULT 0,
  max_attempts smallint NOT NULL DEFAULT 3,
  not_before   timestamptz NOT NULL DEFAULT now(),
  locked_by    text,
  locked_at    timestamptz,
  last_error   text,
  dedupe_key   text UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz
);
CREATE INDEX ON jobs (not_before) WHERE state = 'pending';
```

The whole queue (ADR-012):

```sql
UPDATE jobs SET state='running', locked_by=:worker, locked_at=now(), attempts=attempts+1
WHERE id = (
  SELECT id FROM jobs
  WHERE state='pending' AND not_before <= now()
  ORDER BY not_before
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

```sql
CREATE TABLE review_queue (
  id         bigserial PRIMARY KEY,
  kind       text NOT NULL,   -- 'merge_candidate'|'conflict'|'low_confidence_extraction'
  payload    jsonb NOT NULL,
  score      numeric,         -- match score, for merge candidates
  state      text NOT NULL DEFAULT 'open',  -- open|accepted|rejected
  decided_by text,
  decided_at timestamptz,
  note       text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON review_queue (kind, score DESC) WHERE state = 'open';

-- Human corrections. Applied LAST by the resolver, above every source.
-- These MUST survive every rebuild - that is the entire point.
CREATE TABLE overrides (
  id          bigserial PRIMARY KEY,
  entity_type text NOT NULL,
  entity_id   bigint NOT NULL,
  field       text NOT NULL,
  value_json  jsonb NOT NULL,
  reason      text NOT NULL,     -- mandatory: why the automated value was wrong
  author      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz        -- optional: re-open to automation after this date
);
CREATE UNIQUE INDEX ON overrides (entity_type, entity_id, field)
  WHERE expires_at IS NULL;

-- Merge decisions, so a rebuild reproduces the same entity graph.
CREATE TABLE merge_decisions (
  id           bigserial PRIMARY KEY,
  entity_key_a text NOT NULL,
  entity_key_b text NOT NULL,
  decision     text NOT NULL,   -- 'same'|'different'
  decided_by   text NOT NULL,
  decided_at   timestamptz NOT NULL DEFAULT now(),
  note         text,
  UNIQUE (entity_key_a, entity_key_b)
);

CREATE TABLE pipeline_runs (
  id          bigserial PRIMARY KEY,
  stage       text NOT NULL,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  rows_in     integer,
  rows_out    integer,
  ok          boolean,
  error       text,
  notes       jsonb
);

-- URLs discovered at extraction time to contain student PII. Fetch-time
-- denylist consults this in addition to its static patterns. See COMPLIANCE.md.
CREATE TABLE denylist_learned (
  url        text PRIMARY KEY,
  reason     text NOT NULL,     -- 'student_data_detected' | manual note
  added_at   timestamptz NOT NULL DEFAULT now(),
  added_by   text NOT NULL      -- 'extractor' | a username
);

-- ADR-015. Hand-verified ground truth. Never machine-written.
CREATE TABLE eval_gold (
  institution_key text NOT NULL,   -- e.g. 'cbse:330801'
  field           text NOT NULL,
  expected_json   jsonb NOT NULL,
  verified_by     text NOT NULL,
  verified_at     date NOT NULL,
  note            text,
  PRIMARY KEY (institution_key, field)
);
```

`overrides` + `merge_decisions` are the reason a rebuild is safe: they carry every
human judgement forward, so regenerating the canonical layer never discards work a
person did.

---

## Five things that are easy to get wrong

These are the points where an implementer is most likely to guess, and guess
wrong. They are specified here so nobody has to.

### 1. How `entity_key` becomes `institution_id`, deterministically

`observations.entity_key` is a **natural key from the source**, assigned at
extraction time before any resolution:

| Source | `entity_key` format | Example |
|---|---|---|
| `cbse_saras`, `cbse_mpd` | `cbse:{affiliation_no}` | `cbse:330801` |
| `udise` | `udise:{udise_code}` | `udise:29010100108` |
| `cisce` | `cisce:{school_code}` | `cisce:KA042` |
| `ib` | `ib:{school_code}` | `ib:001234` |
| `cambridge` | `caie:{centre_no}` | `caie:IN123` |
| `group_directory` | `group:{group_slug}:{campus_slug}` | `group:narayana:bengaluru-marathahalli` |

The resolver assigns `institution_id` in two deterministic passes:

1. **Stable-ID pass.** Each `entity_key` prefix maps to exactly one column on
   `institutions` (`cbse:` -> `cbse_affiliation_no`, `udise:` -> `udise_code`,
   and so on). Upsert on that column. Deterministic by construction.
2. **Merge pass.** Replay `merge_decisions` in `(entity_key_a, entity_key_b)`
   sort order, unioning the referenced institutions. **Sort order matters** — it
   is what makes repeated rebuilds produce identical results, so never iterate
   these rows in insertion order or in whatever order Postgres returns them.

`group:` keys have no external unique ID. They are keyed on the composite
`entity_key` itself, stored as-is; a `group_directory` campus that later turns out
to be a known CBSE school is linked by a `merge_decisions` row like any other pair.

### 2. `qualifying_campus_count` is computed BEFORE scoring — there is a cycle here

Scoring's group-leverage component reads `groups.qualifying_campus_count`, and a
naive reading of "qualifying" as "scores well" makes scoring depend on itself.
**It does not mean that.** A campus qualifies if it passes:

- the hard gate (`docs/SCORING.md` step 0), **and**
- the affordability floor: `fee_annual_inr` midpoint >= 100,000, **or** fee unknown
  while `curriculum_tier >= CISCE_ISC`

Nothing else. No Fit score, no Confidence, no ranking. So the pipeline order is:

```
resolve  ->  compute qualifying_campus_count  ->  score
```

`resolve/groups.py` owns that middle step. If you find yourself importing
anything from `score/fit.py` into `resolve/`, you have reintroduced the cycle.

### 3. `search_tsv` is populated by the resolver, not by a trigger

Set it in `resolve/build.py` as part of the canonical write:

```sql
to_tsvector('simple',
  coalesce(canonical_name,'') || ' ' || coalesce(city,'') || ' ' ||
  coalesce(district,'') || ' ' || coalesce(state,'') || ' ' ||
  coalesce(legal_entity_name,''))
```

Use `'simple'`, not `'english'` — stemming English words is wrong for Indian
institution names (`Vidyalaya`, `Bhavan`, `Vidya Mandir`) and actively harms
recall. A database trigger is deliberately avoided so that a rebuild stays a pure
function of its inputs.

### 4. `overrides` can hold fields that are not in the canonical vocabulary

Two control fields are set through `overrides` rather than being columns, because
they are human decisions rather than observed facts:

| `field` | `value_json` | Effect |
|---|---|---|
| `excluded` | `true` | Institution fails the hard gate permanently. Use for "already a competitor's partner", "asked not to be contacted" |
| `priority_boost` | integer | Added to Fit after renormalisation, clamped to 0-100. Use sparingly, and the mandatory `reason` matters |

Any other `field` value in `overrides` must be a real canonical field, and
`resolve/conflict.py` should reject unknown names loudly rather than silently
ignoring an override someone took the trouble to write.

### 5. `metro_area` is a closed, configured vocabulary

Not free text. Lives in `sources/metro_areas.yaml` as a PIN-prefix mapping, and
scoring joins on it (`docs/SCORING.md` component 4). Initial values:

```
Bengaluru · Delhi NCR · MMR (Mumbai) · Hyderabad · Chennai · Pune · Kolkata ·
Ahmedabad · Coimbatore · Kochi · Mysuru · Mangaluru · Visakhapatnam · Jaipur ·
Lucknow · Chandigarh · Indore · Nagpur · Surat
```

`Delhi NCR` deliberately spans Delhi, Haryana and UP PINs — that is the whole
point of having the field (`Project-Doc.md` 6.2). A PIN with no metro mapping
gets `NULL`, which scores in the lowest geography band, and that is correct
behaviour rather than an error.

## Rebuild contract

```
make rebuild
  -> DELETE FROM scores, roles, people, institution_boards   (purely derived)
  -> UPDATE observations SET institution_id = NULL, group_id = NULL
  -> resolve/build.py replays observations, UPSERTING institutions and groups
     on their stable-ID columns
  -> applies merge_decisions   (replayed in sorted order)
  -> applies overrides         (last, highest priority)
  -> prunes institutions with no live observation
  -> score/ recomputes
```

### Why this is not `TRUNCATE ... CASCADE`

`[VERIFIED 2026-09-05]` An earlier version of this contract said:

> `TRUNCATE institutions, institution_boards, groups, people, roles, scores`
> `(CASCADE; signals and observations are NOT truncated)`

**Those two clauses contradict each other, and the first one wins.**
`TRUNCATE ... CASCADE` ignores `ON DELETE` rules and truncates *every* table
with a foreign key into the truncated set. `observations.institution_id`
references `institutions(id)`, so that statement deletes the entire
append-only observation layer. It was not theoretical: running it destroyed
17,628 observations on the development corpus, and only the raw documents made
recovery possible.

So institutions and groups are **upserted on their stable-ID column and pruned**,
never wholesale deleted. Two consequences that are features rather than
workarounds:

- **Institution ids stay stable across rebuilds.** `signals.institution_id` is
  `ON DELETE CASCADE`; recreating rows every run would silently discard signal
  history, which is the thing M4-3 exists to accumulate.
- **The prune step is the only place institutions are deleted**, and it is
  deliberate: an institution with no live observation has genuinely left the
  corpus, so its scores and signals should go with it.

The four required properties are unchanged, and `tests/integration/test_rebuild.py`
asserts property (d) — non-destructiveness — by row-counting the append-only
tables before and after. That test is what would have caught this immediately.

Required properties, and there should be a test for each:

1. **Deterministic.** Same inputs produce the same canonical output. No
   `random`, no unseeded ordering, no `now()` inside resolution logic — pass a
   run timestamp in.
2. **Override-preserving.** Every `overrides` row is reflected in the rebuilt
   canonical layer.
3. **Merge-preserving.** Every `merge_decisions` row is honoured.
4. **Non-destructive.** `observations`, `fetches`, `raw_documents`, `signals`,
   `review_queue`, `overrides`, `merge_decisions`, `eval_gold` are never touched.

Test this early (task M1-6). If rebuild is not trustworthy, nothing downstream is.
