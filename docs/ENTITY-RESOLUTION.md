# Entity Resolution

Deterministic. No embeddings, no LLM in the decision path (ADR-014).

The core insight that makes this tractable: **India has stable national unique
identifiers for schools.** UDISE code and CBSE affiliation number are both
unique and persistent, so within-registry duplication is near zero and the only
real problem is cross-registry linkage. `Project-Doc.md` treated ER as the hard
part; it is mostly a join.

---

## Stage 1 ,  Normalisation

Runs before any matching. Implemented in `extract/normalize.py`, pure functions,
heavily unit-tested. Normalisation is where most ER accuracy actually comes from.

### Institution names

```
lowercase
-> strip punctuation, collapse whitespace
-> expand known abbreviations (ordered, longest-first)
-> drop generic institution words for the MATCH KEY only (never for display)
-> sort remaining tokens
```

Abbreviation expansions (config file, `normalize_data/abbreviations.yaml`, not code):

```
dps        -> delhi public school
kv / k v   -> kendriya vidyalaya
jnv        -> jawahar navodaya vidyalaya
st / st.   -> saint
sr / sr.   -> senior
sec        -> secondary
hr / hr.   -> higher
vidyalaya  -> vidyalaya        (keep; not generic)
convent    -> convent          (keep)
intl / int'l -> international
eng        -> english
mem / meml -> memorial
pu         -> pre university
```

Generic words dropped from the match key: `school`, `academy`, `institution`,
`institute`, `education`, `educational`, `college`, `campus`, `branch`, `the`,
`public`, `high`, `higher`, `secondary`, `senior`.

**Retain the raw name always.** `canonical_name` is for humans;
`match_key` is derived and never displayed. Every alias ever observed stays in
`observations` under `field='name'` ,  that is the alias table `Project-Doc.md` 6.6
asked for, without a separate table.

### Cities and districts

- Rename map, config file: `bangalore -> bengaluru`, `gurgaon -> gurugram`,
  `mysore -> mysuru`, `mangalore -> mangaluru`, `calcutta -> kolkata`,
  `bombay -> mumbai`, `madras -> chennai`, `pondicherry -> puducherry`,
  `trivandrum -> thiruvananthapuram`, `baroda -> vadodara`, ...
- **`city` alone is never a join key.** Always `(city, state)` ,  retaining
  `Project-Doc.md` 6.3. `pincode` is preferred over both where available.
- `district` comes from `pin_centroids` when the parsed value disagrees with the
  PIN-derived value, because district boundaries get redrawn and source data lags.

### Person names and titles

- Names: strip honorifics (`mr`, `mrs`, `ms`, `dr`, `prof`, `smt`, `shri`,
  `sri`, `fr`, `sr`, `rev`), normalise initials (`A.B.` -> `a b`), collapse space.
- Titles: map to the `roles.title_normalized` vocabulary, **and always keep
  `title_raw`** ,  retaining `Project-Doc.md` 6.8.

```
principal, head master, headmaster, headmistress, head of school -> principal
vice principal, deputy principal                                 -> vice_principal
career counsellor, career counselor, career guidance             -> career_counsellor
academic coordinator, academic head, academic director,
  coordinator (senior secondary)                                 -> academic_coordinator
director, executive director, managing director, ceo             -> director
trustee, chairman, chairperson, secretary, correspondent         -> trustee
anything else                                                     -> other
```

### Fees and currency

- Parse Indian numerals: `1,85,000`, `Rs. 1.85 lakh`, `INR 185000`, `1.85 L`.
- Normalise every fee to **annual INR**. A monthly figure is multiplied by 12
  **only** if the source explicitly says monthly; if the period is ambiguous, do
  not record the observation ,  record nothing and let the field stay unknown.
  Ambiguity here is expensive: a 12x error moves an institution across four bands.
- Range convention: `min` = mandatory tuition only; `max` = tuition plus all
  recurring annual mandatory components. **Exclude** one-time admission fees,
  refundable deposits, and optional items (transport, hostel, meals).

---

## Stage 2 ,  Linkage by stable identifier

Runs first and resolves the large majority of cases. Exact match only, no scoring.

