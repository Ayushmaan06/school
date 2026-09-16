## Tensor School Intelligence ,  Project Documentation ( Fully Dynamic and Consistently Updating Itself)

## School Intelligence:

An internal intelligence platform that continuously aggregates and enriches publicly available information about Indian schools, enabling Tensor teams to search, analyze, rank, and prioritize schools and school chains as business/partnership targets.

Core principle: Don't build a scraper. Build a school intelligence system where scraping is one part of a larger data pipeline.


## 1. Objective

Replace the current manual workflow , 

Google/search → multiple websites → spreadsheets → manual research

,  with:

## Search → Enrich → Analyze → Prioritize

The output that matters isn't "we have 40,000 schools in a database." It's: a Tensor employee should be able to go from "Find me good schools in Karnataka" to "here are the top 20 schools

worth approaching, and why" in minutes instead of hours of manual research.

## 2. Core Problem

Tensor's team wants to identify schools by criteria such as: state, region, district, city, board, management type, approximate enrollment, school chain, principal/leadership, size, expansion activity, recent developments, and other publicly available business-relevant signals. Doing this manually today is slow and inconsistent.

The hard part isn't collecting this data ,  it's that almost every one of these filters is more

ambiguous than it looks. Section 6 below goes deep on this, since it's where a naive build will quietly produce wrong or misleading results.

## 3. System Architecture


Scraping is async/background (Celery workers → PostgreSQL). A user search never triggers a live scrape ,  it only ever hits the database/search index.

## Stack

- Backend: FastAPI + SQLAlchemy + PostgreSQL + Redis + Celery

