# Bluesky Analytics on Databricks

Turns the live Bluesky firehose into explorable analytics. Posts stream in through Jetstream and land in Delta tables. A set of jobs then filters out bot and spam traffic, works out where posts are about, tags topics and the people and companies mentioned, and lays posts out on a 2D topic map. A Databricks App serves the results.

## Architecture

```
Jetstream v2 --> [ingest job] --> events_raw (bronze) --> [silver pipeline] --> posts / likes / reposts / follows
                                      |
                                      +--> [enrich job] --> post_enriched, post_topics, post_geo, bot_accounts
                                                 ^                    |
              topic_taxonomy (IPTC) -------------+                    +--> [topic map job, every 2 h] --> post_map, topic_clusters
              [taxonomy jobs, weekly] --> taxonomy_people,            |
                                          taxonomy_companies ---------+
                                                                      v
                                                      Databricks App (reads via SQL warehouse)
```

All tables live in `datawranglers.bluesky` (Unity Catalog). Resource names, the schedules and tuning settings are in [bluesky/config.env](bluesky/config.env).

| Component | What it does | Runs |
|---|---|---|
| `bluesky-jetstream-ingest` | Reads posts, likes, reposts and follows from Jetstream into `events_raw` | Always on |
| `bluesky-silver` | Lakeflow pipeline producing current-state `posts`, `likes`, `reposts`, `follows` | Always on |
| `bluesky-enrich` | Bot and spam scoring, location from post text, embeddings, IPTC topics, people and company matches | Always on |
| `bluesky-topic-map` | UMAP layout, HDBSCAN clusters, cluster names from an LLM | Every 2 hours |
| `bluesky-taxonomy-people` / `-companies` | Pull people and company lists from Wikipedia and Wikidata | Weekly |
| `bluesky-analytics` (app) | Tabs: Topic map, World map, Taxonomies, Pipelines, Costs, Architecture, Data model, Ask (chat with charts), Control | Databricks App |

## Repository layout

```
bluesky/          pipeline: jobs (src/), tests, and setup / status / stop / teardown scripts
app/              Databricks App: FastAPI backend, plain-JS frontend (no build step), tests, deploy script
docs/             specifications
```

## Getting started

Prerequisites: the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) logged in to your workspace (`databricks auth login --host <workspace-url>`), `jq`, a SQL warehouse, and permission to create a schema in the catalog set in `config.env`.

**Pipeline.** Review [bluesky/config.env](bluesky/config.env), then:

```
cd bluesky
./setup.sh            # create or update all jobs and the pipeline, and start them
./status.sh           # job state, data freshness, row counts
./stop.sh             # pause ingest, silver, enrichment and the map schedule; data is kept
./taxonomy.sh         # deploy and run only the people/company taxonomy jobs
./teardown.sh         # delete jobs and pipeline (add --drop-data to drop the tables too)
```

The ingest, silver and enrichment resources bill 24/7 while running. If you stop them, `./setup.sh` starts them again. Ingestion picks up where it left off as long as it was stopped for less than 36 hours (how far back Jetstream keeps events).

**App.** Deploy to the workspace:

```
cd app
./scripts/deploy.sh
```

Or run it locally on demo data, with no workspace needed:

```
cd app
pip install -r requirements.txt pytest httpx
DATA_MODE=demo uvicorn backend.main:app --port 8000     # http://localhost:8000
```

**Tests.**

```
cd bluesky && python -m pytest tests      # topic map tests need umap-learn installed
cd app && python -m pytest tests
```

## Documentation

- [docs/SPEC.md](docs/SPEC.md): overall design, tables, operations, and what is built
- [docs/ENRICHMENT_SPEC.md](docs/ENRICHMENT_SPEC.md): the enrichment job in detail (inputs, outputs, scoring rules, versioning)
- [app/README.md](app/README.md): app tabs, code layout, deployment and access notes

## Data and attribution

All data comes from public Bluesky posts. Taxonomies: IPTC Media Topics (CC BY 4.0, IPTC); GeoNames (CC BY 4.0, geonames.org); IAB Content Taxonomy (CC BY 3.0, IAB Tech Lab); people and companies from Wikidata (CC0) and Wikipedia.
