"""Builds the people and company taxonomy from Wikipedia and Wikidata (network; run once, stored in Delta).

People (who matters, in two lists that cover each other's blind spots):
  * Wikipedia "Vital articles/Level 4/People": about two thousand figures editors judge essential
    (Leonardo, Michelangelo, Einstein, Messi), grouped by field;
  * people who sit in the English Wikipedia monthly top-1000 pageviews in at least PAGEVIEW_MIN_MONTHS of the
    last PAGEVIEW_MONTHS months (Trump, Musk, Taylor Swift): who is being talked about now.
Companies: Wikidata items of business-like types with at least COMPANY_MIN_SITELINKS Wikipedia editions.

Everything is resolved to Wikidata items with an English label, aliases and a sitelink count (importance).
Dry run without Spark:  python entity_loader.py people|companies out.json
"""
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date

import entity_lib as L

log = logging.getLogger("entity_loader")

UA = {"User-Agent": "bluesky-enrich/1.0"}


def set_contact(contact):
    """Wikimedia asks API clients to identify themselves with a contact (URL or address) in the User-Agent."""
    if contact:
        UA["User-Agent"] = f"bluesky-enrich/1.0 ({contact})"

WIKI_API = "https://en.wikipedia.org/w/api.php"
WD_API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
PAGEVIEWS = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y}/{m:02d}/all-days"
VITAL_PEOPLE = "Wikipedia:Vital articles/Level 4/People"

PAGEVIEW_MONTHS = 6
PAGEVIEW_MIN_MONTHS = 2
PERSON_MIN_SITELINKS = 30
COMPANY_MIN_SITELINKS = 30
# Wikidata classes: business, public company, company, multinational, enterprise, corporation, media company,
# technology company, airline, bank, car maker, social networking service, video game company, retailer
COMPANY_CLASSES = ["Q4830453", "Q891723", "Q783794", "Q161726", "Q6881511", "Q167037", "Q1331793", "Q2085381",
                   "Q46970", "Q22687", "Q786820", "Q202833", "Q1137109", "Q507619"]
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def get_json(url, params=None, retries=5, timeout=120):
    """GET JSON with retries. A 429 (Wikimedia rate limit) waits a minute, which is what its limiter asks for."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as e:                                # noqa: BLE001 - any network or JSON failure is retried
            last = e
            if getattr(e, "code", None) in (400, 404):         # will not get better by asking again
                break
            wait = 65 if getattr(e, "code", None) == 429 else 2 * (attempt + 1)
            log.warning("attempt %d/%d failed (%s); retrying in %ds: %s", attempt + 1, retries, e, wait, url[:90])
            time.sleep(wait)
    raise RuntimeError(f"request failed after {retries} attempts: {url[:120]}") from last


def sparql(query):
    return get_json(SPARQL, {"query": query, "format": "json"}, timeout=70)["results"]["bindings"]


def qid_of(uri):
    return uri.rsplit("/", 1)[-1]


def gather(fn, batches, pause=1.0):
    """Merges the dict results of fn over batches, one request at a time with a pause: Wikimedia's limiters punish bursts."""
    out = {}
    for batch in batches:
        out.update(fn(batch))
        time.sleep(pause)
    return out


def chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


