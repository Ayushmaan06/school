# Scoring

Two independent numbers plus flags (ADR-010). **Fit** and **Confidence** are never
blended. Flags are never score points.

All weights, bands, and geography tables in this document live in
`school_intel/score/scoring.yaml`. **Do not hardcode a single number from this
document into Python.** Changing a weight must be a config edit and a version
bump, never a code change — that is what makes rescoring cheap and auditable.

---

## Step 0 — The hard gate

Before scoring, an institution must pass **all** of these. Failing any one means
it is not scored and not shown. These are not penalties; they are eligibility.

| Gate | Rule | Why |
|---|---|---|
| Has class 12 | `has_class_12 = true` | No class 12, no candidates. The single biggest filter — roughly a third of the CBSE list fails it |
| Science stream | `'PCM' = ANY(streams) OR 'PCMB' = ANY(streams)` — or `streams` unknown and the institution is Tier A/B | B.Tech candidates come from PCM. Unknown streams pass provisionally with a `streams_unknown` flag |
| Active | `status = 'active'` | Closed or disaffiliated institutions are excluded |
| Not blocked | no `overrides` row setting `excluded = true` | Human veto, e.g. already a competitor's partner |

An institution failing the gate is retained in the database (it may pass next
cycle after an upgrade) but is excluded from search results by default.

---

## Fit score

Six components. Raw points, then **renormalised over the components that have
data.**

| Component | Max points | Data required |
|---|---|---|
| Affordability | 30 | `fee_annual_inr_min/max` |
| PCM-12 volume | 25 | `pcm_12_count` or `class_12_total` |
| Curriculum tier | 15 | `institution_boards` |
| Geography | 15 | `city`/`district`/`state`/`metro_area` |
| Accessibility | 10 | `roles` + `email`/`phone` |
| Group leverage | 5 | `groups.qualifying_campus_count` |
| **Total** | **100** | |

### Renormalisation — this is the part that matters

`Project-Doc.md` 6.14 is right and its own section 7 contradicted it: a missing
value must reduce **confidence**, not silently zero a sub-score. An institution
with unknown fees is not a cheap institution.

```
earned  = sum(points for components with data)
possible = sum(max    for components with data)
fit = 100 * earned / possible
```

So an institution with everything except fee data is scored out of 70 and scaled
up — it competes on what we know, and its lower Confidence tells the BD user how
much to trust the number.

**Guard:** if `possible < 40`, do not emit a Fit score. Set
`fit = NULL`-equivalent behaviour by flagging `insufficient_data` and sorting the
institution into a separate "needs enrichment" bucket. Scaling 10 points up to 100
is not a score, it is noise.

### Component 1 — Affordability (30 points)

The largest component, because it is the hardest qualifier (ADR-011). Input is
`institutions.fee_annual_inr_mid` — a generated column, so scoring and the
fee-sorted list can never disagree about what "the fee" is. Do not recompute a
midpoint in Python.

Calibrated against Tensor's Rs 6,00,000/year all-in fee.

| Annual school fee (Rs) | Points | Reasoning |
|---|---|---|
| < 50,000 | 0 | A family paying Rs 50k/yr in school fees cannot fund Rs 6L/yr. Not a weak lead — a non-lead |
| 50,000 - 1,00,000 | 6 | Possible with heavy T-SAT scholarship; low probability |
| 1,00,000 - 1,50,000 | 15 | Plausible with scholarship and stretch |
| **1,50,000 - 3,00,000** | **30** | **Peak.** Comfortably able to fund Rs 6L/yr; a private B.Tech is the expected path |
| 3,00,000 - 6,00,000 | 28 | Still excellent. Slightly more likely to be chasing tier-1 alternatives |
| 6,00,000 - 10,00,000 | 22 | Strong ability to pay; overseas study becomes a real competitor |
| > 10,00,000 | 16 | Rs 24L reads as good value vs a Rs 1cr+ overseas degree, but abroad is the default expectation |
| unknown | excluded from denominator; flag `fee_unverified` | |

The curve collapses at the bottom and decays gently at the top. It is
deliberately **not** monotonic-increasing: the most affluent cohort is not the
most convertible cohort.

### Component 2 — PCM-12 volume (25 points)

Class-12 Science headcount. **Not** total enrollment — a 5,000-student K-12
school with 60 PCM students is worth less than an 800-student school with 200.

