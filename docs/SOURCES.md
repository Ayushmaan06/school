# Source Registry

Every data source, what it gives, what it costs, how often to refresh, and its
legal footing. **A source not listed here must not be fetched.** Adding one
requires an ADR entry and a compliance check.

Verification status as of 2026-08-22. `[VERIFIED]` means I fetched it or
confirmed it against a live source; `[UNVERIFIED]` means the source is known to
exist but its exact export shape still needs confirming at implementation time , 
budget an hour of exploration for each of those before writing the importer.

---

## S1. `cbse_saras` ,  CBSE affiliated-school registry

**Tier 1 (government/statutory). Free. Refresh: 30 days. The backbone of the
system.**

`[VERIFIED]` List: <https://saras.cbse.gov.in/SARAS/AffiliatedList/ListOfSchdirReportNew?ID1=D>
,  filterable by state, district, region, and school level; also serves the
disaffiliated/closed list.
Detail: `https://saras.cbse.gov.in/SARAS/AffiliatedList/AfflicationDetails/{affiliation_no}`

`[VERIFIED]` fields on the detail page, and the observation `field` each maps to:

| Page label | `observations.field` | Notes |
|---|---|---|
| Institution Name | `name` | |
| Affiliation Number | (entity key `cbse:{n}`) | Stable national unique ID |
| State / District | `state` / `district` | |
| Postal Address | `address` | |
| Pin Code | `pincode` | Drives geocoding (ADR-004) |
| Website | `website` | **Seeds S2.** Frequently blank or stale ,  see S8 |
| Year of Foundation | `year_founded` | |
| Principal/Head Name | `principal_name` | Statutory publication, best legal footing for person data |
| Principal Qualifications | ,  | Not stored; no decision value, and unnecessary personal data |
| School Status | `grade_high`, `has_class_12` | **"Senior Secondary Level" => has_class_12=true. This is the hard gate.** |
| School Type | `residential` | Values seen include INDEPENDENT; map cautiously, keep raw |
| Gender | ,  | **Trap: this is the PRINCIPAL's gender, not the school's.** It sits directly under the principal name. Do not map it to a school co-ed/boys/girls field. Not stored. |
| Date of First Opening of School | `date_first_opened` | Undocumented previously; present on every sampled page |
| Affiliation Period | `board_valid_from`, `board_valid_to` | |
| Trust/Society Name | `legal_entity_name` | **The legal-entity axis for group resolution (ADR-005)** |

Implementation notes:

- **33,147** affiliated schools nationally `[VERIFIED 2026-09-05]`.
- **Only ~29% are Senior Secondary Level** ,  the earlier "roughly one third are
  Middle or Secondary only" had this backwards. Measured on Karnataka:
  Secondary 1,288 / **Senior Secondary 539** / Middle 20, out of 1,847.
  **Filter them out at import**, before enqueuing any detail or site fetch
  (ADR-007). This is the single largest cost saving in the pipeline, and it is
  roughly twice as large as previously assumed: the national target universe is
  **~9,600 institutions, not ~19,000**.
- The list page is server-rendered HTML with a large table (~479KB for one
  category) ,  parse with `selectolax` or `lxml`, not a browser.
- Detail pages exposed no email or phone in the verified sample. Contacts come
  from S2.
- `[VERIFIED]` The closed/disaffiliated list is a separate category on the same
  endpoint. Import it too ,  it drives `status` and the `disaffiliation` signal.

**Snapshot this source every run.** It is the input to stage 8 signal diffing
(ADR-002) and the only reliable detector of new affiliations, level upgrades, and
principal changes.

**Import scope is state-first.** Karnataka, Tamil Nadu and Maharashtra by default
(`IMPLEMENTATION-PLAN.md` M1-1). Geography weighting is anchored on a single
Bengaluru campus, so a national import spends ten hours of polite gov.in fetching
on rows scoring 3/15. Widening the state list is a config change.

### M0-0 findings ,  `[VERIFIED 2026-09-05]`

| Question | Answer |
|---|---|
| `robots.txt` | **Does not exist** (404). No crawl restriction on any path. |
| Bot protection | Akamai-style fingerprinting JS (`x-bni-fpc`, FingerprintJS) is injected on every page. A plain `httpx` client with a normal UA gets full HTML ,  **no Playwright needed, ADR-012 holds** ,  but see the rate-limit finding below. |
| Pagination | **None.** The whole result set is one response, rendered client-side by DataTables. Karnataka = 2.37MB / 1,847 rows in a single POST. |
| Stated total vs parsed rows | Matches exactly (1,847 = 1,847). Keep the assertion anyway. |
| National affiliated total | **33,147**, not ~29,000. Closed/disaffiliated: 491. |

**The affiliated list is a POST, not a GET.** This is the single biggest
correction to this section:

```
GET  /SARAS/AffiliatedList/ListOfSchdirReportNew?ID1=D   -> the CLOSED list only (491 rows)
POST /SARAS/AffiliatedList/ListOfSchdirReport            -> the affiliated list
```

The POST needs an `__RequestVerificationToken` hidden field harvested from a
prior GET, sent back with the `.AspNetCore.Antiforgery.*` cookie from that same
GET. So the importer holds a session, not a stateless fetch. Form fields:

| Field | Value |
|---|---|
| `MainRadioValue` | `State_wise` (also `Region_wise`, `School_level_wise`, `Keyword_wise`, `Affi_No_wise`, `Disaffiliated_schools`) |
| `State` | **numeric code**, not a name ,  KA=`8`, MH=`11`, TN=`19`, DELHI=`27` |
| `SchoolStatusWise` | `0` = all; the select offers Middle Class / Secondary Level / **Senior Secondary Level** |
| `District`, `Region`, `InstName_orAddress` | optional narrowing |
| `RegiAffNo`, `__Invariant` | `0` / `RegiAffNo` ,  required by the model binder |

**The hard gate is a server-side parameter.** `SchoolStatusWise` can request
Senior Secondary Level directly, so the level filter need not be a post-parse
step at all. Prefer the server filter and assert the count client-side.

**The state-wise list carries far more than documented.** Its columns are
`S No | Aff. No & School Code | State & District | Status | School & Head Name |
Address | Details` ,  meaning **name, affiliation number, school code, state,
district, level, principal name, full address and website all arrive on the list
page.** The detail page adds only: `year_founded`, `date_of_first_opening`,
`Trust/Society name`, `Affiliation Period`, `School Type`, and principal
experience. Detail fetches are therefore an **enrichment** step over the gated
subset ,  needed for ADR-005 group resolution (trust/society) ,  not the primary
parse. Budget them accordingly.

**Website coverage is 99.1%** on Karnataka senior-secondary rows (534/539). S8
Serper gap-fill is close to unnecessary; see S8.

### `[VERIFIED M1-3]` The detail endpoint serves a CAPTCHA wall under load

Draining 539 queued detail fetches at **0.5 rps** succeeded for roughly the
first 150 and then returned this for every subsequent request:

```html
<title>Validation request</title>
<h3>User validation required to continue..</h3>
Please type the text you see in the image into the text box and submit
<img src="/captcha.gif">
```

Served with HTTP 200 **or** 404, about 1KB, from the same infrastructure as the
fingerprinting script. Final yield: **124 of 539 detail pages (23%)**.

Consequences, all now implemented:

- **`rate_limit_rps` for `cbse_saras` is 0.1**, not 0.5. The list endpoint is
  ~38 requests for the whole country and never came close to the limit; it is
  the per-institution detail sweep that trips it.
- **`fetch/client.py` detects interstitials and never stores them.** A bot check
  is not the URL's content, and storing it would let a later extraction pass
  mistake it for one. The `fetches` row records `error='interstitial: ...'` so
  the decision stays auditable.
- **A circuit breaker stops a domain after 3 consecutive interstitials.**
  Grinding through the rest of a queue against a wall is both useless and rude.
- **Detail enrichment is inherently partial and slow.** At 0.1 rps a national
  sweep of ~9,600 senior-secondary schools is ~27 hours of wall-clock, so it
  belongs on the scheduler across several nights rather than in one run.

**This does not threaten the pipeline**, because ADR-017 already put the value
on the list page: name, principal, address, website and the class-12 gate all
arrive from ~38 POSTs. The detail page adds `pincode`, `legal_entity_name`,
`year_founded` and the affiliation period. Of those only `pincode` feeds
scoring, via `metro_area`.

Nothing was corrupted by the 415 failed fetches: label-driven parsing plus hard
rule 6 meant the captcha pages produced **zero** observations rather than
garbage. That is the guard working.

---

## S2. `cbse_mpd` ,  Mandatory Public Disclosure on the school's own website

**Tier 3 (institution self-published). Free. Refresh: 180 days. The only source
of fee data.**

`[VERIFIED]` CBSE requires every affiliated school to publish Appendix-IX on its
own website ,  school email and phone, **class-wise student strength**,
**fee structure**, staff counts, trust/society registration certificates. Revised
proforma compliance deadline was 2026-02-15, so coverage should be good and
improving.

Discovery: from `website` (S1), try in order and stop at the first hit:

```
/mandatory-disclosure  /mandatory-public-disclosure  /cbse/mandatory-disclosure
/public-disclosure     /disclosure                   /cbse-mandatory-disclosure
/mandatory_disclosure  /appendix-ix
```

then the homepage, looking for an anchor whose text matches
`/mandator|disclosure|appendix.?ix/i`. Cap at 6 fetches per institution. Many
schools publish it as a **PDF** ,  handle `application/pdf` with `pdfplumber`,
which preserves the table structure that fee and strength data live in. Do not
flatten a PDF to plain text; the layout *is* the information.