```
udise_code          -> unique, national. The strongest key.
cbse_affiliation_no -> unique, national.
cisce_code / ib_school_code / cambridge_centre_no -> unique within registry.
```

Cross-registry bridges, in order of reliability:

1. **A UDISE code published on the school's own MPD page.** Common, and the
   cleanest CBSE <-> UDISE bridge. Extract it whenever present.
2. **Exact `(pincode, match_key)` match.** Very high precision.
3. **Exact normalised website domain match** (registrable domain, `www` stripped,
   scheme-insensitive). High precision; watch for shared domains across the
   branches of one group ,  a domain match plus a *different* PIN is a group
   relationship, not a duplicate.
4. **Exact phone match.** Good, but shared trust switchboards cause false
   positives across sibling campuses. Never sufficient alone.

Any stable-ID conflict ,  two different UDISE codes claiming the same CBSE
affiliation number ,  is a `review_queue` item, never auto-resolved. It usually
means a source parse bug, so treat it as a defect signal too.

---

## Stage 3 ,  Blocking

Only for records that stage 2 could not link. Generate candidate pairs cheaply;
never compare all-pairs.

Blocking keys ,  a pair is a candidate if it shares **any** one:

| Key | Rationale |
|---|---|
| `pincode` | Tightest useful block |
| `(state, first 6 chars of match_key)` | Catches PIN typos and PIN changes |
| registrable website domain | Catches renames |
| normalised phone | Catches renames |
| `(district, sorted first 2 tokens of match_key)` | Catches PIN + city inconsistency |

At v1 scale (~6k institutions) this is milliseconds in Postgres. At full national
scale (~40k) it is still trivial. This is why ADR-014 rejects embeddings ,  there
is no recall problem to solve.

---

## Stage 4 ,  Match scoring

Weighted sum over a candidate pair. Weights live in
`resolve/match_weights.yaml`.

| Signal | Weight | Computation |
|---|---|---|
| Name similarity | 0.35 | `token_set_ratio` on `match_key`, 0-1 |
| PIN match | 0.25 | exact 1.0; same first 3 digits 0.4; else 0 |
| Website domain | 0.15 | registrable domain equal -> 1.0 |
| Phone | 0.10 | any normalised number in common -> 1.0 |
| Geo proximity | 0.10 | 1.0 if PIN centroids within 2km, decaying to 0 at 25km |
| Legal entity | 0.05 | `legal_entity_norm` equal -> 1.0 |

### Thresholds and the ambiguity band

```
score >= 0.90   -> auto-merge
0.65 - 0.90     -> review_queue (kind='merge_candidate'), NEVER auto-merged
score <  0.65   -> distinct
```

Retaining `Project-Doc.md` 6.10 verbatim in spirit: **never auto-merge inside the
ambiguity band.** Auto-merging is cheap to build and expensive to undo once BD has
already contacted the wrong branch. Both failure directions cost you ,  false
negatives fragment one institution across two rows; false positives silently
delete a lead.

Start conservative. If the review queue proves consistently boring at the top of
the band, raise the auto-merge floor with evidence from the reviewed decisions , 
and record that change in `docs/DECISIONS.md`.

### Hard blockers ,  never merge, regardless of score

These override the score entirely:

- Different `pincode` **and** different `city` ,  different physical places.
- Both records have a `udise_code` and they differ.
- Both records have a `cbse_affiliation_no` and they differ.
- A `merge_decisions` row already says `different`.
- Names differ only by a **branch discriminator**: a distinguishing token from
  `{north, south, east, west, main, annexe, annex, sector, phase, campus,
  ii, iii, 2, 3, junior, senior, primary, pre-primary, boys, girls}` plus a
  differing PIN. This is exactly `Project-Doc.md` 6.10's false-positive case , 
  two real branches merged into one, silently losing a lead.

---

## Stage 5 ,  Group resolution

Two axes, per ADR-005. `Project-Doc.md` 6.6 had the right idea and an
unimplementable mechanism; this is the implementable version.

### Axis A ,  legal entity (authoritative)

`legal_entity_norm`, derived from the CBSE Trust/Society name. Normalisation:
lowercase, strip punctuation, drop `trust`, `society`, `educational`, `education`,
`charitable`, `foundation`, `samiti`, `sangha`, `sabha`, `registered`, `regd`,
`the`; sort tokens.

