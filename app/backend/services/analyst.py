"""Chat assistant: a Pydantic AI agent over a model-serving chat endpoint, with Pydantic AI Harness
capabilities (ToolBudgets, DetectLoops) keeping the loop bounded. Three tools:

  describe_data  what the dataset holds (row count, time span, common values)
  query_data     filter / group / aggregate / pivot from a structured spec, optionally charting the result
  make_chart     build a Plotly or Chart.js figure from a table that query_data produced

The model never writes code and never sees post text. It describes a query as typed arguments; this
module validates every column and operator against a whitelist into a plan, which runs as SQL on the
warehouse (live) or in polars (demo). Charts are built here from the stored table, so the numbers on
screen are the query result and not retyped by the model.

Each model round trip costs about two seconds, so the tools are shaped to need few of them: the system
prompt carries the common values (no describe_data call first), `top` does "top N categories" inside one
query, and `chart` draws the result in the same call.
"""
import re
import threading
from typing import Any, Literal

import anyio
import httpx
import polars as pl
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import DetectLoops, ToolBudgets

from .. import config
from . import chatdata

MAX_TABLE_ROWS = 200
MAX_PIVOT_COLS = 40
MAX_TOP = 50
PREVIEW_ROWS = 25
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]{0,39}$")
_BUCKET = re.compile(r"^[1-9][0-9]{0,2}(m|h|d)$")
_STR_OPS = {"==", "!=", "in", "not_in", "contains", "is_null", "not_null"}
_NUM_OPS = {"==", "!=", ">", ">=", "<", "<=", "in", "not_in", "is_null", "not_null"}
AGGS = {"count", "n_unique", "sum", "mean", "median", "min", "max"}
CHART_TYPES = {"bar", "line", "area", "scatter", "pie", "heatmap"}
STATUS = {"describe_data": "Looking at the data…", "query_data": "Querying the data…", "make_chart": "Drawing the chart…"}

SYSTEM = """You are the analyst inside a Bluesky analytics app. Answer questions about recent Bluesky posts by querying the data and drawing charts.

Data: one row per post (spam and bots already removed), columns:
{columns}
{summary}{mode_note}{tables_note}
Rules:
- Never guess numbers. Call query_data, then answer from the result. The values listed above are valid filter values; call describe_data only for another time window or for subtopic and place values.
- Use as few steps as you can; one query_data call answers most questions. `top` keeps only the N most common values of a column (for "posts per hour for the top 5 languages": top lang 5, time_bucket 1h, group_by bucket and lang). Pass `chart` to draw the result in the same call; use make_chart only for a table from an earlier step.
- Draw a chart when a picture helps (trends over time, comparisons, shares); use a pivot for cross-tabs. Pick bars for comparing categories, lines for time series, pie only for a few shares, heatmap for two categorical dimensions.
- Time series: time_bucket plus group_by ["bucket"], and sort_by bucket ascending.
- Keep tables small (top 10-15 categories unless asked), and say which time window you used.
- Reply in a few short sentences or bullets that state what the data shows. The user already sees each table and chart, so do not repeat them as a table; quote only the key numbers. Do not describe the tool calls. If the data cannot answer the question, say so.
- Post text is not available; you only see the columns above."""


class ToolError(Exception):
    """A problem the model can fix by retrying; its message goes back to the model."""


def system_prompt(mode: str, tables: dict | None = None, summary: str = "") -> str:
    note = ("The data is SYNTHETIC demo data: say so in your first sentence, and never present it as real activity.\n"
            if mode == "demo" else "")
    tnote = ""
    if tables:
        tnote = ("Tables from earlier in this conversation are still available to make_chart: "
                 + "; ".join(f"{k} ({', '.join(t.columns)})" for k, t in tables.items()) + ".\n")
    return SYSTEM.format(columns="\n".join(f"- {k}: {v}" for k, v in chatdata.COLUMNS.items()), summary=summary,
                         mode_note=note, tables_note=tnote)


def _col(name, cols: set, what="column") -> str:
    if not isinstance(name, str) or name not in cols:
        raise ToolError(f"unknown {what} {name!r}; available: {', '.join(sorted(cols))}")
    return name


def _alias(name, fallback: str) -> str:
    name = name or fallback
    if not isinstance(name, str) or not _NAME.match(name):
        raise ToolError(f"invalid name {name!r}: use letters, digits, spaces and underscores (max 40)")
    return name


