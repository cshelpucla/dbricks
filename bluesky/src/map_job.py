"""Build the topic map: lay recent post embeddings out in 2D, find clusters and name them.

Runs as a scheduled Databricks serverless job (see setup.sh). Each run samples clean, embedded posts from
post_enriched, lays them out and clusters them, places every other embedded post of the window through its
nearest sampled neighbours, then overwrites two tables that the app reads together (joined on computed_at):
  post_map        uri, x, y, cluster_id (null when the post is in no cluster), in_sample, cluster_share,
                  cluster_topic (the cluster's subject: the post's main topic), computed_at
  topic_clusters  cluster_id, label, topic_l1, n, x, y, majority IPTC topic, top hashtags, computed_at
Logic: map_lib.py. Contract: docs/SPEC.md section 7 and docs/TOPIC_IDENTIFICATION.md.
"""
import argparse
import logging
import math
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import numpy as np


def _code_dir():
    """Serverless runs the script via exec(), so __file__ may be undefined; setup passes --code-dir."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        argv = sys.argv
        return next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--code-dir"), os.getcwd())


sys.path.insert(0, _code_dir())
import map_lib as M  # noqa: E402

log = logging.getLogger("map_job")

MAP_SCHEMA = ("uri STRING, x DOUBLE, y DOUBLE, cluster_id INT, in_sample BOOLEAN, cluster_share DOUBLE, "
              "cluster_topic STRING, computed_at TIMESTAMP")
CLUSTER_SCHEMA = ("cluster_id INT, label STRING, topic_l1 STRING, label_source STRING, n INT, x DOUBLE, y DOUBLE, "
                  "iptc_label STRING, iptc_l1 STRING, iptc_l1_share DOUBLE, top_hashtags ARRAY<STRING>, computed_at TIMESTAMP")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--code-dir", default=None)
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--hours", type=int, default=24, help="Sample posts from this many recent hours")
    p.add_argument("--max-points", type=int, default=40000)
    p.add_argument("--min-cluster-size", type=int, default=0, help="0 = automatic (max(30, points/500))")
    p.add_argument("--label-endpoint", default="databricks-claude-haiku-5-5", help="Chat endpoint; empty disables LLM names")
    p.add_argument("--assign-chunk", type=int, default=40000, help="Posts read per pass when placing posts outside the sample")
    return p.parse_args()


def to_matrix(pdf):
    return np.stack(pdf["embedding"].map(lambda v: np.asarray(v, dtype=np.float32)).to_numpy())


def assign_rest(spark, table, where, sample_uris, means, pca, Xp, labels, xy, chunk):
    """Yields (uri, xy, cluster_id, share) for every other embedded post in the window, placed through its nearest
    sampled neighbours. Reads the window in hash partitions of about `chunk` posts so driver memory stays bounded."""
    total = spark.sql(f"SELECT count(*) AS n FROM {table} WHERE {where}").first().n
    parts = max(1, math.ceil(total / chunk))
    for p in range(parts):
        pdf = spark.sql(f"SELECT uri, lang, embedding FROM {table} WHERE {where} AND pmod(hash(uri), {parts}) = {p}").toPandas()
        pdf = pdf[~pdf["uri"].isin(sample_uris)].drop_duplicates("uri")
        if pdf.empty:
            continue
        Q = pca.transform(M.centre_languages(to_matrix(pdf), list(pdf["lang"]), means)).astype(np.float32)
        cids, shares, pos = M.knn_assign(Q, Xp, labels, xy)
        yield from zip(pdf["uri"], pos, cids, shares)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    fq = lambda t: f"{args.catalog}.{args.schema}.{t}"
    spark.sql(f"CREATE TABLE IF NOT EXISTS {fq('post_map')} ({MAP_SCHEMA})")
    spark.sql(f"CREATE TABLE IF NOT EXISTS {fq('topic_clusters')} ({CLUSTER_SCHEMA})")

    t0 = time.time()
    where = f"embedded AND NOT is_filtered AND event_time > current_timestamp() - make_interval(0, 0, 0, 0, {int(args.hours)}, 0, 0)"
    pdf = spark.sql(f"""
