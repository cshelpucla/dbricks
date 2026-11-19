"""Enrichment logic for Bluesky posts: bot scoring, hashtags, geo extraction, embeddings and topics.

Pure Python plus numpy/pandas, with no Spark and no network access at import time, so everything here
can be tested locally. The contract is docs/ENRICHMENT_SPEC.md; rule names and thresholds below match
the reason codes in section 5 of that document.
"""
import json
import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import numpy as np
import pandas as pd

VERSION = 1

# ---- bot and spam rules (spec section 5) -------------------------------------------------------
BOT_THRESHOLD = 4
RATE_PER_MINUTE = 6
ACCOUNT_MIN_POSTS = 5
ACCOUNT_SHARE = 0.9
GEO_KEEP_MIN = 0.15      # weakest candidate kept, so a lone language or domain hint can still fill geo_*
GEO_STORE_MIN = 0.25     # weakest candidate written to post_geo
DUP_TEXT_COUNT = 3
DUP_TEXT_MIN_CHARS = 20
DUP_SELF_MIN_CHARS = 10
BACKFILL_DAYS = 7
TAG_STUFFING = 8
MENTION_STUFFING = 6
NEW_BOT_ACCOUNT_SCORE = 3
ADULT_LABELS = {"porn", "sexual", "nudity", "adult"}
AUTOPOSTER_DOMAINS = {"dlvr.it", "ift.tt", "buff.ly", "ow.ly"}
SPAM_RE = re.compile(
    r"airdrop|\bdm me\b|\bdm for\b|telegram|whatsapp|onlyfans|link in bio|click (?:the )?link|"
    r"free (?:money|crypto|nft)|\bt\.me/|\bwa\.me/|crypto signals|\bforex\b|guaranteed profit|\bcashapp\b",
    re.I)

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)

# ---- text helpers ------------------------------------------------------------------------------


def clean_text(text) -> str:
    """Text without URLs and extra whitespace, for embedding."""
    return re.sub(r"\s+", " ", URL_RE.sub(" ", text if isinstance(text, str) else "")).strip()


def embed_text(text, link_title=None, max_chars=500, min_chars=12) -> str | None:
    """The string to embed, or None when the post is too short to be worth embedding."""
    t = clean_text(text)
    if len(t) < 40 and isinstance(link_title, str) and link_title.strip():
        t = f"{t} - {link_title.strip()}".strip(" -")
    t = t[:max_chars]
    return t if len(t) >= min_chars else None


def _norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", URL_RE.sub("", (text or "").lower()))).strip()


def _loads(value):
    if value is None or value == "":
        return None
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None


def facet_features(facets_json) -> list[dict]:
    out = []
    for facet in _loads(facets_json) or []:
        if isinstance(facet, dict):
            out += [f for f in facet.get("features", []) if isinstance(f, dict)]
    return out


def extract_hashtags(facets_json, tags_json, limit=20) -> list[str]:
    tags = [f.get("tag") for f in facet_features(facets_json) if str(f.get("$type", "")).endswith("#tag")]
    tags += [t for t in (_loads(tags_json) or []) if isinstance(t, str)]
    seen, out = set(), []
    for t in tags:
        t = (t or "").strip().lstrip("#").lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def count_mentions(facets_json) -> int:
    return sum(1 for f in facet_features(facets_json) if str(f.get("$type", "")).endswith("#mention"))


def link_domain(url) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower() if url else ""
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


def label_values(labels_json) -> set[str]:
    return {x.get("val") for x in (_loads(labels_json) or []) if isinstance(x, dict) and x.get("val")}


# ---- bot scoring -------------------------------------------------------------------------------

