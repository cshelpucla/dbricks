"""Taxonomies tab: the vocabularies used to tag posts with topics, people and companies, and to name map clusters.

Each taxonomy comes back as one flat tree (nodes with a parent id) plus how many posts in the window each node
tagged. The matching rules shown beside the tree mirror constants in bluesky/src (tests/test_api.py checks them).
"""
import json
import random

from .. import config
from . import sqlexec
from .cache import cached, pmap
from .commons import thumb_url
from .demo import COMPANIES, PEOPLE, SECTOR_OF, TOPICS, _qid

KEYS = ("iptc", "people", "companies", "clusters")
ALIAS_CAP = 8                   # aliases shown per entity

# Mirrors of the enrichment job's settings (bluesky/src/enrich_lib.py, entity_lib.py, entity_loader.py, config.env).
MATCH_LEVELS = (1, 2, 3)
TOPIC_MIN_SCORE = 0.62
TOPIC_MIN_CHARS = 25
TOP_K = 3
ANCHORS = [
    "a greeting such as good morning, good night or hello", "small talk about my day, mood and feelings",
    "a short reply or reaction to another person", "emoji, laughter or an exclamation",
    "a joke, meme or random silly remark", "a question asking followers for recommendations or opinions",
    "self promotion, follow requests and community announcements",
    "a personal life update about family, friends or relationships", "complaining or venting about something personal",
    "thanks, congratulations or good wishes", "a vague opinion without a clear subject",
    "a status update with no particular subject",
]
CONF_FULL, CONF_TAG, CONF_SINGLE = 1.0, 0.9, 0.7
MIN_SITELINKS = 30
NO_CATEGORY = "Popular now (top Wikipedia pageviews)"
NO_SUBJECT = "No shared subject"

ABOUT = {
    "iptc": {
        "name": "IPTC Media Topics", "short": "Topics",
        "source": "IPTC Media Topics, the news industry's subject vocabulary (CC BY 4.0)",
        "source_url": "https://iptc.org/standards/media-topics/",
        "refresh": "Loaded by the enrichment job when it starts; reloaded when IPTC publishes a new release.",
        "table": "topic_taxonomy", "metric": "posts", "metric_note": "posts whose best match is this topic",
        "steps": [
            "Each embedded post is compared with the embeddings of “label: definition” for every concept at levels "
            f"{MATCH_LEVELS[0]} to {MATCH_LEVELS[-1]}, plus {len(ANCHORS)} “no topic” anchors (listed below).",
            f"If the nearest match is an anchor, or the post is under {TOPIC_MIN_CHARS} characters, the post gets no topic.",
            f"Otherwise the top {TOP_K} concepts with cosine similarity of at least {TOPIC_MIN_SCORE} are kept; the best one "
            "is the post’s topic and its top-level ancestor is the post’s subject.",
            "Levels 0, 4 and 5 are never matched directly. Level-0 counts here add up the levels below.",
        ],
        "limits": "Hand checks found about 55 to 65% of per-post topics sensible. Cluster names on the topic map are more reliable.",
    },
    "people": {
        "name": "People", "short": "People",
        "source": "Wikipedia Vital Articles, Level 4 People, plus people in the English Wikipedia monthly top-1000 pageviews "
                  f"in 2 of the last 6 months with at least {MIN_SITELINKS} Wikipedia editions; confirmed as humans through Wikidata",
        "source_url": "https://en.wikipedia.org/wiki/Wikipedia:Vital_articles/Level/4/People",
        "refresh": "Weekly job bluesky-taxonomy-people (Mondays 03:00 UTC).",
        "table": "taxonomy_people", "metric": "mentions", "metric_note": "posts mentioning this person",
        "steps": [
            "Every clean post is matched by name, not by embedding (an embedding cannot tell one politician from another).",
            f"Names of two or more words match regardless of case (confidence {CONF_FULL}); hashtags match the name without "
            f"spaces, such as #donaldtrump ({CONF_TAG}).",
            f"One-word names must be capitalised exactly ({CONF_SINGLE}). A one-word name shared by several people goes to the "
            "best known only if they have 1.5 times the Wikipedia editions of the next; otherwise it matches nobody.",
            "At most 5 people and companies per post.",
        ],
        "limits": "English spellings only; @handles are not used.",
    },
    "companies": {
        "name": "Companies", "short": "Companies",
        "source": "Wikidata items whose class is a business, company, corporation, bank, airline, car maker, media, "
                  f"technology, social network or video game company, with at least {MIN_SITELINKS} Wikipedia editions",
        "source_url": "https://www.wikidata.org/wiki/Q4830453",
        "refresh": "Weekly job bluesky-taxonomy-companies (Mondays 03:30 UTC).",
        "table": "taxonomy_companies", "metric": "mentions", "metric_note": "posts mentioning this company",
        "steps": [
            "Grouped sector > industry > company. The industry is the company’s own Wikidata industry; the 13 GICS-style "
            "sectors are chosen by keyword rules over the industry names, else the description.",
            "Matched by name on every clean post, like people. Legal suffixes are dropped, so “Alphabet Inc.” also "
            "matches “Alphabet”.",
            "A name that is also an everyday word (Apple, Amazon) must appear mid-sentence or in capitals, so “Apple pie” "
            "does not match.",
        ],
        "limits": "English spellings only; @handles are not used.",
    },
    "clusters": {
        "name": "Map clusters", "short": "Clusters",
        "source": "Built from the posts themselves: the topic map job groups embedded posts by meaning and an LLM names each group",
        "source_url": None,
        "refresh": "Every 2 hours (job bluesky-topic-map); the latest run is shown whatever the period.",
        "table": "topic_clusters", "metric": "posts", "metric_note": "posts in this cluster in the latest map run",
        "steps": [
            "Embedded posts are laid out in 2D (UMAP) and grouped by density (HDBSCAN). Posts in no dense group are noise "
            "and belong to no cluster.",
            "Claude Haiku reads each cluster’s most central posts, gives it a 2 to 5 word name, and picks one of the "
            "17 IPTC top-level subjects, or none when the posts are chatter with no shared subject.",
            "If the model is unavailable, the cluster takes its majority IPTC topic (when at least 15% of its posts share it), "
            "else its top hashtag.",
        ],
        "limits": "Cluster ids change between runs, so a cluster cannot be followed over time yet.",
    },
}


