#!/usr/bin/env bash
# Pause ingestion, enrichment and the topic map schedule, and stop the silver pipeline, so no compute runs. Data is kept.
# Run ./setup.sh to start again; ingestion resumes where it left off if it was
# stopped for less than Jetstream's 36-hour lookback window.
#
#   ./stop.sh
source "$(dirname "$0")/lib.sh"
preflight

pause_scheduled_job "$MAP_JOB_NAME"
pause_scheduled_job "$TAXONOMY_PEOPLE_JOB_NAME"
pause_scheduled_job "$TAXONOMY_COMPANIES_JOB_NAME"
pause_continuous_job "$ENRICH_JOB_NAME"
pause_continuous_job "$JOB_NAME"

PIPELINE_ID=$(pipeline_id)
if [[ -n $PIPELINE_ID ]]; then
  log "Stopping silver pipeline $PIPELINE_NAME ($PIPELINE_ID)"
  databricks pipelines stop "$PIPELINE_ID" >/dev/null
fi
log "Stopped"
