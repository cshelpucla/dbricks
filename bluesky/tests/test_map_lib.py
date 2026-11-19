import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import map_lib as M  # noqa: E402

TOP = ["sport", "politics and government", "weather"]


def blobs(n_per=150, k=4, d=64, seed=0):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(k, d)) * 6
    X = np.vstack([c + rng.normal(size=(n_per, d)) for c in centres]).astype(np.float32)
    return X, np.repeat(np.arange(k), n_per)


@pytest.fixture(scope="module")
def mapped():
    X, truth = blobs()
    Xp, _ = M.reduce(X, dims=20)
    xy = M.layout(Xp, n_neighbors=15)
    labels = M.find_clusters(xy, min_cluster_size=40)
    return X, truth, Xp, xy, labels


def test_layout_range_and_shape(mapped):
    _, _, Xp, xy, _ = mapped
    assert xy.shape == (Xp.shape[0], 2)
    assert xy.min() >= 0 and abs(xy.max() - M.LAYOUT_RANGE) < 1e-6


def test_clusters_recover_blobs_and_are_ordered_by_size(mapped):
    _, truth, _, _, labels = mapped
    found = [c for c in set(labels) if c >= 0]
    assert len(found) == 4
    for c in found:                                   # each found cluster is (almost) one true blob
        members = truth[labels == c]
        assert np.bincount(members).max() / len(members) > 0.95
    sizes = [int((labels == c).sum()) for c in sorted(found)]
    assert sizes == sorted(sizes, reverse=True)


def same_share(X, groups, k=10):
    """Mean share of each row's k nearest neighbours (cosine) that are in the row's group."""
    Xn = X / np.linalg.norm(X, axis=1, keepdims=True)
    S = Xn @ Xn.T
    np.fill_diagonal(S, -np.inf)
    nn = np.argsort(-S, axis=1)[:, :k]
    return float((groups[nn] == groups[:, None]).mean())


def test_language_centring_lets_subjects_cross_languages():
    rng = np.random.default_rng(1)
    d, n = 64, 60
    topics, offsets = rng.normal(size=(2, d)), rng.normal(size=(3, d)) * 4     # language shifts dwarf subjects
    rows, topic, lang = [], [], []
    for li, code in enumerate(["ja", "en", None]):
        for t in range(2):
            rows.append(topics[t] + offsets[li] + rng.normal(size=(n, d)) * 0.3)
            topic += [t] * n
            lang += [code] * n
    rows.append(topics[0] + rng.normal(size=(5, d)) * 3 + rng.normal(size=d) * 4)  # a rare language
    topic += [0] * 5
    lang += ["eo"] * 5
    X, topic, lang = np.vstack(rows).astype(np.float32), np.array(topic), np.array(lang, dtype=object)
    means = M.language_means(X, lang, min_posts=30)
    assert set(means) == {"ja", "en", "", M.OTHER_LANG}                         # no tag is its own group; rare ones pooled
    Y = M.centre_languages(X, lang, means, strength=1.0)
    assert np.allclose(np.linalg.norm(Y, axis=1), 1, atol=1e-5)
    langs = np.array([str(x) for x in lang])
    assert same_share(X, langs) > 0.95                                          # before: neighbours share a language
    assert same_share(Y, langs) < 0.6 and same_share(Y, topic) > 0.95          # after: they share a subject
    half = M.centre_languages(X, lang, means, strength=0.5)                      # partial centring sits in between
    assert same_share(Y, langs) < same_share(half, langs) < same_share(X, langs)


def test_knn_assign_places_posts_inside_their_island():
    rng = np.random.default_rng(2)
    centres = np.eye(4, 16) * 10
    R = np.vstack([c + rng.normal(size=(40, 16)) for c in centres]).astype(np.float32)
    labels = np.repeat([0, 1, 2, -1], 40)                                       # the fourth blob is noise
    xy = np.repeat([[10.0, 10.0], [50.0, 50.0], [90.0, 10.0], [50.0, 90.0]], 40, axis=0)
    Q = np.vstack([c + rng.normal(size=(5, 16)) * 0.5 for c in centres]).astype(np.float32)
    cids, shares, pos = M.knn_assign(Q, R, labels, xy, k=15)
    assert list(cids) == [0] * 5 + [1] * 5 + [2] * 5 + [-1] * 5
    assert (shares > 0.9).all() and np.allclose(pos[:5], [10, 10]) and np.allclose(pos[-5:], [50, 90])
    split = (centres[0] + centres[1]) / 2                                       # halfway: no clear majority
    cids, shares, _ = M.knn_assign(split[None, :], R, labels, xy, k=15, min_share=0.9)
    assert cids[0] == -1 and shares[0] < 0.9


