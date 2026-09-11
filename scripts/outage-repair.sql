-- Clear enrichment markers left by network outages.
--
-- WHY THIS IS NEEDED: `enrich --per-state N` selects only schools where
-- last_enriched_at IS NULL. A school stamped during an outage - visited, every
-- request failed, nothing extracted - is therefore invisible to every future
-- pass. It is not wrong DATA, it is a wrong "we already did this" marker, and
-- it silently shrinks national coverage.
--
-- HOW THE WINDOWS WERE IDENTIFIED: buckets where >60% of `fetches` carried a
-- transport error, not by absence of observations. Roughly a third of properly
-- enriched schools legitimately yield nothing (dead domain, no MPD page, no
-- footer contacts), so "no observations" alone proves nothing.
--
-- Touches ONLY institutions.last_enriched_at. Deletes nothing from
-- observations, fetches or raw_documents (hard rule 4).
-- Reversal data: logs/outage-reset-20260909.csv (id, affiliation no, old value)

BEGIN;

-- 2026-09-09 17:25-18:30 IST (11:55-13:00 UTC). 98% of fetches errored;
-- 3,295 requests against 3,223 schools, one instant failure each. Expect 3264.
UPDATE institutions i SET last_enriched_at = NULL
WHERE i.last_enriched_at >= '2026-09-09 11:55:00+00'
  AND i.last_enriched_at <  '2026-09-09 13:00:00+00'
  AND NOT EXISTS (SELECT 1 FROM observations o
                  WHERE o.entity_key = 'cbse:' || i.cbse_affiliation_no
                    AND o.source_id = 'cbse_mpd');

-- SKIPPED at operator's request: today's outage only.
-- 2026-09-05 18:25-18:55 IST (12:55-13:25 UTC). Only 79% errored, so some of
-- these 153 are genuine no-yield schools. Re-visiting them costs ~10 minutes
-- and removes the doubt. Comment this statement out to skip it.
-- UPDATE institutions i SET last_enriched_at = NULL
-- WHERE i.last_enriched_at >= '2026-09-05 12:55:00+00'
--   AND i.last_enriched_at <  '2026-09-05 13:25:00+00'
--   AND NOT EXISTS (SELECT 1 FROM observations o
--                   WHERE o.entity_key = 'cbse:' || i.cbse_affiliation_no
--                     AND o.source_id = 'cbse_mpd');

SELECT count(last_enriched_at) AS enriched_after_repair FROM institutions;

COMMIT;
