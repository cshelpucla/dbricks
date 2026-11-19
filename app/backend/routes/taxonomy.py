"""Taxonomies tab: the topic, people and company vocabularies and the map's cluster names."""
from fastapi import APIRouter, HTTPException, Query, Request

from ..services.taxonomy import KEYS

router = APIRouter(prefix="/api")


@router.get("/taxonomies")
def taxonomies(request: Request, hours: int = Query(24, ge=1, le=720)):
    """Size and source of each taxonomy, and how many posts in the window each one tagged."""
    return request.app.state.taxonomy.overview(hours)


@router.get("/taxonomies/{key}")
def taxonomy(request: Request, key: str, hours: int = Query(24, ge=1, le=720)):
    """One taxonomy as a flat tree (nodes with parent ids) with post counts per node, plus how it is applied."""
    if key not in KEYS:
        raise HTTPException(404, f"unknown taxonomy; one of {', '.join(KEYS)}")
    return request.app.state.taxonomy.tree(key, hours)
