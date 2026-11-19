"""Request filters, validated once at the API boundary."""
from dataclasses import dataclass, field

from fastapi import HTTPException

MAX_LIST = 50
MAX_HOURS = 24 * 30


@dataclass
class Filters:
    hours: int = 24
    langs: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)       # Wikidata ids of people mentioned in the post
    companies: list[str] = field(default_factory=list)    # Wikidata ids of companies mentioned in the post
    min_score: float = 0.0
    has_geo: bool = False
    q: str = ""
    limit: int = 50000


def _csv(value: str | None) -> list[str]:
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    if len(items) > MAX_LIST:
        raise HTTPException(422, f"at most {MAX_LIST} values per filter")
    return items


def parse_filters(hours: int, lang: str | None, topic: str | None, country: str | None,
                  min_score: float, has_geo: bool, q: str, limit: int, max_points: int,
                  person: str | None = None, company: str | None = None) -> Filters:
    if not 1 <= hours <= MAX_HOURS:
        raise HTTPException(422, f"hours must be between 1 and {MAX_HOURS}")
    if not 0 <= min_score <= 1:
        raise HTTPException(422, "min_score must be between 0 and 1")
    return Filters(hours=hours, langs=_csv(lang), topics=_csv(topic), countries=_csv(country),
                   people=_csv(person), companies=_csv(company),
                   min_score=min_score, has_geo=has_geo, q=q.strip()[:100],
                   limit=max(100, min(limit, max_points)))
