import json
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from agent.conversation import (
    delete_conversation,
    get_conversation_messages,
    list_conversations,
    search_conversations,
)
from agent.react_agent import USER_MESSAGE_MAX_CHARS, run, run_stream
from agent.router import load_classifier
from core.app_settings import get_capture_settings, set_capture_settings
from core.backlog import get_backlog_status
from core.capture_state import get_capture_status
from core.diagnostics import get_diagnostics
from core.intro_builder import start_intro_rebuild_daemon
from core.memory_store import get_profile, save_identity_field, set_introduction
from core.model_residency import can_load_light, on_capture_stop, warm_for_startup
from core.paths import get_data_dir, get_screenshots_dir
from core.platform_support import platform_label
from core.privacy_settings import list_privacy_targets, set_privacy_enabled
from core.rag import start_event_indexer, stop_event_indexer
from core.screenshot_search import search_screenshots
from core.storage import (
    clear_data,
    export_data,
    get_data_stats,
    get_user_name,
    list_session_events,
    list_timeline_sessions,
    set_user_name,
)


@asynccontextmanager
async def lifespan(app: FastAPI):

    # Weekly intro rebuild: immediate check + periodic background loop
    start_intro_rebuild_daemon()

    # Preload router classifier only when a small torch model still fits.
    if can_load_light():
        threading.Thread(target=load_classifier, daemon=True, name="router-classifier-warmup").start()

    # Summarizer / OCR backlog / distil run with the app, not only while capture
    # is on — pause capture stops new intake, not processing of allowed history.
    from core.background_jobs import start_background_jobs

    start_background_jobs()

    # PARKED: event RAG indexer — only if rag_enabled (default off); ask contributor
    # keep/remove. See core/rag.py and app_settings.
    if get_capture_settings()["rag_enabled"]:
        start_event_indexer()


    try:
        # Model warm is triggered explicitly by Electron (setup / normal launch)
        # via POST /residency/startup — avoids racing a background warm with setup UI.
        yield
    finally:
        stop_event_indexer(wait=True)


app = FastAPI(lifespan=lifespan)

app.add_middleware(

    CORSMiddleware,

    allow_origins=["null"],

    allow_methods=["*"],

    allow_headers=["*"],

)

class QueryRequest(BaseModel):

    message: str

    conversation_id: str



class NameRequest(BaseModel):

    name: str



class ProfileUpdateRequest(BaseModel):

    name: str | None = None

    introduction: str | None = None

    identity: dict[str, str] | None = None



class PrivacyUpdateRequest(BaseModel):

    enabled: dict[str, bool]


class CaptureSettingsRequest(BaseModel):
    capture_screenshots: bool | None = None
    capture_all_monitors: bool | None = None
    capture_clipboard: bool | None = None
    ocr_enabled: bool | None = None
    image_embeddings_enabled: bool | None = None
    rag_enabled: bool | None = None
    min_gap_seconds: float | None = None
    background_interval_seconds: float | None = None
    activity_debounce_seconds: float | None = None
    raw_retention_days: int | None = None
    screenshot_retention_days: int | None = None
    launch_at_login: bool | None = None


class DataClearRequest(BaseModel):
    scopes: list[str]


class XyzConfigRequest(BaseModel):
    enabled: bool | None = None
    rules: list[dict] | None = None



def _validate_user_message(message: str) -> str:
    text = (message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    if len(text) > USER_MESSAGE_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Message too long ({len(text)} chars). Limit is {USER_MESSAGE_MAX_CHARS}.",
        )
    return text


@app.post("/chat")

def chat(req: QueryRequest):

    message = _validate_user_message(req.message)

    result = run(message, req.conversation_id)

    return {"result": result}



@app.post("/chat/stream")

def chat_stream(req: QueryRequest):

    message = _validate_user_message(req.message)

    def event_gen():

        try:

            for event in run_stream(message, req.conversation_id):

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        except Exception as e:

            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(

        event_gen(),

        media_type="text/event-stream",

        headers={

            "Cache-Control": "no-cache",

            "Connection": "keep-alive",

            "X-Accel-Buffering": "no",

        },

    )



@app.get("/user/name")

def read_user_name():

    return {"name": get_user_name()}



@app.post("/user/name")

def write_user_name(req: NameRequest):
    try:
        name = set_user_name(req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"name": name}



@app.get("/user/profile")

def read_user_profile():
    return get_profile()



@app.post("/user/profile")

