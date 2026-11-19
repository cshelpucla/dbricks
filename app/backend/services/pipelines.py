"""Pipelines tab data: status of each resource and rows/bytes written per time bucket.

Throughput comes from Delta table history (one entry per commit), which works for every table
the pipelines write and needs no system tables. Status comes from the Jobs and Pipelines APIs.
"""
import random
from datetime import datetime, timedelta, timezone

from .. import config
from . import sqlexec
from .cache import cached, pmap
from .registry import BY_KEY, RESOURCES, bucket_minutes

CACHE_TTL = 30          # seconds; the tab polls every 60
WRITE_OPS = ("('WRITE', 'MERGE', 'STREAMING UPDATE', 'CREATE TABLE AS SELECT', 'CREATE OR REPLACE TABLE AS SELECT', "
             "'REPLACE TABLE AS SELECT', 'COPY INTO')")  # the map job overwrites its tables each run


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LivePipelines:
    mode = "live"

    def _fq(self, table: str) -> str:
        return f"{config.CATALOG}.{config.SCHEMA}.{table}"

    def status(self, r) -> dict:
        return cached(("status", r.key), CACHE_TTL, lambda: self._status(r))

    def _status(self, r) -> dict:
        w = sqlexec.client()
        out = {"status": "not deployed", "detail": "", "runs": [], "url": None}
        host = (w.config.host or "").rstrip("/")
        if r.kind == "job":
            jobs = list(w.jobs.list(name=r.name))
            if not jobs:
                return out
            job = jobs[0]
            cont = job.settings.continuous if job.settings else None
            runs = list(w.jobs.list_runs(job_id=job.job_id, limit=10))
            out["url"] = f"{host}/jobs/{job.job_id}"
            for run in runs:
                start = run.start_time / 1000 if run.start_time else None
                end = run.end_time / 1000 if run.end_time else None
                state = (run.state.result_state.value if run.state and run.state.result_state
                         else run.state.life_cycle_state.value if run.state else "")
                out["runs"].append({"id": str(run.run_id), "state": state,
                                    "start": _iso(datetime.fromtimestamp(start, timezone.utc)) if start else None,
                                    "seconds": round(end - start) if start and end else None})
            sched = job.settings.schedule if job.settings else None
            running = bool(runs) and runs[0].state and runs[0].state.life_cycle_state.value in ("RUNNING", "PENDING")
            trigger = cont or sched
            paused = trigger is not None and trigger.pause_status and trigger.pause_status.value == "PAUSED"
            idle = "scheduled" if sched and not cont else "stopped"
            out["status"] = "paused" if paused else "running" if running else idle
            out["detail"] = "continuous" if cont else f"schedule {sched.quartz_cron_expression} {sched.timezone_id}" if sched else "manual"
        else:
            found = list(w.pipelines.list_pipelines(filter=f"name LIKE '{r.name}'"))
            if not found:
                return out
            p = found[0]
            out["url"] = f"{host}/pipelines/{p.pipeline_id}"
            state = p.state.value if p.state else ""
            out["status"] = "running" if state == "RUNNING" else "stopped" if state in ("IDLE", "") else state.lower()
            out["detail"] = "continuous pipeline"
            for u in list(w.pipelines.list_updates(p.pipeline_id, max_results=10).updates or []):
                out["runs"].append({"id": u.update_id[:8], "state": u.state.value if u.state else "",
                                    "start": _iso(datetime.fromtimestamp(u.creation_time / 1000, timezone.utc))
                                    if u.creation_time else None, "seconds": None})
        return out

    def _table_series(self, t, hours, bucket):
        sql = f"""
SELECT from_unixtime(floor(unix_timestamp(timestamp) / {bucket * 60}) * {bucket * 60}) AS bucket,
       count(*) AS commits,
       sum(coalesce(cast(operationMetrics['numOutputRows'] AS BIGINT), 0)) AS rows,
       sum(coalesce(cast(operationMetrics['numOutputBytes'] AS BIGINT),
                    cast(operationMetrics['numTargetBytesAdded'] AS BIGINT), 0)) AS bytes,
       avg(cast(operationMetrics['executionTimeMs'] AS DOUBLE)) AS exec_ms
FROM (DESCRIBE HISTORY {self._fq(t)})
WHERE operation IN {WRITE_OPS}
  AND timestamp > current_timestamp() - make_interval(0, 0, 0, 0, :hours, 0, 0)
GROUP BY 1"""
        try:
            rows = sqlexec.run(sql, {"hours": ("INT", hours)})
        except RuntimeError:
            return []             # table not created yet
        return [{"table": t, "ts": b.replace(" ", "T") + "Z", "commits": int(commits), "rows": int(n),
                 "bytes": int(nbytes), "exec_ms": None if ms is None else round(float(ms))}
                for b, commits, n, nbytes, ms in rows]

    def _series(self, tables, hours, bucket):
        parts = pmap(lambda t: cached(("series", t, hours, bucket), CACHE_TTL,
                                      lambda: self._table_series(t, hours, bucket)), tables)
        series = [row for part in parts for row in part]
        series.sort(key=lambda s: (s["ts"], s["table"]))
        return series

    def _table_detail(self, t):
        try:
            detail = sqlexec.run_dicts(f"DESCRIBE DETAIL {self._fq(t)}")[0]
        except RuntimeError:
            return {"table": t, "exists": False}
        fresh = None
        if detail.get("lastModified"):
            modified = datetime.fromisoformat(detail["lastModified"].replace("Z", "+00:00"))
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            fresh = max(0, int((datetime.now(timezone.utc) - modified).total_seconds()))
        return {"table": t, "exists": True, "size_bytes": int(detail["sizeInBytes"]),
                "files": int(detail["numFiles"]), "freshness_s": fresh}

    def _tables(self, tables):
        return pmap(lambda t: cached(("detail", t), CACHE_TTL, lambda: self._table_detail(t)), tables)

    def overview(self, hours: int = 24) -> list[dict]:
        def one(r):
            s = self.status(r)
            series = self._series(r.tables, hours, bucket_minutes(hours)) if s["status"] != "not deployed" else []
            return r, s, series

        result = []
        for r, s, series in pmap(one, RESOURCES):
            rows = sum(x["rows"] for x in series)
            span = hours * 3600
            if series:
                first = datetime.strptime(min(x["ts"] for x in series), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                span = min(span, max(3600, (datetime.now(timezone.utc) - first).total_seconds()))
            result.append({"key": r.key, "name": r.name, "kind": r.kind, "layer": r.layer,
                           "description": r.description, "tables": list(r.tables), "status": s["status"],
                           "detail": s["detail"], "url": s["url"], "rows": rows,
                           "bytes": sum(x["bytes"] for x in series), "rows_per_sec": round(rows / span, 1),
                           "last_run": s["runs"][0]["start"] if s["runs"] else None})
        return result

    def stats(self, key: str, hours: int) -> dict:
        r = BY_KEY[key]
        bucket = bucket_minutes(hours)
        s, series, tables = pmap(lambda f: f(), [lambda: self.status(r), lambda: self._series(r.tables, hours, bucket),
                                                 lambda: self._tables(r.tables)])
        return {"key": key, "hours": hours, "bucket_minutes": bucket, "series": series, "tables": tables, "runs": s["runs"]}


class DemoPipelines:
    """Synthetic numbers shaped like the real ones (about 270 events/s into bronze)."""
    mode = "demo"
    RATES = {"events_raw": 270, "posts": 50, "likes": 205, "reposts": 30, "follows": 18,
             "post_enriched": 40, "post_topics": 60, "post_geo": 6, "post_map": 5, "topic_clusters": 0.01}
    BYTES = {"events_raw": 330, "posts": 900, "likes": 120, "reposts": 120, "follows": 100,
             "post_enriched": 4600, "post_topics": 90, "post_geo": 140, "post_map": 60, "topic_clusters": 300}

    def status(self, r):
        status = {"enrich": "not deployed", "map": "scheduled"}.get(r.key, "running")
        return {"status": status, "detail": "continuous", "url": None}

    def overview(self, hours: int = 24):
        out = []
        for r in RESOURCES:
            s = self.status(r)
            if s["status"] == "not deployed":
                rows = bytes_ = 0
            else:
                rows = sum(self.RATES[t] for t in r.tables) * hours * 3600
                bytes_ = sum(self.RATES[t] * self.BYTES[t] for t in r.tables) * hours * 3600
            out.append({"key": r.key, "name": r.name, "kind": r.kind, "layer": r.layer, "description": r.description,
                        "tables": list(r.tables), "status": s["status"], "detail": s["detail"], "url": None,
                        "rows": rows, "bytes": bytes_, "rows_per_sec": round(rows / (hours * 3600), 1), "last_run": None})
        return out

    def stats(self, key: str, hours: int) -> dict:
        r = BY_KEY[key]
        bucket = bucket_minutes(hours)
        rng = random.Random(f"{key}{hours}")
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now -= timedelta(minutes=now.minute % bucket)
        n = int(hours * 60 / bucket)
        series = []
        if self.status(r)["status"] != "not deployed":
            for i in range(n):
                ts = now - timedelta(minutes=bucket * (n - 1 - i))
                diurnal = 1 + 0.25 * __import__("math").sin((ts.hour + ts.minute / 60) / 24 * 6.283)
                for t in r.tables:
                    rows = int(self.RATES[t] * bucket * 60 * diurnal * rng.uniform(0.9, 1.1))
                    if key == "ingest" and i == n // 3:
                        rows = int(rows * 0.15)        # a restart shows up as a dip
                    series.append({"table": t, "ts": _iso(ts), "commits": max(1, bucket * 2), "rows": rows,
                                   "bytes": rows * self.BYTES[t],
                                   "exec_ms": None if r.kind == "job" else round(rng.uniform(2500, 9000))})
        tables = [{"table": t, "exists": self.status(r)["status"] != "not deployed",
                   "size_bytes": self.RATES[t] * self.BYTES[t] * 3600 * 6, "files": 120,
                   "freshness_s": rng.randint(20, 90)} for t in r.tables]
        start = now - timedelta(hours=7)
        runs = [{"id": "demo1", "state": "RUNNING", "start": _iso(start), "seconds": None},
                {"id": "demo0", "state": "SUCCESS", "start": _iso(start - timedelta(hours=3)), "seconds": 3 * 3600}]
        return {"key": key, "hours": hours, "bucket_minutes": bucket, "series": series, "tables": tables, "runs": runs}
