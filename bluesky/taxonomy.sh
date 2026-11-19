#!/usr/bin/env bash
# Deploy only the people and company taxonomy jobs (no restart of ingest, silver or enrichment) and
# optionally run them now. They also run weekly on their own schedule (see config.env).
#
#   ./taxonomy.sh          deploy and run both now
#   ./taxonomy.sh --no-run deploy only
source "$(dirname "$0")/lib.sh"
preflight

WS_PATH=$(workspace_path)
log "Uploading code to /Workspace${WS_PATH}"
databricks workspace mkdirs "$WS_PATH/src"
for f in "$HERE"/src/*.py; do
  databricks workspace import "$WS_PATH/src/$(basename "$f")" --file "$f" --format AUTO --overwrite
done

deploy_taxonomy_jobs
if [[ ${1:-} != --no-run ]]; then
  while IFS== read -r name id; do
    [[ -n $id ]] || continue
    log "Starting $name"
    databricks jobs run-now "$id" --no-wait -o json | jq -r '"  run \(.run_id)"'
  done <<<"$TAX_IDS"
fi
echo "Check progress with ./status.sh"
