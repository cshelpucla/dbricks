"""World map tab: post volume by inferred location, from post_enriched (no map layout needed)."""
import random
from collections import Counter

from .. import config
from . import sqlexec
from .cache import cached, pmap
from .demo import COMPANIES, PEOPLE, _qid
from .warehouse import _in

LEVELS = ("place", "country")
MAX_POINTS = 1500
ENTITY_LIST_SIZE = 200          # people and companies offered per dropdown


def _filters(f_topics, f_langs, f_people, f_companies, params):
    parts = [_in(col, values, prefix, params)
             for col, values, prefix in (("topic_l1", f_topics, "t"), ("lang", f_langs, "l")) if values]
    # people and companies: the post mentions at least one of the chosen Wikidata ids (matches live in post_topics)
    for kind, ids, prefix in (("person", f_people, "person"), ("company", f_companies, "company")):
        if ids:
            parts.append(f"AND EXISTS (SELECT 1 FROM {config.CATALOG}.{config.SCHEMA}.post_topics pt WHERE pt.uri = e.uri "
                         f"AND pt.topic_type = '{kind}' " + _in("pt.topic_id", ids, prefix, params) + ")")
    return "\n  ".join(parts)


class LiveGeo:
    mode = "live"

    def points(self, hours: int, min_conf: float, level: str, topics: list[str], langs: list[str],
               people: list[str] = (), companies: list[str] = ()) -> dict:
        key = ("geo", hours, min_conf, level, tuple(topics), tuple(langs), tuple(people), tuple(companies))
        return cached(key, 30, lambda: self._points(hours, min_conf, level, topics, langs, people, companies))

    def _points(self, hours: int, min_conf: float, level: str, topics: list[str], langs: list[str],
                people: list[str], companies: list[str]) -> dict:
        t = f"{config.CATALOG}.{config.SCHEMA}.post_enriched"
        params = {"hours": ("INT", hours), "conf": ("DOUBLE", min_conf)}
        extra = _filters(topics, langs, people, companies, params)
        where = "WHERE e.is_filtered = false AND e.event_time > current_timestamp() - make_interval(0, 0, 0, 0, :hours, 0, 0)"
        window = f"FROM {t} e\n{where}"
        name = ("coalesce(geo_place, geo_country, concat(round(geo_lat, 1), ', ', round(geo_lon, 1)))"
                if level == "place" else "geo_country")
        group = f"{name}, geo_country" if level == "place" else "geo_country"
        points_sql = f"""
SELECT {name} AS name, geo_country, round(avg(geo_lat), 3), round(avg(geo_lon), 3), count(*) AS n, mode(topic_l1)
{window} AND geo_confidence >= :conf AND geo_lat IS NOT NULL {extra}
GROUP BY {group}
ORDER BY n DESC
LIMIT {MAX_POINTS}"""
        totals_sql = f"""
SELECT count(*), sum(CASE WHEN geo_confidence >= :conf AND geo_lat IS NOT NULL THEN 1 ELSE 0 END)
{window} {extra}"""
        topics_sql = f"""
SELECT topic_l1, count(*) {window} AND geo_confidence >= :conf AND geo_lat IS NOT NULL AND topic_l1 IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC"""
        # dropdown options, like the topics: every located post in the window, not narrowed by the other filters
        entities_sql = f"""
SELECT kind, qid, label, c FROM (
  SELECT pt.topic_type AS kind, pt.topic_id AS qid, max(pt.topic_label) AS label, count(DISTINCT e.uri) AS c,
         row_number() OVER (PARTITION BY pt.topic_type ORDER BY count(DISTINCT e.uri) DESC) AS rk
  FROM {t} e
  JOIN {config.CATALOG}.{config.SCHEMA}.post_topics pt ON pt.uri = e.uri AND pt.topic_type IN ('person', 'company')
    AND pt.event_time > current_timestamp() - make_interval(0, 0, 0, 0, :hours, 0, 0)
  {where} AND e.geo_confidence >= :conf AND e.geo_lat IS NOT NULL
  GROUP BY 1, 2
) WHERE rk <= {ENTITY_LIST_SIZE}
ORDER BY kind, c DESC"""
        base = {"hours": params["hours"], "conf": params["conf"]}
        rows, totals, topic_counts, entity_counts = pmap(
            lambda q: sqlexec.run(q[0], q[1]),
            [(points_sql, params), (totals_sql, params), (topics_sql, base), (entities_sql, base)])
        totals = totals[0]
        ents = {"person": [], "company": []}
        for kind, qid, label, c in entity_counts:
            ents[kind].append({"value": qid, "label": label, "count": int(c)})
        return {
            "mode": self.mode, "level": level, "hours": hours, "min_conf": min_conf,
            "total": int(totals[0] or 0), "located": int(totals[1] or 0),
            "points": [{"name": r[0], "country": r[1], "lat": float(r[2]), "lon": float(r[3]), "n": int(r[4]),
                        "topic": r[5]} for r in rows if r[2] is not None],
            "topics": [{"value": v, "count": int(c)} for v, c in topic_counts],
            "people": ents["person"], "companies": ents["company"],
        }