def test_summarize_picks_majorities_and_central_samples(mapped):
    _, truth, Xp, xy, labels = mapped
    n = len(labels)
    l1 = ["sport" if t == 0 else None for t in truth]
    lab = ["soccer" if t == 0 else None for t in truth]
    tags = [["worldcup"] if t == 0 else [] for t in truth]
    texts = [f"post {i} about blob {truth[i]}" for i in range(n)]
    s = M.summarize(labels, xy, Xp, l1, lab, tags, texts)
    assert len(s) == 4 and sum(c["n"] for c in s) == int((labels >= 0).sum())
    sport = next(c for c in s if c["iptc_l1"] == "sport")
    assert sport["iptc_label"] == "soccer" and sport["iptc_l1_share"] > 0.9 and sport["top_hashtags"] == ["worldcup"]
    assert len(sport["samples"]) == M.SAMPLES_PER_CLUSTER and all("blob 0" in t for t in sport["samples"])
    other = next(c for c in s if c["iptc_l1"] is None)
    assert other["iptc_label"] is None and other["iptc_l1_share"] == 0.0


def test_summarize_takes_the_concept_from_the_majority_subject():
    xy, Xp = np.zeros((8, 2)), np.zeros((8, 3))
    l1 = ["sport"] * 5 + ["economy, business and finance"] * 3
    lab = ["soccer", "soccer", "tennis", "golf", "rugby", "music industry", "music industry", "music industry"]
    s = M.summarize(np.zeros(8, dtype=int), xy, Xp, l1, lab, [[]] * 8, ["t"] * 8)[0]
    assert (s["iptc_label"], s["iptc_l1"]) == ("soccer", "sport")        # not the overall top concept, music industry


def test_parse_label():
    assert M.parse_label('{"label": "Premier League Football", "topic": "Sport"}', TOP) == ("Premier League Football", "sport")
    assert M.parse_label('Sure! ```json\n{"label": "Morning greetings", "topic": "none"}\n```', TOP) == ("Morning greetings", None)
    assert M.parse_label('{"label": "X", "topic": "astrology"}', TOP) == ("X", None)
    assert M.parse_label("no json here", TOP) == (None, None) and M.parse_label(None, TOP) == (None, None)
    assert len(M.parse_label('{"label": "' + "a" * 200 + '"}', TOP)[0]) == 60
    blocks = [{"type": "reasoning", "summary": [{"text": "thinking {not json}"}]}, {"type": "text", "text": '{"label": "Storm Watch", "topic": "weather"}'}]
    assert M.parse_label(blocks, TOP) == ("Storm Watch", "weather")             # content-block replies


def test_prompt_lists_topics_and_posts():
    c = {"cluster_id": 0, "top_hashtags": ["nba"], "iptc_label": "basketball", "iptc_l1": "sport", "iptc_l1_share": 0.4,
         "samples": ["Celtics win", "Jazz lose"]}
    p = M.build_prompt(c, TOP)
    assert "sport | politics and government | weather | none" in p and "- Celtics win" in p and "#nba" in p
    assert "basketball (sport)" in p
    assert "basketball" not in M.build_prompt({**c, "iptc_l1_share": 0.05}, TOP)     # a stray match is not a hint


def test_name_clusters_uses_llm_then_falls_back():
    base = {"n": 10, "x": 1.0, "y": 2.0, "samples": ["a"], "top_hashtags": []}
    cs = [{**base, "cluster_id": 0, "iptc_label": "basketball", "iptc_l1": "sport", "iptc_l1_share": 0.4},
          {**base, "cluster_id": 1, "iptc_label": None, "iptc_l1": None, "iptc_l1_share": 0.0, "top_hashtags": ["caturday"], "top_hashtag_share": 0.4},
          {**base, "cluster_id": 2, "iptc_label": None, "iptc_l1": None, "iptc_l1_share": 0.0}]
    good = M.name_clusters(cs, TOP, lambda p: '{"label": "NBA Night", "topic": "sport"}')
    assert [(c["label"], c["topic_l1"], c["label_source"]) for c in good][0] == ("NBA Night", "sport", "llm")

    def broken(p):
        raise RuntimeError("endpoint down")
    fb = M.name_clusters(cs, TOP, broken, retries=1)
    assert [(c["label"], c["label_source"]) for c in fb] == [("basketball", "iptc"), ("#caturday", "hashtag"), ("Cluster 2", "none")]
    assert fb[0]["label_error"].startswith("RuntimeError")
    rare = {**cs[1], "top_hashtag_share": 0.02}                         # a hashtag a few posts used does not name a cluster
    assert M.fallback_label(rare)[2] == "none"
    assert M.name_clusters(cs, TOP, None)[0]["label_source"] == "iptc"


def test_auto_min_cluster_size():
    assert M.auto_min_cluster_size(1000) == 30 and M.auto_min_cluster_size(40000) == 80