def write_user_profile(req: ProfileUpdateRequest):

    if req.name is not None:
        try:
            set_user_name(req.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if req.introduction is not None:

        set_introduction(req.introduction.strip(), source="user")

    if req.identity:

        for field, value in req.identity.items():

            field = (field or "").strip().lower().replace(" ", "_")

            if not field:

                continue

            if field in {"name", "introduction"}:
                continue

            value = (value or "").strip()



            # User edits from Settings always win over agent/distiller values.
            save_identity_field(field, value=value, source="user", op="override")

    return get_profile()



@app.get("/settings/privacy")

def read_privacy_settings():

    return {"targets": list_privacy_targets()}



@app.put("/settings/privacy")

def write_privacy_settings(req: PrivacyUpdateRequest):

    set_privacy_enabled(req.enabled)

    return {"targets": list_privacy_targets()}


@app.get("/settings/capture")
def read_capture_settings():
    return get_capture_settings()


@app.put("/settings/capture")
def write_capture_settings(req: CaptureSettingsRequest):
    settings = set_capture_settings(req.dict(exclude_unset=True))
    # PARKED: start/stop event RAG indexer with the toggle (default off).
    if settings["rag_enabled"]:
        start_event_indexer()
    else:
        stop_event_indexer()
    return settings


@app.get("/settings/data")
def read_data_stats():
    return get_data_stats()


@app.get("/settings/data/export")
def export_user_data():
    return JSONResponse(content=export_data())


@app.post("/settings/data/clear")
def clear_user_data(req: DataClearRequest):
    allowed = {"events", "screenshots", "conversations", "memory", "all"}
    scopes = [
        normalized
        for scope in req.scopes
        if (normalized := str(scope).strip().lower()) in allowed
    ]
    if not scopes:
        raise HTTPException(status_code=400, detail="Choose at least one valid data scope.")
    return {"cleared": clear_data(scopes), "remaining": get_data_stats()}


@app.get("/settings/diagnostics")
def diagnostics():
    return get_diagnostics()


@app.get("/screenshots")
def screenshot_search(
    q: str = "",
    since: float | None = None,
    until: float | None = None,
    limit: int = Query(40, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return search_screenshots(q, start_ts=since, end_ts=until, limit=limit, offset=offset)


@app.get("/screenshots/{filename}")
def screenshot_file(filename: str):
    root = get_screenshots_dir().resolve()
    candidate = (root / filename).resolve()
    if Path(filename).name != filename or candidate.parent != root or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found.")
    return FileResponse(candidate, media_type="image/jpeg")



@app.get("/timeline/sessions")
@app.get("/sessions")
def timeline_sessions(
    since: float | None = None,
    until: float | None = None,
    limit: int = Query(40, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return list_timeline_sessions(
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


@app.get("/sessions/{summary_id}")
def session_detail(summary_id: str):
    data = list_session_events(summary_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return data


@app.get("/conversations")

def conversations():

    return {"conversations": list_conversations()}



@app.get("/conversations/search")

def conversations_search(q: str = "", limit: int = Query(20, ge=1, le=100)):

    return {"conversations": search_conversations(q, limit=limit), "query": q}



@app.get("/conversations/{conversation_id}")

def conversation_messages(conversation_id: str):

    return {

        "conversation_id": conversation_id,

        "messages": get_conversation_messages(conversation_id),

    }


@app.delete("/conversations/{conversation_id}")

def conversation_delete(conversation_id: str):

    result = delete_conversation(conversation_id)

    if not result["deleted"]:

        raise HTTPException(status_code=404, detail="Conversation not found.")

    return result



@app.get("/health")

def health():

    return {"status": "ok"}


@app.get("/status")
def status():
    from core.model_residency import load_residency

    return {
        "status": "ok",
        "platform": platform_label(),
        "data_dir": str(get_data_dir()),
        "capture": get_capture_status(),
        "residency": load_residency(),
        "backlog": get_backlog_status(),
    }


def _xyz_payload():
    from dataclasses import asdict
    from skills.when_x_then_y import load_config, load_rules

    return {
        "enabled": load_config()["enabled"],
        "rules": [asdict(rule) for rule in load_rules()],
    }


@app.get("/skills/xyz")
def xyz_get():
    return _xyz_payload()


@app.put("/skills/xyz")
def xyz_put(req: XyzConfigRequest):
    from dataclasses import asdict, fields as dc_fields
    from skills.when_x_then_y import Rule, save_config, save_rules

    if req.enabled is not None:
        save_config({"enabled": req.enabled})
    if req.rules is not None:
        allowed = {f.name for f in dc_fields(Rule)}
        rules = []
        for row in req.rules:
            if not isinstance(row, dict):
                continue
            try:
                rules.append(Rule(**{k: v for k, v in row.items() if k in allowed}))
            except TypeError:
                continue
        save_rules(rules)
    return _xyz_payload()


@app.post("/residency/startup")

def residency_startup():

    """Pin text for app launch; bundled embeddings load on demand."""

    return warm_for_startup()



@app.get("/residency")

def residency_status():

    from core.model_residency import load_residency

    return load_residency()



@app.post("/residency/capture-stop")

def residency_capture_stop():

    """Record that the model-free capture process stopped."""

    return on_capture_stop()



if __name__ == "__main__":
    # Loopback only. This API serves captured screen content and conversation
    # history, so it must never be reachable from the local network.
    # Electron reserves a free port and passes it in; 8000 is only the fallback
    # for running this module directly.
    port = int(os.environ.get("CLIPPY_API_PORT") or 8000)

    uvicorn.run(app, host="127.0.0.1", port=port)