# ---- people ---------------------------------------------------------------------------------------------
def vital_people():
    doc = get_json(WIKI_API, {"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                              "titles": VITAL_PEOPLE, "format": "json", "formatversion": 2})
    return L.parse_vital_people(doc["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"])


def pageview_people_titles(months=PAGEVIEW_MONTHS, min_months=PAGEVIEW_MIN_MONTHS):
    today = date.today()
    y, m = today.year, today.month
    seen = Counter()
    for _ in range(months):
        m -= 1                                                # the current month is incomplete
        if m == 0:
            y, m = y - 1, 12
        try:
            items = get_json(PAGEVIEWS.format(y=y, m=m))["items"][0]["articles"]
        except RuntimeError:
            log.warning("pageviews for %d-%02d unavailable", y, m)
            continue
        seen.update(set(L.pageview_titles(items)))
    return [t for t, c in seen.items() if c >= min_months]


def wiki_pages(titles):
    """English Wikipedia titles to (Wikidata id, lead image) (redirects followed); unknown titles are dropped. The lead
    image is the Commons file name of the article's freely licensed page image (for a person, usually the infobox
    portrait) or None; only formats a browser shows as a thumbnail are kept."""
    def one(batch):
        doc = get_json(WIKI_API, {"action": "query", "titles": "|".join(batch), "prop": "pageprops|pageimages",
                                  "ppprop": "wikibase_item", "piprop": "name", "redirects": 1, "format": "json", "formatversion": 2})
        q = doc.get("query", {})
        redirect = {r["from"]: r["to"] for r in q.get("redirects", [])}
        norm = {n["from"]: n["to"] for n in q.get("normalized", [])}
        by_title = {p["title"]: (p.get("pageprops", {}).get("wikibase_item"), p.get("pageimage")) for p in q.get("pages", [])}
        out = {}
        for t in batch:
            t2 = redirect.get(norm.get(t, t), norm.get(t, t))
            qid, image = by_title.get(t2, (None, None))
            if qid:
                out[t] = (qid, image if image and image.lower().endswith(IMAGE_TYPES) else None)
        return out
    return gather(one, chunks(titles, 50))


def facts(qids):
    """qid -> (True, sitelinks) for those of qids that are humans (others are absent), 400 ids per query."""
    def one(batch):
        vals = " ".join(f"wd:{q}" for q in batch)
        rows = sparql(f"SELECT ?x ?sl WHERE {{ VALUES ?x {{ {vals} }} ?x wdt:P31 wd:Q5; wikibase:sitelinks ?sl }}")   # humans only
        return {qid_of(r["x"]["value"]): (True, int(r["sl"]["value"])) for r in rows}
    return gather(one, chunks(qids, 400), pause=0)


def labels(qids):
    """qid -> {label, description, aliases} in English via wbgetentities, 50 ids per call."""
    def one(batch):
        doc = get_json(WD_API, {"action": "wbgetentities", "ids": "|".join(batch), "props": "labels|descriptions|aliases",
                                "languages": "en", "format": "json"})
        out = {}
        for q, e in doc.get("entities", {}).items():
            label = e.get("labels", {}).get("en", {}).get("value")
            if label:
                out[q] = {"label": label, "description": e.get("descriptions", {}).get("en", {}).get("value"),
                          "aliases": [a["value"] for a in e.get("aliases", {}).get("en", [])]}
        return out
    return gather(one, chunks(qids, 50))


def build_people():
    vital = vital_people()
    log.info("vital articles: %d people", len(vital))
    views = pageview_people_titles()
    log.info("pageview regulars: %d titles", len(views))
    group_of_title = dict(vital)
    page_of = wiki_pages(list(group_of_title) + [t for t in views if t not in group_of_title])
    f = facts(sorted({q for q, _ in page_of.values()}))
    img = {q: image for q, image in page_of.values() if image}
    keep = {}
    for title, (q, _) in page_of.items():
        human, sl = f.get(q, (False, 0))
        if not human:
            continue
        if title in group_of_title:
            keep.setdefault(q, ("vital", group_of_title[title], sl))
        elif sl >= PERSON_MIN_SITELINKS:
            keep.setdefault(q, ("pageviews", None, sl))
    lab = labels(sorted(keep))
    rows = [{"qid": q, "kind": L.PERSON, "label": lab[q]["label"], "description": lab[q]["description"],
             "aliases": lab[q]["aliases"], "sitelinks": sl, "category": cat, "industry": None, "image": img.get(q), "source": src}
            for q, (src, cat, sl) in keep.items() if q in lab]
    log.info("people: %d (%d vital, %d pageview-only, %d with a portrait)", len(rows), sum(r["source"] == "vital" for r in rows),
             sum(r["source"] == "pageviews" for r in rows), sum(r["image"] is not None for r in rows))
    return rows



# ---- companies ------------------------------------------------------------------------------------------
def company_query(classes):
    vals = " ".join(f"wd:{c}" for c in classes)
    return (f"SELECT DISTINCT ?c ?sl WHERE {{ VALUES ?t {{ {vals} }} ?c wdt:P31 ?t; wikibase:sitelinks ?sl. "
            f"FILTER(?sl >= {COMPANY_MIN_SITELINKS}) }}")


def industries_of(qids):
    """qid -> list of industry qids (Wikidata 'industry', P452), 300 companies per query. A failed batch just means no industry."""
    out = {}
    for batch in chunks(qids, 300):
        vals = " ".join(f"wd:{q}" for q in batch)
        try:
            for r in sparql(f"SELECT ?c ?i WHERE {{ VALUES ?c {{ {vals} }} ?c wdt:P452 ?i }}"):
                out.setdefault(qid_of(r["c"]["value"]), []).append(qid_of(r["i"]["value"]))
        except RuntimeError:
            log.warning("industry batch unavailable")
    return out


def build_companies():
    found = {}
    try:
        rows = sparql(company_query(COMPANY_CLASSES))
        found = {qid_of(r["c"]["value"]): int(r["sl"]["value"]) for r in rows}
    except RuntimeError:                                      # the combined query can hit the 60 s limit: go class by class
        log.warning("combined company query failed; trying one class at a time")
        for cls in COMPANY_CLASSES:
            try:
                found.update({qid_of(r["c"]["value"]): int(r["sl"]["value"]) for r in sparql(company_query([cls]))})
            except RuntimeError:
                log.warning("company class %s unavailable", cls)
    lab = labels(sorted(found))
    ind_of = industries_of(sorted(q for q in found if q in lab))
    ind_lab = labels(sorted({i for v in ind_of.values() for i in v}))
    rows = []
    for q, sl in found.items():
        if q not in lab:
            continue
        inds = [L.clean_industry(ind_lab[i]["label"]) for i in ind_of.get(q, []) if i in ind_lab]
        rows.append({"qid": q, "kind": L.COMPANY, "label": lab[q]["label"], "description": lab[q]["description"],
                     "aliases": lab[q]["aliases"], "sitelinks": sl, "source": "wikidata", "image": None,
                     "category": L.sector_of(inds, lab[q]["description"]), "industry": inds[0] if inds else None})
    log.info("companies: %d; sectors: %s; with an industry: %d", len(rows),
             dict(Counter(r["category"] for r in rows).most_common()), sum(r["industry"] is not None for r in rows))
    return rows


BUILDERS = {"people": (build_people, 500), "companies": (build_companies, 300)}


def build(kind):
    """Rows for one taxonomy ('people' or 'companies'); raises if the pull looks incomplete."""
    fn, minimum = BUILDERS[kind]
    rows = fn()
    if len(rows) < minimum:
        raise RuntimeError(f"{kind} taxonomy looks incomplete: {len(rows)} rows (expected at least {minimum})")
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    kind, out = sys.argv[1], sys.argv[2]
    rows = build(kind)
    json.dump(rows, open(out, "w"), ensure_ascii=False)
    print(len(rows), kind, "written to", out)
