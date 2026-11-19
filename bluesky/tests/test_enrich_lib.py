import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import enrich_lib as E  # noqa: E402


# ---- a tiny gazetteer in GeoNames file formats ----------------------------------------------------
def city(gid, name, alts, lat, lon, cc, admin1, pop, fcode="PPL"):
    cols = [str(gid), name, name, ",".join(alts), str(lat), str(lon), "P", fcode, cc, "", admin1, "", "", "", str(pop), "", "", "", ""]
    return "\t".join(cols)


CITIES = [
    city(1, "Paris", ["巴黎", "Paname"], 48.85, 2.35, "FR", "11", 2138551, "PPLC"),
    city(2, "Nice", [], 43.7, 7.26, "FR", "93", 338620),
    city(3, "Tokyo", ["東京", "Tōkyō"], 35.69, 139.69, "JP", "40", 8336599, "PPLC"),
    city(4, "Dallas", [], 32.78, -96.8, "US", "TX", 1300000),
    city(5, "Paris", [], 33.66, -95.55, "US", "TX", 25171),
    city(6, "New York City", ["New York", "NYC"], 40.71, -74.0, "US", "NY", 8804190),
    city(7, "Austin", [], 30.27, -97.74, "US", "TX", 961855),
    city(8, "Buenos Aires", [], -34.6, -58.38, "AR", "07", 3120612, "PPLC"),
    city(9, "Berlin", [], 52.52, 13.4, "DE", "16", 3426354, "PPLC"),
    city(10, "London", [], 51.5, -0.12, "GB", "ENG", 8961989, "PPLC"),
    city(11, "Brussels", [], 50.85, 4.35, "BE", "BRU", 1019022, "PPLC"),
    city(12, "Seoul", ["서울"], 37.57, 126.98, "KR", "11", 10349312, "PPLC"),
    city(13, "Reading", [], 51.45, -0.97, "GB", "ENG", 230000),
    city(14, "Washington", [], 38.9, -77.04, "US", "DC", 689545, "PPLC"),
    city(16, "Tome", ["登米", "トメ"], 38.7, 141.2, "JP", "23", 30000),
    city(17, "Laï", ["ライ", "ال"], 9.4, 16.3, "TD", "05", 25000),
    city(18, "Buy", [], 54.0, 38.0, "RU", "51", 20000),
    city(19, "Casablanca", ["White House", "Dar el Beida"], 33.6, -7.6, "MA", "06", 3359818),
    city(20, "Nancy", [], 48.69, 6.18, "FR", "44", 105000),
    city(21, "Ebolowa", [], 2.9, 11.15, "CM", "04", 75000),
    city(15, "Brasilia", [], -15.78, -47.93, "BR", "DF", 2500000, "PPLC"),
]
COUNTRIES = ["FR\tFRA\t250\tFR\tFrance\tParis\t0\t67000000", "JP\tJPN\t392\tJA\tJapan\tTokyo\t0\t125000000",
             "US\tUSA\t840\tUS\tUnited States\tWashington\t0\t331000000", "DE\tDEU\t276\tGM\tGermany\tBerlin\t0\t83000000",
             "GB\tGBR\t826\tUK\tUnited Kingdom\tLondon\t0\t67000000", "BE\tBEL\t56\tBE\tBelgium\tBrussels\t0\t11000000",
             "AR\tARG\t32\tAR\tArgentina\tBuenos Aires\t0\t45000000", "KR\tKOR\t410\tKS\tSouth Korea\tSeoul\t0\t51000000",
             "BR\tBRA\t76\tBR\tBrazil\tBrasilia\t0\t214000000"]
ADMIN1 = ["US.TX\tTexas\tTexas\t1", "US.NY\tNew York\tNew York\t2", "CM.04\tSouth\tSouth\t3"]


@pytest.fixture(scope="module")
def gaz(tmp_path_factory):
    d = tmp_path_factory.mktemp("geo")
    (d / "cities.txt").write_text("\n".join(CITIES), encoding="utf-8")
    (d / "countries.txt").write_text("# comment\n" + "\n".join(COUNTRIES), encoding="utf-8")
    (d / "admin1.txt").write_text("\n".join(ADMIN1), encoding="utf-8")
    return E.Gazetteer.from_files(str(d / "cities.txt"), str(d / "countries.txt"), str(d / "admin1.txt"))


