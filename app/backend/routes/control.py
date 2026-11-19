"""Pause and resume endpoints. Writes require a custom header, so a cross-site form or script cannot
trigger them (a custom header forces a CORS preflight, which this app never approves)."""
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from ..services.control import ACTIONS, PAUSE_ORDER, RESUME_ORDER
from ..services.registry import BY_KEY

router = APIRouter(prefix="/api/control")
log = logging.getLogger("control")
CSRF_HEADER = "bluesky-analytics"


def _actor(request: Request) -> str:
    # Databricks Apps forwards the signed-in user's identity in these headers.
    return request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-preferred-username") or "unknown"


def _check(action: str, x_requested_with: str | None):
    if x_requested_with != CSRF_HEADER:
        raise HTTPException(403, "missing X-Requested-With header")
    if action not in ACTIONS:
        raise HTTPException(422, f"action must be one of {', '.join(ACTIONS)}")


@router.get("")
def resources(request: Request):
    ctl = request.app.state.control
    return {"mode": ctl.mode, "resources": ctl.resources()}


@router.post("/all/{action}")
def act_all(request: Request, action: str, x_requested_with: str | None = Header(default=None)):
    _check(action, x_requested_with)
    ctl, actor = request.app.state.control, _actor(request)
    results = []
    for key in (PAUSE_ORDER if action == "pause" else RESUME_ORDER):
        try:
            res = ctl.act(key, action)
        except Exception as e:                     # keep going: one failure should not block the others
            res = {"key": key, "ok": False, "message": str(e)[:200]}
        log.warning("control: %s %s %s by %s -> %s", action, key, "ok" if res["ok"] else "failed", actor, res["message"])
        results.append(res)
    return {"actor": actor, "results": results}


@router.post("/{key}/{action}")
def act(request: Request, key: str, action: str, x_requested_with: str | None = Header(default=None)):
    _check(action, x_requested_with)
    if key not in BY_KEY:
        raise HTTPException(404, "unknown resource")
    actor = _actor(request)
    try:
        res = request.app.state.control.act(key, action)
    except Exception as e:
        log.warning("control: %s %s failed by %s: %s", action, key, actor, e)
        raise HTTPException(502, f"{action} failed: {str(e)[:200]}")
    log.warning("control: %s %s by %s -> %s", action, key, actor, res["message"])
    return {"actor": actor, "results": [res]}
