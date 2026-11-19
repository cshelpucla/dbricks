"""Famous people and companies: taxonomy parsing and mention matching for Bluesky posts.

Pure Python, no network and no Spark, so it is testable locally. Network loading of the taxonomy lives in
entity_loader.py; the contract is section 7.5 of docs/ENRICHMENT_SPEC.md.

Matching is a name dictionary, not embeddings: embeddings place "Trump" near "politics" but cannot tell
Trump from Biden. Precision rules, in order of importance:
  * a name of two or more words matches case-insensitively on word boundaries ("donald trump");
  * a one-word name (a surname, a mononym, a short brand) must appear exactly as written. A company name or a
    surname that is also an ordinary English word ("Apple", "Cook", "Page") must also be in the middle of a
    sentence, unless written in capitals: "Apple" matches "I love Apple products" but not "Apple pie". A person's
    own mononym or alias ("Michelangelo", "Leonardo") and distinctive surnames ("Trump says ...") may start one;
  * a one-word name claimed by several entities goes to the best known one if it is at least twice as well
    known as the runner-up, otherwise it is dropped;
  * a one-word name that is an ordinary word (more, time, sky: see common_words.py), a big place (London, Texas) or a
    common first name is only used when it is the entity's own label and the entity is well known;
  * a surname that is also an everyday word or a very common surname (Cook, Smith) is never used on its own;
  * a multi-word alias made only of ordinary words ("the greatest", "sound of music") is dropped.
"""
import re
import unicodedata
from collections import defaultdict

from common_words import COMMON

PERSON, COMPANY = "person", "company"
MAX_NAME_TOKENS = 6
MAX_PER_POST = 5
DOMINANCE = 1.5               # a shared one-word name goes to the entity with at least this many times the sitelinks
FIRST_NAME_BLOCK = 3          # a surname that starts this many other people's names is a first name: never alone
MIN_SINGLE_CHARS = 4          # shorter one-word person names ("Li", "Ng") are too ambiguous
CONF_FULL, CONF_TAG, CONF_SINGLE = 1.0, 0.9, 0.7

MIN_SURNAME_SITELINKS = 40      # a surname alone ("Court", "Cloud") needs a well-known bearer
MIN_PLACE_NAME_SITELINKS = 150  # a name that is also a place ("Victoria", "Orange") needs a very well-known bearer
MIN_LABEL_SITELINKS = 60       # an ordinary-word label ("Amazon", "Apple") needs at least this many Wikipedia editions
COMMON_FIRST_NAMES = set("""Aaron Adam Alan Albert Alex Alexander Alice Andrew Andy Ann Anna Anne Anthony Arthur Ben Benjamin Bill Bob Brian
Carl Charles Charlie Chris Christopher Daniel Dave David Dean Dennis Donald Edward Emily Emma Eric Frank Fred George Gary Gerald Harry Henry
Jack Jacob James Jane Jason Jeff Jeremy Jerry Jim Joe John Jonathan Joseph Josh Julia Justin Karl Keith Ken Kevin Larry Laura Lee Leo Louis
Luke Mark Martin Mary Matt Matthew Max Michael Mike Nathan Neil Nick Nicholas Oliver Oscar Patrick Paul Peter Philip Ralph Richard Rob Robert
Roger Ron Ronald Roy Ryan Sam Samuel Sarah Scott Sean Simon Stephen Steve Steven Susan Ted Thomas Tim Timothy Tom Tony Victor Walter Will
William Zach""".split())
DAYS_MONTHS = set("""January February March April May June July August September October November December
Monday Tuesday Wednesday Thursday Friday Saturday Sunday""".split())
# Words that are capitalised mid-sentence for other reasons, or that name a person only rarely.
NEVER_ALONE = DAYS_MONTHS | set("""The This That These Those There Here Not But And For With Your Their What When Where Which Who
God Lord Pope King Queen Prince Princess Saint Mister Doctor President Prime Minister Chairman Black White Red Blue Green
Mother Father Sister Brother Master Little Great Good Best New Old North South East West Big Group Company Corporation
Limited Holdings International Global World United American British National General Digital Media Network Networks Systems
Technologies Technology Industries Services Bank Air Airlines Motors Energy Capital Partners Studios Entertainment""".split())

# Surnames that are also everyday words: only counted in mid-sentence position ("Cook the pasta" is not Tim Cook).
COMMON_WORD_SURNAMES = set("""Baker Bell Bird Black Brown Bush Carter Cook Cooper Cross Day Fisher Ford Forest Fox Frost Gates Gold Grant
Green Hall Hill Hope Hunt King Knight Lamb Lane Long Love Marshall Mason Miller Mills Moon Moore Page Palmer Park Pope Porter Potter
Powell Price Reed Rice Rose Ross Sharp Shaw Smith Snow Stone Strong Summers Swift Taylor Turner Walker Ward Weaver Wells West White
Wilde Wood Wright Young""".split())

