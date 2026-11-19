"""Pull one entity taxonomy (people or companies) from Wikipedia/Wikidata into its own Delta table.

Runs as a scheduled Databricks serverless job, one job per taxonomy (bluesky-taxonomy-people and
bluesky-taxonomy-companies), so each pull has its own run history, schedule and failure alerts. The table is
replaced only after a complete, sane pull. enrich_job.py reloads the tables when they change.
Logic: entity_loader.py; contract: docs/ENRICHMENT_SPEC.md section 7.5.
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone

TABLES = {"people": "taxonomy_people", "companies": "taxonomy_companies"}
SCHEMA = ("qid STRING, kind STRING, label STRING, description STRING, aliases ARRAY<STRING>, sitelinks INT, "
          "category STRING, industry STRING, image STRING, source STRING, loaded_at TIMESTAMP")
COLUMNS = [c.split(" ")[0] for c in SCHEMA.split(", ")]


def _code_dir():
    """Serverless runs the script via exec(), so __file__ may be undefined; setup passes --code-dir."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        argv = sys.argv
        return next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--code-dir"), os.getcwd())


sys.path.insert(0, _code_dir())
import entity_loader as EL  # noqa: E402

log = logging.getLogger("taxonomy_job")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--code-dir", default=None)
    p.add_argument("--kind", required=True, choices=sorted(TABLES))
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--contact", default="", help="Contact for the Wikimedia User-Agent (URL or address)")
    args = p.parse_args()

    EL.set_contact(args.contact)
    rows = EL.build(args.kind)
    now = datetime.now(timezone.utc)
    data = [tuple({**r, "loaded_at": now}.get(c) for c in COLUMNS) for r in rows]
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    table = f"{args.catalog}.{args.schema}.{TABLES[args.kind]}"
    spark.createDataFrame(data, SCHEMA).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table)
    log.info("%s: %d rows written", table, len(data))


if __name__ == "__main__":
    main()
