# Database snapshots

`school_intel_YYYY-MM-DD.dump` is a `pg_dump` custom-format snapshot of the
whole database — schools, observations, scores, resolved contacts. Restore it
and the tool works immediately, with no importing and no fetching.

## What is in the one dated 2026-09-06

| | |
|---|---|
| Schools | 33,636 (all 38 states) |
| Observations | 302,091 |
| Scored | 24,520 |
| Enriched (visited their own website) | 3,822 |
| Principal names | 33,139 |
| Phone / email | 1,704 / 1,296 |

Verified by restoring it into a scratch database and counting the rows, not
just by the dump exiting 0.

**No student-level data.** URLs that look like results, merit lists or
admission lists are blocked before the request is made, and 9 documents that
slipped through were detected and deleted during the last run. That is hard
rule 1 and the DPDP boundary — see `docs/COMPLIANCE.md`. It does contain
principal names, which CBSE publishes in its public registry.

## Restoring

Needs Postgres 16. From the repo root:

```bash
docker compose up -d
docker compose cp backups/school_intel_2026-09-06.dump db:/tmp/restore.dump
docker compose exec -T db psql -U school_intel -d postgres \
  -c "DROP DATABASE IF EXISTS school_intel WITH (FORCE);" \
  -c "CREATE DATABASE school_intel;"
docker compose exec -T db pg_restore -U school_intel -d school_intel --no-owner /tmp/restore.dump
```

Then check it and start the UI:

```bash
uv run python -m school_intel.cli serve     # http://127.0.0.1:8000
uv run python -m school_intel.cli worker    # required for "Get more details"
```

## What is NOT in it

`raw/` — the fetched HTML and PDFs, about 3,400 files. The dump references them
by `content_hash`, so a restored database has every extracted value but cannot
re-run `extract --reextract` without them. That only matters if you intend to
improve a parser and re-apply it to already-fetched pages.

## Making a new one

```bash
docker compose exec -T db pg_dump -U school_intel -d school_intel -Fc -f /tmp/d.dump
docker compose cp db:/tmp/d.dump backups/school_intel_$(date +%F).dump
docker compose exec -T db rm -f /tmp/d.dump
```