| PCM class-12 students | Points |
|---|---|
| >= 300 | 25 |
| 200 - 299 | 22 |
| 120 - 199 | 18 |
| 60 - 119 | 12 |
| 25 - 59 | 6 |
| < 25 | 2 |
| unknown | see estimation below |

**Estimation fallback.** If `pcm_12_count` is unknown:

1. If `class_12_total` is known, estimate `pcm_12_count = class_12_total * 0.45`.
2. Else if `total_enrollment` and grade range are known and the institution spans
   K-12, estimate `pcm_12_count = total_enrollment * 0.06`.
3. Else exclude the component from the denominator.

Any estimate sets `institutions.pcm_12_is_estimated = true`, adds the
`pcm_estimated` flag, and applies a **0.6 multiplier to the component's points**
(not to Fit overall) so an estimated 300 cannot outrank a verified 200. Both
ratios are config values in `scoring.yaml` and should be recalibrated against the
eval gold set once ~50 institutions have verified stream splits.

### Component 3 — Curriculum tier (15 points)

Maximum over the institution's boards. Proxies both affluence and the
"considering alternatives to a conventional Indian degree" mindset.

| Board | Points |
|---|---|
| `IB_DP` | 15 |
| `CAIE_ALEVEL` | 15 |
| `CISCE_ISC` | 12 |
| `CBSE` | 10 |
| `STATE_*` | 5 |

`IB_PYP`, `IB_MYP`, and `CAIE_IGCSE` alone score 0 for this component — and
usually fail the hard gate anyway, since they stop before class 11.

### Component 4 — Geography (15 points)

Anchored on Bengaluru. `[STAKEHOLDER]` delivery is mostly online with some
physical sessions, so geography is a BD-efficiency weighting, **not** a market
boundary.

The four-region North/West/South/East split from `Project-Doc.md` 6.1 is
**dropped** — it is useless for this ICP, where the real gradient is distance and
travel corridor from a single Bengaluru campus.

Default campaign (`campaign='default'`):

| Location | Points |
|---|---|
| Bengaluru Urban / Rural | 15 |
| Rest of Karnataka | 12 |
| Hyderabad, Chennai, Pune, Mumbai (MMR) | 11 |
| Other tier-1 metro (Delhi NCR, Kolkata, Ahmedabad) | 9 |
| South India tier-2 (Coimbatore, Mysuru, Kochi, Vizag, Mangaluru, ...) | 8 |
| Other tier-2 | 5 |
| Everything else | 3 |

This is a **table in `scoring.yaml`**, and `campaign` exists so alternative
weightings can be run without a code change or losing history — e.g. a
`campaign='north_push'` profile that re-weights Delhi NCR to 15. Multiple
campaigns coexist in the `scores` table.

`metro_area` is the join key here, not `city` — retaining `Project-Doc.md` 6.2's
correct point that NCR spans three states and 6.3's point that city is never a
safe join key alone.

### Component 5 — Accessibility (10 points)

An institution nobody can reach is unactionable regardless of how good it looks.

| Condition | Points |
|---|---|
| Named `career_counsellor` or `academic_coordinator` role | 4 |
| Named `principal` role, verified within 12 months | 3 |
| Institutional `email` present | 2 |
| `phone` present | 1 |

Additive, capped at 10. A principal whose `verified_at` is older than 12 months
scores 0 for that line and raises the `stale_leadership` flag — retaining
`Project-Doc.md` 6.8, which correctly identified annual leadership churn as the
field most likely to be scraped once and silently go stale.

### Component 6 — Group leverage (5 points)

Uses `groups.qualifying_campus_count` — campuses that pass the hard gate **and**
the affordability floor — never raw `campus_count`.

| Qualifying campuses in group | Points |
|---|---|
| >= 20 | 5 |
| 5 - 19 | 3 |
| 2 - 4 | 1 |
| standalone | 0 |

`group_type='possible_franchise_network'` is **capped at 1 point** regardless of
size. Retaining `Project-Doc.md` 6.6's central warning: a brand match is not a
decision-maker, and treating a franchise network as one chain sends BD to the
wrong contact and inflates the score.

---

## Confidence score

Separate 0-100. Answers "how much should a BD person trust the Fit number?"