def _fq(table: str) -> str:
    return f"{config.CATALOG}.{config.SCHEMA}.{table}"


def _arr(v) -> list:
    """ARRAY columns arrive as JSON text from the statement API."""
    if v is None:
        return []
    return v if isinstance(v, list) else json.loads(v)


def _int(v) -> int:
    return int(v or 0)


# ---- tree builders, shared by the live and demo sources --------------------------------------------------------

def iptc_tree(rows, counts) -> list[dict]:
    """rows: (qcode, label, parent, level, definition); counts: {qcode: (best, top3)}."""
    out = []
    for q, label, parent, level, definition in rows:
        best, any_ = counts.get(q, (0, 0))
        level = int(level)
        out.append({"id": q, "label": label, "parent": parent, "kind": "concept", "posts": best,
                    "info": {"level": level, "definition": definition, "top3": any_, "matched": level in MATCH_LEVELS}})
    return out


def people_tree(rows, counts) -> list[dict]:
    """rows: (qid, label, description, aliases, n_aliases, sitelinks, category, source, image file name or None)."""
    groups, out = {}, []
    for qid, label, desc, aliases, n_aliases, sitelinks, category, source, image in rows:
        cat = category or NO_CATEGORY
        gid = groups.setdefault(cat, "cat:" + cat)
        out.append({"id": qid, "label": label, "parent": gid, "kind": "entity", "posts": counts.get(qid, 0),
                    "info": {"description": desc, "aliases": _arr(aliases)[:ALIAS_CAP], "n_aliases": _int(n_aliases),
                             "sitelinks": _int(sitelinks), "source": source, "image": thumb_url(image)}})
    return [{"id": g, "label": c, "parent": None, "kind": "group", "posts": 0, "info": {}} for c, g in groups.items()] + out


