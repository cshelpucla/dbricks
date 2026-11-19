"""The pipeline resources the Pipelines tab reports on, and the tables each one writes."""
from dataclasses import dataclass

from .. import config


@dataclass(frozen=True)
class Resource:
    key: str
    kind: str            # "job" or "pipeline"
    name: str
    layer: str           # bronze, silver, gold
    tables: tuple[str, ...]
    description: str


RESOURCES = (
    Resource("ingest", "job", config.INGEST_JOB_NAME, "bronze", ("events_raw",),
             "Streams Bluesky Jetstream events into the raw table."),
    Resource("silver", "pipeline", config.SILVER_PIPELINE_NAME, "silver",
             ("posts", "likes", "reposts", "follows"),
             "Lakeflow pipeline: deduplicated current-state tables with deletes applied."),
    Resource("enrich", "job", config.ENRICH_JOB_NAME, "gold",
             ("post_enriched", "post_topics", "post_geo"),
             "Bot filtering, geo extraction, embeddings and topics."),
    Resource("map", "job", config.MAP_JOB_NAME, "gold", ("post_map", "topic_clusters"),
             "Lays recent posts out in 2D, finds topic clusters and names them. Runs every 2 hours."),
)
BY_KEY = {r.key: r for r in RESOURCES}


def bucket_minutes(hours: int) -> int:
    """About 48 to 96 buckets across the window, on round sizes."""
    target = hours * 60 / 72
    for size in (1, 5, 10, 15, 30, 60, 120, 360, 1440):
        if size >= target:
            return size
    return 1440