SELECT uri, lang, embedding, topic_l1, topic_label, hashtags, text
FROM {fq('post_enriched')}
WHERE {where}
ORDER BY rand() LIMIT {int(args.max_points)}""").toPandas().drop_duplicates("uri").reset_index(drop=True)
    if len(pdf) < 200:
        log.warning("only %d embedded posts in the last %d hours; leaving the previous map in place", len(pdf), args.hours)
        return
    X = to_matrix(pdf)
    log.info("sampled %d posts in %.0fs", len(X), time.time() - t0)

    t0 = time.time()
    means = M.language_means(X, list(pdf["lang"]))
    Xp, pca = M.reduce(M.centre_languages(X, list(pdf["lang"]), means))
    xy = M.layout(Xp)
    size = args.min_cluster_size or M.auto_min_cluster_size(len(X))
    labels = M.find_clusters(xy, size)
    log.info("layout and clustering %.0fs: %d clusters (min size %d), %.0f%% of posts in none",
             time.time() - t0, labels.max() + 1, size, 100 * float(np.mean(labels < 0)))

    listify = lambda v: list(v) if v is not None else []
    summaries = M.summarize(labels, xy, Xp, list(pdf["topic_l1"]), list(pdf["topic_label"]),
                            [listify(v) for v in pdf["hashtags"]], list(pdf["text"]))
    top_levels = [r.label for r in spark.sql(f"SELECT label FROM {fq('topic_taxonomy')} WHERE level = 0 ORDER BY label").collect()]

    ask = None
    if args.label_endpoint:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
        client = WorkspaceClient()

        def ask(prompt):
            # Room for a reasoning block before the answer; the model sometimes thinks first.
            resp = client.serving_endpoints.query(name=args.label_endpoint, max_tokens=1000,
                                                  messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt)])
            return resp.choices[0].message.content

    t0 = time.time()
    named = M.name_clusters(summaries, top_levels, ask)
    sources = Counter(c["label_source"] for c in named)
    errors = Counter(c["label_error"] for c in named if c["label_error"])
    log.info("named %d clusters in %.0fs: %s%s", len(named), time.time() - t0, dict(sources),
             f"; errors {errors.most_common(2)}" if errors else "")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    clusters = [(c["cluster_id"], c["label"], c["topic_l1"], c["label_source"], c["n"], c["x"], c["y"], c["iptc_label"],
                 c["iptc_l1"], float(c["iptc_l1_share"]), c["top_hashtags"], now) for c in named]
    subject = {c["cluster_id"]: c["topic_l1"] for c in named}
    points = [(u, float(x), float(y), int(c) if c >= 0 else None, True, None, subject.get(int(c)), now)
              for u, (x, y), c in zip(pdf["uri"], xy, labels)]

    t0 = time.time()
    placed = 0
    for u, (x, y), c, share in assign_rest(spark, fq("post_enriched"), where, set(pdf["uri"]), means, pca, Xp, labels,
                                           xy, args.assign_chunk):
        points.append((u, float(x), float(y), int(c) if c >= 0 else None, False, round(float(share), 3),
                       subject.get(int(c)), now))
        placed += c >= 0
    rest = len(points) - len(pdf)
    log.info("placed %d of %d posts outside the sample in a cluster (%.0f%%) in %.0fs", placed, rest,
             100 * placed / max(rest, 1), time.time() - t0)

    # The app joins the two tables on computed_at, so a reader between these writes sees no labels
    # for a moment rather than mismatched ones.
    spark.createDataFrame(clusters, CLUSTER_SCHEMA).write.mode("overwrite").saveAsTable(fq("topic_clusters"))
    (spark.createDataFrame(points, MAP_SCHEMA).write.mode("overwrite").option("overwriteSchema", "true")
     .saveAsTable(fq("post_map")))
    log.info("wrote %d points (%d sampled) and %d clusters (computed_at %s)", len(points), len(pdf), len(clusters),
             now.isoformat())


if __name__ == "__main__":
    main()
