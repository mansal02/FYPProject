import json
import threading
import uuid

import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import StreamingResponse

from app_config import CONFIG
from database import MarieDB
from index import get_marie_response_stream
from multi_agent_orchestrator import run_multi_agent_round
from rag_memory import get_rag_context
from screen_vision import describe_screen_snapshot
from streaming_utils import drain_complete_sentences

app = FastAPI()
db = MarieDB()

_cancel_map = {}
_cancel_lock = threading.RLock()


def _register_cancel_event(request_id):
    with _cancel_lock:
        cancel_event = threading.Event()
        _cancel_map[request_id] = cancel_event
        return cancel_event


def _pop_cancel_event(request_id):
    with _cancel_lock:
        _cancel_map.pop(request_id, None)


def _cancel_request(request_id=None):
    stopped = 0
    with _cancel_lock:
        if request_id:
            event = _cancel_map.get(request_id)
            if event:
                event.set()
                stopped = 1
            return stopped

        for event in _cancel_map.values():
            event.set()
            stopped += 1
    return stopped


def _resolve_request_context(payload):
    user_text = payload.get("text", "")
    rag_context = payload.get("memory_context")
    if not rag_context:
        rag_context = db.get_all_rad_data()

    doc_context = payload.get("doc_context") or get_rag_context(user_text, top_k=4)
    if doc_context:
        rag_context = f"{rag_context}\n\nRetrieved local knowledge:\n{doc_context}".strip()

    action_result = payload.get("action_result", "")
    if action_result:
        rag_context = f"{rag_context}\n\nLatest action/tool output:\n{action_result}".strip()

    screen_context = payload.get("screen_context", "")
    if not screen_context:
        screen_context = describe_screen_snapshot(
            image_path=payload.get("screen_image_path", ""),
            user_text=user_text,
            window_title=payload.get("screen_window_title", ""),
        )
    if screen_context:
        rag_context = f"{rag_context}\n\nLive screen context:\n{screen_context}".strip()

    return user_text, rag_context


@app.post("/chat")
def chat_endpoint(payload: dict = Body(...)):
    user_text, rag_context = _resolve_request_context(payload)

    full_response = ""
    for token in get_marie_response_stream(user_text, memory_context=rag_context):
        full_response += token

    return {"response": full_response}


@app.post("/chat/multi-agent")
def chat_multi_agent_endpoint(payload: dict = Body(...)):
    user_text, rag_context = _resolve_request_context(payload)
    if not user_text:
        return {"researcher": "", "coder": "", "final": ""}

    try:
        return run_multi_agent_round(user_text, memory_context=rag_context)
    except Exception as e:
        return {
            "researcher": "",
            "coder": "",
            "final": f"Multi-agent mode failed: {e}",
        }


@app.post("/chat/stream")
def chat_stream_endpoint(payload: dict = Body(...)):
    user_text, rag_context = _resolve_request_context(payload)
    request_id = payload.get("request_id") or str(uuid.uuid4())
    cancel_event = _register_cancel_event(request_id)

    def generate_events():
        full_response = ""
        sentence_buffer = ""
        try:
            for token in get_marie_response_stream(user_text, memory_context=rag_context):
                if cancel_event.is_set():
                    break

                if not token:
                    continue

                full_response += token
                sentence_buffer += token
                yield json.dumps({"type": "token", "content": token}) + "\n"

                complete_sentences, sentence_buffer = drain_complete_sentences(sentence_buffer)
                for sentence in complete_sentences:
                    yield json.dumps({"type": "sentence", "content": sentence}) + "\n"

            if not cancel_event.is_set() and sentence_buffer.strip():
                yield json.dumps({"type": "sentence", "content": sentence_buffer.strip()}) + "\n"

            yield json.dumps(
                {
                    "type": "done",
                    "full_response": full_response,
                    "request_id": request_id,
                    "cancelled": bool(cancel_event.is_set()),
                }
            ) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "content": str(e), "request_id": request_id}) + "\n"
        finally:
            _pop_cancel_event(request_id)

    return StreamingResponse(
        generate_events(),
        media_type="application/x-ndjson",
        headers={"X-MARIE-REQUEST-ID": request_id},
    )


@app.post("/chat/stop")
def stop_chat_endpoint(payload: dict = Body(None)):
    payload = payload or {}
    request_id = payload.get("request_id")
    stopped = _cancel_request(request_id=request_id)
    return {"status": "stopping", "request_id": request_id, "stopped": stopped}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=CONFIG["servers"]["reasoning_host"],
        port=int(CONFIG["servers"]["reasoning_port"]),
    )