Two institutions with the **same** `legal_entity_norm` are the same owner.
`group_type = 'verified_single_owner'`. This is a government-published fact, not
an inference.

### Axis B ,  brand

`match_key` prefix overlap plus a shared website domain. Weaker.

### The decision table

| Legal entity | Brand | Result |
|---|---|---|
| same | same | `verified_single_owner`. Merge into one group. Decision-maker at group level. |
| same | different | `verified_single_owner`. Merge ,  one trust running differently-branded institutions is common and real. |
| different | same | **`possible_franchise_network`.** Do **not** merge ownership. Keep decision-maker data at branch level. Capped at 1 scoring point. Flagged in the UI. |
| unknown | same | `possible_franchise_network`. Same treatment. |
| different | different | Unrelated. |

The `different legal entity, same brand` row is the one that protects the
product's credibility. `[EXTERNAL-FACT]` "Delhi Public School" is a
society-licensed model, not a single owner ,  treating it as one chain both
overstates scale and sends BD to a contact with no authority over the branch.
Same for "St. Mary's" and every other generic name.

### Coaching and PU chains ,  the easy case

`[EXTERNAL-FACT]` Narayana (950+ institutions, 250+ cities), Deeksha (50
campuses), BASE and others publish first-party campus directories. For these,
group membership is an **authoritative import**, not a match:
`group_type='coaching_chain'`, `evidence` = the directory URL. No fuzzy matching
at all. ADR-008.

A chain that has **fragmented** (dispute or split) surfaces as a
`review_queue` conflict when the directory and the trust field disagree ,  never a
silent merge. `Project-Doc.md` 6.6 flagged this case and it is retained.

---

## Stage 6 ,  Person resolution

Deliberately shallow, for two reasons: person matching is genuinely hard, and
under-merging people is cheap while over-merging is a data-protection problem
(ADR-006).

Match a person only on **exact `normalized_name` within the same institution or
group.** Do not match people across institutions ,  "Sunita Sharma" is not one
person nationally, and asserting she is creates a false career history for a real
named individual.

A principal who genuinely moves institutions therefore appears as two `people`
rows. That is the correct trade-off: nothing downstream needs a unified career
history, and a wrong one is worse than none.

Role lifecycle:

- A new `principal_name` observation for an institution that differs from the
  current one closes the old role (`effective_to = observed_at`), opens a new one,
  and emits a `principal_change` signal (ADR-002).
- `verified_at` updates on every re-observation of the **same** name ,  that is
  what keeps a stable principal from drifting into `stale_leadership`.
- A role unverified for > 12 months raises `stale_leadership` and scores 0 for
  accessibility, retaining `Project-Doc.md` 6.8.

---

## Testing requirements

ER is the component where a silent bug is most expensive, so it carries the
heaviest test burden.

`tests/unit/test_normalize.py` ,  table-driven, and must include at minimum:

```
"DPS Bangalore" / "Delhi Public School, Bengaluru"      -> same match_key
"St. Mary's High School" / "Saint Marys School"          -> same match_key
"Gurgaon" / "Gurugram"                                   -> same city
"Sri Chaitanya Jr College" / "Sri Chaitanya Junior College" -> same match_key
"Rs. 1.85 lakh" / "1,85,000" / "INR 185000"              -> 185000
"Rs 15,000 per month" (explicit)                         -> 180000
"Rs 15,000" (period ambiguous)                           -> None
```

`tests/unit/test_match.py` ,  must assert the hard blockers fire:

```
"ABC School North" (PIN 560001) vs "ABC School South" (PIN 560078) -> NOT merged
same name, differing udise_code                                     -> NOT merged
same name, same PIN, no conflicting IDs                             -> merged
mid-band pair                                                       -> review_queue, not merged
```

`tests/unit/test_groups.py` ,  must assert the franchise case:

```
same brand, different legal_entity_norm -> possible_franchise_network, NOT merged
same legal_entity_norm, different brand -> verified_single_owner, merged
```

`tests/integration/test_rebuild.py` ,  the four rebuild properties from
`docs/DATA-MODEL.md`: deterministic, override-preserving, merge-preserving,
non-destructive.