| Component | Max | Rule |
|---|---|---|
| Field completeness | 60 | Fraction of the 8 decision-critical fields present, x 60 |
| Source authority | 20 | Mean `authority_tier` of winning observations, mapped: tier 1 -> 20, 2 -> 16, 3 -> 11, 4 -> 4 |
| Freshness | 20 | Age of the **oldest** decision-critical observation: <= 6mo -> 20, 6-12mo -> 12, 12-24mo -> 6, > 24mo -> 0 |

The 8 decision-critical fields: `fee_annual_inr`, `pcm_12_count`, `board`,
`streams`, `principal_name`, `email`-or-`phone`, `pincode`, `total_enrollment`.

Confidence is **never** added to Fit and never used to sort by default. It is
displayed alongside Fit and is available as a filter ("show me only high-Fit,
high-Confidence institutions") and as a work queue ("high Fit, low Confidence" is
exactly the enrichment backlog).

---

## Flags

Never score points. Rendered as chips in the UI and available as filters.

| Flag | Meaning |
|---|---|
| `fee_unverified` | No fee observation |
| `fee_ocr` | Fee read by OCR from a scanned document (ADR-016). Always visible; never silently equivalent to a parsed fee |
| `unreadable_fee_document` | A fee document was found but has no text layer and OCR produced nothing usable |
| `fee_disputed` | Two sources disagree by > 20% |
| `pcm_estimated` | PCM count derived, not observed |
| `streams_unknown` | Passed the stream gate provisionally |
| `stale_leadership` | Principal data older than 12 months |
| `no_website` | No usable website found, including after S8 gap-fill |
| `fetch_failed` | Site fetches exhausted retries |
| `possible_franchise_network` | Group membership by brand only, not verified ownership |
| `insufficient_data` | `possible < 40`; Fit suppressed |
| `enrollment_disputed` | Sources disagree > 20% on enrollment |
| `review_open` | An open `review_queue` item touches this institution |

---

## Explainability

`scores.components` is a JSONB blob, mandatory, with one entry per component
carrying the raw value, points earned, max possible, and a human-readable reason.

```json
{
  "affordability":  {"raw": 195000, "points": 30, "max": 30,
                     "reason": "Rs 1,95,000/yr sits in the 1.5-3L peak band"},
  "pcm_12_volume":  {"raw": 168, "points": 18, "max": 25,
                     "reason": "168 PCM class-12 students (observed, cbse_mpd 2025)"},
  "curriculum":     {"raw": ["CBSE","CAIE_IGCSE"], "points": 10, "max": 15,
                     "reason": "CBSE; IGCSE alone contributes 0"},
  "geography":      {"raw": "Bengaluru Urban", "points": 15, "max": 15,
                     "reason": "Home metro"},
  "accessibility":  {"raw": {"counsellor": true, "principal_fresh": true,
                             "email": true, "phone": false},
                     "points": 9, "max": 10,
                     "reason": "Named counsellor, principal verified 2026-03, email present"},
  "group_leverage": {"raw": null, "points": 0, "max": 5, "reason": "Standalone"},
  "_meta": {"earned": 82, "possible": 100, "fit": 82.0,
            "model_version": "fit_v1", "campaign": "default"}
}
```

The UI must render this breakdown, not just the number — `Project-Doc.md` 6.14 was
right that an unexplained 91/100 stops being trusted the first time it disagrees
with a BD person's local knowledge. The same applies to score movement: an
overnight 60 -> 90 jump is explained by diffing two `components` blobs, which is
why score history is retained rather than overwritten.

---

## Natural-language search

Retaining `Project-Doc.md` 6.13, which was correct: NL is a **translation layer
onto the same structured filters**, never a second search path.

```
user text
  -> Claude, strict structured output -> FilterSpec (pydantic)
  -> validate against a whitelist of fields, operators, and values
  -> deterministic SQL builder
  -> the same query path the filter UI uses
```

Hard rules:

- The LLM emits a `FilterSpec` object. It **never** emits SQL, never sees the
  schema beyond the whitelist, and never influences ranking or ordering.
- Any field, operator, or enum value outside the whitelist is rejected — return a
  clarifying question to the user rather than guessing.
- The rendered `FilterSpec` is shown to the user as editable filter chips, so an
  NL query and the equivalent manual filter selection are provably the same query
  and return identical results.
- Range semantics are fixed and applied consistently, per 6.13's warning: a
  threshold like "> 2000 students" tests **`fee_annual_inr_max`/`_min`
  midpoints** and documented range endpoints, decided once in
  `api/nlq.py` and never varied per query.