def company_tree(rows, counts) -> list[dict]:
    """rows: (qid, label, description, aliases, n_aliases, sitelinks, sector, industry)."""
    groups, out = {}, []
    for qid, label, desc, aliases, n_aliases, sitelinks, sector, industry in rows:
        sector = sector or "Other"
        sid = "sector:" + sector
        groups.setdefault(sid, (sector, None, "sector"))
        parent = sid
        if industry:
            parent = f"ind:{sector}/{industry}"
            groups.setdefault(parent, (industry, sid, "industry"))
        out.append({"id": qid, "label": label, "parent": parent, "kind": "entity", "posts": counts.get(qid, 0),
                    "info": {"description": desc, "aliases": _arr(aliases)[:ALIAS_CAP], "n_aliases": _int(n_aliases),
                             "sitelinks": _int(sitelinks), "industry": industry, "sector": sector}})
    return [{"id": g, "label": label, "parent": p, "kind": "group", "posts": 0, "info": {"level": lvl}}
            for g, (label, p, lvl) in groups.items()] + out


def cluster_tree(rows) -> list[dict]:
    """rows: (cluster_id, label, topic_l1, label_source, n, iptc_label, iptc_l1, iptc_l1_share, top_hashtags)."""
    groups, out = {}, []
    for cid, label, topic, source, n, iptc_label, iptc_l1, share, tags in rows:
        subject = topic or NO_SUBJECT
        gid = groups.setdefault(subject, "l1:" + subject)
        out.append({"id": f"c:{cid}", "label": label, "parent": gid, "kind": "cluster", "posts": _int(n),
                    "info": {"cluster_id": int(cid), "subject": topic, "label_source": source, "iptc_label": iptc_label,
                             "iptc_l1": iptc_l1, "iptc_l1_share": float(share or 0), "top_hashtags": _arr(tags)[:8]}})
    return [{"id": g, "label": s, "parent": None, "kind": "group", "posts": 0, "info": {}} for s, g in groups.items()] + out


def _about(key: str) -> dict:
    a = dict(ABOUT[key])
    if key == "iptc":
        a["anchors"] = ANCHORS
    return a


# ---- live -------------------------------------------------------------------------------------------------------

