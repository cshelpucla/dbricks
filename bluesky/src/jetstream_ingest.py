"""Continuously ingest Bluesky Jetstream (v2) events into a bronze Delta table.

Runs as a Databricks serverless job task. On start it resumes from the newest
event already in the table (minus a short rewind), so restarts and host
failovers don't lose data. Jetstream delivers at-least-once and the rewind
re-reads a few seconds, so the silver pipeline deduplicates.

Cursors are unix-microsecond timestamps rather than seq numbers, because seq
differs between Jetstream hosts. Jetstream keeps a 36-hour lookback window; a
cursor older than that starts at the oldest retained event (a gap in the data).

Local smoke test (no Spark needed):
    python jetstream_ingest.py --hosts jetstream.us-east.bsky.network \
        --collections app.bsky.feed.post --dry-run-seconds 10
"""
import argparse
import json
import logging
import queue
import random
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from websockets.sync.client import connect

log = logging.getLogger("jetstream_ingest")

ENDPOINT = "/xrpc/network.bsky.jetstream.subscribeEvents"
STALL_SECONDS = 300  # fail the run (and let the job restart it) if no events arrive for this long
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Bronze columns, extracted server-side from the event payload.
BRONZE_COLUMNS = [
    "event:seq::bigint AS seq",
    "unix_micros(to_timestamp(event:time::string)) AS time_us",
    "to_timestamp(event:time::string) AS event_time",
    "to_date(to_timestamp(event:time::string)) AS event_date",
    "substring_index(event:['$type']::string, '#', -1) AS kind",
    "event:did::string AS did",
    "event:collection::string AS collection",
    "event:operation::string AS operation",
    "event:rkey::string AS rkey",
    "event:rev::string AS rev",
    "event",
    "current_timestamp() AS ingested_at",
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--table", help="Bronze table, e.g. catalog.schema.events_raw")
    p.add_argument("--hosts", required=True, help="Comma-separated Jetstream v2 hosts, in failover order")
    p.add_argument("--collections", required=True, help="Comma-separated collection NSIDs")
    p.add_argument("--flush-seconds", type=float, default=30)
    p.add_argument("--max-batch-rows", type=int, default=50000)
    p.add_argument("--rewind-seconds", type=float, default=10)
    p.add_argument("--dry-run-seconds", type=float, default=0,
                   help="Read for N seconds, print counts, and exit without writing")
    args = p.parse_args()
    if not args.dry_run_seconds and not args.table:
        p.error("--table is required unless --dry-run-seconds is set")
    return args


def to_micros(iso_time):
    return (datetime.fromisoformat(iso_time) - EPOCH) // timedelta(microseconds=1)


class JetstreamReader(threading.Thread):
    """Pushes (label, frame) pairs onto a queue, reconnecting and failing over between hosts."""

    def __init__(self, hosts, collections, start_cursor, rewind_us, out):
        super().__init__(daemon=True)
        self.hosts = hosts
        self.collections = collections
        self.start_cursor = start_cursor
        self.rewind_us = rewind_us
        self.out = out
        self.last_time = None  # `time` of the last event received
        self.last_event = time.monotonic()

    def url(self, host):
        params = [("collections", c) for c in self.collections]
        cursor = to_micros(self.last_time) - self.rewind_us if self.last_time else self.start_cursor
        if cursor:
            params.append(("cursor", cursor))
        return f"wss://{host}{ENDPOINT}?{urlencode(params)}", cursor

    def run(self):
        host_idx, failures = 0, 0
        while True:
            host = self.hosts[host_idx % len(self.hosts)]
            url, cursor = self.url(host)
            try:
                with connect(url, subprotocols=["xrpc.v1.json"], open_timeout=30,
                             ping_interval=20, ping_timeout=60, max_size=2**22) as ws:
                    log.info("connected to %s (cursor=%s)", host, cursor)
                    failures = 0
                    for frame in ws:
                        self.handle(frame)
            except Exception as exc:  # network errors, server closes, bad handshakes
                failures += 1
                log.warning("connection to %s lost (%s: %s)", host, type(exc).__name__, exc)
            if failures >= 2 and len(self.hosts) > 1:
                host_idx, failures = host_idx + 1, 0
                log.info("failing over to %s", self.hosts[host_idx % len(self.hosts)])
            time.sleep(min(60, 2 ** failures) + random.random())

    def handle(self, frame):
        try:
            msg = json.loads(frame)
            payload = msg["payload"]
            kind = payload["$type"].rpartition("#")[2]
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("skipping unexpected frame: %.300s", frame)
            return
        if msg.get("$type") != "message" or kind == "info":
            log.warning("server message: %.300s", frame)  # e.g. OutdatedCursor
            return
        self.out.put((payload.get("collection") or kind, frame))
        self.last_time = payload.get("time") or self.last_time
        self.last_event = time.monotonic()


def drain(q, limit):
    rows = []
    try:
        while len(rows) < limit:
            rows.append(q.get_nowait())
    except queue.Empty:
        pass
    return rows


def resume_cursor(spark, table, rewind_us):
    latest = spark.sql(f"SELECT max(time_us) AS t FROM {table}").first().t
    return latest - rewind_us if latest else None


def write(spark, table, rows):
    df = (spark.createDataFrame([(frame,) for _, frame in rows], "raw STRING")
          .selectExpr("parse_json(raw):payload AS event")
          .selectExpr(*BRONZE_COLUMNS))
    for attempt in range(1, 4):
        try:
            df.write.mode("append").saveAsTable(table)
            return
        except Exception:
            if attempt == 3:
                raise  # the job restarts the run, which resumes from the table's cursor
            log.exception("write attempt %d failed; retrying", attempt)
            time.sleep(10 * attempt)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    collections = [c.strip() for c in args.collections.split(",") if c.strip()]
    rewind_us = int(args.rewind_seconds * 1_000_000)

    spark, cursor = None, None
    if not args.dry_run_seconds:
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        cursor = resume_cursor(spark, args.table, rewind_us)
    log.info("starting from %s", f"cursor {cursor}" if cursor else "the live stream")

    q = queue.Queue(maxsize=args.max_batch_rows * 4)
    reader = JetstreamReader(hosts, collections, cursor, rewind_us, q)
    reader.start()

    started = last_flush = time.monotonic()
    totals = Counter()
    while True:
        time.sleep(1)
        now = time.monotonic()
        if now - reader.last_event > STALL_SECONDS:
            raise RuntimeError(f"no events received for {STALL_SECONDS}s")
        done = args.dry_run_seconds and now - started >= args.dry_run_seconds
        if not done and now - last_flush < args.flush_seconds and q.qsize() < args.max_batch_rows:
            continue

        rows = drain(q, args.max_batch_rows)
        last_flush = now
        if rows:
            if spark:
                write(spark, args.table, rows)
            batch = Counter(label for label, _ in rows)
            totals.update(batch)
            lag = time.time() - to_micros(reader.last_time) / 1e6 if reader.last_time else float("nan")
            log.info("%s %d rows, lag %.1fs, queued %d: %s", "wrote" if spark else "read",
                     len(rows), lag, q.qsize(), dict(batch.most_common()))
        if done:
            secs = now - started
            log.info("dry run: %d events in %.0fs (%.0f/s): %s",
                     sum(totals.values()), secs, sum(totals.values()) / secs, dict(totals.most_common()))
            return


if __name__ == "__main__":
    main()
