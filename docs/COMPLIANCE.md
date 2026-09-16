# Compliance and Scraping Rules

This document is **normative**. A code change that weakens anything marked
**HARD GATE** must be rejected in review regardless of what it enables.

Not legal advice. The DPDP analysis below is well-sourced but should be confirmed
with counsel before the system carries real BD activity ,  see "Open items".

---

## 1. The student data boundary ,  HARD GATE

**Rule: this system stores institution-level data only. Never student-level data.
No exceptions, no feature flag, no "just for testing".**

### Why

`[EXTERNAL-FACT]` India's DPDP Act 2023:

- s.2(f) defines a **child** as anyone who has not completed 18 years. Most of
  class 11-12 are children.
- s.9(1) requires **verifiable parental consent** before processing any child's
  personal data.
- s.9(3) imposes **absolute prohibitions** on tracking, behavioural monitoring,
  and targeted advertising directed at children. These cannot be unlocked by
  parental consent.
- Penalties reach **Rs 200 crore**.

An exemption pathway exists for educational institutions, but it covers an
institution processing its own students' data ,  not a third party assembling a
targeting list about other institutions' students.

### The actual risk is accidental, not deliberate

Nobody intends to build a student database. But school websites are saturated
with exactly the wrong files: board result PDFs, merit lists, toppers' lists with
names and marks and photographs, admission lists, scholarship award lists,
prize-day programmes. A generic "fetch the school's site and extract what you
find" implementation will hoover these up on day one.

`Project-Doc.md` 6.12 got this right ,  "explicitly exclude admission-list or
result PDFs from scraper scope entirely, rather than relying on downstream
filtering" ,  and this document hardens it into code.

### Enforcement: `fetch/denylist.py`, checked before the request is made

Blocking happens at **fetch time**, not extraction time, not storage time. A
blocked URL is never requested, so the bytes never exist on our disk.

```python
# HARD GATE - ADR-006. Do not weaken. Do not add an override parameter.
URL_DENY_PATTERNS = [
    r"result",  r"results",   r"merit",     r"topper",   r"toppers",
    r"marks",   r"marksheet", r"scorecard", r"rank[-_]?list",
    r"admission[-_]?list",    r"selected[-_]?candidate",
    r"student[-_]?list",      r"studentlist",
    r"class[-_]?x{1,2}i{0,2}[-_]?result",
    r"board[-_]?result",      r"cbse[-_]?result",
    r"award", r"prize[-_]?list", r"scholarship[-_]?list",
    r"attendance", r"admit[-_]?card", r"hall[-_]?ticket",
    r"transfer[-_]?certificate", r"tc[-_]?issued",
]
```

Rules for this module:

1. Match case-insensitively against the **full URL** (path and query).
2. A hit writes a `fetches` row with `denylist_hit` set and `http_status = NULL`.
   We record that we *declined* to fetch ,  an auditable decision, not a gap.
3. `fee`, `fees`, and `fee-structure` must **not** be blocked. Fee pages are the
   highest-value target in the system. Test this explicitly; a broad `r"fee"`
   pattern would be a catastrophic own-goal.
4. **PDF discipline.** A PDF whose link text or filename matches any deny pattern
   is not fetched. A PDF that passes and turns out to contain a student list is
   discarded without extraction, and its URL is appended to a persisted
   `denylist_learned` list so it is never fetched again.
5. `tests/unit/test_denylist.py` is **mandatory**, with both directions asserted:
   result/merit/topper URLs blocked, and `fee-structure` / `mandatory-disclosure`
   / `about-us` / `contact` allowed.
6. **URL-level only. Never apply these patterns to page text.** `[VERIFIED M0-0]`
   The compliant CBSE Appendix-IX proforma titles its section
   **"C: RESULT AND ACADEMICS"**, and the fee-structure link sits inside that
   section. A text-level `r"result"` rule would therefore blocklist essentially
   every compliant Mandatory Disclosure page in the country ,  silently deleting
   the only source of fee data in the system while looking like it was working.
   The per-URL design already handles this correctly: it declines to follow the
   sibling *board-result* links while still permitting the fee link one row
   above. Keep it that way, and add a test fixture containing that heading which
   asserts the page is **not** blocked.

### Extraction-side backstop

Defence in depth. Every extraction prompt carries this instruction verbatim, and
`extract/llm/schemas.py` has no field capable of holding a student's identity:

> Never extract the name, photograph, marks, rank, contact details, or any other
> identifying information of any student. If the document contains student
> records, return `{"student_data_detected": true}` and nothing else.

A `student_data_detected` response causes the document to be deleted from raw
storage and its URL added to `denylist_learned`.

### What we do collect about people, and why it is defensible

Only **work-context professional data** for named staff in a decision-making
role: name, job title, and the institution's own published institutional contact.

`[EXTERNAL-FACT]` The best-footed of these is the principal's name from CBSE
SARAS ,  published by a government body as part of a statutory affiliation
disclosure. Prefer registry-sourced person data over website-scraped person data
wherever both exist (this is also why `principal_name` from `cbse_saras` is only
outranked by `cbse_mpd`, itself a statutory disclosure).