- Scraping: Scrapy (default) + Playwright (only for JS-rendered pages ,  heavier, don't default to it)

- Search: PostgreSQL full-text + pg_trgm for v1 → OpenSearch/Elasticsearch once volume/faceting needs grow

- Frontend: React + Next.js + Tailwind

- Maps: later phase, not v1


## 4. Data Model

## schools

id, name, canonical_name, school_code, board, management_type, legal_entity_type, address, city, district, state, region, metro_area, latitude, longitude, website, phone, email, student_count, student_count_min, student_count_max, student_count_year, principal_id, chain_id, opportunity_score, created_at, updated_at, last_verified_at

## school_aliases

Every school realistically has 3–8 name variants (English, regional script, abbreviated, branch aka). This needs its own table from day one ,  see 6.6.

## people

id, name, designation, school_id, source, source_url, verified_at, effective_from

## school_chains

id, name, canonical_name, brand_name, legal_owning_entity, description, website, school_count, states_present, cities_present, estimated_students, growth, opportunity_score

## school_sources

id, school_id, source_type, source_url, retrieved_at, scraping_allowed

## school_signals

id, school_id, signal_type, title, description, severity, source_url, event_date, confidence, is_active, resolved_at

Signal types: Expansion / New Campus, Leadership Change, Ownership Change, Regulatory Event, Accreditation Change, Fee/Operational Issue, Reputation Issue.

Every field of business consequence should retain: value, source, source URL, source date,

last verified date, confidence ,  this is what stops the platform from presenting old or uncertain information as fact.


## school_edits (audit log)

Since many fields here are estimates/confidence-scored, track who/what changed a field and when ,  especially once staff can manually correct enrichment errors in the UI.

## 5. Intelligence Engine

The intelligence engine is the differentiator, not the scraper. It has three distinct layers:

## 5.1 Normalization

Different sources represent the same thing differently ,  "Bangalore" / "Bengaluru" / "Bangalore Urban" need to collapse into one standard representation before anything downstream can trust them.

## 5.2 Entity Resolution

Determining whether different records are the same school ,  "DPS Bangalore" / "Delhi Public School Bangalore" / "Delhi Public School, Bengaluru" may or may not be the same institution. Matching uses name similarity, address, phone, website, school code, and coordinates, with embeddings/AI assistance for ambiguous cases in later phases. See 6.10 for the false-positive/false-negative tradeoff.

## 5.3 Enrichment

Combining government data + school website + news into one coherent school profile (principal, management, enrollment, chain affiliation, expansion, leadership changes, other signals).

## 5.4 Where AI fits (and doesn't)

AI should not run the system. Traditional software handles search, filtering, sorting, aggregation, geography, scoring, and database operations ,  these need to be deterministic and auditable. AI's job is narrower and specific:

- Information extraction from unstructured text (news → structured signal)

- News classification (is this article actually about this school, and about what)

- School-chain identification assistance (ambiguous name-matching cases)

- Entity resolution assistance

- Summarization


Example: a news article says a school group plans five new campuses. AI extracts {organization, event: Expansion, new_campuses: 5, regions, date, confidence} ,  that becomes structured data, not prose sitting in a database.

## 6. Edge Cases & Definitional Ambiguities

This is the section worth reading before writing a single scraper ,  every filter above sounds like a simple dropdown and isn't.

## 6.1 "Region" ,  undefined in Indian administrative geography

There is no official Indian government "region" classification, and sources draw the lines differently. Decision (locked for v1): keep it simple ,  four regions only. region is a derived, editable state → region mapping table, not a scraped field:

Region

North

Jammu & Kashmir, Ladakh, Himachal Pradesh, Punjab, Chandigarh, Haryana, Delhi/NCR, Uttarakhand, Uttar Pradesh

West

Rajasthan, Gujarat, Maharashtra, Goa, Dadra & Nagar Haveli and Daman & Diu

South

Karnataka, Kerala, Tamil Nadu, Andhra Pradesh, Telangana, Puducherry, Andaman & Nicobar Islands

East

West Bengal, Odisha, Bihar, Jharkhand, Madhya Pradesh, Chhattisgarh, Sikkim, Assam, Meghalaya, Manipur, Mizoram, Nagaland, Tripura, Arunachal Pradesh

Note the deliberate simplification: Central states (MP, Chhattisgarh) and all Northeast states are

folded into East rather than getting their own bucket, purely to keep the v1 filter to four options. This is an editable mapping table, not hardcoded logic ,  if BD ends up needing Central or Northeast broken out separately later (e.g. once there's real school density/leads in Assam or MP), it's a config change, not a schema change. Delhi/NCR sits in North as a state-level assignment; the NCR cross-state issue is still handled separately via metro_area (6.2), independent of this region split.

## States/UTs


## 6.2 "State" ,  includes UTs, and NCR is a cross-state mess

Gurugram/Faridabad (Haryana), Noida/Ghaziabad (UP), Delhi itself ,  a BD search for "Delhi" that literally means Delhi state misses most of NCR. Add an explicit metro_area field (Delhi NCR, Mumbai Metropolitan Region, Bangalore Urban, etc.) as a first-class filter, since that's how BD teams actually think about markets.

## 6.3 "District" / "City" ,  inconsistent and non-unique

District boundaries get redrawn periodically. "City" isn't administratively defined (municipal boundary vs. urban agglomeration vs. colloquial usage). Gurgaon/Gurugram is one city with two names post-2016 rename ,  normalize or it silently fragments data. City is never a safe join key alone; always require city + state.

## 6.4 "Board" ,  not mutually exclusive, not stable

A school can be dual-affiliated (CBSE + IB streams in parallel, or a switch mid-lifecycle). Model board as many-to-one (school_boards join table), not a single field, or real schools get dropped/misclassified. "State board" is actually 28+ distinct boards ,  decide whether "State" needs to expand into a sub-filter.

## 6.5 "Management type" ,  a spectrum

Not just Private vs. Government. UDISE+-style categories: Government, Government-aided, Private unaided, Central Government (KV/Navodaya), Private-aided. Add a separate legal_entity_type (trust / society / company) distinct from management_type, since "who actually signs a contract" matters more to BD than the funding category.

## 6.6 "School chain" ,  the hardest edge case in the system

Naive string-matching will actively mislead the business team here. Patterns to design for:

| Pattern | Risk if mishandled |
| --- | --- |
| True single-owner chain | Low risk |
| Franchise/licensing model (brand licensed to independent local operators ,  common in Indian K-12) | Overstates chain size AND wrongly implies one decision-maker across branches, when each franchisee negotiates independently |


Same generic name, unrelated schools ("St. Mary's," "Delhi Public School" ,  itself a society-licensed model, not single-owner)

False chain-matching inflates scores and sends BD to the wrong contact

Regional sub-brand of a larger group

Under-counts true scale if brand ≠ legal entity

Chain that has since fragmented (dispute/split) Should surface as a school_signal

(Management Conflict), not silently merged

Decision: detect chains on two separate axes ,  brand_name (fuzzy-matched) and legal_owning_entity (verified where possible from registration docs/website About Us). Only merge into one chain record when both align; when only brand matches, flag possible_franchise_network and keep decision-maker data at branch level. This single call is what protects the credibility of the "Top Chains" feature.

## 6.7 Enrollment ,  ranges, not points

Numbers from a school's own website skew high (marketing); UDISE+ numbers skew stale. When sources disagree by >20%, surface both with attribution rather than averaging ,  the disagreement itself is signal. Watch for "sanctioned strength" (approved capacity) getting conflated with actual enrollment in scraped copy, and for multi-campus schools reporting trust-level numbers that shouldn't be attributed to one branch.

## 6.8 Principal/leadership ,  high churn

Turnover is frequent, often annual, in Indian private schools ,  exactly the kind of field that gets scraped once and silently goes stale. Use effective_from/verified_at, and visually flag anything older than ~12 months as stale rather than presenting it as current fact. Titles vary (Principal, Director, Headmistress, Academic Director, trust CEO) ,  normalize into a controlled vocabulary but retain the raw scraped title.

## 6.9 "Management issues" / signals ,  highest-risk feature in the product

- Tier source reliability explicitly: government/regulatory notice > established news > local press > social/forums. Never let a severity: High signal render without at least one Tier-1/2 source.

- Time-decay signals ,  a 2019 leadership dispute showing as a live flag in 2026 misleads a BD person about to make a call. Default-hide anything past ~18–24 months unless reviewed.

- Don't auto-classify news sentiment without human review in early phases (satire, quoted critic inside an otherwise positive piece, opinion vs. fact are all genuinely hard). Route


flagged articles to human review before a signal goes live ,  false positives here carry real reputational/defamation-adjacent risk toward named institutions.

## 6.10 Duplicate detection ,  two failure directions

False negatives (same school missed due to transliteration/spelling/rename variants ,  see 6.6's aliases table) and false positives (two real franchise branches merged into one, silently losing a lead) both cost you. Dedup on weighted match (name similarity + geocoded proximity + phone/website overlap); never auto-merge above an ambiguity threshold ,  route to manual review instead. Auto-merging is cheap to build and expensive to undo once BD has already contacted "the wrong branch."

## 6.11 Source conflicts, generally

When two sources disagree on any field, use an explicit resolution policy, not implicit "last scrape wins": government/regulatory > official school website > reputable news > directory > forum/social as a default hierarchy, overridable per-field (a school's own website is often more current than a stale government dataset for something like "current principal"). Store the losing value too, with source + timestamp ,  needed for the staleness and manual-review logic above.

## 6.12 Compliance edge cases

- Scraped people records (principal names, sometimes personal contact info on About Us pages) are personal data under India's DPDP Act 2023, even though the target is institutions ,  collect only what serves a legitimate business purpose, and get anything beyond a work-context name/title reviewed rather than assumed fine.

- Never collect student-level data ,  explicitly exclude admission-list or result PDFs from scraper scope entirely, rather than relying on downstream filtering.

- Track scraping_allowed per source domain (robots.txt/ToS check before a scraper targets a new domain), and rate-limit per-domain, not just globally ,  a small school's shared hosting can go down under scraping load.

## 6.13 Search relevance

A natural-language query ("private CBSE chains in South India with >2,000 students and expansion signals in the last 12 months") decomposes into: board filter + region filter (6.1) + enrollment threshold against a range not a point (6.7) + signal-type + signal recency. Build the NL interface as a translation layer onto the structured filters, not a separate freeform search


path, or the two will return inconsistent results. Decide explicitly how ranges interact with thresholds (student_count_min vs _max vs midpoint) and apply it consistently.

## 6.14 Opportunity Score

- Show the breakdown, not just the number ,  "91/100" with no explanation stops being trusted the first time it disagrees with a BD person's local knowledge.

- Missing data should reduce confidence in the score, not silently zero out a sub-score ,  a school with no enrollment data isn't necessarily low-opportunity, it's unknown.

- Recompute on a schedule and keep score history ,  an overnight 60→90 jump with no visible reason reads as a bug even when it's a legitimate expansion signal.

## 7. Opportunity Score ,  Starting Model

Opportunity Score =

30% Enrollment Potential 20% School/Chain Scale 15% Geographic Fit 15% Growth Signals 10% Decision-Maker Availability 10% Data Confidence

Treat these weights as a starting point to validate with Tensor's business/partnership team, not a fixed formula ,  see 6.14 on making the score explainable and confidence-aware rather than a black box.

## Example surfaced in the UI:

Opportunity Score: 91/100 Why: 3,000+ estimated students · target geography · part of a large chain · two new campuses announced · principal identified ·

high-confidence data


## 8. Chain Intelligence

Group branches into parent organizations wherever the two-axis detection in 6.6 supports it:

ABC School, ABC School Bangalore, ABC School Hyderabad, ABC School Pune

→ ABC Education Group

Then compute: number of campuses, states/cities covered, estimated students, growth, geographic penetration, opportunity score ,  letting Tensor target school groups rather than researching every campus individually, while keeping possible_franchise_network cases (6.6) visibly separate from verified single-owner chains.

## 9. Geographic Intelligence

Every school carries region (6.1), state, district, city, metro_area (6.2), latitude/longitude ,  enabling state/district/city filtering, region analysis, school density, enrollment-by-geography, chain presence, and identification of under-penetrated regions. A map interface is a later-phase addition, not v1.

## 10. User Interface (v1 scope)

## School Search

Filters: Region · State · District · City · Board · Management · Enrollment · School Chain · Opportunity Score

## Search Results

School · Location · Students · Board · Chain · Opportunity Score

## School Profile

Basic Information · Leadership · Enrollment · Chain · Signals · Sources · Contacts


## Chain Dashboard

Chain · Number of Schools · States · Cities · Estimated Students · Growth · Opportunity Score

## 11. MVP Scope

Don't start by scraping all of India. Start with roughly 500–1,000 schools in one state.

MVP data: school, location, board, management, enrollment, website, principal, chain, sources. MVP functionality: search, filters, school profiles, chain grouping, basic opportunity score.

Success criterion: a Tensor employee should be able to go from "Find me good schools in Karnataka" to "here are the top 20 schools worth approaching" in a few minutes instead of manual research ,  and every one of the edge cases in Section 6 that applies within that single state's data should already be handled correctly at this scale, since they only get harder nationwide.

## 12. Phased Roadmap

Phase

1 ,  MVP / Core

database

## Scope

Single state, ~500–1,000 schools. Location hierarchy with region/metro-area handling (6.1–6.3) built in from day one, board as many-to-one (6.4), management type + legal entity type (6.5), enrollment as ranges (6.7), source attribution, basic search/filter, school profiles, chain grouping, basic opportunity score

Two-axis chain detection (6.6), better entity resolution (5.2), dedup with manual-review queue (6.10), geographic dashboards, more sources

News ingestion, signal classification with human review gate before publish (6.9), time-decay/staleness rules

Opportunity forecasting, composite score with visible breakdown and confidence-aware missing-data handling (6.14), territory planning, automated lead suggestions

2 ,  Nationwide + chain intelligence

3 ,  Signals

4 ,  AI

recommendations


## 5 ,  Continuous monitoring

Compliance review (6.12) should happen before Phase 3, not retrofitted after signals are already live ,  that's the phase carrying actual reputational/legal exposure.

Full async pipeline, scheduled re-scoring, alerts when high-value schools change

## 13. Open Decisions Checklist (needs Tensor stakeholder input before build)

- [x] ~~Which region-to-state mapping convention to ship with~~ ,  resolved: simple 4-region split (North/West/South/East), see 6.1

- [ ] Is metro-area (NCR, MMR, Bangalore Urban) a v1 filter or v2?

- [ ] Which single state to pick for the MVP, and why (existing BD presence? data availability? target market)?

- [ ] What's the acceptable staleness window for principal/leadership data before it's flagged?

- [ ] Who signs off on a school_signal before it goes live in Phase 3 ,  designated internal reviewer, or does this need legal review given the defamation-adjacent risk?

- [ ] Auto-merge threshold for dedup ,  how conservative should Phase 2 be by default?

- [ ] Does Opportunity Score need per-team customizable weights, or one global formula?

## 14. Final Product Definition

## Tensor School Intelligence

An internal intelligence platform that continuously aggregates and enriches publicly available information about Indian schools, enabling Tensor teams to search, analyze, rank, and prioritize schools and school chains as business/partnership opportunities.
