import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


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
from core.memory_store import get_profile, save_identity_field, set_introduction, get_introduction, get_identity
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
    set_user_name,
)


@asynccontextmanager
async def lifespan(app: FastAPI):

    # Weekly intro rebuild: immediate check + periodic background loop
    start_intro_rebuild_daemon()
    # Preload router classifier so the first chat does not pay the load cost
    threading.Thread(
        target=load_classifier, daemon=True, name="router-classifier-warmup"
    ).start()
    # Model warm is triggered explicitly by Electron (setup / normal launch)
    # via POST /residency/startup — avoids racing a background warm with setup UI.
    yield


app = FastAPI(lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

    return {
        "name": get_user_name(),
        "introduction": get_introduction(),
        "identity": get_identity(),
    }


@app.post("/user/profile")
def write_user_profile(req: ProfileUpdateRequest):

    if req.name is not None:
        set_user_name(req.name)

    if req.introduction is not None:
        set_introduction(req.introduction.strip(), source="user")

    if req.identity:
        for field, value in req.identity.items():
            field = (field or "").strip().lower().replace(" ", "_")

            if not field:
                continue

            value = (value or "").strip()



            # User edits from Settings always win over agent/distiller values.
            save_identity_field(field, value=value, source="user", op="override")

    return {
        "name": get_user_name(),
        "introduction": get_introduction(),
        "identity": get_identity(),
    }


@app.get("/settings/privacy")
def read_privacy_settings():

    return {"targets": list_privacy_targets()}


@app.put("/settings/privacy")
def write_privacy_settings(req: PrivacyUpdateRequest):

    set_privacy_enabled(req.enabled)

    return {"targets": list_privacy_targets()}


@app.get("/conversations")
def conversations():

    return {"conversations": list_conversations()}


@app.get("/conversations/search")
def conversations_search(q: str = "", limit: int = 20):

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


@app.post("/residency/startup")
def residency_startup():
    """Pin text + embed for app launch; vision stays idle until capture starts."""

    return warm_for_startup()


@app.get("/residency")
def residency_status():

    from core.model_residency import load_residency

    return load_residency()


@app.post("/residency/capture-stop")
def residency_capture_stop():
    """Unload vision when Electron stops screen capture (process may be force-killed)."""

    return on_capture_stop()


if __name__ == "__main__":
    # Loopback only. This API serves captured screen content and conversation
    # history, so it must never be reachable from the local network.
    # Electron reserves a free port and passes it in; 8000 is only the fallback
    # for running this module directly.
    port = int(os.environ.get("CLIPPY_API_PORT") or 8000)

    uvicorn.run(app, host="127.0.0.1", port=port)
