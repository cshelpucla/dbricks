"""Pipelines, costs and data-model endpoints."""
from fastapi import APIRouter, HTTPException, Query, Request

from .. import config
from ..models import _csv
from ..services.costs import GROUPS
from ..services.geo import LEVELS
from ..services.registry import BY_KEY

router = APIRouter(prefix="/api")


@router.get("/pipelines")
def pipelines(request: Request, hours: int = Query(24, ge=1, le=720)):
    return {"mode": request.app.state.pipelines.mode, "hours": hours,
            "pipelines": request.app.state.pipelines.overview(hours)}


@router.get("/pipelines/{key}/stats")
def pipeline_stats(request: Request, key: str, hours: int = Query(24, ge=1, le=720)):
    if key not in BY_KEY:
        raise HTTPException(404, "unknown pipeline")
    return request.app.state.pipelines.stats(key, hours)


@router.get("/costs")
def costs(request: Request, days: int = Query(30, ge=1, le=365), group: str = "product"):
    if group not in GROUPS:
        raise HTTPException(422, f"group must be one of {', '.join(GROUPS)}")
    return {"mode": request.app.state.costs.mode, **request.app.state.costs.costs(days, group)}


@router.get("/model/status")
def model_status(request: Request):
    """Which tables of the data model exist right now (the ERD marks the rest as planned)."""
    if config.DATA_MODE == "demo":
        return {"mode": "demo", "existing": ["events_raw", "posts", "likes", "reposts", "follows"]}
    from ..services import sqlexec
    rows = sqlexec.run(f"SELECT table_name FROM {config.CATALOG}.information_schema.tables "
                       f"WHERE table_schema = '{config.SCHEMA}'")
    return {"mode": "live", "existing": [r[0] for r in rows]}


@router.get("/geo")
def geo(request: Request, hours: int = Query(24, ge=1, le=720), min_conf: float = Query(0.4, ge=0, le=1),
        level: str = "place", topic: str | None = None, lang: str | None = None,
        person: str | None = None, company: str | None = None):
    """Post volume by inferred location (World map tab). person/company: Wikidata ids the posts must mention."""
    if level not in LEVELS:
        raise HTTPException(422, f"level must be one of {', '.join(LEVELS)}")
    return request.app.state.geo.points(hours, min_conf, level, _csv(topic), _csv(lang), _csv(person), _csv(company))
