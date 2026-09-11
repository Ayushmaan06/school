#!/usr/bin/env bash
# National enrichment in small chunks, one fresh process each.
#
# ponytail: a shell loop, not a --chunk flag. A single long `enrich` process
# holds the OCR models plus thousands of sockets and gets OOM-killed by the
# operating system after roughly 400 schools on a laptop that is also running
# Docker and an editor. Exiting between chunks is what actually frees that
# memory, and only a new process can do it. Move this into Python only if the
# per-chunk startup cost ever matters - today it is seconds against ~15 minutes
# of fetching.
#
# --per-state N counts only schools NOT yet enriched, so each chunk picks up
# where the last one stopped and the pass stays spread across all 38 states.
set -u
CHUNK=${CHUNK:-6}          # schools per state per chunk => ~230 per process
CHUNKS=${CHUNKS:-60}
LOG=${LOG:-logs/enrich-$(date +%Y%m%d).log}

for i in $(seq 1 "$CHUNKS"); do
  echo "=== chunk $i/$CHUNKS ===" >> "$LOG"
  uv run python -m school_intel.cli enrich --per-state "$CHUNK" >> "$LOG" 2>&1
  status=$?
  # 0 = clean. Anything else (OOM kill, Ctrl-C) still committed its work every
  # 5 schools, so the next chunk resumes rather than repeats.
  if [ "$status" -ne 0 ]; then
    echo "chunk $i exited $status; continuing" >> "$LOG"
  fi
  if tail -40 "$LOG" | grep -q '"considered": 0'; then
    echo "nothing left to enrich; stopping after chunk $i" >> "$LOG"
    break
  fi
done
echo "=== chunked pass done ===" >> "$LOG"
