"""The dataset behind the Chat tab: one row per post, spam and bots removed.

Live mode compiles each validated query plan (see analyst.plan) into one SQL statement over post_enriched,
joined to post_map only when cluster_id is used, and runs it on the warehouse. Every answer covers all posts
in the window, and only the small result leaves the warehouse. Demo mode (or a live workspace whose gold
tables are still empty) runs the same plan in polars over the synthetic source. Post text is left out: the
assistant only needs to aggregate, and free text would let post content steer the model.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

import polars as pl

from .. import config
from . import cache, sqlexec

COLUMNS = {
    "event_time": "timestamp (UTC)", "age_hours": "float, hours before now", "lang": "string, language code",
    "country": "string, ISO country code (often null)", "place": "string, inferred place (often null)",
    "topic": "string, IPTC top-level subject (null for most posts, which have no clear topic)",
    "subtopic": "string, finer topic label",
    "topic_score": "float 0-1, topic confidence", "bot_score": "int, higher = more bot-like",
    "cluster_id": "int, topic-map cluster",
}
SCHEMA = {
    "event_time": pl.Datetime("us", "UTC"), "age_hours": pl.Float64, "lang": pl.String, "country": pl.String,
    "place": pl.String, "topic": pl.String, "subtopic": pl.String, "topic_score": pl.Float64,
    "bot_score": pl.Int64, "cluster_id": pl.Int64,
}
CATEGORICAL = ["lang", "country", "topic", "subtopic", "place"]
SUMMARY_HOURS = 24
MAX_GROUPS = 20_000  # grouped rows fetched for a pivot, which runs here after the query
_TTL = 300
_SQL = {  # chat column -> expression over post_enriched e and post_map m
    "event_time": "e.event_time", "age_hours": "(unix_micros(:now) - unix_micros(e.event_time)) / 3.6e9",
    "lang": "e.lang", "country": "e.geo_country", "place": "e.geo_place", "topic": "e.topic_l1",
    "subtopic": "e.topic_label", "topic_score": "e.topic_score", "bot_score": "e.bot_score", "cluster_id": "m.cluster_id",
}
_AGG_SQL = {"count": "count({})", "n_unique": "count(DISTINCT {})", "sum": "sum({})", "mean": "avg({})",
            "median": "median({})", "min": "min({})", "max": "max({})"}
_UNIT = {"m": 60, "h": 3600, "d": 86400}
log = logging.getLogger(__name__)


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _param_type(col: str) -> str:
    dtype = SCHEMA[col]
    return "STRING" if dtype == pl.String else "BIGINT" if dtype.is_integer() else "DOUBLE"


def _posts(uses: set, bucket: str | None, params: dict) -> str:
    """CTE `posts`: the window's posts with only the columns a query uses, under their chat names."""
    t = f"{config.CATALOG}.{config.SCHEMA}"
    now = datetime.now(timezone.utc)
    hours = params.pop("hours")
    params["cutoff"] = ("TIMESTAMP", _ts(now - timedelta(hours=hours)))
    if "age_hours" in uses:
        params["now"] = ("TIMESTAMP", _ts(now))
    cols = [f"{_SQL[c]} AS `{c}`" for c in sorted(uses or {"event_time"})]
    if bucket:
        s = int(bucket[:-1]) * _UNIT[bucket[-1]]
        cols.append(f"timestamp_seconds(floor(unix_timestamp(e.event_time) / {s}) * {s}) AS `bucket`")
    join = f" LEFT JOIN {t}.post_map m ON m.uri = e.uri" if "cluster_id" in uses else ""
    return (f"posts AS (SELECT {', '.join(cols)} FROM {t}.post_enriched e{join} "
            f"WHERE e.is_filtered = false AND e.event_time > :cutoff)")