**Extraction is deterministic** (ADR-013). Appendix-IX is a standardised statutory
proforma with numbered rows and fixed labels, so tier 1 finds the cell matching a
known label and reads its neighbour; tier 2 applies patterns where the page is
free-layout. Label synonyms live in `extract/mpd/labels.yaml` and will need
extending as non-conforming pages turn up ,  that file is the main maintenance
surface for this source.

Fields extracted:

| `observations.field` | Notes |
|---|---|
| `fee_annual_inr` + `fee_component_breakdown` + `fee_year` | **Highest-value field in the system** (ADR-011). Store as a range: tuition alone is the min, all mandatory annual components the max. Exclude one-off admission fees and refundable deposits. |
| `class_12_total`, `pcm_12_count`, `pcm_12_count_year` | From class-wise strength. If only a class-12 total is given with no stream split, record `class_12_total` only ,  **do not guess the PCM share here**; estimation happens in scoring with an explicit flag. |
| `email`, `phone` | Institutional contact. Never an individual's personal mobile. |
| `principal_name` | Usually fresher than S1 ,  wins for this field (see priority table) |
| `counsellor_name`, `counsellor_title` | Often absent, high value when present |
| `streams` | Science / Commerce / Arts availability |
| `total_enrollment` | Secondary to UDISE |

### `[VERIFIED 2026-09-05]` The fee is a LINK, not a value ,  this changes M2

The single most important M0-0 finding. On the real Appendix-IX proforma the fee
does not appear on the MPD page at all. Section **`C: RESULT AND ACADEMICS`**
renders as a table of label/link pairs:

```
SL No. | DOCUMENTS/INFORMATION                   | LINKS OF UPLOADED DOCUMENTS
1      | FEE STRUCTURE OF THE SCHOOL             | View            -> .../Fee-Structure-for-2024-25.pdf
2      | ANNUAL ACADEMIC CALENDER                | View
3      | LIST OF SCHOOL MANAGEMENT COMMITTEE     | View
```

Verified identically on `rcis.in`, `vvi.edu.in` and others. Consequences:

1. **Discovery must not stop at the MPD page.** "Stop at the first page whose
   text contains two of fee / student strength / affiliation / trust" lands on an
   index and extracts nothing. A v2 probe that stopped there measured fee
   keywords on 9 of 10 pages and an INR figure on **0 of 10**.
2. **The chain is three hops:** `homepage -> MPD index -> fee-structure PDF`.
   Reserve budget for it inside the 6-fetch cap.
3. **The anchor text is generic** ,  "View", "Click here to view". The *row label*
   is what identifies the link, so tier 1 finds the cell matching a
   `fee_structure` label and takes **the href in that same row** rather than the
   neighbouring text. Still deterministic; ADR-013 holds. `labels.yaml` needs a
   `fee_structure_link` entry alongside `fee_annual_inr`.
4. **Class-wise strength usually IS inline** on the MPD page, so strength and fee
   come from two different documents and two different extractor passes.

**Compliance trap on this exact page.** The section is titled "RESULT AND
ACADEMICS" and its sibling rows link to board-result documents. The denylist is
per-URL and must stay that way: it has to block *following the result links*
while still allowing the fee link in the row above it. **Do not extend the
denylist to page text** ,  the literal word "RESULT" appears in the proforma
heading of every compliant MPD page, and a text-level rule would blocklist the
entire source. `docs/COMPLIANCE.md`.

### `[VERIFIED 2026-09-05]` Measured coverage ,  140 Bengaluru senior-secondary schools

| Hop | Result |
|---|---|
| Website reachable | **81%** (114/140). Failures are dead domains, uppercase hostnames, and `http://`-only legacy sites ,  a browser-grade client (redirects, https upgrade, `www` toggle) recovers most |
| MPD index page found | **45%** (63/140), 56 HTML / 7 PDF |
| Fee-structure link resolved from it | **44** of those 63 |
| Class-wise strength inline on the index | **26%** (37/140) |

Of the 44 resolved fee documents:

| Fee document type | Count | Readable deterministically? |
|---|---|---|
| PDF, **scanned image, no text layer** | **13 (30%)** | **No. `pdfplumber` returns 0-1 chars.** |
| HTML fee page | 18 | Yes |
| PDF with a text layer | 11 | Yes |
| Link resolved to the index page itself | 2 | Parser bug, fixable |

**The 30% scanned share is the one number that is not a parser problem.** No
label parser, no regex and no text LLM can read an image. Everything else in the
table above is ordinary extraction work that M2-2 is already scoped to do.

**Do not require a currency marker.** The single largest cause of the crude
probe's 5% end-to-end figure was an `INR` regex demanding `Rs`/`₹`/`/-` next to
the digits. Real fee tables put the currency in a column header and the figures
bare:

```
SCHOOL FEE FOR 2023-2024   GRADE   SCHOOL FEE
Pre-KG 62020   LKG 61830   ...   11 PCMC 73880   11 PCMB 74300
                                 12 PCMC 74080   12 PCMB 74510
```

**And take the class-11/12 science row when it exists.** That example is better
data than this document previously assumed was available: the fee is broken out
**per grade and per stream**, so `12 PCMC` / `12 PCMB` is the exact figure this
ICP needs, not a school-wide average. Prefer, in order: class 12 PCM/PCB row ->
class 12 any row -> class 11 -> highest grade published. Record which was used in
`evidence_span`; a class-1 fee presented as the class-12 fee is a wrong value,
not a missing one.

**Counter-example, and why hard rule 6 exists.** A fee page that is really just a
nav shell yields `560076` (a PIN code) and `9483338135` (a phone number) as its
only 5-6 digit numbers. "Take the first number near a fee keyword" would emit a
PIN code as an annual fee. Match the label, or emit nothing.

### `[MEASURED M2-4]` Actual coverage over 449 real Karnataka institutions

The estimate above was ~30%. **The measured figure is 10.9%.** Full run, all
non-government senior-secondary Karnataka schools with a website:

| Outcome | Count | Share |
|---|---|---|
| Considered | 449 | |
| MPD page found | 129 | **29%** |
| ...of which reached a fee document | 107 | |
| ...index found but no fee link | 22 | |
| Login wall | 10 | 2% |
| Nothing found | 309 | 69% |
| **Fee extracted** | **49** | **10.9%** |
| of those, from OCR (ADR-016) | 9 | 18% of all fees |
| Fee document found but unreadable | 19 | |
| Contacts (email/phone) extracted | 121 | 27% |
| Class-12 strength extracted | **0** | |
| Student-data documents blocked | 2 | |
| OCR fees routed to review | 5 | |

**Why lower than M0-0's 45% discovery.** That probe sampled Bengaluru schools;
this is all of Karnataka, including small-town schools with weaker websites.
A metro-only campaign will do better than 10.9%, and the per-district number
is the one to plan a campaign around rather than the state average.

**OCR earned its place.** 9 of 49 fees came from scanned documents that no
parser could read. ADR-016 is carrying about a fifth of all fee coverage.

**19 fee documents were found and could not be read even after OCR.** These are
in `review_queue` as `unreadable_fee_document` - a known, addressable backlog
rather than a silent gap.

### `[MEASURED M2-4]` Class-wise strength is ALSO a link, not a value

`strength_found` was 0, and that is a data-shape finding rather than a parser
bug. Of 17 index pages mentioning strength, **15 link out to a separate
"Student Strength Details" page** and only 2 carry numbers inline. Scanning 120
stored documents found 2 strength tables and **zero** class-12 rows in them.

This is exactly the fee finding repeated: the Appendix-IX index links to its
documents rather than containing them. Discovery currently resolves only the
fee link, so a fourth hop is needed for strength. The budget allows it -
homepage + index + fee + strength is 4 of the 6 permitted fetches.

**This matters more than the fee gap in one respect:** PCM-12 volume is 25 of
100 Fit points, the second-largest component, and it is currently unobtainable
for essentially every institution. Until the strength hop exists, scoring will
fall back to estimating PCM from `total_enrollment` with the `pcm_estimated`
flag and the 0.6 multiplier - which is what ADR-010 designed for, but it should
be a fallback rather than the normal case.

**Compliance gate.** This source fetches arbitrary school websites, so it is the
one that must obey `fetch/denylist.py` most strictly. Result PDFs, merit lists,
toppers' lists, and admission lists are common on these sites and are hard-blocked
(ADR-006, `docs/COMPLIANCE.md`).

---

## S3. `cisce` ,  CISCE affiliated schools (ICSE / ISC)

**Tier 2 (board/official). Free. Refresh: 90 days.**

### M0-CISCE findings ,  `[VERIFIED 2026-09-15]`

The list lives on a **separate host from `cisce.org`**: the School Locator at
`https://locate.cisce.org/`. `cisce.org` itself publishes no school directory , 
its only relevant link is to this locator.

| Question | Answer |
|---|---|
| `robots.txt` | Present on both hosts, Cloudflare-managed. `User-agent: *` -> `Allow: /` (only `/wp-admin/` disallowed on `cisce.org`). Named AI crawlers (`GPTBot`, `CCBot`, `ClaudeBot`, `Google-Extended`, ...) are `Disallow: /`. **We are not any of those**; our own UA falls under `*`. A `Content-Signal: ai-train=no, use=reference` header is set ,  we do not train on it, and we cite the source, so both are satisfied. Record this in `tos_note`. |
| Bot protection | Cloudflare. A bare `Mozilla/5.0` UA drew an intermittent 403; the project UA and a full browser UA both got 200. No JS challenge, no CAPTCHA seen. |
| Rendering | **Server-rendered HTML.** DataTables is used with `paging/searching/ordering/info` all `false` ,  it is decoration. Laravel + Blade behind it. |
| Total rows | **3,334** across 334 pages (all countries, all affiliations). |
| **ISC rows, India** | **1,928** across 193 pages. |

