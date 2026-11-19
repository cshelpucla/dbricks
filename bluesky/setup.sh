#!/usr/bin/env bash
# Deploy (or update) the Bluesky ingestion pipeline and start it.
# Safe to re-run: existing resources are updated in place and restarted on the new code.
#
#   ./setup.sh
source "$(dirname "$0")/lib.sh"
preflight

log "Creating schema ${CATALOG}.${SCHEMA} and bronze table ${BRONZE}"
run_sql "CREATE SCHEMA IF NOT EXISTS ${CATALOG}.${SCHEMA} COMMENT 'Bluesky data ingested from Jetstream'" >/dev/null
run_sql "CREATE TABLE IF NOT EXISTS ${BRONZE} (
  seq BIGINT COMMENT 'Jetstream sequence number (specific to the host that served it)',
  time_us BIGINT COMMENT 'Jetstream receive time in microseconds; the resume cursor',
  event_time TIMESTAMP,
  event_date DATE,
  kind STRING COMMENT 'commit, identity or account',
  did STRING COMMENT 'Repo (account) DID',
  collection STRING,
  operation STRING COMMENT 'create, update or delete',
  rkey STRING,
  rev STRING COMMENT 'Repo revision; orders changes within a repo',
  event VARIANT COMMENT 'Jetstream event payload',
  ingested_at TIMESTAMP
)
CLUSTER BY (event_date, collection)
COMMENT 'Raw Bluesky Jetstream events'" >/dev/null