**Never store:** an individual's personal mobile number, personal email,
residential address, photograph, date of birth, salary, or qualifications beyond
what is needed to identify the right person. If a page offers a staff member's
personal mobile, do not record it ,  take the school's office number instead.

---

## 2. Robots, rate limits, and being a good citizen

`Project-Doc.md` 6.12 was right that a small school's shared hosting can go down
under scraping load, and that rate limiting must be **per-domain**, not just
global.

| Rule | Implementation |
|---|---|
| Check `robots.txt` before the first request to any new host | `fetch/robots.py`, cached 24h. On `Disallow`, skip and record `robots_allowed = false` |
| Per-domain concurrency: **1** | `asyncio.Semaphore(1)` keyed by registrable domain |
| Per-domain rate: `source_registry.rate_limit_rps`, default **1.0 rps** | Token bucket per domain |
| Government hosts (`*.gov.in`, `*.nic.in`): **0.5 rps**, and never in parallel across paths | Separate stricter bucket |
| Honest `User-Agent` with a contact URL | `TensorSchoolIntel/1.0 (+https://tensorschool.com; contact: <ops email>)` |
| Respect `Retry-After` | Always, no cap-and-ignore |
| Back off on 429/5xx: 2s, 8s, 30s, then mark the job dead | `jobs.attempts` |
| Total cap per institution per cycle: **6 fetches** | Prevents a misconfigured discovery loop from hammering one small host |
| No overnight-unattended first runs on a new source | Run a 50-URL canary, inspect, then scale |

`source_registry.robots_ok` and `tos_note` are populated per source before any
importer for that source is enabled.

---

## 3. Terms of service

| Source | Position |
|---|---|
| CBSE SARAS, UDISE+, data.gov.in | Government publications, public interest use, respectful rate. Cite the source in the UI. |
| CISCE / IB / Cambridge directories | Official board publications. Check each site's terms before enabling its importer and record the finding in `tos_note`. |
| Individual school websites | Statutory public disclosure (Appendix-IX) is published precisely to be read. Respect robots. |
| Group campus directories | First-party marketing pages, intended for public consumption. |
| Serper | Commercial API, used within its terms. Snippets are not stored as facts (`docs/SOURCES.md` S8). |
| **Google Places / Maps** | **EXCLUDED.** `[EXTERNAL-FACT]` ToS permits storing only `place_id` indefinitely and coordinates for 30 days; everything else must not be warehoused. A permanent DB built on it is a terms violation, not a cost question. ADR-003. |

**Attribution.** Every institution profile shows its sources with links.
This is both a ToS courtesy and the "why does the system believe this" feature
(`docs/ARCHITECTURE.md`) ,  the same mechanism serves both.

---

## 4. Data protection housekeeping

**Purpose limitation.** Collect only what serves the stated purpose: ranking
institutions and identifying the right professional contact. If a field cannot be
tied to that purpose, do not collect it ,  this is why principal *qualifications*
are parsed from CBSE and deliberately discarded (`docs/SOURCES.md` S1).

**Retention.** `raw_documents` and `observations` are append-only by design
(ADR-009). That is a deliberate trade-off in favour of auditability, and it means
retention is a policy question rather than an accident. Before production:

- Decide a retention period for `raw_documents` containing any personal data.
- Implement erasure by `person_id`: delete `people` and `roles` rows, and null
  the person fields in the corresponding `observations` while retaining the
  observation row shell for audit continuity.
- Keep an erasure log.

**Access.** The system is internal. Any authenticated user can read institution
data; only designated reviewers can write `overrides` and decide
`review_queue` items. `overrides.author` and `review_queue.decided_by` are
mandatory, which gives the audit trail `Project-Doc.md` asked for via
`school_edits`.

**No enrichment of personal data from third parties.** Do not look up a named
principal on LinkedIn, a people-search service, or a data broker. Institution
sources only.

---

## 5. Pre-launch checklist

Complete before the system is used for real outreach. `Project-Doc.md` was right
that compliance review must precede launch rather than be retrofitted.

- [ ] `fetch/denylist.py` implemented, and `tests/unit/test_denylist.py` passing
      in both directions
- [ ] Extraction schemas verified to contain **no** field able to hold student
      identity
- [ ] `robots.txt` checking live, with per-domain rate limits enforced and
      observable in `fetches`
- [ ] `source_registry.tos_note` populated for every enabled source
- [ ] Google Places confirmed absent from the codebase (grep for `places`,
      `maps.googleapis`)
- [ ] Attribution rendered on every institution profile
- [ ] Retention period decided and erasure procedure implemented and tested
- [ ] A spot audit of 20 random `raw_documents` confirming no student PII was
      stored
- [ ] Counsel sign-off on the DPDP position, specifically the professional-contact
      basis and the institution-as-consent-gateway outreach model

## Open items ,  `[BUSINESS]`

1. **Counsel review** of the DPDP position in section 1. The analysis is
   well-sourced but the conclusion is ours, not a lawyer's.
2. **Retention period** for raw documents containing personal data.
3. **Named data owner** accountable for erasure requests.
4. **Outreach copy review.** The system produces target lists; how BD then
   contacts an institution is outside this system but inside the same regulatory
   perimeter. Worth reviewing together, since a compliant database feeding
   non-compliant outreach solves nothing.