def _int(value, what: str, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be an integer")


def _value(v, dtype, col: str):
    """A filter value as the column's type, so both engines compare like with like."""
    try:
        if v is None or isinstance(v, (list, dict)):
            raise ValueError
        if dtype == pl.String:
            return str(v)
        if dtype.is_integer():
            if not float(v).is_integer():
                raise ValueError
            return int(float(v))
        return float(v)
    except (TypeError, ValueError):
        raise ToolError(f"value {v!r} does not fit column {col} ({dtype}); use is_null / not_null for missing values")


def _filter(f: dict, schema: dict) -> tuple:
    col = _col(f.get("column"), set(schema))
    op, val = f.get("op"), f.get("value")
    dtype = schema[col]
    if dtype.is_temporal():
        raise ToolError("filter on age_hours or use `hours` instead of filtering event_time")
    allowed = _NUM_OPS if dtype.is_numeric() or dtype == pl.Boolean else _STR_OPS
    if op not in allowed:
        raise ToolError(f"operator {op!r} is not valid for column {col} ({dtype}); use one of {sorted(allowed)}")
    if op in ("is_null", "not_null"):
        return col, op, None
    if val is None:
        raise ToolError(f"filter on {col} needs a value")
    if op in ("in", "not_in"):
        if not isinstance(val, list) or len(val) > 100:
            raise ToolError("in / not_in need an array of at most 100 values")
        return col, op, [_value(v, dtype, col) for v in val]
    if isinstance(val, (list, dict)):
        raise ToolError(f"operator {op} needs a single value")
    return col, op, _value(val, dtype, col)


def _agg(a: dict, cols: set) -> tuple[str, str | None, str]:
    fn = a.get("fn")
    if fn not in AGGS:
        raise ToolError(f"unknown aggregate {fn!r}; use one of {sorted(AGGS)}")
    if fn == "count" and not a.get("column"):
        return fn, None, _alias(a.get("as"), "posts")
    col = _col(a.get("column"), cols)
    return fn, col, _alias(a.get("as"), f"{fn}_{col}")


def plan(spec: dict, schema: dict | None = None) -> dict:
    """A query_data spec, validated against the columns, operators and names allowed. Order of operations:
    filters, top, time_bucket, group_by + aggs (or select), then pivot, sort and limit (see finish)."""
    schema = dict(schema or chatdata.SCHEMA)
    filters = [_filter(f, schema) for f in spec.get("filters") or []]
    top = None
    if (t := spec.get("top")):
        if not isinstance(t, dict):
            raise ToolError("top needs a column and n")
        top = (_col(t.get("column"), {c for c, d in schema.items() if not d.is_temporal()}),
               _int(t.get("n") or 10, "top.n", 1, MAX_TOP))
    bucket = spec.get("time_bucket") or None
    if bucket:
        if not _BUCKET.match(str(bucket)):
            raise ToolError("time_bucket must look like 15m, 1h or 1d")
        schema["bucket"] = pl.Datetime("us", "UTC")
    cols = set(schema)
    group_by = [_col(g, cols) for g in spec.get("group_by") or []]
    aggs = [_agg(a, cols) for a in spec.get("aggs") or []]
    if group_by and not aggs:
        aggs = [("count", None, "posts")]
    select = []
    if group_by or aggs:
        out = group_by + [name for _, _, name in aggs]
    elif spec.get("select"):
        select = out = [_col(c, cols) for c in spec["select"]]
    else:
        raise ToolError("give group_by and aggs, or select, so the table is not the whole dataset")
    if len(set(out)) != len(out):
        raise ToolError(f"result column names must be unique: {out}")
    pivot = None
    if (pv := spec.get("pivot")):
        pivot = tuple(_col(pv.get(k), set(out)) for k in ("index", "on", "values"))
    sort = spec.get("sort_by") or None
    if sort and not pivot:  # after a pivot the columns are known only once it has run
        _col(sort, set(out))
    return {"hours": _int(spec.get("hours") or 24, "hours", 1, 720), "filters": filters, "top": top, "bucket": bucket,
            "group_by": group_by, "aggs": aggs, "select": select, "pivot": pivot, "sort": sort,
            "descending": bool(spec.get("descending")), "limit": _int(spec.get("limit") or 50, "limit", 1, MAX_TABLE_ROWS)}


def _expr(col: str, op: str, val) -> pl.Expr:
    c = pl.col(col)
    if op == "is_null":
        return c.is_null()
    if op == "not_null":
        return c.is_not_null()
    if op in ("in", "not_in"):
        return c.is_in(val) if op == "in" else ~c.is_in(val)
    return {"==": c == val, "!=": c != val, ">": c > val, ">=": c >= val, "<": c < val, "<=": c <= val,
            "contains": c.str.contains(str(val), literal=True)}[op]


_POLARS_AGG = {"count": pl.Expr.count, "n_unique": pl.Expr.n_unique, "sum": pl.Expr.sum, "mean": pl.Expr.mean,
               "median": pl.Expr.median, "min": pl.Expr.min, "max": pl.Expr.max}


def run_polars(df: pl.DataFrame, p: dict) -> pl.DataFrame:
    """A plan over an in-memory frame (demo mode); the pivot, if any, is still to do."""
    lf = df.lazy()
    for f in p["filters"]:
        lf = lf.filter(_expr(*f))
    if p["top"]:
        c, n = p["top"]
        keep = lf.filter(pl.col(c).is_not_null()).group_by(c).len().sort("len", descending=True).head(n).select(c)
        lf = lf.join(keep, on=c, how="semi")
    if p["bucket"]:
        lf = lf.with_columns(pl.col("event_time").dt.truncate(p["bucket"]).alias("bucket"))
    aggs = [pl.len().alias(name) if col is None else _POLARS_AGG[fn](pl.col(col)).alias(name) for fn, col, name in p["aggs"]]
    if p["group_by"]:
        lf = lf.group_by(p["group_by"]).agg(aggs)
    elif aggs:
        lf = lf.select(aggs)
    else:
        lf = lf.select(p["select"])
    return lf.collect()


def finish(out: pl.DataFrame, p: dict) -> pl.DataFrame:
    """Pivot, sort and limit, on the (small) grouped result of either engine."""
    if p["pivot"]:
        idx, on, values = p["pivot"]
        if out[on].n_unique() > MAX_PIVOT_COLS:
            raise ToolError(f"pivot column {on} has more than {MAX_PIVOT_COLS} values; filter or limit it first")
        out = out.pivot(on=on, index=idx, values=values, aggregate_function="first").fill_null(0)
    if p["sort"]:
        out = out.sort(_col(p["sort"], set(out.columns)), descending=p["descending"], nulls_last=True)
    return out.head(p["limit"])


def _jsonable(df: pl.DataFrame, n: int | None = None) -> list[list]:
    rows = df.head(n).rows() if n else df.rows()
    return [[v.isoformat() if hasattr(v, "isoformat") else (round(v, 4) if isinstance(v, float) else v) for v in r]
            for r in rows]


def _wide(df: pl.DataFrame, x: str, y: list[str], series: str | None) -> tuple[list, dict[str, list]]:
    cols = set(df.columns)
    x = _col(x, cols)
    if series:
        series = _col(series, cols)
        if len(y) != 1:
            raise ToolError("with `series`, give exactly one y column")
        if df[series].n_unique() > MAX_PIVOT_COLS:
            raise ToolError(f"series column {series} has more than {MAX_PIVOT_COLS} values")
        df = df.pivot(on=series, index=x, values=_col(y[0], cols), aggregate_function="first", sort_columns=True)
        y = [c for c in df.columns if c != x]
    else:
        y = [_col(c, cols) for c in y]
    if not y:
        raise ToolError("give at least one y column")
    xs = [v.isoformat() if hasattr(v, "isoformat") else v for v in df[x].to_list()]
    return xs, {str(c): df[c].to_list() for c in y}


def build_chart(df: pl.DataFrame, a: dict) -> dict:
    kind, lib = a.get("type"), a.get("library") or "plotly"
    if kind not in CHART_TYPES:
        raise ToolError(f"type must be one of {sorted(CHART_TYPES)}")
    if lib not in ("plotly", "chartjs"):
        raise ToolError("library must be plotly or chartjs")
    if lib == "chartjs" and kind == "heatmap":
        raise ToolError("chartjs cannot draw heatmaps; use plotly")
    y = a.get("y")
    y = [y] if isinstance(y, str) else y
    if not isinstance(y, list):
        raise ToolError("y must be an array of column names")
    xs, ys = _wide(df, a.get("x"), y, a.get("series"))
    title = str(a.get("title") or "")[:120]
    xt, yt = str(a.get("x_title") or a.get("x") or "")[:60], str(a.get("y_title") or ("" if len(ys) > 1 else next(iter(ys))))[:60]
    stacked, horiz = bool(a.get("stacked")), bool(a.get("horizontal"))
    if lib == "plotly":
        layout = {"title": {"text": title}, "xaxis": {"title": {"text": xt}}, "yaxis": {"title": {"text": yt}}}
        if kind == "heatmap":
            names = list(ys)
            return {"library": lib, "title": title, "spec": {"data": [{"type": "heatmap", "x": xs, "y": names,
                    "z": [[v or 0 for v in ys[n]] for n in names], "colorscale": "Blues"}], "layout": layout}}
        if kind == "pie":
            n = next(iter(ys))
            return {"library": lib, "title": title, "spec": {"data": [{"type": "pie", "labels": xs, "values": ys[n], "hole": 0.4}],
                    "layout": {"title": {"text": title}}}}
        traces = []
        for n, v in ys.items():
            t = {"name": n, "x": xs, "y": v}
            if kind == "bar":
                t.update(type="bar")
            elif kind == "scatter":
                t.update(type="scatter", mode="markers")
            else:
                t.update(type="scatter", mode="lines", **({"fill": "tozeroy" if len(ys) == 1 else "tonexty"} if kind == "area" else {}))
                if kind == "area" and stacked:
                    t["stackgroup"] = "one"
            if horiz and kind == "bar":
                t.update(x=v, y=xs, orientation="h")
            traces.append(t)
        if kind == "bar":
            layout["barmode"] = "stack" if stacked else "group"
        if horiz and kind == "bar":
            layout["xaxis"], layout["yaxis"] = layout["yaxis"], {**layout["xaxis"], "autorange": "reversed"}
        layout["showlegend"] = len(traces) > 1
        return {"library": lib, "title": title, "spec": {"data": traces, "layout": layout}}
    # Chart.js: colours are applied by the UI so they follow the theme.
    ctype = {"bar": "bar", "line": "line", "area": "line", "scatter": "scatter", "pie": "doughnut"}[kind]
    if kind == "scatter":
        datasets = [{"label": n, "data": [{"x": a_, "y": b_} for a_, b_ in zip(xs, v)]} for n, v in ys.items()]
        data = {"datasets": datasets}
    elif kind == "pie":
        data = {"labels": xs, "datasets": [{"label": next(iter(ys)), "data": ys[next(iter(ys))]}]}
    else:
        data = {"labels": xs, "datasets": [{"label": n, "data": v, **({"fill": True} if kind == "area" else {})}
                                           for n, v in ys.items()]}
    opts: dict = {"plugins": {"title": {"display": bool(title), "text": title}, "legend": {"display": len(ys) > 1 or kind == "pie"}}}
    if kind != "pie":
        opts["scales"] = {"x": {"title": {"display": bool(xt), "text": xt}, "stacked": stacked},
                          "y": {"title": {"display": bool(yt), "text": yt}, "stacked": stacked}}
        if horiz and kind == "bar":
            opts["indexAxis"] = "y"
    return {"library": lib, "title": title, "spec": {"type": ctype, "data": data, "options": opts}}


class Session:
    """State for one chat turn: the tables produced so far and the artifacts to show the user."""

    def __init__(self, source, tables: dict | None = None):
        self.source = getattr(source, "current", lambda: source)()  # live or demo, fixed for the turn
        self.mode = self.source.mode
        self.tables, self.artifacts = dict(tables or {}), []
        self.summary = chatdata.summary(self.source)
        self._lock = threading.Lock()  # the model may call tools in parallel

    def describe(self, hours: int) -> dict:
        return chatdata.describe(self.source, _int(hours or 24, "hours", 1, 720))

    def query(self, spec: dict) -> dict:
        p = plan(spec)
        if self.mode == "live":
            raw = chatdata.query(p)
            if p["pivot"] and raw.height > chatdata.MAX_GROUPS:
                raise ToolError(f"more than {chatdata.MAX_GROUPS} groups to pivot; filter, use `top`, or use a coarser time_bucket")
        else:
            raw = run_polars(chatdata.demo_frame(self.source, p["hours"]), p)
        res = finish(raw, p)
        with self._lock:
            tid = f"t{len(self.tables) + 1}"
            self.tables[tid] = res
            self.artifacts.append({"kind": "table", "id": tid, "columns": res.columns, "rows": _jsonable(res, 100),
                                   "total_rows": res.height})
        out = {"table": tid, "rows": res.height, "columns": [f"{c} ({t})" for c, t in res.schema.items()],
               "preview": _jsonable(res, PREVIEW_ROWS)}
        if spec.get("chart"):
            # The table is kept even if the chart is wrong, so the model fixes the chart without re-querying.
            try:
                self.chart({**spec["chart"], "table": tid})
                out["chart"] = "shown to the user"
            except (ToolError, pl.exceptions.PolarsError, TypeError, ValueError, KeyError) as e:
                out["chart"] = f"not drawn ({e}); call make_chart on table {tid} to fix it"
        return out

    def chart(self, spec: dict) -> dict:
        tid = spec.get("table")
        if tid not in self.tables:
            raise ToolError(f"unknown table {tid!r}; run query_data first (have: {', '.join(self.tables) or 'none'})")
        art = {"kind": "chart", "table": tid, **build_chart(self.tables[tid], spec)}
        with self._lock:
            self.artifacts.append(art)
        return {"ok": True, "note": "chart shown to the user"}


class Filter(BaseModel):
    column: str
    op: Literal[tuple(sorted(_NUM_OPS | _STR_OPS))]  # type: ignore[valid-type]
    value: Any = Field(None, description="string, number, or an array for in / not_in")


class Agg(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    fn: Literal[tuple(sorted(AGGS))]  # type: ignore[valid-type]
    column: str | None = Field(None, description="not needed for count")
    as_: str | None = Field(None, alias="as", description="name of the result column")


class Pivot(BaseModel):
    index: str
    on: str
    values: str = Field(description="an agg alias")


class Top(BaseModel):
    column: str
    n: int = Field(10, ge=1, le=MAX_TOP, description="how many of the most common values to keep")


class Chart(BaseModel):
    type: Literal["bar", "line", "area", "scatter", "pie", "heatmap"]
    x: str
    y: list[str]
    title: str
    library: Literal["plotly", "chartjs"] = "plotly"
    series: str | None = None
    stacked: bool = False
    horizontal: bool = False
    x_title: str | None = None
    y_title: str | None = None


def _guard(fn, *args):
    """Tool errors the model can fix go back to it as retries."""
    try:
        return fn(*args)
    except ToolError as e:
        raise ModelRetry(str(e))
    except (pl.exceptions.PolarsError, TypeError, ValueError, KeyError) as e:
        raise ModelRetry(f"{type(e).__name__}: {str(e)[:300]}")


def _instructions(ctx: RunContext[Session]) -> str:
    return system_prompt(ctx.deps.mode, ctx.deps.tables, ctx.deps.summary)


def build_agent(model) -> Agent:
    agent = Agent(
        model, deps_type=Session, instructions=_instructions, retries=3, name="bluesky-analyst",
        # Harness capabilities: a hard budget on tool calls, refusal of identical repeats, and loop detection.
        capabilities=[ToolBudgets(total=config.CHAT_MAX_STEPS * 3, per_tool={"describe_data": 2},
                                  refuse_repeats={"describe_data", "query_data", "make_chart"}),
                      DetectLoops()])

    @agent.tool
    def describe_data(ctx: RunContext[Session], hours: int = 24) -> dict:
        """Row count, time span and the 15 most common values of each categorical column for the last N hours (1-720)."""
        return _guard(ctx.deps.describe, hours)

    @agent.tool
    def query_data(ctx: RunContext[Session], hours: int = 24, filters: list[Filter] = [], top: Top | None = None,
                   time_bucket: str | None = None, group_by: list[str] = [], aggs: list[Agg] = [], pivot: Pivot | None = None,
                   select: list[str] = [], sort_by: str | None = None, descending: bool = False, limit: int = 50,
                   chart: Chart | None = None) -> dict:
        """Query the posts and store the result as a table (t1, t2, ...); optionally draw it in the same call.

        Order: filters, top, time_bucket, group_by + aggs, pivot, sort, limit. `top` keeps only rows whose `column` is
        one of its N most common values (after filters). time_bucket (e.g. 15m, 1h, 1d) adds a `bucket` column. pivot
        (after group_by) makes one row per `index` and one column per `on` value; group_by must contain both. Without
        group_by or aggs it returns raw rows (use select). `chart` takes make_chart's arguments, minus `table`, using
        this result's column names. Returns the columns and a preview of the rows.
        """
        spec = {"hours": hours, "filters": [f.model_dump() for f in filters], "top": top.model_dump() if top else None,
                "time_bucket": time_bucket, "group_by": group_by, "aggs": [a.model_dump(by_alias=True) for a in aggs],
                "pivot": pivot.model_dump() if pivot else None, "select": select, "sort_by": sort_by,
                "descending": descending, "limit": limit, "chart": chart.model_dump() if chart else None}
        return _guard(ctx.deps.query, spec)

    @agent.tool
    def make_chart(ctx: RunContext[Session], table: str, type: Literal["bar", "line", "area", "scatter", "pie", "heatmap"],
                   x: str, y: list[str], title: str, library: Literal["plotly", "chartjs"] = "plotly",
                   series: str | None = None, stacked: bool = False, horizontal: bool = False,
                   x_title: str | None = None, y_title: str | None = None) -> dict:
        """Draw a chart from a stored table. plotly is the only library that can do heatmap.

        Wide table: x and y (one or more numeric columns, one series each). Long table: x, a single y and `series`
        (the column whose values become series). Heatmap: x, series (rows) and one y.
        """
        spec = {"table": table, "type": type, "x": x, "y": y, "title": title, "library": library, "series": series,
                "stacked": stacked, "horizontal": horizontal, "x_title": x_title, "y_title": y_title}
        return _guard(ctx.deps.chart, spec)

    return agent


def databricks_model():
    """Chat model on the Databricks serving endpoint, authenticated as the app's service principal (or the CLI profile).

    Its HTTP client belongs to the event loop that first uses it, so the model is only ever run on the server's loop
    (async routes): driving it from run_sync in worker threads, one loop per thread, fails with "bound to a different
    event loop" whenever two requests overlap."""
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from . import sqlexec

    cfg = sqlexec.client().config

    class _Auth(httpx.Auth):  # token refreshed per request by the SDK
        def auth_flow(self, request):
            request.headers.update(cfg.authenticate())
            yield request

    client = AsyncOpenAI(base_url=f"{cfg.host.rstrip('/')}/serving-endpoints", api_key="unused",
                         http_client=httpx.AsyncClient(auth=_Auth(), timeout=120))
    return OpenAIChatModel(config.CHAT_ENDPOINT, provider=OpenAIProvider(openai_client=client))


def to_history(messages: list[dict]) -> list[ModelMessage]:
    """Stored text turns back into model messages. Tool calls are not replayed; tables are kept in the Session."""
    out: list[ModelMessage] = []
    for m in messages:
        out.append(ModelRequest(parts=[UserPromptPart(m["content"])]) if m["role"] == "user"
                   else ModelResponse(parts=[TextPart(m["content"])]))
    return out


def tables_from(messages: list[dict]) -> dict[str, pl.DataFrame]:
    """Rebuild the tables shown earlier in the conversation so follow-ups can chart them."""
    tables: dict[str, pl.DataFrame] = {}
    for m in messages:
        for a in m.get("artifacts") or []:
            if a.get("kind") == "table" and a.get("rows") is not None:
                try:
                    tables[a["id"]] = pl.DataFrame({c: [r[i] for r in a["rows"]] for i, c in enumerate(a["columns"])})
                except Exception:  # an unreadable stored table only loses that table
                    continue
    return tables


async def run_turn(source, model, prior: list[dict], question: str):
    """Runs one turn. prior: stored messages of the conversation so far ({role, content, artifacts}).

    Yields events as it goes: {"type": "status", "text"} before tools run, {"type": "artifact", "artifact"} for
    each table or chart as soon as it exists, and finally {"type": "done", "reply", "artifacts", "mode"}."""
    yield {"type": "status", "text": "Reading the data…"}
    session = await anyio.to_thread.run_sync(Session, source, tables_from(prior))
    sent, reply = 0, ""
    try:
        async with build_agent(model).iter(question, deps=session, message_history=to_history(prior),
                                           usage_limits=UsageLimits(request_limit=config.CHAT_MAX_STEPS * 2)) as run:
            async for node in run:
                for a in session.artifacts[sent:]:  # made by the tool calls that just ran
                    yield {"type": "artifact", "artifact": a}
                sent = len(session.artifacts)
                if Agent.is_call_tools_node(node):
                    calls = [p.tool_name for p in node.model_response.parts if isinstance(p, ToolCallPart)]
                    if calls:
                        yield {"type": "status", "text": STATUS.get(calls[0], "Working…")}
            reply = (run.result.output or "").strip() if run.result else ""
    except UsageLimitExceeded:
        reply = "I ran out of steps before finishing. Try a narrower question."
    for a in session.artifacts[sent:]:
        yield {"type": "artifact", "artifact": a}
    yield {"type": "done", "reply": reply or "I could not produce an answer.", "artifacts": session.artifacts, "mode": session.mode}
