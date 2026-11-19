#!/usr/bin/env bash
# Show the state of the ingestion job and silver pipeline, plus data freshness and row counts.
#
#   ./status.sh
source "$(dirname "$0")/lib.sh"
preflight
HOST=$(workspace_host)

JOB_ID=$(job_id)
log "Ingestion job: $JOB_NAME"
if [[ -n $JOB_ID ]]; then
  databricks jobs get "$JOB_ID" -o json | jq -r '"  trigger: continuous, \(.settings.continuous.pause_status)"'
  databricks jobs list-runs --job-id "$JOB_ID" --limit 3 -o json | jq -r '.[] |
    "  run \(.run_id): \(.status.state)\(if .status.termination_details then " (\(.status.termination_details.code))" else "" end), started \(.start_time / 1000 | strftime("%Y-%m-%d %H:%M:%SZ"))"'
  echo "  $HOST/jobs/$JOB_ID"
else
  echo "  not deployed"
fi

PIPELINE_ID=$(pipeline_id)
log "Silver pipeline: $PIPELINE_NAME"
if [[ -n $PIPELINE_ID ]]; then
  databricks pipelines get "$PIPELINE_ID" -o json | jq -r \
    '"  state: \(.state), latest update: \(.latest_updates[0].state // "none")"'
  echo "  $HOST/pipelines/$PIPELINE_ID"
else
  echo "  not deployed"
fi

log "Bronze ${BRONZE}, last hour"
if rows=$(run_sql "SELECT collection_or_kind, events,
    round(events / greatest(1, timestampdiff(SECOND, earliest, latest)), 1), latest FROM (
    SELECT coalesce(collection, kind) AS collection_or_kind, count(*) AS events,
      min(event_time) AS earliest, max(event_time) AS latest
    FROM ${BRONZE} WHERE event_time > current_timestamp() - INTERVAL 1 HOUR GROUP BY 1)
  ORDER BY events DESC"); then
  { echo "COLLECTION EVENTS PER_SEC LATEST"; jq -r '.[] | @tsv' <<<"$rows"; } | column -t
  run_sql "SELECT timestampdiff(SECOND, max(event_time), current_timestamp()) FROM ${BRONZE}" \
    | jq -r '"  newest event is \(.[0][0] // "n/a")s old"'
fi

ENRICH_ID=$(job_id "$ENRICH_JOB_NAME")
log "Enrichment job: $ENRICH_JOB_NAME"
if [[ -n $ENRICH_ID ]]; then
  databricks jobs get "$ENRICH_ID" -o json | jq -r '"  trigger: continuous, \(.settings.continuous.pause_status)"'
  databricks jobs list-runs --job-id "$ENRICH_ID" --limit 2 -o json | jq -r '.[] |
    "  run \(.run_id): \(.status.state)\(if .status.termination_details then " (\(.status.termination_details.code))" else "" end), started \(.start_time / 1000 | strftime("%Y-%m-%d %H:%M:%SZ"))"'
  run_sql "SELECT count(*), timestampdiff(SECOND, max(event_time), current_timestamp()), sum(cast(embedded AS INT)), count(topic_qcode)
    FROM ${CATALOG}.${SCHEMA}.post_enriched WHERE event_time > current_timestamp() - INTERVAL 1 HOUR" 2>/dev/null \
    | jq -r '.[0] | "  last hour: \(.[0]) posts enriched, \(.[2]) embedded, \(.[3]) with a topic; newest is \(.[1] // "n/a")s old"' || true
  echo "  $HOST/jobs/$ENRICH_ID"
else
  echo "  not deployed"
fi

MAP_ID=$(job_id "$MAP_JOB_NAME")
log "Topic map job: $MAP_JOB_NAME"
if [[ -n $MAP_ID ]]; then
  databricks jobs get "$MAP_ID" -o json | jq -r '"  schedule: \(.settings.schedule.quartz_cron_expression) UTC, \(.settings.schedule.pause_status)"'
  databricks jobs list-runs --job-id "$MAP_ID" --limit 2 -o json | jq -r '.[] |
    "  run \(.run_id): \(.status.state)\(if .status.termination_details then " (\(.status.termination_details.code))" else "" end), started \(.start_time / 1000 | strftime("%Y-%m-%d %H:%M:%SZ"))"'
  run_sql "SELECT max(computed_at), count(*), count(DISTINCT cluster_id) FROM ${CATALOG}.${SCHEMA}.post_map" 2>/dev/null \
    | jq -r '.[0] | "  current map: \(.[1]) posts in \(.[2]) clusters, computed \(.[0] // "never")"' || true
  echo "  $HOST/jobs/$MAP_ID"
else
  echo "  not deployed"
fi

for spec in "people:$TAXONOMY_PEOPLE_JOB_NAME" "companies:$TAXONOMY_COMPANIES_JOB_NAME"; do
  kind=${spec%%:*}; name=${spec#*:}; id=$(job_id "$name")
  log "Taxonomy job: $name"
  if [[ -n $id ]]; then
    databricks jobs get "$id" -o json | jq -r '"  schedule: \(.settings.schedule.quartz_cron_expression) UTC, \(.settings.schedule.pause_status)"'
    databricks jobs list-runs --job-id "$id" --limit 2 -o json | jq -r '.[] |
      "  run \(.run_id): \(.status.state)\(if .status.termination_details then " (\(.status.termination_details.code))" else "" end), started \(.start_time / 1000 | strftime("%Y-%m-%d %H:%M:%SZ"))"'
    run_sql "SELECT count(*), max(loaded_at) FROM ${CATALOG}.${SCHEMA}.taxonomy_${kind}" 2>/dev/null \
      | jq -r '.[0] | "  table: \(.[0]) rows, loaded \(.[1] // "never")"' || true
    echo "  $HOST/jobs/$id"
  else
    echo "  not deployed"
  fi
done

log "Silver tables (current rows)"
for t in posts likes reposts follows; do
  if n=$(run_sql "SELECT count(*) FROM ${CATALOG}.${SCHEMA}.${t}" 2>/dev/null); then
    printf '  %-8s %s\n' "$t" "$(jq -r '.[0][0]' <<<"$n")"
  else
    printf '  %-8s %s\n' "$t" "not created yet"
  fi
done
