"""Topic map logic: 2D layout of post embeddings, clusters ("islands"), their names, and placing posts
outside the sample on the map.

Pure numpy / scikit-learn / umap-learn plus an injected chat client, so it can be tested without Spark.
Used by map_job.py. Contract: docs/SPEC.md section 7.
"""
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import numpy as np

SAMPLES_PER_CLUSTER = 12      # posts shown to the LLM, the ones nearest the cluster centre
LAYOUT_RANGE = 100.0          # x and y are scaled into [0, LAYOUT_RANGE]
MIN_LANG_POSTS = 30           # languages with fewer sampled posts share one pooled mean
OTHER_LANG = "*"              # key of that pooled mean
# Share of each language's mean removed before layout. On 8,000 real posts, 1.0 broke up the language islands but
# left 28% of posts in no cluster; 0.7 gave the most subject-coherent clusters (0.66 of a cluster's topical posts
# agree on the IPTC subject, against 0.44 with no centring) with 22% in none, mostly chatter.
LANG_CENTRE = 0.7
KNN = 15                      # sampled neighbours that place a post outside the sample
MIN_KNN_SHARE = 0.5           # vote share a cluster needs to claim such a post
IPTC_MIN_SHARE = 0.15         # majority IPTC subject share needed to hint the namer or name a cluster without it


def language_means(X: np.ndarray, langs, min_posts: int = MIN_LANG_POSTS) -> dict:
    """Mean vector per language ("" = no language tag); languages with fewer than min_posts posts are pooled
    under OTHER_LANG."""
    keys = np.array([lang or "" for lang in langs], dtype=object)
    counts = Counter(keys)
    big = sorted(k for k, c in counts.items() if c >= min_posts)
    means = {k: X[keys == k].mean(axis=0) for k in big}
    small = ~np.isin(keys, big)
    means[OTHER_LANG] = (X[small] if small.any() else X).mean(axis=0)
    return means


def centre_languages(X: np.ndarray, langs, means: dict, strength: float = LANG_CENTRE) -> np.ndarray:
    """Subtracts `strength` times each post's language mean and re-normalises. The embedding separates languages
    more strongly than subjects; without this, 99% of Japanese posts landed in subject-less language islands
    whatever they were about (docs/TOPIC_IDENTIFICATION.md)."""
    keys = np.array([(lang or "") if (lang or "") in means else OTHER_LANG for lang in langs], dtype=object)
    Y = np.array(X, dtype=np.float32, copy=True)
    for k in set(keys):
        Y[keys == k] -= strength * means[k]
    return Y / np.maximum(np.linalg.norm(Y, axis=1, keepdims=True), 1e-9)


def reduce(X: np.ndarray, dims: int = 50, seed: int = 42):
    """PCA to `dims` dimensions (fast, and UMAP behaves better on it than on 1,024 raw dimensions).
    Returns (reduced vectors, fitted PCA), so posts outside the sample can be projected the same way."""
    from sklearn.decomposition import PCA
    dims = min(dims, X.shape[0] - 1, X.shape[1])
    pca = PCA(n_components=dims, random_state=seed)
    return pca.fit_transform(X).astype(np.float32), pca


def layout(Xp: np.ndarray, n_neighbors: int = 15, min_dist: float = 0.05, seed: int = 42) -> np.ndarray:
    """UMAP to 2D, scaled into [0, LAYOUT_RANGE] on both axes (aspect ratio kept)."""
    import umap
    xy = umap.UMAP(n_components=2, n_neighbors=n_neighbors, min_dist=min_dist, metric="cosine",
                   random_state=seed).fit_transform(Xp)
    xy = xy - xy.min(axis=0)
    return (xy / max(float(xy.max()), 1e-9) * LAYOUT_RANGE).astype(np.float64)