class LiveTaxonomy:
    mode = "live"

    def overview(self, hours: int) -> dict:
        return cached(("tax-overview", hours), 60, lambda: self._overview(hours))

    def tree(self, key: str, hours: int) -> dict:
        nodes = cached(("tax-tree", key, hours), 60, lambda: self._tree(key, hours))
        return {"mode": self.mode, "key": key, "hours": hours, "about": _about(key), "meta": self._sizes()[key], "nodes": nodes}

    def _sizes(self) -> dict:
        """Size, load time and two key counts per taxonomy table; available false where the table is missing."""
        queries = {
            "iptc": (("levels", "matchable"),
                     f"SELECT count(*), max(loaded_at), count(DISTINCT level), sum(CASE WHEN level IN "
                     f"({', '.join(map(str, MATCH_LEVELS))}) THEN 1 ELSE 0 END) FROM {_fq('topic_taxonomy')}"),
            "people": (("groups", "from_pageviews"),
                       f"SELECT count(*), max(loaded_at), count(DISTINCT coalesce(category, '')), "
                       f"sum(CASE WHEN source = 'pageviews' THEN 1 ELSE 0 END) FROM {_fq('taxonomy_people')}"),
            "companies": (("sectors", "industries"),
                          f"SELECT count(*), max(loaded_at), count(DISTINCT category), count(DISTINCT industry) "
                          f"FROM {_fq('taxonomy_companies')}"),
            "clusters": (("posts", "posts_with_subject"),
                         f"SELECT count(*), max(computed_at), sum(n), sum(CASE WHEN topic_l1 IS NOT NULL THEN n ELSE 0 END) "
                         f"FROM {_fq('topic_clusters')}"),
        }

        def one(item):
            key, (names, sql) = item
            try:
                size, loaded, *rest = sqlexec.run(sql)[0]
            except Exception as e:  # noqa: BLE001 - a missing table shows as "not loaded", not as a failed tab
                return key, {"available": False, "error": str(e)[:200]}
            return key, {"available": bool(_int(size)), "size": _int(size), "loaded_at": loaded,
                         **{n: _int(v) for n, v in zip(names, rest)}}
        return cached(("tax-sizes",), 600, lambda: dict(pmap(one, queries.items())))

    def _overview(self, hours: int) -> dict:
        params = {"hours": ("INT", hours)}
        since = "event_time > current_timestamp() - make_interval(0, 0, 0, 0, :hours, 0, 0)"
        posts_sql = f"""
SELECT sum(CASE WHEN NOT is_filtered THEN 1 ELSE 0 END), sum(CASE WHEN NOT is_filtered AND embedded THEN 1 ELSE 0 END),
       sum(CASE WHEN NOT is_filtered AND topic_qcode IS NOT NULL THEN 1 ELSE 0 END),
       (SELECT max(event_time) FROM {_fq('post_enriched')} WHERE event_date >= current_date() - 7)
FROM {_fq('post_enriched')} WHERE {since}"""
        tags_sql = f"""
SELECT topic_type, count(DISTINCT uri), count(DISTINCT topic_id)
FROM {_fq('post_topics')} WHERE {since} AND topic_type IN ('iptc', 'person', 'company')
GROUP BY 1"""
        sizes, posts, tags = pmap(lambda f: f(), [self._sizes, lambda: sqlexec.run(posts_sql, params),
                                                  lambda: sqlexec.run(tags_sql, params)])
        clean, embedded, with_topic = (_int(v) for v in posts[0][:3])
        used = {t: (_int(u), _int(d)) for t, u, d in tags}
        window = {"clean": clean, "embedded": embedded, "with_topic": with_topic,
                  "with_person": used.get("person", (0, 0))[0], "people_seen": used.get("person", (0, 0))[1],
                  "with_company": used.get("company", (0, 0))[0], "companies_seen": used.get("company", (0, 0))[1],
                  "concepts_seen": used.get("iptc", (0, 0))[1], "newest": posts[0][3]}
        return {"mode": self.mode, "hours": hours, "window": window, "sizes": sizes,
                "about": {k: {f: ABOUT[k][f] for f in ("name", "short", "metric")} for k in KEYS}}

    def _tree(self, key: str, hours: int) -> list[dict]:
        params = {"hours": ("INT", hours)}
        since = "event_time > current_timestamp() - make_interval(0, 0, 0, 0, :hours, 0, 0)"
        if key == "clusters":
            return cluster_tree(sqlexec.run(
                f"SELECT cluster_id, label, topic_l1, label_source, n, iptc_label, iptc_l1, iptc_l1_share, top_hashtags "
                f"FROM {_fq('topic_clusters')}"))
        if key == "iptc":
            rows_sql = f"SELECT qcode, label, parent_qcode, level, definition FROM {_fq('topic_taxonomy')}"
            counts_sql = (f"SELECT topic_id, count(DISTINCT CASE WHEN rank = 1 THEN uri END), count(DISTINCT uri) "
                          f"FROM {_fq('post_topics')} WHERE {since} AND topic_type = 'iptc' GROUP BY 1")
            rows, counts = pmap(lambda q: sqlexec.run(*q), [(rows_sql, None), (counts_sql, params)])
            return iptc_tree(rows, {q: (_int(b), _int(a)) for q, b, a in counts})
        kind, table, last = (("person", "taxonomy_people", "category, source, get_json_object(to_json(struct(*)), '$.image')") if key == "people"
                             else ("company", "taxonomy_companies", "category, industry"))
        rows_sql = (f"SELECT qid, label, description, slice(aliases, 1, {ALIAS_CAP}), size(aliases), sitelinks, {last} "
                    f"FROM {_fq(table)}")
        counts_sql = (f"SELECT topic_id, count(DISTINCT uri) FROM {_fq('post_topics')} "
                      f"WHERE {since} AND topic_type = '{kind}' GROUP BY 1")
        rows, counts = pmap(lambda q: sqlexec.run(*q), [(rows_sql, None), (counts_sql, params)])
        counts = {q: _int(c) for q, c in counts}
        return (people_tree if key == "people" else company_tree)(rows, counts)


# ---- demo -------------------------------------------------------------------------------------------------------

DEMO_CATEGORY = {"politics and government": "Politicians and leaders", "conflict, war and peace": "Politicians and leaders",
                 "crime, law and justice": "Politicians and leaders", "sport": "Sports figures",
                 "science and technology": "Scientists, inventors and mathematicians",
                 "arts, culture, entertainment and media": "Entertainers", "economy, business and finance": "Businesspeople",
                 "environment": "Rebels, revolutionaries and activists", "health": "Scientists, inventors and mathematicians"}


