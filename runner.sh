#!/usr/bin/env bash
set -euo pipefail

publish_checkpoint() {
  git -C ../data config user.name 'TenderBot collector'
  git -C ../data config user.email 'tenderbot@users.noreply.github.com'
  git -C ../data add records index.json collector.json state.json
  if ! git -C ../data diff --cached --quiet; then
    git -C ../data commit -m 'Refresh verified public GeM collection'
    git -C ../data push origin HEAD:data
  fi
}

# Leave time for the last bounded request, PDF parse and publication before
# GitHub's six-hour job limit. Future schedules resume any incomplete work.
collection_deadline=$((SECONDS + 18900))
mode=${COLLECT_MODE:-collect}
case "$mode" in
  collect|documents) ;;
  *) echo 'Unsupported collection mode.' >&2; exit 2 ;;
esac
batch_limit=$((10#$BATCH_COUNT))
batch=1
failures=0
while ((batch <= batch_limit)); do
  if ((SECONDS >= collection_deadline)); then
    echo 'Stopping at the job budget; the published checkpoint will resume later.'
    break
  fi
  echo "$mode batch $batch of $batch_limit"
  result=0
  if [ "$mode" = documents ]; then
    python collector.py --documents-only --data-dir ../data --seconds "$COLLECT_SECONDS" || result=$?
  else
    python collector.py --data-dir ../data --seconds "$COLLECT_SECONDS" --pdf-seconds "$PDF_SECONDS" || result=$?
  fi
  publish_checkpoint
  if [ "$result" -ne 0 ]; then
    # Human verification and authorization failures are never retried here.
    if python - <<'PY'
import json
from pathlib import Path
state = json.loads(Path('../data/state.json').read_text(encoding='utf-8'))
raise SystemExit(0 if state.get('status') == 'blocked' else 1)
PY
    then
      exit "$result"
    fi
    failures=$((failures + 1))
    if ((failures >= 3)); then
      echo 'Source remains unavailable; failure and checkpoint are published.'
      exit "$result"
    fi
    delay=$((failures * 180))
    echo "Collection failed; preserving data and waiting $delay seconds before a bounded retry."
    sleep "$delay"
    continue
  fi
  failures=0
  if python - "$mode" <<'PY'
import json
import sys
from pathlib import Path
if sys.argv[1] == 'documents':
    status = json.loads(Path('../data/collector.json').read_text(encoding='utf-8'))
    finished = status.get('eligible_documents') == 0
else:
    state = json.loads(Path('../data/state.json').read_text(encoding='utf-8'))
    finished = state.get('complete') is True and state.get('newest_refresh_complete') is True
raise SystemExit(0 if finished else 1)
PY
  then
    if [ "$mode" = documents ]; then
      echo 'No document jobs are due now; deferred and exhausted jobs remain visible in diagnostics.'
    else
      echo 'The full-feed and newest passes have traversed their source pages; see coverage diagnostics.'
    fi
    break
  fi
  batch=$((batch + 1))
done