def auto_min_cluster_size(n: int) -> int:
    return max(30, n // 500)


def find_clusters(xy: np.ndarray, min_cluster_size: int, min_samples: int = 10) -> np.ndarray:
    """HDBSCAN on the 2D layout, so clusters match the islands people see. -1 = not in any cluster.
    Cluster ids are renumbered by size, 0 being the largest."""
    from sklearn.cluster import HDBSCAN
    raw = HDBSCAN(min_cluster_size=min_cluster_size, min_samples=min_samples).fit_predict(xy)
    sizes = Counter(int(c) for c in raw if c >= 0)
    remap = {c: i for i, (c, _) in enumerate(sizes.most_common())}
    return np.array([remap.get(int(c), -1) for c in raw], dtype=np.int64)


def knn_assign(Q: np.ndarray, R: np.ndarray, labels: np.ndarray, xy: np.ndarray, k: int = KNN,
               min_share: float = MIN_KNN_SHARE, chunk: int = 4096):
    """Places posts outside the map sample. Q: their projected vectors; R, labels, xy: the sample's projected
    vectors, cluster ids (-1 = none) and layout. Each post takes the label with the largest cosine-weighted vote
    among its k most similar sampled posts; it joins that cluster when the label is not -1 and holds at least
    min_share of the vote. Returns (cluster ids, vote shares, xy), xy being the mean position of the neighbours
    that voted for the winning label, so a placed post lands inside its island."""
    unit = lambda m: m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    Rn, Qn = unit(np.asarray(R, dtype=np.float32)), unit(np.asarray(Q, dtype=np.float32))
    labels = np.asarray(labels)
    k = min(k, len(Rn))
    cids, shares, pos = np.full(len(Qn), -1, dtype=np.int64), np.zeros(len(Qn)), np.zeros((len(Qn), 2))
    for s in range(0, len(Qn), chunk):
        S = Qn[s:s + chunk] @ Rn.T
        idx = np.argpartition(-S, k - 1, axis=1)[:, :k]
        sims = np.take_along_axis(S, idx, axis=1).clip(min=1e-6)
        labs = labels[idx]
        for i in range(len(idx)):
            votes = Counter()
            for lab, w in zip(labs[i], sims[i]):
                votes[int(lab)] += float(w)
            best, w = votes.most_common(1)[0]
            shares[s + i] = w / float(sims[i].sum())
            pos[s + i] = xy[idx[i][labs[i] == best]].mean(axis=0)
            if best >= 0 and shares[s + i] >= min_share:
                cids[s + i] = best
    return cids, shares, pos


def summarize(labels: np.ndarray, xy: np.ndarray, Xp: np.ndarray, topic_l1, topic_label, hashtags, texts) -> list[dict]:
    """Per cluster: size, centre, majority IPTC subject (with share) and its most common concept, top hashtags,
    and the posts nearest its centre."""
    out = []
    for cid in sorted(set(int(c) for c in labels if c >= 0)):
        idx = np.flatnonzero(labels == cid)
        centre = Xp[idx].mean(axis=0)
        nearest = idx[np.argsort(np.linalg.norm(Xp[idx] - centre, axis=1))]
        l1 = Counter(topic_l1[i] for i in idx if topic_l1[i])
        # the concept is taken from inside the majority subject, so the pair shown to the namer is consistent
        top_l1 = l1.most_common(1)[0][0] if l1 else None
        lab = Counter(topic_label[i] for i in idx if topic_label[i] and topic_l1[i] == top_l1)
        tags = Counter(t for i in idx for t in set(hashtags[i] or []))
        samples, seen = [], set()
        for i in nearest:
            t = re.sub(r"\s+", " ", texts[i] or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                samples.append(t[:220])
            if len(samples) >= SAMPLES_PER_CLUSTER:
                break
        out.append({
            "cluster_id": cid, "n": int(len(idx)),
            "x": float(np.median(xy[idx, 0])), "y": float(np.median(xy[idx, 1])),
            "iptc_l1": l1.most_common(1)[0][0] if l1 else None,
            "iptc_l1_share": round(l1.most_common(1)[0][1] / len(idx), 3) if l1 else 0.0,
            "iptc_label": lab.most_common(1)[0][0] if lab else None,
            "top_hashtags": [t for t, _ in tags.most_common(5)],
            "top_hashtag_share": round(tags.most_common(1)[0][1] / len(idx), 3) if tags else 0.0,
            "samples": samples,
        })
    return out


def build_prompt(c: dict, top_levels: list[str]) -> str:
    hints = []
    if c["top_hashtags"]:
        hints.append("Most common hashtags: " + ", ".join("#" + t for t in c["top_hashtags"]))
    if c["iptc_label"] and c.get("iptc_l1_share", 0) >= IPTC_MIN_SHARE:
        hints.append(f"A rough automatic classifier most often matched: {c['iptc_label']} ({c['iptc_l1']})")
    posts = "\n".join(f"- {s}" for s in c["samples"])
    return (
        "You are naming one cluster on a topic map of recent Bluesky social media posts. "
        "Below are posts from the middle of the cluster.\n\n"
        "Reply with JSON only, no other text: "
        '{"label": "<2 to 5 word English name for what these posts have in common>", '
        '"topic": "<exactly one of: ' + " | ".join(top_levels) + ' | none>"}\n'
        'Use "none" for topic when the posts are personal chatter with no shared subject. '
        "If they share a language, format or kind of post rather than a subject, name that "
        '(for example "Japanese Morning Greetings" or "Now Playing Radio Bots").\n\n'
        + ("\n".join(hints) + "\n\n" if hints else "")
        + "Posts:\n" + posts
    )


def as_text(content) -> str:
    """Chat replies arrive as a string or as a list of content blocks (for example a reasoning block followed
    by a text block); keep only the text blocks."""
    if content is None or isinstance(content, str):
        return content or ""
    parts = []
    for block in content if isinstance(content, list) else [content]:
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text and kind in (None, "text", "output_text"):
            parts.append(text)
    return "\n".join(parts)


def parse_label(text, top_levels: list[str]) -> tuple[str | None, str | None]:
    """(label, topic) from the model's reply; topic is None for "none" or anything not in the list."""
    m = re.search(r"\{.*\}", as_text(text), re.S)
    if not m:
        return None, None
    try:
        d = json.loads(m.group())
    except ValueError:
        return None, None
    label = re.sub(r"\s+", " ", str(d.get("label") or "")).strip().strip("\"'.").strip()[:60] or None
    topic = str(d.get("topic") or "").strip().lower()
    match = next((t for t in top_levels if t.lower() == topic), None)
    return label, match


def fallback_label(c: dict) -> tuple[str, str | None, str]:
    """(label, topic, source) when the LLM is unavailable: majority IPTC label, else top hashtag."""
    if c["iptc_label"] and c["iptc_l1_share"] >= IPTC_MIN_SHARE:
        return c["iptc_label"], c["iptc_l1"], "iptc"
    if c["top_hashtags"] and c.get("top_hashtag_share", 0) >= 0.1:
        return "#" + c["top_hashtags"][0], c["iptc_l1"], "hashtag"
    return f"Cluster {c['cluster_id']}", c["iptc_l1"], "none"


def name_clusters(summaries: list[dict], top_levels: list[str], ask, workers: int = 4, retries: int = 4) -> list[dict]:
    """Adds label, topic_l1, label_source and label_error to each summary. `ask(prompt) -> str` calls the LLM
    (or is None). Pay-per-token endpoints rate-limit bursts, so calls back off and retry before falling back."""
    import time

    def one(c):
        error = None
        if ask is not None:
            for attempt in range(retries):
                try:
                    reply = ask(build_prompt(c, top_levels))
                    label, topic = parse_label(reply, top_levels)
                    if label:
                        return {**c, "label": label, "topic_l1": topic, "label_source": "llm", "label_error": None}
                    error = f"unparseable reply: {as_text(reply)[:120]!r} ({type(reply).__name__})"
                except Exception as e:                       # rate limit, timeout, refusal
                    error = f"{type(e).__name__}: {str(e)[:160]}"
                time.sleep(min(20, 2 ** attempt))
        label, topic, source = fallback_label(c)
        return {**c, "label": label, "topic_l1": topic, "label_source": source, "label_error": error}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, summaries))