**The endpoint is a GET, and the CSRF token is not required.**

```
GET https://locate.cisce.org/?country=India&affiliation=ISC&page={n}
```

The on-page form declares `method=POST` to `/result` and carries a Laravel
`_token`, but **POST returns 405** ,  the route is GET-only. Verified that
`page=2` and `page=5` return the correct slices with no `_token` and no cookie.
Ten rows per page, fixed; no page-size parameter was found.

**Use the root route, not `/result`.** The importer's first live run fetched
nothing: the ADR-006 PII denylist blocks any URL containing `result`, which is
the pattern that stops us ever requesting a student result page (hard rule 1).
`/result` and `/` serve **identical** bytes ,  `[VERIFIED 2026-09-15]` both report
`of 1928 entries` and return the same page-3 slice (`AP055, AP067, AP071`) for
the same query string. Routing round the gate costs nothing, so the denylist
stays untouched. If the root route ever stops accepting the filters, the answer
is still not a bypass.

Form fields, all optional, all exact-string match against the dropdown values:

| Field | Values |
|---|---|
| `country` | `India`, `United Arab Emirates`, `Indonesia`, `Singapore`, `Thailand` |
| `state` | free string, populated per country by `GET /getState/{country}` (JSON) |
| `affiliation` | `ICSE`, `ISC`, `CVE` |
| `gender` | `Boys`, `Girls`, `Co-ed.` (note the trailing dot) |
| `duration` | `day`, `day_boarding`, `day_residential`, `residential` |

**The hard gate is a server-side parameter**, exactly as with SARAS
(`SchoolStatusWise`). `affiliation=ISC` returns only schools that run class 12.
Request it directly; assert the `Showing 1 to 10 of {N} entries` count
client-side. **This is the whole import: 193 GETs.** Do not sweep all 334 pages
and filter locally, and do not assume ICSE implies ISC ,  3,334 minus 1,928 says
roughly 42% of CISCE schools stop at class 10.

**Everything is on the list page.** There is no detail page. One `<td>` per
school holding a `.school-card`:

| Card element | `observations.field` | Example |
|---|---|---|
| `.top-right-badges` badge 1 | `gender` | `Co-ed.` |
| `.top-right-badges` badge 2 | `residential` | `Day`, `Day/Residential` |
| `h6.fw-bold`, before the ` - ` | (entity key `cisce:{code}`) | `AN001` ,  state-prefixed, nationally unique |
| `h6.fw-bold`, after the ` - ` | `name` | `Don Bosco School` |
| `p.mb-1` line 1 | `principal_name` | `Fr. BHARATHRAJA C` |
| `p.mb-1` line 2 | `address`, `district`, `state`, `pincode` | `PORT BLAIR, South Andaman, Andaman and Nicobar Islands, 744103, India` |
| `.school-badges` | `board` | `ICSE` and/or `ISC` and/or `CVE` -> `CISCE_ICSE`, `CISCE_ISC` |
| `.text-end a` | `website` | often absent ,  **seeds S2** |

The address is a single comma-joined string ending `{pincode}, India`. Parse it
from the right ,  pincode, country ,  and do not assume a fixed field count on the
left; locality and district are sometimes merged. Pincodes appear both as
`744103` and `533 201`; normalise the space out.

`[VERIFIED]` No email or phone anywhere on the locator. Contacts come from S2 , 
the CBSE MPD proforma does not apply to a CISCE-only school, so expect the
generic homepage mining path (`cbse_mpd.py` discovery) to do the work.

**Google Maps is embedded on the locator page.** Hard rule 2 still binds: take
the address text, never a Maps or Places response. Geocode via S9 PIN centroids
like everything else.

`[EXTERNAL-FACT]` A 2018 open dataset of 2,341 CISCE schools exists at
`github.com/deedy/cisce_schools_data` (16 fields). **Useful as a cross-check and
for building the eval gold set; far too stale to import as canonical.** Same
author's `deedy/cbse_schools_data` has 20,367 CBSE schools ,  same caveat.

### `[VERIFIED 2026-09-15]` Cloudflare challenges our HTTP client

The first live run fetched **nothing**: every request returned `403` with
`cf-mitigated: challenge` and a "Just a moment..." body ,  the current Cloudflare
JS interstitial.

It is not the User-Agent, the headers, the rate, or the IP. Measured, same UA,
same machine, same minute:

| Client | Result |
|---|---|
| `httpx`, HTTP/2 | 403 challenge, every page, every attempt |
| `httpx`, HTTP/1.1 | 403 challenge |
| `httpx` + full browser `Accept` / `Accept-Language` / `Accept-Encoding` | 403 challenge |
| `curl`, identical UA | **200**, every page, every attempt |

The discriminator is the TLS/client fingerprint. Cloudflare scores `httpx`'s
handshake as automated and `curl`'s as not. Note what this means: the challenge
is **not** an access decision by CISCE. `robots.txt` allows us, the data is
public, and a bare `curl` with our own honest UA is served it on request.

**Resolution: this domain is fetched with `curl`.**
`fetch/client.py` carries a `CURL_TRANSPORT_DOMAINS` set, and `cisce.org` is in
it. The swap happens at the very last step of `Fetcher.fetch`, *after* the
denylist, the circuit breaker, `robots.txt` and the rate limiter, and the result
comes back as an `httpx.Response` so nothing downstream can tell the difference.
There is no path to `curl` that skips a gate.

This is a transport swap, not an impersonation. We keep announcing ourselves as
`TensorSchoolIntel/0.1 (+internal BD research; contact: bd@tensor.school)`, we
obey the same `robots.txt` that explicitly allows us, and we hold the same 1 rps.
CISCE can disallow us by name whenever they like ,  which is precisely why
announcing ourselves honestly matters more than which TLS library we use.

**Not done, and not to be done:** `curl_cffi` / `curl-impersonate` TLS
impersonation, or a headless browser. The first is deliberate evasion; the second
is ADR-012's rejected Playwright dependency arriving through the back door. A
domain that needs either gets dropped as a source instead, with the reason
recorded here.

Worth asking CISCE for a direct export anyway. It costs an email, it removes the
fingerprint dependency entirely, and 193 quarterly GETs are not the kind of load
anyone minds being asked about.

Side effect already fixed: `fetch/client.py` did not recognise this challenge , 
its markers were the 2019-era `Checking your browser` wording. Any Cloudflare-
protected school site was scoring as a plain 403, so nothing was stored (correct)
but the circuit breaker never fired (not correct). `is_interstitial` now reads
`cf-mitigated` and the current body markers.

---

### `[VERIFIED 2026-09-15]` Adding a registry means editing the priority table

The first successful import resolved 1,928 institutions with the correct board
and a **null address, pincode, state, district, website, principal and gender**.
Nothing had failed: `resolve()` drops any candidate whose `source_id` is absent
from that field's `FIELD_PRIORITY` tuple, and the table still read as though CBSE
were the only registry ,  `cisce` appeared under `name`, `institution_type` and
the four board fields and nowhere else. The observations were all written and all
discarded, each with a recorded reason, visible one institution at a time on
`/institutions/{id}/why` and invisible in aggregate.

`cisce` now sits directly after `cbse_saras` in every field the locator
publishes. **Every future importer must do the same edit in the same commit** , 
M3-2 (IB), M3-3 (Cambridge) and M3-4 (UDISE) all currently rank for `name` and
their board fields and nothing more, so each will reproduce this exactly. A test
in `tests/unit/test_cisce_parser.py` asserts that every field `cisce` emits also
ranks `cisce`; copy it per source.

---

### Live figures ,  `[VERIFIED 2026-09-15]`

```
1,928 ISC institutions imported over 193 pages
   20,161 observations from 194 stored documents
  Parsed row count == the locator's own stated total, exactly
```

Fixtures for the importer: `tests/fixtures/cisce/locate_index.html` (dropdown
vocabularies) and `tests/fixtures/cisce/locate_result_isc_india_p1.html`.

---

## S4. `ib` ,  IB World Schools in India

**Tier 2. Free. Refresh: 90 days. Highest value density in the system (ADR-007
Tier A).**

`[UNVERIFIED]` IB's official school finder at `ibo.org`. `[EXTERNAL-FACT]`
~245-270 IB World Schools in India, up from 11 in 2003.

Capture which programmes each school runs ,  `IB_DP` (classes 11-12) is the one
that matters. `IB_PYP`/`IB_MYP` only means the school stops before class 11 and
fails the hard gate.

Small list, so **hand-verify all of it.** ~250 rows is an afternoon, and these are
your best leads. Do not let a fuzzy importer decide anything here.

---

## S5. `cambridge` ,  Cambridge (CAIE) schools in India

**Tier 2. Free. Refresh: 90 days. ADR-007 Tier A.**

`[UNVERIFIED]` Cambridge International's school directory. `[EXTERNAL-FACT]`
~550-600 schools offering IGCSE.

`CAIE_ALEVEL` / `AS Level` implies classes 11-12. `CAIE_IGCSE` alone tops out
around class 10 (ages 14-16) ,  same trap as ICSE-without-ISC. Check for A-Level
explicitly.