class DemoGeo:
    """Synthetic located posts around real cities, labelled as demo data by the UI."""
    mode = "demo"
    CITIES = [("New York City", "US", 40.71, -74.0, 9), ("Los Angeles", "US", 34.05, -118.24, 6), ("Chicago", "US", 41.88, -87.63, 4),
              ("Washington", "US", 38.9, -77.04, 5), ("London", "GB", 51.51, -0.13, 7), ("Toronto", "CA", 43.65, -79.38, 3),
              ("Sydney", "AU", -33.87, 151.21, 2), ("Tokyo", "JP", 35.69, 139.69, 8), ("Osaka", "JP", 34.69, 135.5, 3),
              ("Seoul", "KR", 37.57, 126.98, 3), ("Berlin", "DE", 52.52, 13.4, 3), ("Paris", "FR", 48.86, 2.35, 3),
              ("Madrid", "ES", 40.42, -3.7, 2), ("Mexico City", "MX", 19.43, -99.13, 2), ("São Paulo", "BR", -23.55, -46.63, 4),
              ("Buenos Aires", "AR", -34.6, -58.38, 2), ("Kyiv", "UA", 50.45, 30.52, 3), ("Gaza", "PS", 31.5, 34.47, 2),
              ("Istanbul", "TR", 41.01, 28.98, 1), ("Amsterdam", "NL", 52.37, 4.9, 1), ("Lisbon", "PT", 38.72, -9.14, 1),
              ("Mumbai", "IN", 19.08, 72.88, 1), ("Lagos", "NG", 6.52, 3.38, 1), ("Auckland", "NZ", -36.85, 174.76, 1)]
    TOPICS = ["politics and government", "sport", "arts, culture, entertainment and media", "economy, business and finance",
              "weather", "conflict, war and peace", "science and technology"]

    def points(self, hours, min_conf, level, topics, langs, people=(), companies=()):
        rng = random.Random(f"{hours}{min_conf}{level}")
        scale = hours / 24 * (1.2 - min_conf)
        pts, mentions = [], {"people": Counter(), "companies": Counter()}
        for name, cc, lat, lon, w in self.CITIES:
            n = int(w * 40 * scale * rng.uniform(0.7, 1.3))
            topic = rng.choice(self.TOPICS)
            named = {"people": PEOPLE.get(topic, [])[:2], "companies": COMPANIES.get(topic, [])[:2]}
            for k, pool in named.items():
                mentions[k].update({x: n for x in pool})
            if topics and topic not in topics:
                continue
            if any(sel and not {_qid(x) for x in named[k]} & set(sel) for k, sel in (("people", people), ("companies", companies))):
                continue
            if n:
                pts.append({"name": name, "country": cc, "lat": lat, "lon": lon, "n": n, "topic": topic})
        if level == "country":
            by = {}
            for p in pts:
                b = by.setdefault(p["country"], {"name": p["country"], "country": p["country"], "lat": 0, "lon": 0, "n": 0, "topic": p["topic"]})
                b["lat"] += p["lat"] * p["n"]; b["lon"] += p["lon"] * p["n"]; b["n"] += p["n"]
            pts = [{**b, "lat": b["lat"] / b["n"], "lon": b["lon"] / b["n"]} for b in by.values()]
        pts.sort(key=lambda p: -p["n"])
        located = sum(p["n"] for p in pts)
        return {"mode": self.mode, "level": level, "hours": hours, "min_conf": min_conf, "total": int(located / 0.09) if located else 0,
                "located": located, "points": pts, "topics": [{"value": t, "count": 1} for t in self.TOPICS],
                **{k: [{"value": _qid(x), "label": x, "count": c} for x, c in m.most_common()] for k, m in mentions.items()}}
