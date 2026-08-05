import sys, os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



from contextlib import asynccontextmanager
import threading

from fastapi import FastAPI, Query

from pydantic import BaseModel

from typing import Optional

import uvicorn, json

from fastapi.middleware.cors import CORSMiddleware



from agent.react_agent import run, run_stream, USER_MESSAGE_MAX_CHARS

from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi import HTTPException

from core.storage import clear_data, export_data, get_data_stats, get_user_name, set_user_name

from core.memory_store import (

    set_introduction,

    save_identity_field,
    get_profile,

)

from core.intro_builder import start_intro_rebuild_daemon

from core.privacy_settings import list_privacy_targets, set_privacy_enabled

from agent.conversation import (
    list_conversations,
    get_conversation_messages,
    search_conversations,
    delete_conversation,
)

from agent.router import load_classifier
from core.model_residency import warm_for_startup, on_capture_stop
from core.capture_state import get_capture_status
from core.paths import get_data_dir
from core.platform_support import platform_label
from core.llm_config import get_llm_config, public_llm_config, save_llm_config
from core.llm_gateway import gateway
from core.rag import start_event_indexer
from core.app_settings import get_capture_settings, set_capture_settings
from core.diagnostics import get_diagnostics
from core.paths import get_screenshots_dir
from core.screenshot_search import search_screenshots



@asynccontextmanager
async def lifespan(app: FastAPI):

    start_intro_rebuild_daemon()

    threading.Thread(target=load_classifier, daemon=True, name="router-classifier-warmup").start()


    start_event_indexer()


    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(

    CORSMiddleware,

    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "null",
    ],

    allow_methods=["*"],

    allow_headers=["*"],

)

class QueryRequest(BaseModel):

    message: str

    conversation_id: str



class NameRequest(BaseModel):

    name: str



class ProfileUpdateRequest(BaseModel):

    name: Optional[str] = None

    introduction: Optional[str] = None

    identity: Optional[dict[str, str]] = None



class PrivacyUpdateRequest(BaseModel):

    enabled: dict[str, bool]


class ProviderUpdateRequest(BaseModel):

    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    cli_command: Optional[str] = None
    chat_model: Optional[str] = None
    vision_model: Optional[str] = None
    embedding_model: Optional[str] = None


class CaptureSettingsRequest(BaseModel):
    capture_screenshots: Optional[bool] = None
    capture_all_monitors: Optional[bool] = None
    capture_clipboard: Optional[bool] = None
    ocr_enabled: Optional[bool] = None
    image_embeddings_enabled: Optional[bool] = None
    min_gap_seconds: Optional[float] = None
    background_interval_seconds: Optional[float] = None
    activity_debounce_seconds: Optional[float] = None
    raw_retention_days: Optional[int] = None
    screenshot_retention_days: Optional[int] = None
    launch_at_login: Optional[bool] = None


class DataClearRequest(BaseModel):
    scopes: list[str]



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



            save_identity_field(field, value=value, source="user", op="override")

    return get_profile()



@app.get("/settings/privacy")

def read_privacy_settings():

    return {"targets": list_privacy_targets()}



@app.put("/settings/privacy")

def write_privacy_settings(req: PrivacyUpdateRequest):

    set_privacy_enabled(req.enabled)

    return {"targets": list_privacy_targets()}


@app.get("/settings/provider")
def read_provider_settings():
    return public_llm_config(get_llm_config())


@app.put("/settings/provider")
def write_provider_settings(req: ProviderUpdateRequest):
    try:
        config = save_llm_config(req.dict(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return public_llm_config(config)


@app.post("/settings/provider/test")
def test_provider_settings():
    return gateway.test_connection()


@app.get("/settings/provider/capabilities")
def provider_capabilities():
    return gateway.capabilities()


@app.get("/settings/capture")
def read_capture_settings():
    return get_capture_settings()


@app.put("/settings/capture")
def write_capture_settings(req: CaptureSettingsRequest):
    return set_capture_settings(req.dict(exclude_unset=True))


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
    since: Optional[float] = None,
    until: Optional[float] = None,
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
def remove_conversation(conversation_id: str):
    deleted = delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"conversation_id": conversation_id, "deleted": deleted}



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
        "llm": public_llm_config(),
    }



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
    host = os.environ.get("CLIPPY_API_HOST", "127.0.0.1")
    port = int(os.environ.get("CLIPPY_API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
