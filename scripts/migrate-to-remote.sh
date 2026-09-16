#!/usr/bin/env bash
# One-time seed of a remote Postgres (Aiven, Neon, ...) from the local DB, for
# deploying the read-only UI. Safe to re-run: verifies row counts before
# declaring success and never touches the local database.
#
# ponytail: plain pg_dump/pg_restore through the local db container - not a
# migration tool, not a sync job. This runs once per deploy, not on a
# schedule; add one only if the remote ever needs to track local changes
# on an ongoing basis.
#
# Usage (never pass the URL as a bare argument - it would land in shell
# history and `ps aux`; export it instead so it only ever lives in the
# environment):
#
#   export TARGET_DATABASE_URL='postgres://user:pass@host:port/db?sslmode=require'
#   bash scripts/migrate-to-remote.sh
#
set -euo pipefail
export MSYS_NO_PATH_CONV=1
export MSYS2_ARG_CONV_EXCL="*"

: "${TARGET_DATABASE_URL:?Set TARGET_DATABASE_URL to the destination postgres:// URI}"

cd "$(dirname "$0")/.."

STAMP=$(date +%F_%H%M)
DUMP="backups/school_intel_${STAMP}_pre-deploy.dump"

echo "1. Dumping local DB -> $DUMP"
docker compose exec -T db pg_dump -U school_intel -d school_intel -Fc -f /tmp/migrate.dump
docker compose cp db:/tmp/migrate.dump "$DUMP"
docker compose exec -T db rm -f /tmp/migrate.dump

LOCAL_COUNT=$(docker compose exec -T db psql -U school_intel -d school_intel -tAc \
  "select count(*) from institutions")
echo "   local institutions: $LOCAL_COUNT"

echo "2. Restoring into the target (schema + data; no owner/privilege changes,"
echo "   since the target's role name will not match 'school_intel')"
# Copied into the container and restored from there, rather than piped over
# stdin - `docker compose exec -T` stdin forwarding mangled the binary dump
# on Windows/Git Bash.
docker compose cp "$DUMP" db:/tmp/migrate_restore.dump
docker compose exec -T -e TARGET_DATABASE_URL db \
  pg_restore --no-owner --no-acl --clean --if-exists \
  --dbname="$TARGET_DATABASE_URL" /tmp/migrate_restore.dump
docker compose exec -T db rm -f /tmp/migrate_restore.dump

echo "3. Verifying the target actually has the data (not just an exit 0)"
REMOTE_COUNT=$(docker compose exec -T -e TARGET_DATABASE_URL db \
  psql "$TARGET_DATABASE_URL" -tAc "select count(*) from institutions")
echo "   target institutions: $REMOTE_COUNT"

if [ "$LOCAL_COUNT" != "$REMOTE_COUNT" ]; then
  echo "MISMATCH: local=$LOCAL_COUNT remote=$REMOTE_COUNT" >&2
  echo "Do not point the deployed app's DATABASE_URL at this target yet." >&2
  exit 1
fi

echo "OK - $DUMP restored and verified ($REMOTE_COUNT institutions)."
echo "Point the deployed app's DATABASE_URL at the target and deploy."