def _condition(i: int, col: str, op: str, val, params: dict) -> str:
    c, typ = f"`{col}`", _param_type(col)
    if op == "is_null":
        return f"{c} IS NULL"
    if op == "not_null":
        return f"{c} IS NOT NULL"
    if op in ("in", "not_in"):
        names = []
        for j, v in enumerate(val):
            params[f"f{i}_{j}"] = (typ, v)
            names.append(f":f{i}_{j}")
        if not names:
            return "false" if op == "in" else "true"
        return f"{c} {'NOT IN' if op == 'not_in' else 'IN'} ({', '.join(names)})"
    params[f"f{i}"] = (typ, val)
    if op == "contains":
        return f"contains({c}, :f{i})"
    return f"{c} {'=' if op == '==' else '<>' if op == '!=' else op} :f{i}"


def _uses(p: dict) -> set:
    cols = {c for c, _, _ in p["filters"]} | set(p["group_by"]) | set(p["select"]) | {c for _, c, _ in p["aggs"] if c}
    if p["top"]:
        cols.add(p["top"][0])
    if p["bucket"]:
        cols.add("event_time")
    return cols - {"bucket"}


def compile_sql(p: dict) -> tuple[str, dict]:
    """A validated plan as one SQL statement with bound parameters. Identifiers come from the plan, which
    allows only known columns and names made of letters, digits, spaces and underscores."""
    params: dict = {"hours": p["hours"]}
    where = " AND ".join(_condition(i, *f, params) for i, f in enumerate(p["filters"])) or "true"
    sql = f"WITH {_posts(_uses(p), p['bucket'], params)}, f AS (SELECT * FROM posts WHERE {where}) "
    src = "f"
    if p["top"]:
        c, n = p["top"]
        src = (f"(SELECT * FROM f WHERE `{c}` IN (SELECT `{c}` FROM f WHERE `{c}` IS NOT NULL "
               f"GROUP BY 1 ORDER BY count(*) DESC LIMIT {int(n)}))")
    aggs = ", ".join(("count(*)" if col is None else _AGG_SQL[fn].format(f"`{col}`")) + f" AS `{name}`"
                     for fn, col, name in p["aggs"])
    if p["group_by"]:
        keys = ", ".join(f"`{c}`" for c in p["group_by"])
        sql += f"SELECT {keys}, {aggs} FROM {src} GROUP BY {keys}"
    elif aggs:
        sql += f"SELECT {aggs} FROM {src}"
    else:
        sql += f"SELECT {', '.join(f'`{c}`' for c in p['select'])} FROM {src}"
    if p["pivot"]:  # pivot, sort and limit run here on the grouped rows
        return sql + f" LIMIT {MAX_GROUPS + 1}", params
    if p["sort"]:
        sql += f" ORDER BY `{p['sort']}` {'DESC' if p['descending'] else 'ASC'} NULLS LAST"
    return sql + f" LIMIT {int(p['limit'])}", params


def _frame(cols: list[tuple[str, str]], rows: list[list]) -> pl.DataFrame:
    """A JSON_ARRAY result (every value a string) as a typed DataFrame."""
    df = pl.DataFrame({n: [r[i] for r in rows] for i, (n, _) in enumerate(cols)}, schema={n: pl.String for n, _ in cols})

    def cast(name, typ):
        c = pl.col(name)
        if typ == "TIMESTAMP":
            return c.str.to_datetime(time_zone="UTC", strict=False)
        if typ in ("LONG", "INT", "SHORT", "BYTE"):
            return c.cast(pl.Int64)
        if typ in ("DOUBLE", "FLOAT", "DECIMAL"):
            return c.cast(pl.Float64)
        if typ == "BOOLEAN":
            return c == "true"
        return c
    return df.with_columns(cast(n, t) for n, t in cols)


def query(p: dict) -> pl.DataFrame:
    """Runs a plan on the warehouse; the pivot, if any, is still to do."""
    return _frame(*sqlexec.run_typed(*compile_sql(p)))


