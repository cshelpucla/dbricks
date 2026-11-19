#!/usr/bin/env bash
# Create (or update) the Databricks App, upload the code, grant its service principal access, deploy.
# Safe to re-run. Reuses the workspace profile and names from ../bluesky/config.env.
#
#   ./scripts/deploy.sh                 deploy
#   APP_NAME=other-name ./scripts/deploy.sh
APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$APP_ROOT/../bluesky/lib.sh"
APP_NAME="${APP_NAME:-bluesky-analytics}"
preflight

WH=$(warehouse_id); [[ -n $WH ]] || die "no SQL warehouse found; set WAREHOUSE_ID in bluesky/config.env"
USER_NAME=$(databricks current-user me -o json | jq -r .userName)
WS_PATH="/Users/$USER_NAME/$APP_NAME-app"

if ! databricks apps get "$APP_NAME" >/dev/null 2>&1; then
  log "Creating app $APP_NAME (read access to SQL warehouse $WH)"
  databricks apps create --json "$(jq -n --arg n "$APP_NAME" --arg wh "$WH" '{name: $n,
      description: "Bluesky topic map, pipeline health and costs",
      resources: [{name: "sql-warehouse", sql_warehouse: {id: $wh, permission: "CAN_USE"}}]}')" \
    --timeout 20m >/dev/null
fi
SP=$(databricks apps get "$APP_NAME" -o json | jq -r .service_principal_client_id)
[[ -n $SP && $SP != null ]] || die "could not read the app's service principal"

log "Uploading code to /Workspace$WS_PATH"
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
cp -r "$APP_ROOT/app.yaml" "$APP_ROOT/requirements.txt" "$APP_ROOT/backend" "$APP_ROOT/frontend" "$STAGE/"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
databricks workspace mkdirs "$WS_PATH"
databricks workspace import-dir "$STAGE" "$WS_PATH" --overwrite >/dev/null

log "Granting the app (service principal $SP) access"
run_sql "GRANT USE CATALOG ON CATALOG ${CATALOG} TO \`$SP\`" >/dev/null
run_sql "GRANT USE SCHEMA, SELECT ON SCHEMA ${CATALOG}.${SCHEMA} TO \`$SP\`" >/dev/null
# The Ask tab keeps chat history in ${CATALOG}.${SCHEMA}.chat_messages, which the app creates and writes.
run_sql "GRANT CREATE TABLE, MODIFY ON SCHEMA ${CATALOG}.${SCHEMA} TO \`$SP\`" >/dev/null
# The Control tab pauses and resumes resources: pausing a continuous job edits its settings (needs CAN_MANAGE);
# stopping or starting the pipeline needs CAN_RUN.
grant() {
  [[ -n $2 ]] && databricks permissions update "$1" "$2" \
    --json "$(jq -n --arg sp "$SP" --arg lvl "$3" '{access_control_list: [{service_principal_name: $sp, permission_level: $lvl}]}')" >/dev/null
  return 0
}
grant jobs "$(job_id "$JOB_NAME")" CAN_MANAGE
grant jobs "$(job_id "$ENRICH_JOB_NAME")" CAN_MANAGE
grant jobs "$(job_id "$MAP_JOB_NAME")" CAN_MANAGE
grant pipelines "$(pipeline_id)" CAN_RUN

log "Deploying"
databricks apps deploy "$APP_NAME" --source-code-path "/Workspace$WS_PATH" >/dev/null
URL=$(databricks apps get "$APP_NAME" -o json | jq -r .url)
log "Done"
echo "  App URL: $URL"
