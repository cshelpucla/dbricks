"""Live data source: reads post_map joined to post_enriched through a SQL warehouse.

Runs as the app's service principal. Filter values are always bound as named parameters; only
the validated catalog and schema identifiers are formatted into the SQL text.
"""
import json
import logging
from pathlib import Path

from .. import config
from . import sqlexec
from .commons import thumb_url

QUERIES = Path(__file__).resolve().parent.parent / "queries"
ENTITY_LIST_SIZE = 200          # people and companies offered per panel
log = logging.getLogger(__name__)


def _sql(name: str, filters_sql: str = "", **extra) -> str:
    base = (QUERIES / "_base.sql").read_text().format(
        catalog=config.CATALOG, schema=config.SCHEMA, filters=filters_sql)
    return base + (QUERIES / name).read_text().format(catalog=config.CATALOG, schema=config.SCHEMA, **extra)


def entity_info() -> str:
    """Subquery: qid, description, sector, industry, image for every person and company in the taxonomies."""
    return (QUERIES / "entity_info.sql").read_text().format(catalog=config.CATALOG, schema=config.SCHEMA)


def _in(column: str, values: list[str], prefix: str, params: dict) -> str:
    names = []
    for i, v in enumerate(values):
        params[f"{prefix}{i}"] = ("STRING", v)
        names.append(f":{prefix}{i}")
    return f"AND {column} IN ({', '.join(names)})"


def _where(f) -> tuple[str, dict]:
    params: dict = {"hours": ("INT", f.hours)}
    parts = []
    if f.langs:
        parts.append(_in("e.lang", f.langs, "lang", params))
    if f.topics:
        parts.append(_in("coalesce(c.topic_l1, 'no clear topic')", f.topics, "topic", params))
    if f.countries:
        parts.append(_in("e.geo_country", f.countries, "country", params))
    for kind, ids, prefix in (("person", f.people, "person"), ("company", f.companies, "company")):
        if ids:
            parts.append(f"AND EXISTS (SELECT 1 FROM {config.CATALOG}.{config.SCHEMA}.post_topics pt WHERE pt.uri = e.uri "
                         f"AND pt.topic_type = '{kind}' " + _in("pt.topic_id", ids, prefix, params) + ")")
    if f.min_score:
        params["min_score"] = ("DOUBLE", f.min_score)
        parts.append("AND e.topic_score >= :min_score")
    if f.has_geo:
        parts.append("AND e.geo_confidence >= 0.4")
    if f.q:
        params["q"] = ("STRING", "%" + f.q.replace("%", r"\%").replace("_", r"\_") + "%")
        parts.append("AND e.text ILIKE :q")
    return "\n    ".join(parts), params


def _post_url(uri: str) -> str | None:
    # at://<did>/app.bsky.feed.post/<rkey>
    try:
        _, _, did, _, rkey = uri.split("/")
    except ValueError:
        return None
    return f"https://bsky.app/profile/{did}/post/{rkey}"


class WarehouseSource:
    mode = "live"

    def _run(self, statement: str, params: dict) -> list[list]:
        return sqlexec.run(statement, params)

    def meta(self, hours: int) -> dict:
        from ..models import Filters
        where, params = _where(Filters(hours=hours))
        out = {"topics": [], "langs": [], "countries": [], "people": [], "companies": []}
        total = 0
        for kind, value, c in self._run(_sql("meta.sql", where), params):
            if kind == "total":
                total = int(c)
            else:
                out[{"topic": "topics", "lang": "langs", "country": "countries"}[kind]].append(
                    {"value": value, "count": int(c)})
        for k in out:
            out[k].sort(key=lambda d: -d["count"])
        for kind, qid, label, description, sector, industry, image, c in self._exec_entities(where, params):
            out["people" if kind == "person" else "companies"].append(
                {"value": qid, "label": label, "description": description, "count": int(c),
                 "sector": sector if kind == "company" else None, "industry": industry, "image": thumb_url(image)})
        computed, points = self._run(f"SELECT cast(max(computed_at) AS STRING), count(*) FROM {config.CATALOG}.{config.SCHEMA}.post_map", {})[0]
        return {"mode": self.mode, "hours": hours, "total": total, **out,
                "map_computed_at": computed.replace(" ", "T") + "Z" if computed else None, "map_points": int(points or 0)}

    def _exec_entities(self, where: str, params: dict) -> list[list]:
        """People and companies for the filter panels. Empty (not an error) until the taxonomy jobs have run."""
        try:
            return self._run(_sql("meta_entities.sql", where, limit=ENTITY_LIST_SIZE, entity_info=entity_info()), params)
        except Exception as e:                               # noqa: BLE001 - tables may not exist yet
            log.warning("entity lists unavailable (taxonomy tables not built yet?): %s", str(e)[:120])
            return []

    def clusters(self, f) -> dict:
        where, params = _where(f)
        rows = self._run(_sql("clusters.sql", where), params)
        langs: dict = {}
        total = sum(int(r[1]) for r in rows)
        for cid, lang, c in self._run(_sql("cluster_langs.sql", where), params):
            langs.setdefault(int(cid), []).append([lang, int(c)])
        mentions: dict = {}
        names: dict = {}
        try:
            for cid, kind, qid, label, c in self._run(_sql("cluster_entities.sql", where), params):
                mentions.setdefault(int(cid), {"person": [], "company": []})[kind].append([qid, int(c)])
                names[qid] = label
        except Exception as e:                               # noqa: BLE001 - tables may not exist yet
            log.warning("cluster entities unavailable: %s", str(e)[:120])
        topics: list[str] = []
        clusters = []
        # Trend compares a cluster's share of newer posts with the share across all clusters, so a
        # cluster growing faster than the stream as a whole heats up (+1) and a fading one cools (-1).
        all_recent = sum(int(r[8]) for r in rows)
        base = all_recent / total if total else 0.5
        for cid, n, x, y, spread, label, topic, score, recent in rows:
            n, recent = int(n), int(recent)
            if topic not in topics:
                topics.append(topic)
            trend = (recent / n - base) / max(base, 1 - base) if n else 0.0
            clusters.append({
                "id": int(cid), "label": label or "", "topic": topics.index(topic), "n": n,
                "x": round(float(x), 3), "y": round(float(y), 3), "r": round(float(spread or 0), 3),
                "score": None if score is None else round(float(score), 3),
                "trend": round(max(-1.0, min(1.0, trend)), 3),
                "langs": [[l, round(c / n, 3)] for l, c in langs.get(int(cid), [])],
                "people": mentions.get(int(cid), {}).get("person", []),
                "companies": mentions.get(int(cid), {}).get("company", []),
            })
        clusters.sort(key=lambda c: -c["n"])
        return {"window_hours": f.hours, "total": total, "topics": topics, "clusters": clusters, "entities": names}

    def cluster_posts(self, cluster_id: int, f, limit: int = 25) -> list[dict]:
        where, params = _where(f)
        params["cid"] = ("INT", cluster_id)
        out = []
        for uri, text, topic, sub, score, lang, country, place, bot, tags, domain, age in \
                self._run(_sql("cluster_posts.sql", where, limit=int(limit)), params):
            out.append({"id": uri, "text": text, "topic": topic, "subtopic": sub,
                        "score": None if score is None else round(float(score), 3), "lang": lang,
                        "country": country, "place": place, "bot_score": int(bot or 0),
                        "hashtags": json.loads(tags) if tags else [], "age_hours": round(float(age), 2),
                        "url": _post_url(uri)})
        return out