class DemoTaxonomy:
    """Invented trees with the live shape: the demo map's topics, people and companies, fabricated counts."""
    mode = "demo"

    def __init__(self):
        rng = random.Random(7)
        self.iptc = [(f"demo:{i}", l1, None, 0, f"Demo top-level subject: {l1}.") for i, l1 in enumerate(TOPICS)]
        for i, (l1, subs) in enumerate(TOPICS.items()):
            self.iptc += [(f"demo:{i}.{j}", s, f"demo:{i}", 1, f"Demo concept under {l1}.") for j, s in enumerate(subs)]
        self.weights = {r[0]: rng.random() ** 2 for r in self.iptc if r[3] == 1}
        seen_p, seen_c = {}, {}
        for topic, names in PEOPLE.items():
            for n in names:
                seen_p.setdefault(n, DEMO_CATEGORY.get(topic))
        for names in COMPANIES.values():
            for n in names:
                seen_c.setdefault(n, SECTOR_OF.get(n, ("Other", None)))
        self.people = [(_qid(n), n, "Demo entry", "[]", 0, 300 - 7 * i, cat, "vital" if cat else "pageviews", None)
                       for i, (n, cat) in enumerate(seen_p.items())]
        self.companies = [(_qid(n), n, "Demo entry", "[]", 0, 250 - 6 * i, s, ind) for i, (n, (s, ind)) in enumerate(seen_c.items())]
        self.entity_w = {r[0]: rng.random() ** 2 for r in self.people + self.companies}
        self.clusters = [(i, f"Demo cluster: {s}", l1 if rng.random() > 0.2 else None, "llm", int(200 + 3000 * rng.random() ** 2),
                          s, l1, round(rng.random() * 0.6, 3), json.dumps([s.split()[0]])) for i, (l1, s) in
                         enumerate((l1, s) for l1, subs in TOPICS.items() for s in subs)]

    def overview(self, hours: int) -> dict:
        k = 40 * hours
        n_people, n_comp = len(self.people), len(self.companies)
        sizes = {"iptc": {"available": True, "size": len(self.iptc), "loaded_at": None, "levels": 2,
                          "matchable": len(self.iptc) - len(TOPICS)},
                 "people": {"available": True, "size": n_people, "loaded_at": None,
                            "groups": len({r[6] for r in self.people}), "from_pageviews": sum(r[7] == "pageviews" for r in self.people)},
                 "companies": {"available": True, "size": n_comp, "loaded_at": None,
                               "sectors": len({r[6] for r in self.companies}), "industries": len({r[7] for r in self.companies})},
                 "clusters": {"available": True, "size": len(self.clusters), "loaded_at": None,
                              "posts": sum(r[4] for r in self.clusters), "posts_with_subject": sum(r[4] for r in self.clusters if r[2])}}
        window = {"clean": int(k * 50), "embedded": int(k * 19), "with_topic": int(k * 5.2), "with_person": int(k * 1.9),
                  "people_seen": n_people, "with_company": int(k * 0.8), "companies_seen": n_comp,
                  "concepts_seen": len(self.weights), "newest": None}
        return {"mode": self.mode, "hours": hours, "window": window, "sizes": sizes,
                "about": {k_: {f: ABOUT[k_][f] for f in ("name", "short", "metric")} for k_ in KEYS}}

    def tree(self, key: str, hours: int) -> dict:
        k = 40 * hours
        if key == "iptc":
            nodes = iptc_tree(self.iptc, {q: (int(w * k), int(w * k * 1.8)) for q, w in self.weights.items()})
        elif key == "people":
            nodes = people_tree(self.people, {q: int(self.entity_w[q] * k * 0.3) for q, *_ in self.people})
        elif key == "companies":
            nodes = company_tree(self.companies, {q: int(self.entity_w[q] * k * 0.15) for q, *_ in self.companies})
        else:
            nodes = cluster_tree(self.clusters)
        sizes = self.overview(hours)["sizes"][key]
        return {"mode": self.mode, "key": key, "hours": hours, "about": _about(key), "meta": sizes, "nodes": nodes}