def geo(gaz, text, langs=None, link=None, tags=None):
    return E.geo_candidates(gaz, text, langs, link, tags or [])


def kinds(c):
    return {(x["kind"], x["place"]) for x in c}


# ---- text helpers ---------------------------------------------------------------------------------
def test_hashtags_and_mentions():
    facets = '[{"features":[{"$type":"app.bsky.richtext.facet#tag","tag":"Caturday"},{"$type":"app.bsky.richtext.facet#mention","did":"x"}]},' \
             '{"features":[{"$type":"app.bsky.richtext.facet#tag","tag":"caturday"},{"$type":"app.bsky.richtext.facet#link","uri":"u"}]}]'
    assert E.extract_hashtags(facets, '["News", "#Cats"]') == ["caturday", "news", "cats"]
    assert E.count_mentions(facets) == 1
    assert E.extract_hashtags(None, None) == [] and E.extract_hashtags("not json", "{bad") == []


def test_embed_text_cleaning():
    assert E.embed_text("Hello https://x.co/abc   world, this is a long enough post about things") .startswith("Hello world")
    assert E.embed_text("ok") is None
    assert E.embed_text("short", "A long article title about politics") == "short - A long article title about politics"
    assert len(E.embed_text("x" * 900)) == 500


def test_links_without_a_scheme_are_not_embedded():
    assert E.embed_text("en.wikipedia.org/wiki/Roger_C...") is None                # a link alone is not a topic
    assert E.clean_text("ﾊﾔｵﾁｬﾝ好きそう youtu.be/IWWwBIq05yY?...") == "ﾊﾔｵﾁｬﾝ好きそう"
    assert E.clean_text("Read archive.ph/yIJcs and open.spotify.com/episode/4KOs... now") == "Read and now"
    assert E.embed_text("archive.ph/yIJcs", "Loyalists were present at Drumcree") == "Loyalists were present at Drumcree"
    kept = "The U.S./China deal, 24/7 news, and/or e.g. a 1.5x/day dose"           # not links
    assert E.clean_text(kept) == kept
    assert E.URL_RE.findall("see archive.ph/x") == []                               # bot and geo rules unchanged


def test_link_domain():
    assert E.link_domain("https://www.bbc.co.uk/news/1") == "bbc.co.uk" and E.link_domain(None) is None


# ---- bot scoring ----------------------------------------------------------------------------------
def posts(rows):
    base = pd.Timestamp("2026-11-19 06:00:00")
    out = []
    for i, r in enumerate(rows):
        out.append({"uri": f"u{i}", "did": r.get("did", f"d{i}"), "text": r.get("text", f"a perfectly normal post number {i} about my day"),
                    "langs": r.get("langs", ["en"]), "embed_type": r.get("embed", None), "link_domain": r.get("domain"),
                    "created_at": r.get("created", base), "event_time": base, "hashtags": r.get("tags", []),
                    "mentions": r.get("mentions", 0), "labels": r.get("labels", set()), "via": r.get("via"), "bridged": r.get("bridged", False)})
    return pd.DataFrame(out)


def test_normal_users_are_clean():
    df, new = E.score_bots(posts([{} for _ in range(30)]), set(), 120)
    assert not df["is_filtered"].any() and (df["bot_score"] == 0).all() and new.empty


def test_fast_link_only_account_is_flagged():
    rows = [{"did": "bot1", "text": f"Headline {i} https://news.example/{i}", "langs": [], "embed": "app.bsky.embed.external", "domain": "news.example"}
            for i in range(30)] + [{"did": "human"}]
    df, new = E.score_bots(posts(rows), set(), 120)
    b = df[df["did"] == "bot1"]
    assert b["is_filtered"].all() and {"rate", "links", "nolang"} <= set(b["bot_reasons"].iloc[0])
    assert not df[df["did"] == "human"]["is_filtered"].any()
    assert list(new["did"]) == ["bot1"]
    # once known, a single quiet post is still filtered
    again, _ = E.score_bots(posts([{"did": "bot1"}]), {"bot1"}, 120)
    assert again["is_filtered"].iloc[0] and "known_bot" in again["bot_reasons"].iloc[0]


