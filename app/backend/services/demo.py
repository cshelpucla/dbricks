"""Synthetic data source, used while the enrichment and map jobs have not produced real data.

Everything here is fabricated and the UI labels it as demo data. The shape matches the real
tables so the live source can replace it without touching the UI.
"""
import math
import random

from .shape import build_clusters

# IPTC Media Topics top-level subjects, each with a few invented sub-topic clusters.
TOPICS = {
    "politics and government": ["elections and voting", "government policy", "diplomacy", "protests"],
    "sport": ["football", "basketball", "baseball", "motor racing", "tennis"],
    "science and technology": ["artificial intelligence", "space exploration", "software and apps", "physics"],
    "arts, culture, entertainment and media": ["film and tv", "music", "books", "gaming", "anime"],
    "economy, business and finance": ["stock markets", "inflation", "startups", "crypto"],
    "health": ["public health", "mental health", "medical research"],
    "environment": ["climate change", "wildlife", "energy transition"],
    "lifestyle and leisure": ["food and cooking", "travel", "pets", "fashion"],
    "conflict, war and peace": ["ukraine conflict", "middle east", "peace talks"],
    "crime, law and justice": ["court cases", "policing"],
    "society": ["gender and equality", "immigration", "social media"],
    "education": ["universities", "schools"],
    "human interest": ["viral stories", "greetings and chatter"],
    "weather": ["storms", "heatwaves"],
    "disaster, accident and emergency incident": ["earthquakes", "transport accidents"],
    "religion": ["faith communities"],
    "labour": ["strikes and unions"],
}
# Invented mention data for the People and Companies panels (real names, fabricated counts and assignments).
PEOPLE = {
    "politics and government": ["Donald Trump", "Joe Biden", "Keir Starmer", "Emmanuel Macron", "Narendra Modi"],
    "conflict, war and peace": ["Volodymyr Zelensky", "Vladimir Putin", "Benjamin Netanyahu", "Donald Trump"],
    "sport": ["Lionel Messi", "LeBron James", "Cristiano Ronaldo", "Serena Williams", "Max Verstappen"],
    "science and technology": ["Elon Musk", "Sam Altman", "Albert Einstein", "Jensen Huang"],
    "arts, culture, entertainment and media": ["Taylor Swift", "Leonardo da Vinci", "Michelangelo", "Beyoncé", "Leonardo DiCaprio"],
    "economy, business and finance": ["Elon Musk", "Warren Buffett", "Jerome Powell", "Jensen Huang"],
    "environment": ["Greta Thunberg", "David Attenborough"],
    "health": ["Anthony Fauci", "Robert F. Kennedy Jr."],
    "crime, law and justice": ["Donald Trump", "Luigi Mangione"],
}
COMPANIES = {
    "science and technology": ["Apple", "Google", "Microsoft", "OpenAI", "Nvidia", "Meta Platforms", "Anthropic"],
    "economy, business and finance": ["Tesla", "Amazon", "JPMorgan Chase", "Nvidia", "Berkshire Hathaway"],
    "arts, culture, entertainment and media": ["Netflix", "Disney", "Spotify", "Nintendo", "Sony"],
    "sport": ["Nike", "Adidas", "Ferrari", "Red Bull"],
    "lifestyle and leisure": ["Starbucks", "McDonald's", "IKEA", "Airbnb"],
    "politics and government": ["Meta Platforms", "X", "Boeing"],
    "disaster, accident and emergency incident": ["Boeing", "Airbus", "Toyota"],
}
LANGS = ["en", "ja", "es", "pt", "de", "fr", "ko", "tr", "it", "nl"]
LANG_W = [55, 12, 7, 6, 5, 4, 4, 2, 3, 2]
COUNTRY_OF = {"en": ["US", "GB", "CA", "AU"], "ja": ["JP"], "es": ["ES", "MX", "AR"], "pt": ["BR", "PT"],
              "de": ["DE", "AT"], "fr": ["FR", "BE"], "ko": ["KR"], "tr": ["TR"], "it": ["IT"], "nl": ["NL"]}


