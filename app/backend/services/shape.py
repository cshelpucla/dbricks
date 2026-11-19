"""Aggregates point rows into topic clusters, the main object on the map.

Row format (shared by every data source), one tuple per post:
    (id, x, y, cluster_id, topic, subtopic, score, lang, country, age_hours[, people, companies])

The optional last two items are tuples of entity ids (Wikidata ids of people and companies the post mentions).

A cluster is one island on the map: its centre is the mean position of its posts, its size the
post count, and `trend` compares the newer half of the time window with the older half
(+1 all new, -1 all old, 0 steady).
"""
import math
from collections import Counter, defaultdict

TOP_LANGS = 3
TOP_ENTITIES = 5


def build_clusters(rows, hours: int) -> dict:
    half = hours / 2
    groups = defaultdict(list)
    for r in rows:
        if r[3] is not None:
            groups[r[3]].append(r)

    topic_names: list[str] = []
    clusters = []
    for cid, members in groups.items():
        n = len(members)
        mx = sum(m[1] for m in members) / n
        my = sum(m[2] for m in members) / n
        spread = math.sqrt(sum((m[1] - mx) ** 2 + (m[2] - my) ** 2 for m in members) / n)
        topic = Counter(m[4] for m in members).most_common(1)[0][0]
        if topic not in topic_names:
            topic_names.append(topic)
        label = Counter(m[5] for m in members if m[5]).most_common(1)
        scores = [m[6] for m in members if m[6] is not None]
        recent = sum(1 for m in members if m[9] < half)
        langs = Counter(m[7] for m in members if m[7]).most_common(TOP_LANGS)
        people = Counter(q for m in members if len(m) > 10 for q in m[10]).most_common(TOP_ENTITIES)
        companies = Counter(q for m in members if len(m) > 11 for q in m[11]).most_common(TOP_ENTITIES)
        clusters.append({
            "id": cid, "label": label[0][0] if label else "", "topic": topic_names.index(topic),
            "n": n, "x": round(mx, 3), "y": round(my, 3), "r": round(spread, 3),
            "score": round(sum(scores) / len(scores), 3) if scores else None,
            "trend": round((recent - (n - recent)) / n, 3),
            "langs": [[code, round(c / n, 3)] for code, c in langs],
            "people": [[q, c] for q, c in people], "companies": [[q, c] for q, c in companies],
        })
    clusters.sort(key=lambda c: -c["n"])
    return {"window_hours": hours, "total": len(rows), "topics": topic_names, "clusters": clusters}
