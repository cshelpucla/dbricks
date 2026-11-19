"""Settings read from the environment (see app.yaml)."""
import os
import re

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def _ident(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not _IDENT.match(value):
        raise ValueError(f"{name} must be a plain identifier, got {value!r}")
    return value


CATALOG = _ident("CATALOG", "datawranglers")
SCHEMA = _ident("SCHEMA", "bluesky")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
# "warehouse" reads the Unity Catalog tables; "demo" serves synthetic points so the UI can be
# built and reviewed before the enrichment and map jobs have produced data. Defaults to
# warehouse when a warehouse is configured.
DATA_MODE = os.environ.get("DATA_MODE") or ("warehouse" if WAREHOUSE_ID else "demo")
MAX_POINTS = int(os.environ.get("MAX_POINTS", "50000"))

# Pipelines tab: the resources to report on. Names match bluesky/config.env.
INGEST_JOB_NAME = os.environ.get("INGEST_JOB_NAME", "bluesky-jetstream-ingest")
SILVER_PIPELINE_NAME = os.environ.get("SILVER_PIPELINE_NAME", "bluesky-silver")
ENRICH_JOB_NAME = os.environ.get("ENRICH_JOB_NAME", "bluesky-enrich")
MAP_JOB_NAME = os.environ.get("MAP_JOB_NAME", "bluesky-topic-map")

# Costs tab: Databricks billing system tables (override if your cost data lives elsewhere).
COST_USAGE_TABLE = os.environ.get("COST_USAGE_TABLE", "system.billing.usage")
COST_PRICES_TABLE = os.environ.get("COST_PRICES_TABLE", "system.billing.list_prices")

# Chat tab: a Databricks Model Serving chat endpoint with tool calling.
CHAT_ENDPOINT = os.environ.get("CHAT_ENDPOINT", "databricks-claude-sonnet-5-5")
CHAT_MAX_STEPS = int(os.environ.get("CHAT_MAX_STEPS", "6"))
CHAT_MAX_ROWS = int(os.environ.get("CHAT_MAX_ROWS", "100000"))
# Persistent chat history: a Delta table when live (needs CREATE TABLE and MODIFY on the schema),
# a SQLite file in demo mode (":memory:" for tests).
CHAT_TABLE = _ident("CHAT_TABLE", "chat_messages")
CHAT_DB = os.environ.get("CHAT_DB", ".chat.sqlite")