def test_copy_paste_campaign_and_greetings():
    spam = "Win big now, visit my profile and follow for amazing daily giveaways today"
    df, _ = E.score_bots(posts([{"text": spam} for _ in range(4)] + [{"text": "good morning"} for _ in range(10)]), set(), 120)
    assert df.iloc[:4]["is_filtered"].all() and "dup_text" in df.iloc[0]["bot_reasons"]
    assert not df.iloc[4:]["is_filtered"].any()          # many people say "good morning"


def test_backfill_adult_empty_and_stuffing():
    old = pd.Timestamp("2026-10-01")
    df, _ = E.score_bots(posts([{"created": old}, {"labels": {"porn"}}, {"text": "  "}, {"tags": list("abcdefghij")},
                                {"mentions": 8, "text": "hello @a @b @c @d @e @f @g @h"}, {"text": "free crypto airdrop, dm me"},
                                {"via": "SomeBot 1.0"}, {"bridged": True}]), set(), 120)
    flags = df["is_filtered"].tolist()
    assert flags[:2] == [True, True]                      # archive import, adult label
    assert "backfill" in df.iloc[0]["bot_reasons"] and "adult" in df.iloc[1]["bot_reasons"]
    assert flags[2:] == [False] * 6                       # an image-only post is normal; one weak signal does not filter
    assert df.iloc[2]["bot_reasons"] == ["empty"] and df.iloc[2]["bot_score"] == 0
    assert df.iloc[3]["bot_score"] == 2 and df.iloc[7]["bot_score"] == 1


# ---- geo ------------------------------------------------------------------------------------------
def test_city_needs_context_and_capital(gaz):
    assert ("city", "Paris") in kinds(geo(gaz, "Spent the weekend in Paris with friends"))
    assert geo(gaz, "I love Nice") == [] and geo(gaz, "nice weather today") == []
    assert ("city", "Nice") in kinds(geo(gaz, "Flying to Nice next week"))
    assert not any(c["kind"] == "city" for c in geo(gaz, "Reading a book"))
    assert ("city", "Reading") in kinds(geo(gaz, "Moving to Reading soon"))


def test_multiword_state_and_country(gaz):
    c = geo(gaz, "Protest outside City Hall, New York today")
    assert ("city", "New York City") in kinds(c)
    assert next(x for x in c if x["place"] == "New York City")["confidence"] == 0.7
    assert ("admin1", "Texas") in kinds(geo(gaz, "Huge storm hits Texas"))
    assert ("country", "United States") in kinds(geo(gaz, "Opinion: the USA needs better trains"))
    assert ("country", "United Kingdom") in kinds(geo(gaz, "Brexit and the UK economy"))
    assert geo(gaz, "paris is lovely") == []


def test_most_populous_wins_ties_and_shouting(gaz):
    c = geo(gaz, "Landing in Paris tomorrow")
    assert next(x for x in c if x["place"] == "Paris")["country_code"] == "FR"
    assert ("city", "Berlin") in kinds(geo(gaz, "BERLIN IS ON FIRE TONIGHT"))


def test_non_latin_names_and_flags(gaz):
    assert ("city", "Tokyo") in kinds(geo(gaz, "今日は東京で雨でした", ["ja"]))
    assert ("city", "Seoul") in kinds(geo(gaz, "서울 날씨 좋다", ["ko"]))
    assert ("country", "Japan") in kinds(geo(gaz, "日本の経済について"))
    assert ("flag", "Brazil") in kinds(geo(gaz, "Vamos 🇧🇷 hoje"))


def test_non_latin_fragments_do_not_match(gaz):
    assert not [c for c in geo(gaz, "バタバタで遅くなってごめん", ["ja"]) if c["kind"] == "city"]
    assert not [c for c in geo(gaz, "ライ麦パンのサンドイッチ", ["ja"]) if c["kind"] == "city"]      # katakana fragment, small place
    assert not [c for c in geo(gaz, "المسح بالباركود أم الإدخال اليدوي", ["ar"]) if c["kind"] == "city"]
    assert not [c for c in geo(gaz, "今日からそうなります", ["ja"]) if c["kind"] == "city"]
    assert ("city", "Tome") in kinds(geo(gaz, "登米市の話", ["ja"]))                                # admin suffix makes a small Han name a place
    assert ("city", "Tome") not in kinds(geo(gaz, "登米は好き", ["ja"]))
    assert not [c for c in geo(gaz, "日本語の勉強") if c["kind"] == "country"]                       # the language, not the country
    assert not [c for c in geo(gaz, "12 rooms to BUY or SELL") if c["kind"] == "city"]            # acronym-style capitals


