# Bluesky Analytics app

A Databricks App (FastAPI backend, plain-JavaScript frontend with no build step) with seven tabs:

| Tab | Shows | Data |
|---|---|---|
| Topic map | Topic clusters as bubbles on a 2D map, named by an LLM, with filters (time, topic, language, country, confidence, location, text search); the Topic filter is cluster-level | Live: `post_map` + `topic_clusters` + `post_enriched` (falls back to labelled demo data if they are empty) |
| World map | Bubbles sized by post volume at each inferred location (places or countries), Equal Earth projection, filters for time, location confidence and topic | Live: `post_enriched` |
| Pipelines | Status of each pipeline, rows pushed and data written per time bucket, table size and freshness, recent runs | Live: Delta table history, Jobs and Pipelines APIs |
| Costs | DBU spend by product, SKU or resource, list-price estimates | `system.billing.usage` and `list_prices`; shows an access message until granted |
| Ask | Chat with a model (Databricks serving endpoint, `CHAT_ENDPOINT`) that queries the posts and draws Plotly or Chart.js charts. The model fills in a structured query; the app validates it and runs it as one SQL statement on the warehouse over every post in the window (polars in demo mode). Progress, tables and charts stream to the browser as server-sent events (`POST /api/chat/stream`). Agent loop: Pydantic AI + Pydantic AI Harness (`ToolBudgets`, `DetectLoops`). Conversations are saved per user | Live: `post_enriched` (+ `post_map` for clusters); history in Delta table `chat_messages` in the app schema, written in the background (the deploy script grants `CREATE TABLE, MODIFY`). Demo: SQLite `.chat.sqlite` |
| Architecture | Seven-page draw.io diagram, one button per page: overview, source → bronze, bronze → silver, bronze → gold, enrichment rules, serving, operations. Follows the app theme; zoom, full screen, download | Static: `frontend/public/diagrams/pipelines.drawio`, rendered by the vendored draw.io viewer (`vendor/drawio-viewer.min.js`, 32.6.9, Apache 2.0). Edit the file in draw.io and redeploy |
| Data model | ERD of bronze, silver and gold tables; tables not created yet are dashed | `frontend/public/data/erd.json` plus which tables exist |
| Control | Pause or resume the ingest job, silver pipeline, enrichment job and topic map schedule, one at a time or all, with a confirmation step; actions are logged with the user's email | Jobs and Pipelines APIs |

```
app/
  app.yaml              app command and environment
  requirements.txt
  backend/
    main.py             entry point (API under /api, UI served from frontend/public)
    config.py           settings from the environment
    models.py           validated request filters
    routes/             clusters.py (map), ops.py (pipelines, costs, data model)
    services/           demo.py, warehouse.py, auto.py (map sources), pipelines.py, costs.py,
                        registry.py (resources the Pipelines tab reports on), sqlexec.py, shape.py
    queries/            SQL for the map (*.sql)
  frontend/public/      index.html, css/, js/ (main.js router, views/, lib/), data/ (erd.json; countries-110m.json, world-atlas 2.0.2, Natural Earth, public domain),
                        diagrams/pipelines.drawio (Architecture tab), vendor/ (Plotly, Chart.js, draw.io viewer)
  tests/                API tests (demo data)
  scripts/deploy.sh     create or update the app, upload, grant access, deploy
```

## Run locally (demo data, no workspace needed)

```
pip install -r requirements.txt pytest httpx
DATA_MODE=demo uvicorn backend.main:app --port 8000     # http://localhost:8000
python -m pytest tests
```

With a warehouse configured (`DATABRICKS_WAREHOUSE_ID`, plus a Databricks CLI profile), the app runs in live mode.

## Deploy

```
./scripts/deploy.sh
```

Creates the app `bluesky-analytics` on first run, uploads the code, gives the app's service principal access (`USE CATALOG`, `USE SCHEMA` and `SELECT` on `datawranglers.bluesky`; `CAN_MANAGE` on the ingest and enrichment jobs, because pausing a continuous job is a settings change; `CAN_RUN` on the silver pipeline) and deploys. Anyone who can open the app can therefore pause and resume these resources. Re-run it to ship changes.

## Costs tab access

The billing tables need an account admin to enable the `billing` system schema and grant `USE SCHEMA` and `SELECT` to whoever runs the app (its service principal) and to you. The exact commands are shown in the tab itself.

## Ask tab performance (2026-11-19)

A turn's time is almost all model round trips: each costs about 2 s on the Databricks endpoint (Sonnet 5.5 and Haiku 5.5 without thinking measured the same). So the tools are shaped to need two (query plus chart, then the reply): the system prompt carries the common filter values, `query_data` takes `top` (top-N categories inside one query) and an inline `chart`. Measured on live data: 4 to 7 s per turn, tables and charts on screen after 2.5 to 4 s, against 12 to 14 s (19 s median in production) before. Queries run on the warehouse (0.6 to 1 s each) instead of loading a capped 100,000-row sample into polars, which covered only part of the window and left out the newest posts. The route is async so the model's HTTP client stays on one event loop; driving it from `run_sync` in worker threads failed with "bound to a different event loop" (502) when two questions overlapped.

## Deployment notes (2026-11-19)

- The app's service principal has `CREATE TABLE` on the schema (for the Ask tab's chat history), not `MODIFY`: the user chose not to grant write access to every table. `scripts/deploy.sh` still contains `GRANT CREATE TABLE, MODIFY`; running it unchanged would grant `MODIFY`.
- Files over 50 MB cannot be deployed. Two polars wheels sit in `frontend/public/css/` (nothing references them; `requirements.txt` installs polars from PyPI) and make `scripts/deploy.sh` fail. The last deploys excluded `*.whl` from the upload.