LEGAL_SUFFIX = re.compile(
    r"[,\s]+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|llc|plc|lp|llp|ag|sa|s\.a|nv|n\.v|se|ab|asa|oyj|gmbh|"
    r"kgaa|bv|b\.v|spa|s\.p\.a|pte|pty|holdings|holding|group|the)\.?(?=$|[,\s])", re.I)
GENERATIONAL = {"jr", "sr", "ii", "iii", "iv", "v"}
POSSESSIVE = re.compile(r"[’'](?:s)?$", re.I)
WORD_RE = re.compile(r"[^\W\d_]+(?:[’'\-.][^\W\d_]+)*|\d+")
SENTENCE_BREAK = re.compile(r"[.!?:;\n\"“”(\[—–]")
HASHTAG_SAFE = re.compile(r"[^a-z0-9]")


def _fold(s: str) -> str:
    """Accent-stripped form: 'Beyoncé' and 'Beyonce' meet in the middle."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalise(text) -> str:
    return _fold(unicodedata.normalize("NFKC", text or ""))


def tokens_of(text: str) -> list[tuple[str, int, int]]:
    """(token, start, end) for words in normalise(text); possessives are removed ('Trump's' -> 'Trump')."""
    out = []
    for m in WORD_RE.finditer(text):
        tok = POSSESSIVE.sub("", m.group()).strip(".-")
        if tok:
            out.append((tok, m.start(), m.end()))
    return out


def company_names(label: str) -> list[str]:
    """The label plus the label without legal suffixes: 'Alphabet Inc.' -> ['Alphabet Inc.', 'Alphabet']."""
    names = [label]
    short = label
    for _ in range(3):                                   # 'Toyota Motor Corporation' / 'X Holdings Co., Ltd.'
        new = LEGAL_SUFFIX.sub("", short).strip(" ,.")
        if new == short:
            break
        short = new
    if short and short != label:
        names.append(short)
    return names


def person_names(label: str) -> list[tuple[str, int]]:
    """(name, tier) derived from a label besides the label itself: the surname and first + last name. tier 2 = derived."""
    toks = [t for t, _, _ in tokens_of(normalise(label))]
    out = []
    if len(toks) >= 2:
        last = toks[-1]
        i = len(toks) - 1
        while last.lower().strip(".") in GENERATIONAL and i > 1:
            i -= 1
            last = toks[i]
        if last[:1].isupper() and not toks[i - 1].islower() and last.lower().strip(".") not in GENERATIONAL:
            out.append((last, 2))
        if len(toks) >= 3 and not any(t.islower() for t in toks):
            out.append((f"{toks[0]} {last}", 2))
    return out


class EntityMatcher:
    """Builds the dictionaries once from taxonomy rows, then matches post by post.

    entities: dicts with qid, kind ('person' | 'company'), label, aliases (list), sitelinks (int, importance).
    places: lower-case one-word names of countries, regions and big cities that must not be read as an alias or surname.
    """

    def __init__(self, entities: list[dict], places: frozenset = frozenset()):
        self.info = {e["qid"]: {"qid": e["qid"], "kind": e["kind"], "label": e["label"]} for e in entities}
        weight = {e["qid"]: e.get("sitelinks") or 0 for e in entities}
        first_names = defaultdict(int)
        for e in entities:
            if e["kind"] == PERSON:
                toks = [t for t, _, _ in tokens_of(normalise(e["label"]))]
                if len(toks) >= 2:
                    first_names[toks[0]] += 1

        multi_c: dict[tuple, dict] = defaultdict(dict)    # key -> {qid: best (tier, -weight)}
        single_c: dict[str, dict] = defaultdict(dict)

        def offer(name, qid, tier):
            toks = [t for t, _, _ in tokens_of(normalise(name))]
            if not toks or len(toks) > MAX_NAME_TOKENS:
                return
            kind = self.info[qid]["kind"]
            if len(toks) >= 2:
                bucket, key = multi_c, tuple(t.lower() for t in toks)
                if len(" ".join(toks)) < 5:
                    return
                if tier >= 1 and all(len(t) < 3 or t.lower() in COMMON or not t.isalpha() for t in toks):
                    return                                   # "the greatest", "take 2": a phrase, not a name
            else:
                tok = toks[0]
                low = tok.lower()
                if tok.title() in NEVER_ALONE or not tok[:1].isupper() or len(tok) < 3:
                    return                                   # one-word names must be written capitalised
                if len(tok) < MIN_SINGLE_CHARS and tier != 0:
                    return                                   # "ICE", "CNR": acronyms must be the entity's own label
                if tier >= 1 and tok in COMMON_FIRST_NAMES or kind == PERSON and tier >= 1 and first_names.get(tok, 0) >= FIRST_NAME_BLOCK:
                    return
                if kind == PERSON and tier == 2 and (tok in COMMON_WORD_SURNAMES or len(tok) < MIN_SINGLE_CHARS or weight[qid] < MIN_SURNAME_SITELINKS):
                    return
                if low in places and (tier != 0 or weight[qid] < MIN_PLACE_NAME_SITELINKS):
                    return                                   # "London" is not Jack London, "Texas" is not Texaco
                if low in COMMON and not (tier == 0 and weight[qid] >= MIN_LABEL_SITELINKS):
                    return
                bucket, key = single_c, tok
            cur = bucket[key].get(qid)
            if cur is None or tier < cur:
                bucket[key][qid] = tier

        for e in entities:
            qid, label = e["qid"], e["label"]
            names = [(label, 0)]
            names += [(a, 1) for a in e.get("aliases") or []]
            if e["kind"] == COMPANY:
                names += [(n, 0) for n in company_names(label)[1:]]     # the label without its legal suffix is still the label
                names += [(n, 1) for a in e.get("aliases") or [] for n in company_names(a)[1:]]
            else:
                names += person_names(label)
            for name, tier in names:
                offer(name, qid, tier)

        def resolve(cands: dict):
            """One winner per name: best tier, then dominant sitelinks; None if it is a toss-up."""
            best = min(cands.values())
            top = sorted((q for q, t in cands.items() if t == best), key=lambda q: -weight[q])
            if len(top) == 1:
                return top[0]
            w0, w1 = weight[top[0]], weight[top[1]]
            return top[0] if w0 >= DOMINANCE * max(w1, 1) else None

        self.multi = {k: q for k, c in multi_c.items() if (q := resolve(c))}
        self.single = {k: (q, self._starts_sentence_ok(k, q, single_c[k][q])) for k, c in single_c.items() if (q := resolve(c))}
        self.max_len = max((len(k) for k in self.multi), default=1)
        self.first_words = {k[0] for k in self.multi}
        # hashtags: the name with spaces removed, lower-case ('#donaldtrump', '#trump')
        self.tags: dict[str, str] = {}
        for k, q in self.multi.items():
            key = HASHTAG_SAFE.sub("", "".join(k))
            if len(key) >= 5:
                self.tags.setdefault(key, q)
        for k, (q, _) in self.single.items():
            key = HASHTAG_SAFE.sub("", k.lower())
            if len(key) >= 4 and key not in COMMON:
                self.tags.setdefault(key, q)

    def match(self, text, hashtags=()) -> list[dict]:
        """Entities mentioned in a post, best first, at most MAX_PER_POST: {qid, kind, label, confidence, matched}."""
        found: dict[str, tuple[float, str]] = {}

        def add(qid, conf, matched):
            if qid not in found or conf > found[qid][0]:
                found[qid] = (conf, matched)

        norm = normalise(_strip_urls(text))
        toks = tokens_of(norm)
        i, n = 0, len(toks)
        while i < n:
            tok, start, _ = toks[i]
            hit = 0
            if tok.lower() in self.first_words:
                for ln in range(min(self.max_len, n - i), 1, -1):
                    q = self.multi.get(tuple(t[0].lower() for t in toks[i:i + ln]))
                    if q:
                        add(q, CONF_FULL, " ".join(t[0] for t in toks[i:i + ln]))
                        hit = ln
                        break
            if not hit:
                allcaps = tok.isupper() and len(tok) >= 3
                key = tok if tok in self.single else (tok.capitalize() if allcaps and tok.capitalize() in self.single else None)
                if key:
                    q, free = self.single[key]
                    if allcaps or free or self._mid_sentence(norm, start, i):
                        add(q, CONF_SINGLE, tok)
            i += hit or 1

        for tag in hashtags or ():
            q = self.tags.get(HASHTAG_SAFE.sub("", normalise(str(tag)).lower()))
            if q:
                add(q, CONF_TAG, "#" + str(tag))

        ranked = sorted(found.items(), key=lambda kv: -kv[1][0])[:MAX_PER_POST]
        return [{**self.info[q], "confidence": c, "matched": m} for q, (c, m) in ranked]

    def _starts_sentence_ok(self, name: str, qid: str, tier: int) -> bool:
        """May a one-word name open a sentence? Only people, and not when the name is an everyday word."""
        return self.info[qid]["kind"] == PERSON and name not in COMMON_WORD_SURNAMES

    @staticmethod
    def _mid_sentence(text: str, start: int, index: int) -> bool:
        """True when the word at `start` is not the first word of a sentence."""
        if index == 0:
            return False
        before = text[max(0, start - 3):start]
        return not SENTENCE_BREAK.search(before)


def _strip_urls(text) -> str:
    return re.sub(r"https?://\S+|www\.\S+", " ", text or "", flags=re.I)


# ---- taxonomy source parsing (used by entity_loader.py) ---------------------------------------------
def parse_vital_people(wikitext: str) -> list[tuple[str, str]]:
    """(article title, section) for each person in Wikipedia's 'Vital articles/Level 4/People' page.
    The section is the level-2 heading ('Entertainers', 'Writers', 'Sports figures' ...)."""
    out, section = [], None
    for line in wikitext.split("\n"):
        h = re.match(r"^==\s*([^=].*?)\s*==\s*$", line)
        if h:
            section = h.group(1)
            continue
        m = re.match(r"^#.*?\[\[([^\]|#]+)", line)
        if m:
            out.append((m.group(1).strip(), section))
    return out


def pageview_titles(items: list[dict]) -> list[str]:
    """Article titles from a Wikimedia top-pageviews response, without special and project pages."""
    out = []
    for it in items:
        t = it["article"].replace("_", " ")
        if ":" in t or t == "Main Page" or t.startswith("List of") or re.match(r"^Deaths in ", t):
            continue
        out.append(t)
    return out


def places_from_gazetteer(gaz, min_population=300_000) -> frozenset:
    """One-word lower-case names of countries, regions and big cities, to keep them out of alias and surname matching."""
    if gaz is None:
        return frozenset()
    return frozenset(k for k, e in gaz.latin.items()
                     if " " not in k and k.isalpha() and (e[0] in ("country", "admin1") or e[5] >= min_population))


# ---- company sectors ----------------------------------------------------------------------------------
# Sector -> industry -> company. The industry is Wikidata's own "industry" value for the company; the sector is a fixed
# GICS-style grouping chosen from the industry names (then the company description). The first rule that matches wins,
# so specific rules come before general ones.
SECTOR_RULES = [
    ("Utilities", r"electric utilit|electricity|power (?:generation|supply|company)|water (?:supply|utilit)|utilit"),
    ("Energy", r"\boil\b|petrol|natural gas|\bgas (?:industry|company)|\bcoal\b|renewable|energy"),
    ("Health Care", r"pharma|biotech|health|medic|drug|hospital|life science"),
    ("Financials", r"bank|insurance|financ|invest|asset management|payment|credit|stock exchange|brokerage|fintech|capital"),
    ("Real Estate", r"real estate|propert"),
    ("Technology", r"software|internet|computer|semiconductor|electronic|information technology|artificial intelligence|"
                   r"cloud|social (?:network|media)|video game|e-commerce|technology|\bit\b|telecommunications equipment"),
    ("Communication Services", r"telecom|media|broadcast|publish|entertainment|film|music|streaming|television|news|advertis|"
                               r"mobile network|wireless"),
    ("Transportation", r"airline|aviation|air (?:transport|travel)|railway|rail|shipping|maritime|logistic|freight|transport|"
                       r"airport|courier"),
    ("Industrials", r"aerospace|defen[cs]e|machinery|construction|engineering|manufactur|industrial|conglomerate|"
                    r"electrical equipment|building"),
    ("Consumer Discretionary", r"automotive|automobile|\bcars?\b|motor vehicle|retail|apparel|fashion|cloth|footwear|hotel|"
                               r"restaurant|leisure|travel|tourism|toy|luxury|sporting goods|e-?tailer|department store|casino|gambling"),
    ("Consumer Staples", r"food|beverage|drink|tobacco|brewing|brewery|supermarket|grocery|household|cosmetic|personal care|"
                         r"agricultur|dairy|confectioner|snack"),
    ("Materials", r"chemical|mining|metal|steel|paper|cement|packaging|forestry|aluminium|aluminum|gold"),
]
SECTORS = [s for s, _ in SECTOR_RULES] + ["Other"]
_SECTOR_RE = [(s, re.compile(r, re.I)) for s, r in SECTOR_RULES]


def sector_of(industries, description=None) -> str:
    """Sector for a company from its industry names, falling back to its description; 'Other' if nothing matches."""
    for text in [*(industries or []), description]:
        for sector, rx in _SECTOR_RE:
            if text and rx.search(text):
                return sector
    return "Other"


def clean_industry(label: str) -> str:
    """'automotive industry' -> 'Automotive', 'software industry' -> 'Software'."""
    s = re.sub(r"\s+(?:industry|sector|business)$", "", label.strip(), flags=re.I)
    return s[:1].upper() + s[1:]