---

## S6. `udise` ,  UDISE+ / Know Your School

**Tier 1 (government). Free. Refresh: 365 days (annual reporting cycle).**

`[VERIFIED reachable]` `kys.udiseplus.gov.in` responds and is an Angular SPA over
JSON endpoints. `[EXTERNAL-FACT]` A commercial reseller has productised those
endpoints (8 endpoints: academic years, states, districts, blocks, categories,
managements, region search, school details returning profile / facility /
statistics / report_card), which confirms they are stable and documents their
shape. `[UNVERIFIED]` The exact endpoint URLs ,  the SPA lazy-loads its chunks and
I did not extract them. **Budget time to capture them from browser devtools
before writing this importer.** data.gov.in also carries UDISE+ (my fetch
returned 403; catalogue contents unconfirmed).

Use it for what only it has:

| `observations.field` | Notes |
|---|---|
| `udise_code` | The national unique school ID. **Primary cross-registry linkage key** (ADR-014) |
| `management_type` | Government / Govt-aided / Private unaided / Central Govt ,  UDISE's own taxonomy, do not invent one |
| `medium` | Medium of instruction |
| `total_enrollment`, `enrollment_year` | Mandatory annual reporting; **authoritative for total enrollment** |
| `grade_low`, `grade_high` | Confirms the class-12 gate independently of S1 |

Deliberately **not** used for: fees (absent), principal (staler than S1/S2), or
discovery of Tier A/B institutions (S1 and S3-S5 are better). Its ~1.5M-school
universe is mostly Tier D (ADR-007) and is not imported in v1.

**Politeness matters here.** Government infrastructure, and `Project-Doc.md` 6.12
was right: rate-limit per domain, not just globally. Default 1 rps, back off hard
on any 5xx.

---

## S7. `group_directory` ,  coaching and PU group campus directories

**Tier 3 (first-party). Free. Refresh: 90 days. ADR-008.**

`[VERIFIED]` These groups publish their own campus lists, which makes group
resolution an authoritative import rather than fuzzy brand matching:

- Narayana ,  <https://narayanagroup.com/centers> ,  `[EXTERNAL-FACT]` 950+
  institutions, 250+ cities, 23 states
- BASE ,  <https://baseedu.in/centers.php> (and `associated_centers.php`)
- Deeksha Vedantu ,  `[EXTERNAL-FACT]` 50 campuses, 3 states
- Sri Chaitanya, Allen, Aakash, FIITJEE ,  `[UNVERIFIED]` directory shapes

One importer, driven by a per-group config (base URL, row selector, field map)
rather than one module per brand. Groups get `group_type='coaching_chain'` and
`evidence` set to the directory URL.

**Filter to the affordability floor.** A Narayana branch charging Rs 30k/yr is Tier
D. Only campuses passing the fee gate become qualifying campuses, and
`groups.qualifying_campus_count` ,  not `campus_count` ,  drives chain leverage in
scoring.

---

## S8. `serper` ,  search API, website gap-fill only

**Tier 4. ~$1/1K queries. On demand. Narrowest possible scope.**

`[EXTERNAL-FACT]` Serper ~$1/1K (2,500 free); Brave $5/1K; Exa ~$5-7/1K; Tavily
$8-16/1K. `[EXTERNAL-FACT]` **Bing Search APIs were retired 2025-08-11** ,  do not
write code against them.

**`[VERIFIED 2026-09-05]` This source is very nearly unnecessary.** 99.1% of
Karnataka senior-secondary rows (534/539) carry a website on the SARAS list page
itself. Build S8 **last, or not at all** ,  implement the `no_website` flag first
and only reach for a paid search API if the gap turns out to matter on the real
corpus. Five schools out of 539 is not worth an API key, a budget cap and a spend
log. This is a promotion trigger, not a v1 task.

**Permitted use: exactly one.** Finding the official website of an institution
whose registry record has no usable `website`. Query shape:
`"{name}" {city} {state} school official website`. Accept a result only if the
domain plausibly matches the institution name, else leave `website` null and set
the `no_website` flag.

**Not permitted:** discovering institutions, gathering facts from snippets,
finding news, or as a general fallback when a parser fails. Facts come from
S1-S7; a search snippet is not a source of record.

---

## S9. `pin_centroids` ,  PIN code geography

**Tier 1. Free. Load once.**

India Post PIN code dataset (also on data.gov.in). Gives PIN -> lat/lon, district,
state, and an office-type hint. Used for `lat`/`lon` with
`geo_precision='pin_centroid'`, and to cross-check the district/state parsed from
S1 addresses. ADR-004.