def _demo_frame(source, hours: int) -> pl.DataFrame:
    now = datetime.now(timezone.utc)
    rows = [r for r in source.rows if r[9] <= hours]
    # demo row: (id, x, y, cluster, topic, subtopic, score, lang, country, age_hours)
    return pl.DataFrame({
        "event_time": [now - timedelta(hours=r[9]) for r in rows], "age_hours": [r[9] for r in rows],
        "lang": [r[7] for r in rows], "country": [r[8] for r in rows], "place": [None] * len(rows),
        "topic": [r[4] for r in rows], "subtopic": [r[5] for r in rows], "topic_score": [r[6] for r in rows],
        "bot_score": [0] * len(rows), "cluster_id": [r[3] for r in rows],
    }, schema_overrides={"event_time": pl.Datetime("us", "UTC"), "place": pl.String})


def demo_frame(source, hours: int) -> pl.DataFrame:
    return cache.cached(("chat-demo", hours), _TTL, lambda: _demo_frame(source, hours))


def _describe_sql(hours: int) -> dict:
    params: dict = {"hours": hours}
    parts = ["SELECT '' AS k, CAST(NULL AS STRING) AS v, count(*) AS n, min(`event_time`) AS t0, max(`event_time`) AS t1 FROM posts"]
    parts += [f"(SELECT '{c}', `{c}`, count(*), NULL, NULL FROM posts WHERE `{c}` IS NOT NULL GROUP BY 2 ORDER BY 3 DESC LIMIT 15)"
              for c in CATEGORICAL]
    rows = sqlexec.run(f"WITH {_posts({'event_time', *CATEGORICAL}, None, params)} " + " UNION ALL ".join(parts), params)
    out: dict = {"rows": 0, "top_values": {c: [] for c in CATEGORICAL}}
    for k, v, n, t0, t1 in rows:
        if k:
            out["top_values"][k].append([v, int(n)])
        else:
            out["rows"] = int(n)
            if t0:
                out["from"], out["to"] = t0, t1
    return out


def _describe_polars(df: pl.DataFrame) -> dict:
    out: dict = {"rows": df.height}
    if df.height:
        out["from"], out["to"] = df["event_time"].min().isoformat(), df["event_time"].max().isoformat()
    out["top_values"] = {c: df[c].drop_nulls().value_counts(sort=True).head(15).rows() for c in CATEGORICAL}
    return out


def describe(source, hours: int) -> dict:
    """Row count, time span and the 15 most common values of each categorical column; cached a few minutes."""
    return cache.cached(("chat-describe", source.mode, hours), _TTL, lambda: _describe_sql(hours) if source.mode == "live"
                        else _describe_polars(demo_frame(source, hours)))


def summary(source) -> str:
    """What the default window holds, for the system prompt: size, time span and the common values of lang,
    topic and country, so the model can usually skip describe_data. Empty when the data cannot be read."""
    try:
        d = describe(source, SUMMARY_HOURS)
    except Exception as e:  # noqa: BLE001 - the turn still runs; describe_data will surface the error
        log.warning("chat data summary unavailable: %s", str(e)[:200])
        return ""
    span = f", {d['from'][:16].replace('T', ' ')} to {d['to'][:16].replace('T', ' ')} UTC" if d.get("from") else ""
    lines = [f"Last {SUMMARY_HOURS} hours: {d['rows']:,} posts{span}."]
    for c, n in (("topic", 15), ("lang", 12), ("country", 12)):
        if d["top_values"][c]:
            lines.append(f"Common {c} values: {', '.join(str(v) for v, _ in d['top_values'][c][:n])}.")
    return "\n".join(lines) + "\n"


def prewarm(source) -> None:
    """Read the summary in the background (waking the warehouse) so the first question does not wait for it."""
    inner = getattr(source, "current", lambda: source)()
    threading.Thread(target=summary, args=(inner,), daemon=True).start()
