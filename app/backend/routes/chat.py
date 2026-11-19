"""Chat tab endpoints: ask a question (one JSON reply, or streamed as server-sent events), and list, reopen
or delete saved conversations.

Asking is async so the model's HTTP client lives on the server's event loop; warehouse calls and the
chat store run in worker threads."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import AgentRunError, ModelHTTPError, UnexpectedModelBehavior
from starlette.concurrency import run_in_threadpool

from .. import config
from ..services import analyst, chatdata, chatstore

router = APIRouter(prefix="/api/chat")
MAX_TURNS = 40  # messages of history sent to the model


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(None, pattern="^[0-9a-f]{32}$")


def _user(request: Request) -> str:
    # Databricks Apps put the signed-in user's email in this header; locally there is none.
    return request.headers.get("x-forwarded-email") or "local"


def _model(request: Request):
    state = request.app.state
    if getattr(state, "chat_model", None) is None:
        try:
            state.chat_model = analyst.databricks_model()
        except Exception as e:
            raise HTTPException(503, f"Chat model not reachable: {e}")
    return state.chat_model


def _failure(e: Exception) -> HTTPException:
    if isinstance(e, (ModelHTTPError, UnexpectedModelBehavior)):
        return HTTPException(503, f"Chat model failed: {e}")
    if isinstance(e, AgentRunError):
        return HTTPException(502, f"The assistant could not finish: {e}")
    return HTTPException(502, f"Could not read the data: {e}")  # warehouse failures while querying


@router.get("/info")
def info(request: Request):
    inner = getattr(request.app.state.source, "current", lambda: request.app.state.source)()
    chatdata.prewarm(inner)  # the tab was just opened; a question is likely next
    return {"endpoint": config.CHAT_ENDPOINT, "mode": inner.mode}


@router.get("/conversations")
def conversations(request: Request):
    return {"conversations": request.app.state.chat_store.list(_user(request))}


@router.get("/conversations/{cid}")
def conversation(request: Request, cid: str):
    msgs = request.app.state.chat_store.get(_user(request), cid)
    if not msgs:
        raise HTTPException(404, "conversation not found")
    return {"id": cid, "messages": msgs}


@router.delete("/conversations/{cid}", status_code=204)
def delete(request: Request, cid: str):
    request.app.state.chat_store.delete(_user(request), cid)


async def _start(request: Request, body: ChatRequest):
    """Checks the request and reads the conversation so far. Returns (save, cid, events), where events is the
    turn's event stream and save(done_event) stores the question and answer."""
    store, user = request.app.state.chat_store, _user(request)
    cid = body.conversation_id or chatstore.new_id()
    prior = (await run_in_threadpool(store.get, user, cid))[-MAX_TURNS:] if body.conversation_id else []
    if body.conversation_id and not prior:
        raise HTTPException(404, "conversation not found")
    events = analyst.run_turn(request.app.state.source, _model(request), prior, body.message)

    async def save(done: dict) -> None:
        await run_in_threadpool(store.save_turn, user, cid, body.message, done["reply"], done["artifacts"])
    return save, cid, events


@router.post("")
async def chat(request: Request, body: ChatRequest):
    save, cid, events = await _start(request, body)
    try:
        done = [ev async for ev in events][-1]
    except Exception as e:
        raise _failure(e)
    await save(done)
    return {"conversation_id": cid, "reply": done["reply"], "artifacts": done["artifacts"], "mode": done["mode"]}


@router.post("/stream")
async def chat_stream(request: Request, body: ChatRequest):
    """The same turn as POST /api/chat as server-sent events: status and artifact events while it runs, then
    done (reply, mode, conversation_id) or error (detail). Tables and charts show as soon as they exist."""
    save, cid, events = await _start(request, body)

    async def sse():
        try:
            async for ev in events:
                if ev["type"] == "done":
                    await save(ev)
                    ev = {"type": "done", "conversation_id": cid, "reply": ev["reply"], "mode": ev["mode"]}
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as e:  # noqa: BLE001 - reported to the client as an event; the status is already sent
            yield f"data: {json.dumps({'type': 'error', 'detail': _failure(e).detail})}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
