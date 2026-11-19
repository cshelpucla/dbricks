"""Continuously enrich Bluesky posts: bot filter, hashtags, geo, embeddings and IPTC topics.

Runs as a Databricks serverless job task. Reads post-create events from the bronze table in time windows,
writes post_enriched, post_topics, post_geo and bot_accounts next to it, and resumes from the newest
time_us already in post_enriched. Contract: docs/ENRICHMENT_SPEC.md; logic: enrich_lib.py.

Local smoke test of the logic with no Spark: see bluesky/tests/test_enrich_lib.py.
"""
import argparse
import io
import json
import logging
import os
import random
import re
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone

import numpy as np
import pandas as pd



def _code_dir():
    """Serverless runs the script via exec(), so __file__ may be undefined; setup passes --code-dir."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        argv = sys.argv
        return next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--code-dir"), os.getcwd())


sys.path.insert(0, _code_dir())
import enrich_lib as E  # noqa: E402
import entity_lib as EN  # noqa: E402

log = logging.getLogger("enrich_job")

GEONAMES = "https://download.geonames.org/export/dump/"
IPTC_URL = "https://cv.iptc.org/newscodes/mediatopic/?lang=x-all&format=json"
UA = {"User-Agent": "bluesky-enrich/1.0"}

DDL = {
    "post_enriched": """(
  uri STRING, did STRING, time_us BIGINT, event_time TIMESTAMP, event_date DATE, created_at TIMESTAMP,
  text STRING, lang STRING, langs ARRAY<STRING>, is_reply BOOLEAN, embed_type STRING,
  link_url STRING, link_domain STRING, hashtags ARRAY<STRING>,
  bot_score INT, bot_reasons ARRAY<STRING>, is_filtered BOOLEAN,
  geo_country STRING, geo_place STRING, geo_lat DOUBLE, geo_lon DOUBLE, geo_kind STRING, geo_confidence DOUBLE,
  topic_qcode STRING, topic_label STRING, topic_l1 STRING, topic_score DOUBLE,
  embedding ARRAY<FLOAT>, embedded BOOLEAN, enrich_version INT, enriched_at TIMESTAMP)""",
    "post_topics": """(uri STRING, event_time TIMESTAMP, event_date DATE, topic_type STRING, topic_id STRING,
  topic_label STRING, rank INT, score DOUBLE)""",
    "post_geo": """(uri STRING, event_time TIMESTAMP, event_date DATE, place STRING, country_code STRING,
  lat DOUBLE, lon DOUBLE, kind STRING, confidence DOUBLE, matched_text STRING)""",
    "topic_taxonomy": """(qcode STRING, label STRING, definition STRING, parent_qcode STRING, level INT,
  path ARRAY<STRING>, path_codes ARRAY<STRING>, embedding ARRAY<FLOAT>, source_released STRING, loaded_at TIMESTAMP)""",
    "bot_accounts": "(did STRING, score INT, reasons ARRAY<STRING>, first_seen TIMESTAMP)",
}
CLUSTER = {"post_enriched": "event_date", "post_topics": "event_date", "post_geo": "event_date"}
SCHEMAS = {name: re.sub(r"\s+", " ", ddl.strip()[1:-1]).strip() for name, ddl in DDL.items()}
COLUMNS = {name: [c.split(" ")[0] for c in re.split(r",\s*(?![^<(]*[>)])", schema)] for name, schema in SCHEMAS.items()}


def pyval(v):
    """pandas/numpy values to plain Python for Spark: Timestamp to datetime, numpy scalars to int/float, NaN to None."""
    if v is None:
        return None
    if v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, (list, tuple, np.ndarray)):
        return [pyval(x) for x in v]
    return v


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--code-dir", default=None, help="Directory holding enrich_lib.py (used when __file__ is undefined)")
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--bronze", required=True, help="Fully qualified bronze table")
    p.add_argument("--window-seconds", type=float, default=120)
    p.add_argument("--interval-seconds", type=float, default=20)
    p.add_argument("--lag-margin-seconds", type=float, default=60)
    p.add_argument("--start-minutes", type=float, default=30)
    p.add_argument("--embed-endpoint", default="databricks-qwen3-embedding-0-6b")
    p.add_argument("--embed-max-per-window", type=int, default=600)
    p.add_argument("--topic-min-score", type=float, default=E.TOPIC_MIN_SCORE)
    p.add_argument("--max-windows", type=int, default=0, help="Stop after this many windows (0 = run forever); for smoke tests")
    return p.parse_args()


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(3):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def load_gazetteer(workdir="/tmp/geonames"):
    """Downloads GeoNames and builds the gazetteer. Returns None (geo falls back to coordinates, flags, TLDs)
    if the download fails, so a GeoNames outage never stops enrichment."""
    try:
        os.makedirs(workdir, exist_ok=True)
        zipfile.ZipFile(io.BytesIO(fetch(GEONAMES + "cities15000.zip"))).extract("cities15000.txt", workdir)
        for name in ("countryInfo.txt", "admin1CodesASCII.txt"):
            with open(os.path.join(workdir, name), "wb") as f:
                f.write(fetch(GEONAMES + name))
        g = E.Gazetteer.from_files(*(os.path.join(workdir, n) for n in ("cities15000.txt", "countryInfo.txt", "admin1CodesASCII.txt")))
        log.info("gazetteer: %d latin names, %d countries", len(g.latin), len(g.country))
        return g
    except Exception:
        log.exception("GeoNames unavailable; running without place-name matching")
        return None


class Enricher:
    def __init__(self, spark, args):
        from databricks.sdk import WorkspaceClient
        self.spark, self.args = spark, args
        self.client = WorkspaceClient()
        self.fq = lambda t: f"{args.catalog}.{args.schema}.{t}"
        self.gaz = load_gazetteer()
        self.ensure_tables()
        self.load_taxonomy()
        self.entities, self.entities_loaded, self.entities_checked = None, None, 0.0
        self.refresh_entities()

    # ---- setup ------------------------------------------------------------------------------------
    def ensure_tables(self):
        for name, ddl in DDL.items():
            cluster = f" CLUSTER BY ({CLUSTER[name]})" if name in CLUSTER else ""
            self.spark.sql(f"CREATE TABLE IF NOT EXISTS {self.fq(name)} {ddl}{cluster}")

    def load_taxonomy(self):
        """Loads (or reuses) the IPTC taxonomy and the no-topic anchors; builds the matching matrix."""
        t = self.fq("topic_taxonomy")
        rows = self.spark.table(t).collect()
        if not rows:
            log.info("loading IPTC Media Topics")
            doc = json.loads(fetch(IPTC_URL))
            concepts = E.parse_iptc(doc)
            vecs, tokens, failed, n = E.embed_texts(self.client, self.args.embed_endpoint,
                                                    [f"{c['label']}: {c['definition']}" for c in concepts])
            if failed:
                raise RuntimeError(f"taxonomy embedding failed ({failed}/{n} batches)")
            released = str(doc.get("dateReleased", ""))
            now = datetime.now(timezone.utc)
            records = [{**c, "embedding": [float(x) for x in v], "source_released": released, "loaded_at": now}
                       for c, v in zip(concepts, vecs)]
            self.write({"topic_taxonomy": records})
            log.info("taxonomy loaded: %d concepts, %d tokens", len(concepts), tokens)
            rows = self.spark.table(t).collect()
        tax = sorted((r.asDict() for r in rows), key=lambda r: r["qcode"])
        self.tax = {r["qcode"]: r for r in tax}
        match = [r for r in tax if r["level"] in E.MATCH_LEVELS]
        self.match = match
        anchors, _, failed, _ = E.embed_texts(self.client, self.args.embed_endpoint, E.ANCHORS)
        if failed:
            raise RuntimeError("anchor embedding failed")
        self.M = np.vstack([E.l2_normalise(np.array([r["embedding"] for r in match])), E.l2_normalise(np.stack(anchors))])
        self.n_anchors = len(E.ANCHORS)
        log.info("topic matrix: %d concepts + %d anchors", len(match), self.n_anchors)

    def refresh_entities(self, every=600):
        """(Re)builds the people/company matcher when the taxonomy tables are new or changed. The tables are filled by
        the separate taxonomy jobs (taxonomy_job.py); until they exist the job runs without entity matching."""
        if time.time() - self.entities_checked < every:
            return
        self.entities_checked = time.time()
        try:
            rows, stamps = [], []
            for name in ("taxonomy_people", "taxonomy_companies"):
                if self.spark.catalog.tableExists(self.fq(name)):
                    rows += [r.asDict() for r in self.spark.table(self.fq(name)).collect()]
            stamps = sorted({str(r["loaded_at"]) for r in rows})
            if not rows or stamps == self.entities_loaded:
                return
            self.entities, self.entities_loaded = EN.EntityMatcher(rows, EN.places_from_gazetteer(self.gaz)), stamps
            log.info("entity matcher: %d people, %d companies, %d names", sum(r["kind"] == EN.PERSON for r in rows),
                     sum(r["kind"] == EN.COMPANY for r in rows), len(self.entities.multi) + len(self.entities.single))
        except Exception:
            log.exception("entity taxonomy unavailable; keeping the previous matcher")

    # ---- one window -------------------------------------------------------------------------------
    def tail_us(self):
        r = self.spark.sql(f"SELECT max(time_us) AS t FROM {self.args.bronze} WHERE collection = 'app.bsky.feed.post' "
                           f"AND event_date >= date_sub(current_date(), 1)").first()
        return r.t

    def cursor_us(self):
        return self.spark.sql(f"SELECT max(time_us) AS t FROM {self.fq('post_enriched')}").first().t

    def read_window(self, lo, hi):
        sql = f"""