WS_PATH=$(workspace_path)
log "Uploading code to /Workspace${WS_PATH}"
databricks workspace mkdirs "$WS_PATH/src"
for f in "$HERE"/src/*.py; do
  databricks workspace import "$WS_PATH/src/$(basename "$f")" --file "$f" --format AUTO --overwrite
done

job_settings=$(jq -n \
  --arg name "$JOB_NAME" \
  --arg file "/Workspace$WS_PATH/src/jetstream_ingest.py" \
  --arg table "$BRONZE" \
  --arg hosts "$JETSTREAM_HOSTS" \
  --arg collections "$COLLECTIONS" \
  --arg flush "$FLUSH_SECONDS" \
  --arg rewind "$REWIND_SECONDS" \
  --arg env "$SERVERLESS_ENV_VERSION" \
  --arg websockets "websockets==$WEBSOCKETS_VERSION" '{
    name: $name,
    continuous: {pause_status: "UNPAUSED"},
    max_concurrent_runs: 1,
    tasks: [{
      task_key: "ingest",
      environment_key: "default",
      spark_python_task: {
        python_file: $file,
        parameters: ["--table", $table, "--hosts", $hosts, "--collections", $collections,
                     "--flush-seconds", $flush, "--rewind-seconds", $rewind]
      }
    }],
    environments: [{environment_key: "default",
                    spec: {environment_version: $env, dependencies: [$websockets]}}],
    tags: {project: "bluesky"}
  }')

JOB_ID=$(upsert_continuous_job "$JOB_NAME" "$job_settings" "ingestion")

pipeline_settings=$(jq -n \
  --arg name "$PIPELINE_NAME" \
  --arg catalog "$CATALOG" \
  --arg schema "$SCHEMA" \
  --arg file "/Workspace$WS_PATH/src/silver_pipeline.py" \
  --arg bronze "$BRONZE" \
  --argjson continuous "$PIPELINE_CONTINUOUS" '{
    name: $name,
    catalog: $catalog,
    schema: $schema,
    serverless: true,
    continuous: $continuous,
    development: false,
    channel: "CURRENT",
    libraries: [{file: {path: $file}}],
    configuration: {"bluesky.bronze_table": $bronze}
  }')

PIPELINE_ID=$(pipeline_id)
if [[ -z $PIPELINE_ID ]]; then
  log "Creating silver pipeline $PIPELINE_NAME"
  PIPELINE_ID=$(databricks pipelines create --json "$pipeline_settings" | jq -r .pipeline_id)
else
  log "Updating silver pipeline $PIPELINE_NAME ($PIPELINE_ID)"
  databricks pipelines update "$PIPELINE_ID" --json "$pipeline_settings"
  if [[ $(databricks pipelines get "$PIPELINE_ID" -o json | jq -r .state) == RUNNING ]]; then
    log "Stopping the running pipeline so it restarts on the new code"
    databricks pipelines stop "$PIPELINE_ID" >/dev/null
  fi
fi
log "Starting silver pipeline"
# Continuous pipelines start on their own when created, so an active update is fine.
if ! out=$(databricks pipelines start-update "$PIPELINE_ID" 2>&1); then
  grep -q "active update" <<<"$out" || die "$out"
  log "Pipeline is already running"
fi

if [[ $ENRICH_ENABLED == true ]]; then
  enrich_settings=$(jq -n \
    --arg name "$ENRICH_JOB_NAME" \
    --arg file "/Workspace$WS_PATH/src/enrich_job.py" \
    --arg dir "/Workspace$WS_PATH/src" \
    --arg catalog "$CATALOG" --arg schema "$SCHEMA" --arg bronze "$BRONZE" \
    --arg window "$ENRICH_WINDOW_SECONDS" --arg interval "$ENRICH_INTERVAL_SECONDS" \
    --arg margin "$ENRICH_LAG_MARGIN_SECONDS" --arg start "$ENRICH_START_MINUTES" \
    --arg endpoint "$EMBED_ENDPOINT" --arg cap "$EMBED_MAX_PER_WINDOW" --arg topic "$TOPIC_MIN_SCORE" \
    --arg env "$SERVERLESS_ENV_VERSION" --arg sdk "databricks-sdk==$DATABRICKS_SDK_VERSION" '{
      name: $name,
      continuous: {pause_status: "UNPAUSED"},
      max_concurrent_runs: 1,
      tasks: [{
        task_key: "enrich",
        environment_key: "default",
        spark_python_task: {
          python_file: $file,
          parameters: ["--code-dir", $dir, "--catalog", $catalog, "--schema", $schema, "--bronze", $bronze,
                       "--window-seconds", $window, "--interval-seconds", $interval, "--lag-margin-seconds", $margin,
                       "--start-minutes", $start, "--embed-endpoint", $endpoint, "--embed-max-per-window", $cap,
                       "--topic-min-score", $topic]
        }
      }],
      environments: [{environment_key: "default", spec: {environment_version: $env, dependencies: [$sdk]}}],
      tags: {project: "bluesky"}
    }')
  ENRICH_ID=$(upsert_continuous_job "$ENRICH_JOB_NAME" "$enrich_settings" "enrichment")
fi

if [[ $MAP_ENABLED == true ]]; then
  map_settings=$(jq -n \
    --arg name "$MAP_JOB_NAME" \
    --arg file "/Workspace$WS_PATH/src/map_job.py" \
    --arg dir "/Workspace$WS_PATH/src" \
    --arg catalog "$CATALOG" --arg schema "$SCHEMA" \
    --arg hours "$MAP_HOURS" --arg points "$MAP_MAX_POINTS" --arg endpoint "$MAP_LABEL_ENDPOINT" \
    --arg cron "$MAP_SCHEDULE" --arg env "$SERVERLESS_ENV_VERSION" \
    --arg sdk "databricks-sdk==$DATABRICKS_SDK_VERSION" --arg umap "umap-learn==$UMAP_VERSION" \
    --arg numba "numba==$NUMBA_VERSION" --arg nnd "pynndescent==$PYNNDESCENT_VERSION" '{
      name: $name,
      schedule: {quartz_cron_expression: $cron, timezone_id: "UTC", pause_status: "UNPAUSED"},
      max_concurrent_runs: 1,
      tasks: [{
        task_key: "map",
        environment_key: "default",
        spark_python_task: {
          python_file: $file,
          parameters: ["--code-dir", $dir, "--catalog", $catalog, "--schema", $schema, "--hours", $hours,
                       "--max-points", $points, "--label-endpoint", $endpoint]
        }
      }],
      environments: [{environment_key: "default", spec: {environment_version: $env, dependencies: [$sdk, $umap, $numba, $nnd]}}],
      tags: {project: "bluesky"}
    }')
  MAP_ID=$(upsert_job "$MAP_JOB_NAME" "$map_settings" "topic map")
fi

if [[ $TAXONOMY_ENABLED == true ]]; then
  deploy_taxonomy_jobs
fi

HOST=$(workspace_host)
log "Done. Check progress with ./status.sh"
echo "  Ingestion job:   $HOST/jobs/$JOB_ID"
echo "  Silver pipeline: $HOST/pipelines/$PIPELINE_ID"
[[ -n ${ENRICH_ID:-} ]] && echo "  Enrichment job:  $HOST/jobs/$ENRICH_ID"
[[ -n ${MAP_ID:-} ]] && echo "  Topic map job:   $HOST/jobs/$MAP_ID (runs on schedule; start one now with: databricks jobs run-now $MAP_ID)"
