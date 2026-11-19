# Shared helpers for the Bluesky pipeline scripts. Sourced, not run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$HERE/config.env"
export DATABRICKS_CONFIG_PROFILE="$DATABRICKS_PROFILE"

BRONZE="${CATALOG}.${SCHEMA}.${BRONZE_TABLE}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

preflight() {
  for c in databricks jq; do command -v "$c" >/dev/null || die "'$c' is required"; done
  databricks current-user me >/dev/null 2>&1 \
    || die "not logged in; run: databricks auth login --profile $DATABRICKS_PROFILE"
}

workspace_host() { databricks auth describe -o json | jq -r .details.host; }

# Workspace path of the uploaded code, without the /Workspace prefix.
workspace_path() { echo "/Users/$(databricks current-user me -o json | jq -r .userName)/$WORKSPACE_SUBDIR"; }

warehouse_id() {
  if [[ -n $WAREHOUSE_ID ]]; then echo "$WAREHOUSE_ID"; return; fi
  databricks warehouses list -o json | jq -r '.[0].id // empty'
}

# run_sql "<statement>": runs on the SQL warehouse (starting it if needed), prints result rows as JSON.
run_sql() {
  local wh resp id state
  wh=$(warehouse_id)
  [[ -n $wh ]] || die "no SQL warehouse found; set WAREHOUSE_ID in config.env"
  resp=$(databricks api post /api/2.0/sql/statements --json "$(jq -n --arg s "$1" --arg w "$wh" \
    '{statement: $s, warehouse_id: $w, wait_timeout: "30s", on_wait_timeout: "CONTINUE"}')")
  id=$(jq -r .statement_id <<<"$resp")
  state=$(jq -r .status.state <<<"$resp")
  while [[ $state == PENDING || $state == RUNNING ]]; do
    sleep 3
    resp=$(databricks api get "/api/2.0/sql/statements/$id")
    state=$(jq -r .status.state <<<"$resp")
  done
  [[ $state == SUCCEEDED ]] || die "SQL $state: $(jq -r '.status.error.message // empty' <<<"$resp")"
  jq -c '.result.data_array // []' <<<"$resp"
}

job_id() { databricks jobs list --name "${1:-$JOB_NAME}" -o json | jq -r '.[0].job_id // empty'; }

# upsert_continuous_job <name> <settings-json> <description>: create the job, or update it and restart its run
# on the new code. Prints the job id.
upsert_continuous_job() {
  local id; id=$(job_id "$1")
  if [[ -z $id ]]; then
    log "Creating continuous $3 job $1"
    databricks jobs create --json "$2" | jq -r .job_id
  else
    log "Updating $3 job $1 ($id) and restarting it on the new code"
    databricks jobs reset --json "$(jq -n --argjson id "$id" --argjson s "$2" '{job_id: $id, new_settings: $s}')" >/dev/null
    # The continuous trigger starts a fresh run, which resumes from the job's own cursor.
    databricks jobs cancel-all-runs --job-id "$id" >/dev/null
    echo "$id"
  fi
}

# upsert_job <name> <settings-json> <description>: create or update a (scheduled) job. Prints the job id.
upsert_job() {
  local id; id=$(job_id "$1")
  if [[ -z $id ]]; then
    log "Creating $3 job $1"
    databricks jobs create --json "$2" | jq -r .job_id
  else
    log "Updating $3 job $1 ($id)"
    databricks jobs reset --json "$(jq -n --argjson id "$id" --argjson s "$2" '{job_id: $id, new_settings: $s}')" >/dev/null
    echo "$id"
  fi
}

# pause_scheduled_job <name>: pause the job's schedule (keeps the cron expression). No-op if missing.
pause_scheduled_job() {
  local id schedule; id=$(job_id "$1")
  [[ -n $id ]] || return 0
  schedule=$(databricks jobs get "$id" -o json | jq -c '.settings.schedule // empty | .pause_status = "PAUSED"')
  [[ -n $schedule ]] || return 0
  log "Pausing schedule of job $1 ($id)"
  databricks jobs update --json "$(jq -n --argjson id "$id" --argjson s "$schedule" '{job_id: $id, new_settings: {schedule: $s}}')" >/dev/null
}

# pause_continuous_job <name>: pause the trigger and cancel the running run. No-op if the job does not exist.
pause_continuous_job() {
  local id; id=$(job_id "$1")
  [[ -n $id ]] || return 0
  log "Pausing job $1 ($id)"
  databricks jobs update --json "{\"job_id\": $id, \"new_settings\": {\"continuous\": {\"pause_status\": \"PAUSED\"}}}" >/dev/null
  databricks jobs cancel-all-runs --job-id "$id" >/dev/null
}

pipeline_id() {
  databricks pipelines list-pipelines --filter "name LIKE '$PIPELINE_NAME'" -o json \
    | jq -r '.[0].pipeline_id // empty'
}

# deploy_taxonomy_jobs: upsert the two scheduled taxonomy jobs (people, companies). Needs WS_PATH; sets TAX_IDS (name=id lines).
deploy_taxonomy_jobs() {
  TAX_IDS=""
  for kind in people companies; do
    if [[ $kind == people ]]; then name=$TAXONOMY_PEOPLE_JOB_NAME cron=$TAXONOMY_PEOPLE_SCHEDULE
    else name=$TAXONOMY_COMPANIES_JOB_NAME cron=$TAXONOMY_COMPANIES_SCHEDULE; fi
    tax_settings=$(jq -n \
      --arg name "$name" --arg kind "$kind" \
      --arg file "/Workspace$WS_PATH/src/taxonomy_job.py" --arg dir "/Workspace$WS_PATH/src" \
      --arg catalog "$CATALOG" --arg schema "$SCHEMA" --arg contact "$WIKI_CONTACT" \
      --arg cron "$cron" --arg env "$SERVERLESS_ENV_VERSION" '{
        name: $name,
        schedule: {quartz_cron_expression: $cron, timezone_id: "UTC", pause_status: "UNPAUSED"},
        max_concurrent_runs: 1,
        tasks: [{
          task_key: $kind,
          environment_key: "default",
          spark_python_task: {
            python_file: $file,
            parameters: ["--code-dir", $dir, "--kind", $kind, "--catalog", $catalog, "--schema", $schema, "--contact", $contact]
          }
        }],
        environments: [{environment_key: "default", spec: {environment_version: $env}}],
        tags: {project: "bluesky", taxonomy: $kind}
      }')
    TAX_ID=$(upsert_job "$name" "$tax_settings" "$kind taxonomy")
    TAX_IDS+="$name=$TAX_ID"$'\n'
    echo "  Taxonomy job:    $(workspace_host)/jobs/$TAX_ID ($name)"
  done
}