def score_bots(df: pd.DataFrame, known_bots: set, span_seconds: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Adds bot_score, bot_reasons and is_filtered to df; returns (df, new_bot_accounts).

    df columns used: uri, did, text, langs (list or None), embed_type, link_domain, created_at,
    event_time, hashtags (list), mentions (int), labels (set), via, bridged (bool).
    span_seconds is the time covered by the window, used for the posting-rate rule (minimum one minute).
    """
    df = df.copy()
    if df.empty:
        for c, v in (("bot_score", 0), ("bot_reasons", None), ("is_filtered", False)):
            df[c] = v
        return df, pd.DataFrame(columns=["did", "score", "reasons"])

    minutes = max(span_seconds, 60) / 60
    text = df["text"].fillna("")
    norm = text.map(_norm)
    is_link = (df["embed_type"].fillna("").str.contains("external") | df["link_domain"].notna()).astype(float)
    no_lang = df["langs"].map(lambda v: 0.0 if v is not None and len(v) else 1.0)
    by_did = df.groupby("did")
    n = by_did["uri"].transform("size")
    long_enough = norm.str.len() >= DUP_TEXT_MIN_CHARS
    dup_text_n = norm.where(long_enough).groupby(norm.where(long_enough)).transform("size").fillna(0)
    self_key = pd.Series(list(zip(df["did"], norm)), index=df.index)
    dup_self_n = self_key.where(norm.str.len() >= DUP_SELF_MIN_CHARS).groupby(self_key).transform("size").fillna(0)

    age_days = (df["event_time"] - df["created_at"]).dt.total_seconds() / 86400
    labels = df["labels"].map(lambda s: s or set())
    via = df["via"].fillna("").str.lower()

    rules = {
        "rate": (n / minutes >= RATE_PER_MINUTE, 3),
        "links": ((n >= ACCOUNT_MIN_POSTS) & (by_did["uri"].transform("size") > 0)
                  & (is_link.groupby(df["did"]).transform("mean") >= ACCOUNT_SHARE), 2),
        "nolang": ((n >= ACCOUNT_MIN_POSTS) & (no_lang.groupby(df["did"]).transform("mean") >= ACCOUNT_SHARE), 1),
        "dup_text": (dup_text_n >= DUP_TEXT_COUNT, 4),
        "dup_self": (dup_self_n >= 2, 2),
        "backfill": (age_days > BACKFILL_DAYS, 4),
        "future": (age_days < -1 / 24, 1),
        "tag_stuffing": (df["hashtags"].map(len) >= TAG_STUFFING, 2),
        "mention_stuffing": (df["mentions"] >= MENTION_STUFFING, 2),
        "spam_words": (text.str.contains(SPAM_RE), 2),
        "label_bot": (labels.map(lambda s: "bot" in s), 4),
        "via_bot": (via.str.contains("bot"), 2),
        "autoposter": (df["link_domain"].isin(AUTOPOSTER_DOMAINS), 1),
        "bridged": (df["bridged"].fillna(False).astype(bool), 1),
        "adult": (labels.map(lambda s: bool(s & ADULT_LABELS)), 2),
        "known_bot": (df["did"].isin(known_bots), 4),
    }
    score = pd.Series(0, index=df.index)
    reasons = [[] for _ in range(len(df))]
    pos = {idx: i for i, idx in enumerate(df.index)}
    for code, (mask, pts) in rules.items():
        mask = mask.fillna(False).astype(bool)
        score += mask.astype(int) * pts
        for idx in mask[mask].index:
            reasons[pos[idx]].append(code)

    always = rules["adult"][0]                                 # adult-labelled posts are always filtered
    for idx in text[text.str.strip().str.len() < 3].index:
        reasons[pos[idx]].append("empty")                      # informational: image-only posts are normal, just not embeddable
    df["bot_score"] = score.astype(int)
    df["bot_reasons"] = reasons
    df["is_filtered"] = (score >= BOT_THRESHOLD) | always

    # Accounts that look automated on window-level behaviour alone
    acct = sum(rules[c][0].astype(int) * rules[c][1] for c in ("rate", "links", "nolang", "dup_self"))
    acct = acct.groupby(df["did"]).transform("max")
    flagged = df[(acct >= NEW_BOT_ACCOUNT_SCORE) & ~df["did"].isin(known_bots)]
    new_bots = (flagged.assign(score=acct[flagged.index])
                .groupby("did").agg(score=("score", "max"), reasons=("bot_reasons", lambda r: sorted({c for x in r for c in x
                                                                                                       if c in ("rate", "links", "nolang", "dup_self")})))
                .reset_index())
    return df, new_bots


# ---- geo ---------------------------------------------------------------------------------------
PREPS = {"in", "at", "near", "from", "to", "around", "outside", "across", "of", "inside", "into", "visiting", "visit"}
AMBIGUOUS = {"nice", "reading", "mobile", "turkey", "chad", "jordan", "georgia", "orange", "bath", "wells", "hell",
             "normal", "mars", "nevada city", "mexico city", "reading", "buffalo", "bend", "hamburg"}
HACK_TLDS = set("tv ly co io ai me fm am gl gd sh to cc ws nu ms vc ac ag dj st la cx tk ml ga cf gq sx be".split())
LANG_COUNTRY = {"ja": "JP", "ko": "KR", "th": "TH", "vi": "VN", "id": "ID", "tr": "TR", "pl": "PL", "sv": "SE",
                "da": "DK", "fi": "FI", "nb": "NO", "nn": "NO", "no": "NO", "cs": "CZ", "hu": "HU", "el": "GR",
                "he": "IL", "ro": "RO", "bg": "BG", "hr": "HR", "sk": "SK", "sl": "SI", "lt": "LT", "lv": "LV",
                "et": "EE", "is": "IS"}
COUNTRY_ALIASES = {"usa": "US", "u.s.": "US", "us": None, "america": "US", "united states": "US",
                   "united states of america": "US", "uk": "GB", "u.k.": "GB", "britain": "GB", "great britain": "GB",
                   "england": "GB", "scotland": "GB", "wales": "GB", "uae": "AE", "the netherlands": "NL",
                   "holland": "NL", "south korea": "KR", "north korea": "KP", "russia": "RU", "czech republic": "CZ",
                   "czechia": "CZ", "ivory coast": "CI", "palestine": "PS", "taiwan": "TW", "vietnam": "VN",
                   "turkiye": "TR", "türkiye": "TR"}
NONLATIN_COUNTRIES = {"日本": "JP", "中国": "CN", "中國": "CN", "韩国": "KR", "韓国": "KR", "한국": "KR", "台湾": "TW",
                      "台灣": "TW", "香港": "HK", "米国": "US", "アメリカ": "US", "イギリス": "GB", "ドイツ": "DE",
                      "フランス": "FR", "ロシア": "RU", "ウクライナ": "UA", "россия": "RU", "россии": "RU",
                      "украина": "UA", "украине": "UA", "مصر": "EG", "السعودية": "SA", "ประเทศไทย": "TH"}
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
KATAKANA_RE = re.compile(r"[\u30a0-\u30ff]+")
HANGUL_RE = re.compile(r"[\uac00-\ud7af]+")
OTHER_RE = re.compile(r"[^\W\d_]+")
ADMIN_SUFFIX = set("市県府都区町村省州郡国")        # a Han place name followed by one of these is a place
NOT_PLACE_AFTER = set("語人製円料理酒軍")             # 日本語, 日本人, 中国製 ...
CITY_ALIASES = {"new york": "New York City"}
GENERIC_REGIONS = {"north", "south", "east", "west", "central", "eastern", "western", "northern", "southern", "capital",
                   "federal", "upper", "lower", "centre", "center", "coast", "littoral", "highlands", "islands", "region"}
KOREAN_PARTICLES = {"", "에", "에서", "의", "은", "는", "이", "가", "을", "를", "도", "로", "으로", "과", "와", "에는", "에서는", "에도", "만", "이나"}
KIND_RANK = {"coords": 6, "city": 5, "admin1": 4, "country": 3, "lang_region": 2, "flag": 1, "tld": 1, "lang": 0}

WORD_RE = re.compile(r"[^\W\d_]+(?:[’'\-][^\W\d_]+)*")
RUN_RE = re.compile(r"[^\W\d_]+")
FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]{2}")
COORD_PATTERNS = [
    re.compile(r"@(-?\d{1,2}\.\d{3,}),(-?\d{1,3}\.\d{3,})"),
    re.compile(r"[?&](?:q|ll|query|center)=(-?\d{1,2}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})"),
    re.compile(r"mlat=(-?\d{1,2}\.\d{3,})&(?:amp;)?mlon=(-?\d{1,3}\.\d{3,})"),
    re.compile(r"geo:(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)"),
    re.compile(r"(?<![\d.])(-?\d{1,2}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})(?![\d.])"),
]


def _is_latin(s: str) -> bool:
    return all(ord(c) < 0x250 for c in s if c.isalpha())


def _has_nonlatin(s: str) -> bool:
    return any(c.isalpha() and ord(c) >= 0x250 for c in s)


def _script(s: str) -> str | None:
    """han, kana (katakana only), hangul, or other, for a name made of a single script; None if mixed."""
    kinds = set()
    for c in s:
        o = ord(c)
        if 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF:
            kinds.add("han")
        elif 0x30A0 <= o <= 0x30FF:
            kinds.add("kana")
        elif 0xAC00 <= o <= 0xD7AF:
            kinds.add("hangul")
        elif c.isalpha() and o >= 0x250:
            kinds.add("other")
        else:
            return None
    return kinds.pop() if len(kinds) == 1 else None


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


class Gazetteer:
    """Place-name lookup built from GeoNames files. entry = (kind, name, country_code, lat, lon, population)."""

    def __init__(self):
        self.latin: dict[str, tuple] = {}
        self.nonlatin: dict[str, dict] = {"han": {}, "kana": {}, "hangul": {}, "other": {}}
        self.han_maxlen = 2
        self.country: dict[str, tuple] = {}          # ISO code -> country entry
        self.iso: set[str] = set()

    # priority when two places share a name: country > city > admin1, then population
    _PRIO = {"country": 3, "city": 2, "admin1": 1}

    def _put(self, table: dict, key: str, entry: tuple):
        old = table.get(key)
        if old is None or (self._PRIO[entry[0]], entry[5]) > (self._PRIO[old[0]], old[5]):
            table[key] = entry

    @classmethod
    def from_files(cls, cities_path: str, country_path: str, admin1_path: str) -> "Gazetteer":
        g = cls()
        cities = []
        with open(cities_path, encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) < 15:
                    continue
                cities.append({"name": p[1], "ascii": p[2], "alts": p[3].split(",") if p[3] else [], "lat": float(p[4]),
                               "lon": float(p[5]), "fcode": p[7], "cc": p[8], "admin1": p[10], "pop": int(p[14] or 0)})
        # countries, with the capital's coordinates (falling back to the biggest city)
        top_city = {}
        for c in cities:
            if c["cc"] not in top_city or c["pop"] > top_city[c["cc"]]["pop"]:
                top_city[c["cc"]] = c
        with open(country_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                p = line.rstrip("\n").split("\t")
                iso, name, capital = p[0], p[4], p[5]
                cap = next((c for c in cities if c["cc"] == iso and c["name"] == capital), None) or top_city.get(iso)
                if not cap:
                    continue
                g.iso.add(iso)
                entry = ("country", name, iso, cap["lat"], cap["lon"], int(p[7] or 0))
                g.country[iso] = entry
                g._put(g.latin, name.lower(), entry)
        for alias, iso in COUNTRY_ALIASES.items():
            if iso in g.country:
                g._put(g.latin, alias, g.country[iso])
        for alias, iso in NONLATIN_COUNTRIES.items():
            if iso in g.country and _script(alias.lower()):
                g.nonlatin[_script(alias.lower())][alias.lower()] = g.country[iso]
        # cities
        weights: dict[tuple, list] = {}
        for c in cities:
            entry = ("city", c["name"], c["cc"], c["lat"], c["lon"], c["pop"])
            for name in {c["name"], c["ascii"]}:
                if len(name) >= 3:
                    g._put(g.latin, name.lower(), entry)
                    g._put(g.latin, _strip_accents(name).lower(), entry)
            for alt in c["alts"]:
                if len(alt) < 2:
                    continue
                if _has_nonlatin(alt):
                    sc = _script(alt.lower())
                    if sc:
                        g._put(g.nonlatin[sc], alt.lower(), entry)
                elif (c["pop"] >= 200000 and len(alt) >= 5 and " " not in alt and _is_latin(alt)
                      and _strip_accents(alt)[0].lower() == c["ascii"][0].lower()):
                    g._put(g.latin, alt.lower(), entry)
            w = weights.setdefault((c["cc"], c["admin1"]), [0.0, 0.0, 0.0])
            w[0] += c["lat"] * c["pop"]; w[1] += c["lon"] * c["pop"]; w[2] += c["pop"]
        for alias, city_name in CITY_ALIASES.items():
            hit = next((c for c in cities if c["name"] == city_name), None)
            if hit:
                g.latin[alias] = ("city", hit["name"], hit["cc"], hit["lat"], hit["lon"], hit["pop"])
        # states / provinces, at the population-weighted centre of their cities
        with open(admin1_path, encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) < 3 or "." not in p[0]:
                    continue
                cc, code = p[0].split(".", 1)
                w = weights.get((cc, code))
                if not w or not w[2]:
                    continue
                entry = ("admin1", p[1], cc, w[0] / w[2], w[1] / w[2], int(w[2]))
                for name in {p[1], p[2]}:
                    if len(name) >= 4 and name.lower() not in GENERIC_REGIONS:
                        g._put(g.latin, name.lower(), entry)
        g.han_maxlen = min(8, max((len(k) for k in g.nonlatin["han"]), default=2))
        return g

    # ---- candidate generation ------------------------------------------------------------------
    @staticmethod
    def _cand(entry, kind, conf, matched):
        return {"place": entry[1], "country_code": entry[2], "lat": entry[3], "lon": entry[4],
                "kind": kind, "confidence": conf, "matched_text": matched}

    def _from_entry(self, entry, conf_city, matched):
        kind = entry[0]
        conf = {"city": conf_city, "admin1": 0.5, "country": 0.4}[kind]
        return self._cand(entry, kind, conf, matched)

    def latin_places(self, text: str) -> list[dict]:
        tokens = [(m.start(), m.end(), m.group()) for m in WORD_RE.finditer(text)]
        shout = len(text) > 10 and text.isupper()
        out, i = [], 0
        while i < len(tokens):
            hit = None
            for n in (4, 3, 2, 1):
                if i + n > len(tokens):
                    continue
                if any(text[tokens[j][1]:tokens[j + 1][0]].strip() for j in range(i, i + n - 1)):
                    continue                                    # punctuation between words: not one name
                surface = " ".join(t[2] for t in tokens[i:i + n])
                entry = self.latin.get(_strip_accents(surface).lower()) or self.latin.get(surface.lower())
                if not entry:
                    continue
                if not shout and not surface[0].isupper():
                    continue
                if not shout and entry[0] != "country" and surface.isupper() and len(surface) <= 4:
                    continue
                hit = (n, surface, entry)
                break
            if not hit:
                i += 1
                continue
            n, surface, entry = hit
            if n == 1 and not shout and entry[0] in ("city", "admin1") and i + 1 < len(tokens):
                nxt = tokens[i + 1]
                if not text[tokens[i][1]:nxt[0]].strip() and nxt[2][0].isupper() and nxt[2].lower() not in self.latin:
                    i += 1
                    continue
            prev = ""
            if i > 0 and not text[tokens[i - 1][1]:tokens[i][0]].strip():
                prev = tokens[i - 1][2].lower()
            prep = prev in PREPS
            ambiguous = surface.lower() in AMBIGUOUS
            if entry[0] == "city":
                if n > 1 and not ambiguous:
                    conf = 0.7
                elif prep:
                    conf = 0.6
                elif entry[5] >= 1_000_000 and not ambiguous:
                    conf = 0.5
                else:
                    i += n
                    continue
            else:
                if ambiguous and not prep:
                    i += n
                    continue
                conf = 0.5
            out.append(self._from_entry(entry, conf, surface))
            i += n
        return out

    def nonlatin_places(self, text: str) -> list[dict]:
        """Whole-word matching per script. Fragments of ordinary words must not match, so:
        Han names need 1M+ people or a following admin suffix (市, 県, ...); katakana, Hangul and other
        scripts must match a whole word (Hangul may carry a particle) and belong to a large place."""
        out = []
        han, kana, hangul, other = (self.nonlatin[k] for k in ("han", "kana", "hangul", "other"))
        for m in HAN_RE.finditer(text):
            run, i = m.group(), 0
            while i < len(run):
                hit = None
                for L in range(min(self.han_maxlen, len(run) - i), 1, -1):
                    entry = han.get(run[i:i + L])
                    if not entry:
                        continue
                    nxt = run[i + L] if i + L < len(run) else (text[m.end()] if m.end() < len(text) else "")
                    if nxt in NOT_PLACE_AFTER:
                        continue
                    if entry[0] == "country" or entry[5] >= 1_000_000 or nxt in ADMIN_SUFFIX:
                        hit = (L, entry)
                        break
                if hit:
                    out.append(self._from_entry(hit[1], 0.6, run[i:i + hit[0]]))
                    i += hit[0]
                else:
                    i += 1
        for m in KATAKANA_RE.finditer(text):
            entry = kana.get(m.group().lower())
            if entry and (entry[0] == "country" or entry[5] >= (1_000_000 if len(m.group()) < 3 else 100_000)):
                out.append(self._from_entry(entry, 0.6, m.group()))
        for m in HANGUL_RE.finditer(text):
            run = m.group()
            for L in range(len(run), 1, -1):
                entry = hangul.get(run[:L])
                if entry and run[L:] in KOREAN_PARTICLES and (entry[5] >= 100_000 or entry[0] == "country"):
                    out.append(self._from_entry(entry, 0.6, run[:L]))
                    break
        for m in OTHER_RE.finditer(text):
            word = m.group().lower()
            if len(word) >= 3 and _script(word) == "other":
                entry = other.get(word)
                if entry and (entry[0] == "country" or entry[5] >= 100_000):
                    out.append(self._from_entry(entry, 0.6, m.group()))
        return out

    def hashtag_places(self, tags: list[str]) -> list[dict]:
        out = []
        for t in tags:
            if len(t) < 4:
                continue
            entry = self.latin.get(t) or self.latin.get(_strip_accents(t))
            if entry:
                out.append(self._from_entry(entry, 0.6, "#" + t))
        return out


def coordinate_candidates(text: str, link_url: str | None) -> list[dict]:
    out = []
    for source in (link_url or "", text or ""):
        for pat in COORD_PATTERNS:
            for m in pat.finditer(source):
                lat, lon = float(m.group(1)), float(m.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
                    out.append({"place": None, "country_code": None, "lat": lat, "lon": lon, "kind": "coords",
                                "confidence": 1.0, "matched_text": m.group(0)[:60]})
            if out:
                return out
    return out


def geo_candidates(gaz: "Gazetteer | None", text: str, langs, link_url, hashtags) -> list[dict]:
    text = text or ""
    cands = coordinate_candidates(text, link_url)
    if gaz:
        cands += gaz.latin_places(text) + gaz.nonlatin_places(text) + gaz.hashtag_places(hashtags or [])
        for m in FLAG_RE.finditer(text):
            iso = "".join(chr(ord(c) - 0x1F1E6 + 65) for c in m.group())
            if iso in gaz.country:
                cands.append(gaz._cand(gaz.country[iso], "flag", 0.3, m.group()))
        urls = [link_url] if link_url else []
        urls += URL_RE.findall(text)[:3]
        for u in urls:
            host = link_domain(u if u.startswith("http") else "http://" + u)
            if not host or host == "youtu.be" or "." not in host:
                continue
            tld = host.rsplit(".", 1)[1]
            iso = "GB" if tld == "uk" else tld.upper()
            if len(tld) == 2 and tld not in HACK_TLDS and iso in gaz.country:
                cands.append(gaz._cand(gaz.country[iso], "tld", 0.25, host))
                break
        for lang in langs or []:
            if "-" in lang:
                region = lang.rsplit("-", 1)[1].upper()
                if len(region) == 2 and region in gaz.country:
                    cands.append(gaz._cand(gaz.country[region], "lang_region", 0.5, lang))
        primary = (langs or [None])[0]
        iso = LANG_COUNTRY.get((primary or "").split("-")[0])
        if iso and iso in gaz.country:
            cands.append(gaz._cand(gaz.country[iso], "lang", 0.15, primary))
    # drop duplicates; best_geo and consumers apply their own confidence cut-off
    seen, out = set(), []
    for c in sorted(cands, key=lambda c: -c["confidence"]):
        key = (c["kind"], c["place"] or c["matched_text"])
        if c["confidence"] >= GEO_KEEP_MIN and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def best_geo(cands: list[dict]) -> dict | None:
    """Highest-confidence candidate, boosted +0.1 per other source that agrees on the country (cap 0.95)."""
    if not cands:
        return None
    by_country: dict[str, int] = {}
    for c in cands:
        if c["country_code"]:
            by_country[c["country_code"]] = by_country.get(c["country_code"], 0) + 1
    scored = []
    for c in cands:
        conf = c["confidence"]
        if c["country_code"] and c["kind"] != "coords":
            conf = min(0.95, conf + 0.1 * (by_country[c["country_code"]] - 1))
        scored.append((conf, KIND_RANK[c["kind"]], c))
    conf, _, best = max(scored, key=lambda s: (s[0], s[1]))
    return {**best, "confidence": round(conf, 3)}


# ---- IPTC taxonomy and topic assignment ---------------------------------------------------------
MATCH_LEVELS = (1, 2, 3)
# Posts are embedded with a task instruction (measurably better topic matching than the bare text) and
# compared against concept descriptions, plus these "no topic" anchors: a post whose nearest match is an
# anchor is chatter and gets no topic.
EMBED_INSTRUCTION = "Instruct: Identify which news topic category this social media post is about\nQuery: "
TOPIC_MIN_CHARS = 25
TOPIC_MIN_SCORE = 0.62
ANCHORS = [
    "a greeting such as good morning, good night or hello", "small talk about my day, mood and feelings",
    "a short reply or reaction to another person", "emoji, laughter or an exclamation",
    "a joke, meme or random silly remark", "a question asking followers for recommendations or opinions",
    "self promotion, follow requests and community announcements",
    "a personal life update about family, friends or relationships", "complaining or venting about something personal",
    "thanks, congratulations or good wishes", "a vague opinion without a clear subject",
    "a status update with no particular subject",
]


def parse_iptc(doc: dict) -> list[dict]:
    """Active concepts with parent, level and label path. Raises if the hierarchy looks broken."""
    concepts = doc["conceptSet"]
    by_q = {c["qcode"]: c for c in concepts}

    def to_q(ref: str) -> str:
        return ref if ref.startswith("medtop:") else "medtop:" + ref.rstrip("/").rsplit("/", 1)[-1]

    def label(c, key="prefLabel"):
        d = c.get(key) or {}
        return d.get("en-GB") or d.get("en-US") or next(iter(d.values()), "")

    active = [c for c in concepts if not c.get("retired")]
    rows = {}
    for c in active:
        parents = [to_q(b) for b in c.get("broader", [])]
        parent = next((p for p in parents if p in by_q and not by_q[p].get("retired")), None)
        rows[c["qcode"]] = {"qcode": c["qcode"], "label": label(c), "definition": label(c, "definition"),
                            "parent_qcode": parent}
    for q, r in rows.items():
        path, seen, cur = [], set(), q
        while cur and cur not in seen:
            seen.add(cur)
            path.append(cur)
            cur = rows[cur]["parent_qcode"] if cur in rows else None
        path.reverse()
        r["level"] = len(path) - 1
        r["path_codes"] = path
        r["path"] = [rows[p]["label"] for p in path if p in rows]
    if not rows or not any(r["level"] == 0 for r in rows.values()):
        raise ValueError("IPTC file has no top-level concepts")
    return list(rows.values())


def l2_normalise(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.maximum(norms, 1e-12)


def assign_topics(E: np.ndarray, T: np.ndarray, top_k: int = 3, min_score: float = 0.62, n_anchors: int = 0):
    """E: n x d post vectors, T: m x d concept vectors (both L2-normalised); the last n_anchors rows of T are
    "no topic" anchors. Returns n lists of (concept_index, score), best first, only scores >= min_score.
    A post whose single nearest row is an anchor gets an empty list."""
    if len(E) == 0:
        return []
    S = E @ T.T
    m = S.shape[1] - n_anchors
    out = []
    for i in range(len(E)):
        if n_anchors and int(S[i].argmax()) >= m:
            out.append([])
            continue
        row = S[i, :m]
        k = min(top_k, m)
        idx = np.argpartition(-row, k - 1)[:k]
        out.append([(int(j), float(row[j])) for j in sorted(idx, key=lambda j: -row[j]) if row[j] >= min_score])
    return out


# ---- embeddings ---------------------------------------------------------------------------------

def embed_texts(client, endpoint: str, texts: list[str], batch_size=32, workers=6, retries=3):
    """Returns (vectors, tokens, failed_batches, total_batches). vectors[i] is a float32 array or None."""
    import time

    batches = [(s, texts[s:s + batch_size]) for s in range(0, len(texts), batch_size)]
    vectors = [None] * len(texts)
    tokens = 0
    failed = 0

    def call(item):
        start, chunk = item
        for attempt in range(retries):
            try:
                resp = client.serving_endpoints.query(name=endpoint, input=chunk)
                data = sorted(resp.data, key=lambda d: d.index if getattr(d, "index", None) is not None else 0)
                used = resp.usage.total_tokens if getattr(resp, "usage", None) and resp.usage.total_tokens else 0
                return start, [np.asarray(d.embedding, dtype=np.float32) for d in data], used
            except Exception:
                if attempt == retries - 1:
                    return start, None, 0
                time.sleep(2 ** attempt)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start, vecs, used in pool.map(call, batches):
            if vecs is None or len(vecs) != len(texts[start:start + batch_size]):
                failed += 1
                continue
            tokens += used
            for i, v in enumerate(vecs):
                vectors[start + i] = v
    return vectors, tokens, failed, len(batches)


# ---- window arithmetic --------------------------------------------------------------------------

def next_window(cursor_us, tail_us, margin_us, window_us, start_us, floor_us=None):
    """Returns (lo, hi) for the next window, or None when there is nothing safe to read yet.
    Rows with lo < time_us <= hi are processed. The upper bound stays behind the bronze tail.
    floor_us: the end of the last full-width window that held no posts. The cursor (newest post written) cannot move
    past a hole in bronze longer than a window, such as an ingest outage, so the job steps over it from there."""
    lo = cursor_us if cursor_us is not None else start_us
    if floor_us is not None and floor_us > lo:
        lo = floor_us
    hi = min(tail_us - margin_us, lo + window_us)
    return (lo, hi) if hi > lo else None


def row_to_python(value):
    """Spark/pandas scalars to plain Python, with NaN as None."""
    if isinstance(value, float) and math.isnan(value):
        return None
    return value
