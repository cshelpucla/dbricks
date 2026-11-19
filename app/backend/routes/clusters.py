from fastapi import APIRouter, Query, Request

from ..config import MAX_POINTS
from ..models import parse_filters

router = APIRouter(prefix="/api")


def _filters(hours: int, lang: str | None, topic: str | None, country: str | None, min_score: float,
             has_geo: bool, q: str, person: str | None = None, company: str | None = None):
    return parse_filters(hours, lang, topic, country, min_score, has_geo, q, MAX_POINTS, MAX_POINTS, person, company)


@router.get("/meta")
def meta(request: Request, hours: int = 24):
    """Filter options and counts for the time window (not narrowed by the other filters)."""
    return request.app.state.source.meta(_filters(hours, None, None, None, 0, False, "").hours)


@router.get("/clusters")
def clusters(request: Request, hours: int = 24, lang: str | None = None, topic: str | None = None,
             country: str | None = None, min_score: float = 0.0, has_geo: bool = False, q: str = "",
             person: str | None = None, company: str | None = None):
    f = _filters(hours, lang, topic, country, min_score, has_geo, q, person, company)
    return request.app.state.source.clusters(f)


@router.get("/clusters/{cluster_id}/posts")
def cluster_posts(request: Request, cluster_id: int, hours: int = 24, lang: str | None = None,
                  topic: str | None = None, country: str | None = None, min_score: float = 0.0,
                  has_geo: bool = False, q: str = "", person: str | None = None, company: str | None = None,
                  limit: int = Query(25, ge=1, le=50)):
    f = _filters(hours, lang, topic, country, min_score, has_geo, q, person, company)
    return {"posts": request.app.state.source.cluster_posts(cluster_id, f, limit)}