Also holds the `metro_area` mapping ,  a PIN-to-metro table, editable config, not
hardcoded logic. `Project-Doc.md` 6.2 was right that NCR-style cross-state metros
need a first-class field; the four-region enum in 6.1 is dropped as useless for
this ICP (see `docs/SCORING.md` geography).

---

## Excluded sources

| Source | Why excluded |
|---|---|
| **Google Places / Maps** | `[EXTERNAL-FACT]` ToS permits storing only `place_id` indefinitely and coordinates for 30 days; all other content must not be warehoused. A permanent DB built on it is a terms violation, not a cost trade-off. ADR-003. |
| **Bing Search API** | Retired 2025-08-11. |
| **News APIs / GDELT** | Deferred with the signal pipeline. ADR-002. GDELT is free and keyless if it is ever revived. |
| **NewsAPI** | Free tier forbids commercial use; paid tier starts around $449/mo. Poor value even if signals return. |
| **Any student result / merit / toppers / admission list** | Hard-blocked. ADR-006, `docs/COMPLIANCE.md`. |
| **Third-party school directories** (schoolmykids, edustoke, careers360, ...) | Tier 4, unlicensed re-publication, unknown freshness, and frequently wrong. May be consulted **by a human** when building the eval gold set. Never imported. |

---

## Conflict resolution ,  per-field source priority

Implemented in `resolve/conflict.py`. First source in the list that has a live
observation wins. Ties within one source: newer `observed_at` wins. **The loser is
never deleted** ,  it stays in `observations` and is shown by
`/institutions/{id}/why`. This implements `Project-Doc.md` 6.11 literally,
including its correct carve-out that a school's own site beats a stale government
dataset for leadership.

| Field | Priority order |
|---|---|
| `name` | `cbse_saras` > `cisce` > `ib` > `cambridge` > `udise` > `cbse_mpd` |
| `address`, `pincode` | `cbse_saras` > `udise` > `cbse_mpd` |
| `city`, `district`, `state` | `pin_centroids` > `cbse_saras` > `udise` |
| `board`, `board_status`, `board_valid_*` | the owning board registry **only** ,  an institution's own site never wins here |
| `has_class_12`, `grade_low`, `grade_high` | `cbse_saras` > `udise` > `cbse_mpd` |
| `streams` | `cbse_mpd` > `cambridge`/`ib` > `cbse_saras` |
| `fee_annual_inr`, `fee_year` | `cbse_mpd` **only** ,  no other source has it |
| `class_12_total`, `pcm_12_count` | `cbse_mpd` > `udise` |
| `total_enrollment` | `udise` > `cbse_mpd` |
| `total_teachers` | `cbse_mpd` **only** ,  only the school's own disclosure publishes staff counts. `[M5]` Also the basis for an approximate student count when none is published: `teachers x students_per_teacher` from `scoring.yaml`, written with the `estimated:` marker in `evidence_span` so `enrollment_is_estimated` is set and the value renders as "~N (estimated)" rather than as a published figure |
| `medium`, `management_type` | `udise` > `cbse_saras` |
| `legal_entity_name` | `cbse_saras` > `cbse_mpd` |
| `principal_name` | `cbse_mpd` > `cbse_saras` > `udise` ,  the school's site is fresher for leadership (6.8, 6.11) |
| `counsellor_name` | `cbse_mpd` only |
| `website` | `cbse_mpd` > `cbse_saras` > `serper` |
| `email`, `phone` | `cbse_mpd` > `cbse_saras` |
| `year_founded` | `cbse_saras` > `udise` |
| `status` | `cbse_saras` (incl. closed list) > `udise` |

**Above all of these sits `overrides`.** A human correction beats every source,
survives every rebuild, and requires a `reason`. See `docs/DATA-MODEL.md`.

### Disagreement is itself signal

Retaining `Project-Doc.md` 6.7: when two sources disagree by **more than 20%** on
a numeric field, do not average and do not silently drop the loser. Store both,
surface both with attribution, and raise a `review_queue` row with
`kind='conflict'`. Set the `fee_disputed` or `enrollment_disputed` flag so the
disagreement is visible in the UI rather than hidden behind a single number.

Watch specifically for the trap 6.7 names: "sanctioned strength" (approved
capacity) presented as actual enrollment, and multi-campus schools reporting
trust-level totals that must not be attributed to one branch.

---

## Refresh cadence summary

| Source | Cadence | Why that number |
|---|---|---|
| `cbse_saras` | 30 days | Drives signal diffing; principals and affiliations change through the year |
| `cbse_mpd` | 180 days | Fee structure changes annually, around the academic year boundary |
| `cisce`, `ib`, `cambridge` | 90 days | Slow-moving lists |
| `group_directory` | 90 days | Campus openings are the point |
| `udise` | 365 days | Annual reporting cycle; more often gains nothing |
| `serper` | on demand | Only when `website` is missing |
| `pin_centroids` | once | Reload only on a district reorganisation |