SELECT concat('at://', did, '/app.bsky.feed.post/', rkey) AS uri, did, rkey, rev, time_us, event_time,
  event:record.text::string AS text, try_cast(event:record.langs AS ARRAY<STRING>) AS langs,
  event:record.embed['$type']::string AS embed_type,
  coalesce(event:record.embed.external.uri::string, event:record.embed.media.external.uri::string) AS link_url,
  coalesce(event:record.embed.external.title::string, event:record.embed.media.external.title::string) AS link_title,
  try_to_timestamp(event:record.createdAt::string) AS created_at,
  event:record.reply.parent.uri::string AS reply_parent_uri,
  event:record.facets::string AS facets, event:record.tags::string AS tags, event:record.labels.values::string AS labels,
  event:record.via::string AS via,
  (event:record.bridgyOriginalUrl IS NOT NULL OR event:record.fediverseId IS NOT NULL) AS bridged
FROM {self.args.bronze}
WHERE collection = 'app.bsky.feed.post' AND operation = 'create'
  AND event_date >= to_date(timestamp_micros({lo})) AND time_us > {lo} AND time_us <= {hi}"""
        pdf = self.spark.sql(sql).toPandas()
        pdf = pdf.drop_duplicates(subset=["did", "rkey", "rev"]).reset_index(drop=True)
        return pdf

    def known_bots(self):
        return {r.did for r in self.spark.table(self.fq("bot_accounts")).select("did").collect()}

    def process(self, lo, hi):
        t0 = time.time()
        df = self.read_window(lo, hi)
        stats = {"posts": len(df)}
        if df.empty:
            return stats, df
        df["langs"] = df["langs"].map(lambda v: list(v) if v is not None and len(v) else None)
        df["hashtags"] = [E.extract_hashtags(f, t) for f, t in zip(df["facets"], df["tags"])]
        df["mentions"] = df["facets"].map(E.count_mentions)
        df["labels"] = df["labels"].map(E.label_values)
        df["link_domain"] = df["link_url"].map(E.link_domain)
        df["bridged"] = df["bridged"].fillna(False).astype(bool)
        df["created_at"] = pd.to_datetime(df["created_at"])
        df["event_time"] = pd.to_datetime(df["event_time"])
        df, new_bots = E.score_bots(df, self.known_bots(), (hi - lo) / 1e6)
        stats["filtered"] = int(df["is_filtered"].sum())
        stats["reasons"] = Counter(r for rs in df["bot_reasons"] for r in rs)

        clean = df[~df["is_filtered"]]
        # geo
        geo_rows, best = [], {}
        for i, r in clean.iterrows():
            cands = E.geo_candidates(self.gaz, r["text"], r["langs"], r["link_url"], r["hashtags"])
            b = E.best_geo(cands)
            if b:
                best[i] = b
            for c in cands:
                if c["confidence"] >= E.GEO_STORE_MIN:
                    geo_rows.append({"uri": r["uri"], "event_time": r["event_time"].to_pydatetime(), "place": c["place"],
                                     "country_code": c["country_code"], "lat": c["lat"], "lon": c["lon"], "kind": c["kind"],
                                     "confidence": float(c["confidence"]), "matched_text": c["matched_text"]})
        stats["geo_hits"] = sum(1 for b in best.values() if b["confidence"] >= 0.4)

        # embeddings for a capped random sample of eligible clean posts
        df["embed_text"] = [E.embed_text(t, lt) for t, lt in zip(df["text"], df["link_title"])]
        elig = [i for i in clean.index if df.at[i, "embed_text"] is not None]
        if len(elig) > self.args.embed_max_per_window:
            elig = random.Random(lo).sample(elig, self.args.embed_max_per_window)
        vecs, tokens, failed, nb = E.embed_texts(self.client, self.args.embed_endpoint,
                                                 [E.EMBED_INSTRUCTION + df.at[i, "embed_text"] for i in elig])
        if nb and failed * 2 > nb:
            raise RuntimeError(f"embedding endpoint failing: {failed}/{nb} batches")
        stats.update(eligible=len(elig), embed_tokens=tokens, embed_failed=failed)
        embedded = {i: v for i, v in zip(elig, vecs) if v is not None}

        # topics
        topic_rows, topic_of = [], {}
        if embedded:
            idx = list(embedded)
            P = E.l2_normalise(np.stack([embedded[i] for i in idx]))
            results = E.assign_topics(P, self.M, 3, self.args.topic_min_score, self.n_anchors)
            for i, res in zip(idx, results):
                if len(df.at[i, "embed_text"]) < E.TOPIC_MIN_CHARS or not res:    # the text that was embedded, links removed
                    continue
                for rank, (j, score) in enumerate(res, 1):
                    c = self.match[j]
                    topic_rows.append({"uri": df.at[i, "uri"], "event_time": df.at[i, "event_time"].to_pydatetime(), "topic_type": "iptc",
                                       "topic_id": c["qcode"], "topic_label": c["label"], "rank": rank, "score": float(score)})
                    if rank == 1:
                        topic_of[i] = (c, float(score))
        stats["topics"] = len(topic_of)
        self.refresh_entities()
        if self.entities is not None:
            n_people = n_companies = 0
            for i, r in clean.iterrows():
                for m in self.entities.match(r["text"], r["hashtags"]):
                    topic_rows.append({"uri": r["uri"], "event_time": r["event_time"].to_pydatetime(), "topic_type": m["kind"],
                                       "topic_id": m["qid"], "topic_label": m["label"], "rank": None, "score": m["confidence"]})
                    n_people += m["kind"] == EN.PERSON
                    n_companies += m["kind"] == EN.COMPANY
            stats.update(people=n_people, companies=n_companies)
        for i, r in clean.iterrows():
            for tag in r["hashtags"]:
                topic_rows.append({"uri": r["uri"], "event_time": r["event_time"].to_pydatetime(), "topic_type": "hashtag", "topic_id": tag,
                                   "topic_label": tag, "rank": None, "score": None})

        # assemble post_enriched
        now = datetime.now(timezone.utc)
        out = []
        for i, r in df.iterrows():
            b = best.get(i)
            topic = topic_of.get(i)
            l1 = self.tax[E.subject_qcode(topic[0]["path_codes"])]["label"] if topic else None
            v = embedded.get(i)
            out.append({
                "uri": r["uri"], "did": r["did"], "time_us": int(r["time_us"]), "event_time": r["event_time"].to_pydatetime(),
                "created_at": None if pd.isna(r["created_at"]) else r["created_at"].to_pydatetime(), "text": r["text"],
                "lang": r["langs"][0] if r["langs"] else None, "langs": r["langs"],
                "is_reply": bool(r["reply_parent_uri"]), "embed_type": r["embed_type"], "link_url": r["link_url"],
                "link_domain": r["link_domain"], "hashtags": r["hashtags"], "bot_score": int(r["bot_score"]),
                "bot_reasons": r["bot_reasons"], "is_filtered": bool(r["is_filtered"]),
                "geo_country": b["country_code"] if b else None, "geo_place": b["place"] if b else None,
                "geo_lat": b["lat"] if b else None, "geo_lon": b["lon"] if b else None,
                "geo_kind": b["kind"] if b else None, "geo_confidence": b["confidence"] if b else None,
                "topic_qcode": topic[0]["qcode"] if topic else None, "topic_label": topic[0]["label"] if topic else None,
                "topic_l1": l1, "topic_score": topic[1] if topic else None,
                "embedding": None if v is None else [float(x) for x in E.l2_normalise(v[None, :])[0]],
                "embedded": v is not None, "enrich_version": E.VERSION, "enriched_at": now})
        for rows in (geo_rows, topic_rows):
            for r in rows:
                r["event_date"] = r["event_time"].date()
        for r in out:
            r["event_date"] = r["event_time"].date()
        bot_rows = [{"did": r.did, "score": int(r.score), "reasons": list(r.reasons), "first_seen": now} for r in new_bots.itertuples()]
        self.write({"post_geo": geo_rows, "post_topics": topic_rows, "bot_accounts": bot_rows, "post_enriched": out})
        stats["seconds"] = round(time.time() - t0, 1)
        return stats, df

    # ---- writes: post_enriched last, because it holds the cursor -------------------------------------
    def write(self, tables: dict):
        for name in ("topic_taxonomy", "post_geo", "post_topics", "bot_accounts", "post_enriched"):
            records = tables.get(name)
            if not records:
                continue
            cols = COLUMNS[name]
            data = [tuple(pyval(r.get(c)) for c in cols) for r in records]
            self.spark.createDataFrame(data, SCHEMAS[name]).write.mode("append").saveAsTable(self.fq(name))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    enricher = Enricher(spark, args)
    margin, window = int(args.lag_margin_seconds * 1e6), int(args.window_seconds * 1e6)
    windows = 0
    floor = None        # end of the last empty full-width window (see E.next_window)
    while True:
        tail = enricher.tail_us()
        if tail is None:
            time.sleep(args.interval_seconds)
            continue
        start = tail - int(args.start_minutes * 60 * 1e6)
        w = E.next_window(enricher.cursor_us(), tail, margin, window, start, floor)
        if w is None:
            if args.max_windows and windows >= args.max_windows:
                return
            time.sleep(args.interval_seconds)
            continue
        for attempt in range(1, 4):
            try:
                stats, _ = enricher.process(*w)
                break
            except Exception:
                log.exception("window %s failed (attempt %d)", w, attempt)
                if attempt == 3:
                    raise
                time.sleep(10 * attempt)
        windows += 1
        if not stats["posts"] and w[1] - w[0] == window:
            floor = w[1]    # a hole in bronze at least a window long: nothing will ever land here, so move on
        lag = (tail - w[1]) / 1e6
        log.info("window done: lag %.0fs %s", lag, {k: (dict(v.most_common(6)) if isinstance(v, Counter) else v) for k, v in stats.items()})
        if args.max_windows and windows >= args.max_windows:
            return


if __name__ == "__main__":
    main()