class DemoSource:
    mode = "demo"

    def __init__(self, n_points: int = 40000, seed: int = 7):
        rng = random.Random(seed)
        self.rows = []
        self.detail = {}
        topics = list(TOPICS)
        cid = 0
        subs = []
        for ti, topic in enumerate(topics):
            ang = 2 * math.pi * ti / len(topics) + rng.uniform(-0.1, 0.1)
            cx, cy = 10 * math.cos(ang), 10 * math.sin(ang)
            for sub in TOPICS[topic]:
                a2 = rng.uniform(0, 2 * math.pi)
                d = rng.uniform(0.8, 3.2)
                subs.append((cid, topic, sub, cx + d * math.cos(a2), cy + d * math.sin(a2),
                             rng.uniform(0.25, 0.6), rng.uniform(0.4, 2.2)))
                cid += 1
        weights = [s[6] for s in subs]
        for i in range(n_points):
            c = rng.choices(subs, weights)[0]
            cluster, topic, sub, mx, my, sd, _ = c
            lang = rng.choices(LANGS, LANG_W)[0]
            if topic == "sport" and rng.random() < 0.3:
                lang = rng.choice(["es", "pt", "ja"])
            country = rng.choice(COUNTRY_OF[lang]) if rng.random() < 0.4 else None
            # recent hours are busier; a few topics are rising or fading
            age = min(167.9, rng.expovariate(1 / 30))
            if sub in ("ukraine conflict", "earthquakes", "stock markets", "football"):
                age *= 0.5
            if sub in ("peace talks", "universities", "heatwaves"):
                age = min(167.9, age * 1.8)
            score = min(0.95, max(0.3, rng.gauss(0.62, 0.12)))
            pid = f"demo{i}"
            people = (_pick(rng, PEOPLE.get(topic)) if rng.random() < 0.28 else ())
            companies = (_pick(rng, COMPANIES.get(topic)) if rng.random() < 0.18 else ())
            self.rows.append((pid, rng.gauss(mx, sd), rng.gauss(my, sd), cluster, topic, sub,
                              score, lang, country, age, people, companies))
            self.detail[pid] = {"id": pid, "text": f"[demo] a post about {sub}", "topic": topic,
                                "subtopic": sub, "lang": lang, "country": country, "score": round(score, 3),
                                "age_hours": round(age, 2), "place": None, "bot_score": rng.choice([0, 0, 0, 1, 2]),
                                "hashtags": [], "url": None}

    def _match(self, r, f):
        if r[9] > f.hours:
            return False
        if f.langs and r[7] not in f.langs:
            return False
        if f.topics and r[4] not in f.topics:
            return False
        if f.countries and r[8] not in f.countries:
            return False
        if f.people and not {_qid(p) for p in r[10]} & set(f.people):
            return False
        if f.companies and not {_qid(c) for c in r[11]} & set(f.companies):
            return False
        if f.min_score and (r[6] or 0) < f.min_score:
            return False
        if f.has_geo and not r[8]:
            return False
        return not f.q or f.q.lower() in self.detail[r[0]]["text"].lower()

    def meta(self, hours: int) -> dict:
        rows = [r for r in self.rows if r[9] <= hours]
        count = lambda i: sorted(({"value": k, "count": v} for k, v in _count(r[i] for r in rows).items() if k),
                                 key=lambda d: -d["count"])
        entities = lambda i: sorted(({"value": _qid(k), "label": k, "description": "demo data", "count": v,
                                      "sector": SECTOR_OF.get(k, (None, None))[0] if i == 11 else None,
                                      "industry": SECTOR_OF.get(k, (None, None))[1] if i == 11 else None}
                                     for k, v in _count(q for r in rows for q in r[i]).items()), key=lambda d: -d["count"])
        return {"mode": self.mode, "hours": hours, "total": len(rows),
                "topics": count(4), "langs": count(7), "countries": count(8),
                "people": entities(10), "companies": entities(11)}

    def clusters(self, f) -> dict:
        out = build_clusters([r for r in self.rows if self._match(r, f)], f.hours)
        for c in out["clusters"]:                             # entity ids are slugs of the names; the panel looks the names up
            for key in ("people", "companies"):
                c[key] = [[_qid(q), n] for q, n in c[key]]
        out["entities"] = {_qid(n): n for n in NAMES}
        return out

    def cluster_posts(self, cluster_id: int, f, limit: int = 25) -> list[dict]:
        rows = [r for r in self.rows if r[3] == cluster_id and self._match(r, f)]
        rows.sort(key=lambda r: r[9])
        return [self.detail[r[0]] for r in rows[:limit]]


SECTOR_OF = {"Apple": ("Technology", "Consumer electronics"), "Google": ("Technology", "Internet"), "Microsoft": ("Technology", "Software"),
             "OpenAI": ("Technology", "Artificial intelligence"), "Nvidia": ("Technology", "Semiconductors"),
             "Meta Platforms": ("Technology", "Social media"), "Anthropic": ("Technology", "Artificial intelligence"),
             "X": ("Communication Services", "Social media"), "Tesla": ("Consumer Discretionary", "Automotive"),
             "Amazon": ("Consumer Discretionary", "E-commerce"), "JPMorgan Chase": ("Financials", "Banking"),
             "Berkshire Hathaway": ("Financials", "Conglomerate"), "Netflix": ("Communication Services", "Streaming"),
             "Disney": ("Communication Services", "Entertainment"), "Spotify": ("Communication Services", "Music streaming"),
             "Nintendo": ("Technology", "Video games"), "Sony": ("Technology", "Electronics"), "Nike": ("Consumer Discretionary", "Apparel"),
             "Adidas": ("Consumer Discretionary", "Apparel"), "Ferrari": ("Consumer Discretionary", "Automotive"),
             "Red Bull": ("Consumer Staples", "Beverages"), "Starbucks": ("Consumer Discretionary", "Restaurants"),
             "McDonald's": ("Consumer Discretionary", "Restaurants"), "IKEA": ("Consumer Discretionary", "Retail"),
             "Airbnb": ("Consumer Discretionary", "Travel"), "Boeing": ("Industrials", "Aerospace"), "Airbus": ("Industrials", "Aerospace"),
             "Toyota": ("Consumer Discretionary", "Automotive")}
NAMES = sorted({n for pool in (*PEOPLE.values(), *COMPANIES.values()) for n in pool})


def _qid(name: str) -> str:
    return "demo:" + name.lower().replace(" ", "-")


def _pick(rng, pool):
    """One or two names from a topic's pool, earlier names being far more popular than later ones."""
    if not pool:
        return ()
    k = 1 if rng.random() < 0.85 else 2
    return tuple({rng.choices(pool, [1 / (i + 1) ** 1.2 for i in range(len(pool))])[0] for _ in range(k)})


def _count(it):
    out: dict = {}
    for v in it:
        out[v] = out.get(v, 0) + 1
    return out