def test_names_that_are_not_places(gaz):
    assert not [c for c in geo(gaz, "The White House said on Tuesday") if c["kind"] == "city"]       # literal translation of Casablanca
    assert not [c for c in geo(gaz, "I dislike the hell out of Nancy Pelosi") if c["kind"] == "city"]  # a person
    assert ("city", "Nancy") in kinds(geo(gaz, "Train to Nancy leaves at nine"))
    assert not [c for c in geo(gaz, "Dandenong South Status: Responding") if c["kind"] == "admin1"]   # generic region word
    assert ("city", "Dallas") in kinds(geo(gaz, "Weather in Dallas, Texas"))
    assert not [c for c in geo(gaz, "말랑이였던 시절", ["ko"]) if c["kind"] == "city"]
    assert ("city", "Seoul") in kinds(geo(gaz, "서울에서 만나요", ["ko"]))                              # a case particle is fine


def test_accent_and_translation_handling(gaz):
    assert ("city", "Tokyo") in kinds(geo(gaz, "Flying to Tōkyō soon"))          # accent-insensitive lookup
    assert not [c for c in geo(gaz, "Palantir und Ruanda sind schlimmer") if c["kind"] == "city"]


def test_coordinates_and_links(gaz):
    c = geo(gaz, "see https://www.google.com/maps/@48.8584,2.2945,17z")
    assert c[0]["kind"] == "coords" and c[0]["confidence"] == 1.0 and abs(c[0]["lat"] - 48.8584) < 1e-6
    assert geo(gaz, "weights 12.5, 80.2 kg") == [] or all(x["kind"] != "coords" for x in geo(gaz, "weights 12.5, 80.2 kg"))
    assert geo(gaz, "bad 95.5551, 200.1234") == [] or all(x["kind"] != "coords" for x in geo(gaz, "bad 95.5551, 200.1234"))


def test_tld_language_and_region_signals(gaz):
    assert ("tld", "Belgium") not in kinds(geo(gaz, "video", link="https://youtu.be/abc"))      # youtu.be is not Belgium
    assert ("tld", "Germany") in kinds(geo(gaz, "Nachrichten", link="https://www.spiegel.de/x"))
    assert not any(x["kind"] == "tld" for x in geo(gaz, "stream", link="https://twitch.tv/x"))
    assert ("lang_region", "Brazil") in kinds(geo(gaz, "oi", ["pt-BR"]))
    lone = geo(gaz, "konnichiwa", ["ja"])
    assert kinds(lone) == {("lang", "Japan")} and lone[0]["confidence"] == 0.15      # kept as a hint, too weak to store in post_geo


def test_hashtag_place_and_best_choice(gaz):
    c = geo(gaz, "great trip", tags=["tokyo", "cat"])
    assert ("city", "Tokyo") in kinds(c)
    b = E.best_geo(geo(gaz, "Dinner in Austin, Texas tonight", ["en-US"]))
    assert b["place"] == "Austin" and b["country_code"] == "US" and b["confidence"] > 0.6
    assert E.best_geo([]) is None
    # coordinates always beat names
    assert E.best_geo(geo(gaz, "In Paris https://maps.example/?q=48.8584,2.2945"))["kind"] == "coords"


def test_no_gazetteer_still_finds_coordinates():
    assert E.geo_candidates(None, "at 48.8584, 2.2945", None, None, [])[0]["kind"] == "coords"
    assert E.geo_candidates(None, "hello", ["ja"], None, []) == []


# ---- IPTC and topics ------------------------------------------------------------------------------
def iptc_doc():
    def c(q, label, broader=None, retired=None):
        d = {"qcode": q, "uri": f"http://cv.iptc.org/newscodes/mediatopic/{q.split(':')[1]}", "prefLabel": {"en-GB": label},
             "definition": {"en-GB": f"{label} definition"}}
        if broader:
            d["broader"] = [f"http://cv.iptc.org/newscodes/mediatopic/{broader.split(':')[1]}"]
        if retired:
            d["retired"] = retired
        return d
    return {"conceptSet": [c("medtop:1", "sport"), c("medtop:2", "soccer", "medtop:1"), c("medtop:3", "premier league", "medtop:2"),
                           c("medtop:4", "old thing", "medtop:1", retired="2020-01-01"), c("medtop:5", "politics")]}


