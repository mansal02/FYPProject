import json
import os
import re
import threading
import uuid

import ollama
import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import StreamingResponse

from aiassistant.core.llm_core import run_crew_assist, run_multi_agent_round
from aiassistant.backend.streaming_utils import drain_complete_sentences
from aiassistant.infra.config.app_config import CONFIG
from aiassistant.infra.db.database import MarieDB
from aiassistant.infra.rag_memory import get_rag_context

# Mid-tier optimization: Apply quantization settings
try:
    from aiassistant.infra.optimization import QuantizationHelper
    QuantizationHelper.apply_quantization_env()
except Exception:
    pass

try:
    import requests
except Exception:  # pragma: no cover - optional dependency
    requests = None

app = FastAPI()
db = MarieDB()

os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
os.environ.setdefault("OLLAMA_KV_CACHE_TYPE", "q4_0")
os.environ.setdefault("OLLAMA_KEEP_ALIVE", "0")

OLLAMA_MODEL = CONFIG["ollama"]["model"]
OLLAMA_NUM_PREDICT = int(CONFIG["ollama"]["num_predict"])
OLLAMA_NUM_CTX = int(CONFIG["ollama"]["num_ctx"])
OLLAMA_TEMPERATURE = float(CONFIG["ollama"].get("temperature", 0.2))
OLLAMA_SYSTEM_PROMPT = CONFIG["ollama"].get("system_prompt", "You are MARIE, a friendly assistant.")
RUNTIME_ONLINE_MODE = str(CONFIG.get("runtime", {}).get("online_mode", "auto")).strip().lower()
RUNTIME_HYBRID_MODE = bool(CONFIG.get("runtime", {}).get("hybrid_mode", False))
EXTERNAL_MODEL = str(CONFIG.get("runtime", {}).get("external_model", "gemini-2.0-flash")).strip()


def _toggle_voice_rvc(enabled: bool) -> None:
    if requests is None:
        return

    host = str(CONFIG.get("servers", {}).get("voice_host", "127.0.0.1")).strip()
    port = int(CONFIG.get("servers", {}).get("voice_port", 8001))
    action = "load" if enabled else "unload"
    url = f"http://{host}:{port}/rvc/{action}"

    try:
        requests.post(url, json={}, timeout=2)
    except Exception:
        pass

_cancel_map = {}
_cancel_lock = threading.RLock()

_SENTIMENT_TAGS = {
    "happy",
    "sad",
    "angry",
    "excited",
    "surprised",
    "confused",
    "neutral",
    "concerned",
    "friendly",
    "serious",
    "annoyed",
    "bored",
}
_SENTIMENT_KEYWORDS = {
    "happy": {"happy", "great", "awesome", "love", "fantastic", "glad", "delight"},
    "sad": {"sad", "sorry", "down", "upset", "unhappy", "depressed"},
    "angry": {"angry", "mad", "furious", "annoyed", "rage"},
    "excited": {"excited", "thrilled", "can't wait", "hyped"},
    "surprised": {"surprised", "wow", "whoa", "unexpected"},
    "confused": {"confused", "unsure", "not sure", "unclear"},
    "concerned": {"concerned", "worried", "issue", "problem"},
}


def _truncate_memory_context(memory_context, max_lines=60, max_chars=7000):
    if not memory_context:
        return ""

    lines = [line for line in memory_context.splitlines() if line.strip()]
    recent = lines[-max_lines:]
    joined = "\n".join(recent)
    if len(joined) <= max_chars:
        return joined
    return joined[-max_chars:]


def _normalize_online_mode(mode: str) -> str:
    clean = str(mode or "").strip().lower()
    if clean not in {"auto", "online", "offline"}:
        return "auto"
    return clean


def _resolve_online_mode(payload: dict) -> str:
    if isinstance(payload, dict):
        if "online_mode" in payload:
            return _normalize_online_mode(payload.get("online_mode"))
        if "use_online" in payload:
            return "online" if bool(payload.get("use_online")) else "offline"
    return _normalize_online_mode(RUNTIME_ONLINE_MODE)


def _should_use_crew(prompt: str, settings: dict) -> bool:
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        return False

    router = str(settings.get("router", "complex_only")).strip().lower()
    if router == "always":
        return True
    if router == "never":
        return False
    words = re.findall(r"[a-zA-Z0-9_]+", str(prompt or ""))
    if len(words) < 6:
        return False
    return _is_complex_reasoning_request(prompt)


def _is_complex_reasoning_request(text: str) -> bool:
    lowered = (text or "").lower()
    words = re.findall(r"[a-zA-Z0-9_]+", lowered)
    if len(words) >= 36:
        return True

    triggers = {
        "analyze",
        "compare",
        "architecture",
        "strategy",
        "tradeoff",
        "design",
        "optimize",
        "debug plan",
        "step-by-step",
        "root cause",
    }
    hits = sum(1 for token in triggers if token in lowered)
    return hits >= 2


def _chunk_text(text: str, size: int = 24):
    if not text:
        return []
    return [text[idx : idx + size] for idx in range(0, len(text), size)]


def _extract_sentiment_tag(text: str) -> str:
    if not text:
        return "neutral"

    match = re.search(r"\[([a-zA-Z]+)\]", text)
    if match:
        tag = match.group(1).lower()
        if tag in _SENTIMENT_TAGS:
            return tag

    lowered = text.lower()
    scores = {}
    for sentiment, keywords in _SENTIMENT_KEYWORDS.items():
        hits = sum(1 for token in keywords if token in lowered)
        if hits:
            scores[sentiment] = hits

    if not scores:
        return "neutral"

    return max(scores.items(), key=lambda item: item[1])[0]


