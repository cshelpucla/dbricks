import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import entity_lib as L  # noqa: E402


def person(qid, label, sl, aliases=()):
    return {"qid": qid, "kind": "person", "label": label, "aliases": list(aliases), "sitelinks": sl}


def company(qid, label, sl, aliases=()):
    return {"qid": qid, "kind": "company", "label": label, "aliases": list(aliases), "sitelinks": sl}


ENTITIES = [
    person("Q22686", "Donald Trump", 250, ["Donald J. Trump"]),
    person("Q1", "Melania Trump", 80), person("Q2", "Ivanka Trump", 70),
    person("Q76", "Barack Obama", 260), person("Q13133", "Michelle Obama", 140),
    person("Q3", "George W. Bush", 200), person("Q22", "George H. W. Bush", 190), person("Q4", "Jeb Bush", 90),
    person("Q762", "Leonardo da Vinci", 300, ["Leonardo"]),
    person("Q5", "Leonardo DiCaprio", 150),
    person("Q5592", "Michelangelo", 220),
    person("Q6", "Theresa May", 140), person("Q7", "George Michael", 120),
    person("Q8", "Michael Jackson", 200), person("Q9", "Michael Jordan", 180), person("Q10", "Michael Phelps", 100),
    person("Q11", "Michael Caine", 90), person("Q12", "Elon Musk", 250), person("Q13", "Beyoncé", 160),
    person("Q14", "Martin Luther King Jr.", 170), person("Q15", "Tim Cook", 90),
    company("Q312", "Apple Inc.", 180, ["Apple"]), company("Q95", "Google", 210),
    company("Q16", "Alphabet Inc.", 80), company("Q17", "Meta Platforms", 70, ["Meta"]),
    company("Q18", "Toyota Motor Corporation", 100, ["Toyota"]), company("Q19", "IBM", 150),
    company("Q20", "Target Corporation", 60), company("Q21", "Tesla, Inc.", 120, ["Tesla"]),
]


@pytest.fixture(scope="module")
def m():
    return L.EntityMatcher(ENTITIES)


def ids(m, text, tags=()):
    return {x["qid"] for x in m.match(text, tags)}


def test_full_names_case_insensitive_and_aliases(m):
    assert ids(m, "donald trump said something") == {"Q22686"}
    assert ids(m, "Donald J. Trump and Barack Obama met") == {"Q22686", "Q76"}
    assert ids(m, "Michelangelo painted the ceiling") == {"Q5592"}
    assert ids(m, "new Beyonce album") == {"Q13"}              # accent-insensitive


def test_surname_goes_to_dominant_entity(m):
    assert ids(m, "I think Trump is wrong") == {"Q22686"}      # 250 vs 80: at least twice as well known
    assert ids(m, "Not sure what Obama thinks") == {"Q76"}
    assert ids(m, "Trump's tariffs") == {"Q22686"}             # possessive
    assert ids(m, "Not sure about Bush today") == set()        # Bush: 200 vs 190 is a toss-up, dropped
    assert ids(m, "Not sure about Jeb Bush today") == {"Q4"}


def test_surnames_that_are_first_names_months_or_common_never_alone(m):
    assert ids(m, "See you in May") == set()
    assert ids(m, "I love Michael so much") == set()           # "Michael" (George Michael) starts four other names
    assert ids(m, "Theresa May spoke") == {"Q6"}
    assert ids(m, "Michael Jordan and Michael Jackson") == {"Q8", "Q9"}


def test_one_word_names_need_capitals_and_mid_sentence(m):
    assert ids(m, "I bought an Apple laptop") == {"Q312"}
    assert ids(m, "Apple pie is my favourite") == set()        # sentence start: could be the fruit
    assert ids(m, "Cook the pasta first") == set()             # an everyday word that is also a surname
    assert ids(m, "Trump says it again") == {"Q22686"}         # a distinctive surname may open a sentence
    assert ids(m, "an apple a day") == set()
    assert ids(m, "Not sure. Apple") == set()
    assert ids(m, "BREAKING: APPLE announces") == {"Q312"}     # all capitals are deliberate
    assert ids(m, "shares of IBM rose") == {"Q19"}
    assert ids(m, "what a great day, Meta is down") == {"Q17"}


def test_company_legal_suffixes_stripped(m):
    assert ids(m, "Toyota Motor outsold rivals") == {"Q18"}
    assert ids(m, "bought from Tesla last week") == {"Q21"}
    assert ids(m, "The Alphabet earnings call") == {"Q16"}


def test_generational_suffix_and_mononyms(m):
    assert ids(m, "Martin Luther King Jr. Day") == {"Q14"}
    assert ids(m, "I met Tim Cook yesterday") == {"Q15"}
    assert ids(m, "Leonardo was a genius, said Leonardo DiCaprio") == {"Q762", "Q5"}


def test_hashtags(m):
    assert ids(m, "so true", ["donaldtrump"]) == {"Q22686"}
    assert ids(m, "so true", ["Tesla"]) == {"Q21"}
    assert ids(m, "so true", ["nonsense"]) == set()


def test_urls_ignored_and_cap(m):
    assert ids(m, "read https://example.com/donald-trump/Trump") == set()
    out = m.match("Donald Trump Barack Obama Elon Musk Tim Cook Beyonce Michelangelo Google")
    assert len(out) == L.MAX_PER_POST and out[0]["confidence"] == 1.0


def test_parse_vital_people():
    wt = "==Entertainers==\n===Actors===\n# {{Icon|FA}} [[Marlon Brando]]<!--born 1924-->\n==Sports figures==\n# {{Icon|B}} [[Michael Jordan]]\n"
    assert L.parse_vital_people(wt) == [("Marlon Brando", "Entertainers"), ("Michael Jordan", "Sports figures")]


def test_pageview_titles():
    items = [{"article": a} for a in ["Main_Page", "Special:Search", "Taylor_Swift", "List_of_x", "Deaths_in_2026", "Wikipedia:Foo"]]
    assert L.pageview_titles(items) == ["Taylor Swift"]


def test_company_sectors():
    assert L.sector_of(["automotive industry"]) == "Consumer Discretionary"
    assert L.sector_of(["software industry", "internet"]) == "Technology"
    assert L.sector_of([], "American multinational technology company") == "Technology"
    assert L.sector_of(["banking"], "x") == "Financials"
    assert L.sector_of(["oil industry"]) == "Energy"
    assert L.sector_of([], "airline based in Vietnam") == "Transportation"
    assert L.sector_of([], "unknown thing") == "Other"
    assert L.clean_industry("automotive industry") == "Automotive"
    assert set(L.SECTORS) >= {"Technology", "Financials", "Other"}


def test_ordinary_word_and_place_aliases_are_ignored():
    ents = [person("Q1", "Thomas More", 120, ["More"]), person("Q2", "Jack London", 90),
            company("Q3", "Time Inc.", 50), company("Q4", "News Corporation", 80, ["News"]),
            person("Q5", "Donald Trump", 250)]
    m = L.EntityMatcher(ents, frozenset({"london"}))
    assert ids(m, "I want more cake, said London on the news, time flies") == set()
    assert ids(m, "so true", ["news"]) == set()
    assert ids(m, "Thomas More wrote Utopia") == {"Q1"}
    assert ids(m, "see Donald Trump") == {"Q5"}