def test_parse_iptc_levels_paths_and_retired():
    rows = {r["qcode"]: r for r in E.parse_iptc(iptc_doc())}
    assert set(rows) == {"medtop:1", "medtop:2", "medtop:3", "medtop:5"}          # retired concept dropped
    assert [rows[q]["level"] for q in ("medtop:1", "medtop:2", "medtop:3")] == [0, 1, 2]
    assert rows["medtop:3"]["path"] == ["sport", "soccer", "premier league"] and rows["medtop:3"]["parent_qcode"] == "medtop:2"
    with pytest.raises(ValueError):
        E.parse_iptc({"conceptSet": []})


def test_assign_topics_threshold_and_order():
    T = E.l2_normalise(np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]]))
    P = E.l2_normalise(np.array([[0.9, 0.1, 0], [0.5, 0.5, 0.0], [0.1, 0.1, 0.1]]))
    out = E.assign_topics(P, T, top_k=2, min_score=0.5)
    assert out[0][0][0] == 0 and out[0][0][1] > 0.9 and len(out[0]) == 1
    assert sorted(j for j, _ in out[1]) == [0, 1]
    assert len(out[2]) == 2                              # three equal scores, top_k=2 caps it
    assert E.assign_topics(P, T, top_k=2, min_score=0.9)[2] == []
    assert E.assign_topics(np.zeros((0, 3)), T) == []


def test_anchor_means_no_topic():
    T = E.l2_normalise(np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]]))     # last row is the "chatter" anchor
    posts = E.l2_normalise(np.array([[0.9, 0.1, 0.2], [0.2, 0.1, 0.9], [0.1, 0.9, 0.3]]))
    out = E.assign_topics(posts, T, top_k=2, min_score=0.5, n_anchors=1)
    assert [j for j, _ in out[0]] == [0]
    assert out[1] == []                                                 # nearest is the anchor
    assert [j for j, _ in out[2]] == [1]
    assert E.ANCHORS and E.EMBED_INSTRUCTION.endswith("Query: ")


def test_subject_remap_for_consumer_subjects():
    econ, ps = "medtop:04000000", "medtop:20000209"
    assert E.subject_qcode([econ, ps, "medtop:20000304", "medtop:20000308"]) == "medtop:01000000"   # music industry: arts
    assert E.subject_qcode([econ, ps, "medtop:20000304"]) == "medtop:01000000"                      # the branch itself
    assert E.subject_qcode([econ, ps, "medtop:20000225", "medtop:20000231"]) == "medtop:13000000"   # software: science
    assert E.subject_qcode([econ, ps, "medtop:20000304", "medtop:20000305"]) == econ                # advertising stays
    assert E.subject_qcode([econ, ps, "medtop:20000243"]) == econ                                   # consumer goods stay
    assert E.subject_qcode(("medtop:15000000", "medtop:20001124")) == "medtop:15000000"            # elsewhere: the root


def test_next_window_stays_behind_tail():
    assert E.next_window(100, 1_000, 60, 300, 0) == (100, 400)
    assert E.next_window(100, 200, 60, 300, 0) == (100, 140)
    assert E.next_window(100, 150, 60, 300, 0) is None           # nothing safe to read yet
    assert E.next_window(None, 1_000, 60, 300, 50) == (50, 350)


def test_next_window_steps_over_a_hole_in_bronze():
    # the cursor is stuck at 100 because (100, 400] held no posts; the floor moves the next window past it
    assert E.next_window(100, 1_000, 60, 300, 0, floor_us=400) == (400, 700)
    assert E.next_window(500, 1_000, 60, 300, 0, floor_us=400) == (500, 800)   # an older floor is ignored
    assert E.next_window(100, 450, 60, 300, 0, floor_us=400) is None            # still nothing safe to read


def test_text_helpers_tolerate_missing_values():
    assert E.embed_text(None, float("nan")) is None and E.clean_text(float("nan")) == ""
    assert E.embed_text(float("nan"), "A headline about the economy today") == "- A headline about the economy today".strip(" -")