def _gemini_generate_text(prompt: str, temperature: float) -> str:
    if requests is None:
        return ""

    api_key = (
        os.environ.get("GOOGLE_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("MARIE_GEMINI_API_KEY", "").strip()
    )
    if not api_key:
        return ""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{EXTERNAL_MODEL}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": 640,
        },
    }

    try:
        response = requests.post(
            url,
            params={"key": api_key},
            json=payload,
            timeout=28,
        )
        if response.status_code >= 400:
            return ""
        data = response.json()
        for candidate in data.get("candidates", []):
            parts = (candidate.get("content") or {}).get("parts", [])
            text_chunks = [str(part.get("text", "")) for part in parts if part.get("text")]
            merged = " ".join(text_chunks).strip()
            if merged:
                return merged
        return ""
    except Exception:
        return ""


def get_marie_response_stream(prompt, memory_context="", online_mode="auto"):
    """Streams responses from Ollama with memory context injected."""
    try:
        if not prompt:
            yield ""
            return

        system_instructions = OLLAMA_SYSTEM_PROMPT
        try:
            style_profile = db.get_style_profile("train_root")
        except Exception:
            style_profile = None
        if isinstance(style_profile, dict):
            formal = str(style_profile.get("formal", "")).strip()
            casual = str(style_profile.get("casual", "")).strip()
            if formal or casual:
                system_instructions += (
                    "\nWriting style profile for documentation:"
                    f"\nFormal baseline: {formal}"
                    f"\nCasual baseline: {casual}"
                    "\nDefault to formal unless the user requests casual."
                )
        system_instructions += (
            "\nWhen handling office files (.doc, .docx, .xlsx, .xls, .csv, .pdf), "
            "do not echo full file contents. Respond with a concise status and file path."
        )
        system_instructions += (
            "\nIf desktop control is required, output exactly one JSON object using this schema: "
            "{\"action\":\"open|close|search_web|open_website|volume|write_note\","
            "\"target\":\"string\",\"value\":\"optional\"}. "
            "Do not produce shell commands or unsafe code."
        )
        if memory_context:
            recent_facts = _truncate_memory_context(memory_context)
            system_instructions += f"\nFacts about the user you remember:\n{recent_facts}"

        resolved_mode = _normalize_online_mode(online_mode)
        use_online = False
        if resolved_mode == "online":
            use_online = True
        elif resolved_mode == "auto" and RUNTIME_HYBRID_MODE and _is_complex_reasoning_request(prompt):
            use_online = True

        if use_online:
            merged_prompt = f"System:\n{system_instructions}\n\nUser:\n{prompt}".strip()
            external_text = _gemini_generate_text(merged_prompt, temperature=OLLAMA_TEMPERATURE)
            if external_text:
                for chunk in _chunk_text(external_text):
                    yield chunk
                return

        stream = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_instructions},
                {"role": "user", "content": prompt},
            ],
            options={
                "temperature": OLLAMA_TEMPERATURE,
                "num_predict": OLLAMA_NUM_PREDICT,
                "num_ctx": OLLAMA_NUM_CTX,
            },
            stream=True,
            keep_alive=0,
        )

        for chunk in stream:
            yield chunk["message"]["content"]

    except Exception as exc:
        print(f"Ollama Error: {exc}")
        yield "I am having trouble connecting to my brain."


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

    return user_text, rag_context


@app.post("/chat")
def chat_endpoint(payload: dict = Body(...)):
    user_text, rag_context = _resolve_request_context(payload)
    online_mode = _resolve_online_mode(payload)

    crew_config = CONFIG.get("crew", {})
    if _should_use_crew(user_text, crew_config):
        crew_result = run_crew_assist(user_text, memory_context=rag_context, config=crew_config)
        summary = str(crew_result.get("summary") or "").strip()
        if crew_result.get("ok") and summary:
            return {"response": summary, "sentiment": _extract_sentiment_tag(summary)}

    full_response = ""
    for token in get_marie_response_stream(user_text, memory_context=rag_context, online_mode=online_mode):
        full_response += token

    return {"response": full_response, "sentiment": _extract_sentiment_tag(full_response)}


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
    online_mode = _resolve_online_mode(payload)
    crew_config = CONFIG.get("crew", {})

    if _should_use_crew(user_text, crew_config):
        crew_result = run_crew_assist(user_text, memory_context=rag_context, config=crew_config)
        summary = str(crew_result.get("summary") or "").strip()
        request_id = payload.get("request_id") or str(uuid.uuid4())

        def generate_crew_events():
            if not summary:
                yield json.dumps(
                    {
                        "type": "error",
                        "content": str(crew_result.get("error") or "Crew processing failed."),
                        "request_id": request_id,
                    }
                ) + "\n"
                return

            for chunk in _chunk_text(summary):
                yield json.dumps({"type": "token", "content": chunk}) + "\n"

            yield json.dumps(
                {
                    "type": "done",
                    "full_response": summary,
                    "sentiment": _extract_sentiment_tag(summary),
                    "request_id": request_id,
                    "cancelled": False,
                }
            ) + "\n"

        return StreamingResponse(
            generate_crew_events(),
            media_type="application/x-ndjson",
            headers={"X-MARIE-REQUEST-ID": request_id},
        )

    request_id = payload.get("request_id") or str(uuid.uuid4())
    cancel_event = _register_cancel_event(request_id)

    def generate_events():
        full_response = ""
        sentence_buffer = ""
        try:
            for token in get_marie_response_stream(user_text, memory_context=rag_context, online_mode=online_mode):
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
                    "sentiment": _extract_sentiment_tag(full_response),
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