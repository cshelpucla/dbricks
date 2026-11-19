#!/usr/bin/env bash
# Delete the ingestion, enrichment and topic map jobs and the silver pipeline.
# Deleting the pipeline also drops the silver tables it manages (posts, likes, reposts, follows).
# The enrichment and map tables (post_enriched, post_topics, post_geo, topic_taxonomy, bot_accounts,
# post_map, topic_clusters, taxonomy_people, taxonomy_companies) are kept
# unless --drop-data is given.
#
#   ./teardown.sh               delete jobs + pipeline; keep the bronze table, enrichment tables and schema
#   ./teardown.sh --drop-data   also drop the whole schema, including bronze data
#   add --yes to skip the confirmation prompt
source "$(dirname "$0")/lib.sh"

DROP_DATA=false YES=false
for arg in "$@"; do
  case $arg in
    --drop-data) DROP_DATA=true ;;
    --yes) YES=true ;;
    *) die "unknown option: $arg" ;;
  esac
done
preflight

what="jobs $JOB_NAME, $ENRICH_JOB_NAME and $MAP_JOB_NAME, pipeline $PIPELINE_NAME and its silver tables"
$DROP_DATA && what="$what, and schema ${CATALOG}.${SCHEMA} with all its data"
if ! $YES; then
  read -r -p "Permanently delete $what? [y/N] " answer
  [[ $answer == [yY]* ]] || die "aborted"
fi

JOB_ID=$(job_id)
if [[ -n $JOB_ID ]]; then
  log "Deleting job $JOB_NAME ($JOB_ID)"
  databricks jobs delete "$JOB_ID"
fi

for name in "$ENRICH_JOB_NAME" "$MAP_JOB_NAME" "$TAXONOMY_PEOPLE_JOB_NAME" "$TAXONOMY_COMPANIES_JOB_NAME"; do
  id=$(job_id "$name")
  if [[ -n $id ]]; then
    log "Deleting job $name ($id)"
    databricks jobs delete "$id"
  fi
done

PIPELINE_ID=$(pipeline_id)
if [[ -n $PIPELINE_ID ]]; then
  log "Deleting pipeline $PIPELINE_NAME ($PIPELINE_ID)"
  databricks pipelines delete "$PIPELINE_ID"
fi

if $DROP_DATA; then
  log "Dropping schema ${CATALOG}.${SCHEMA}"
  run_sql "DROP SCHEMA IF EXISTS ${CATALOG}.${SCHEMA} CASCADE" >/dev/null
fi
log "Teardown complete"
