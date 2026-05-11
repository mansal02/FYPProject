import gc
import os
import re
import threading
import wave
import uvicorn
from fastapi import FastAPI, Body
from aiassistant.infra.config.app_config import CONFIG
from aiassistant.infra.voice.voice import MarieVoice
from aiassistant.infra.voice.voice_db import get_character_data, RVC_DIR


try:
    from rvc_python.infer import RVCInference
    RVC_AVAILABLE = True
except ImportError:
    print("[WARNING] 'rvc-python' not found. RVC features disabled.")
    RVC_AVAILABLE = False

app = FastAPI()
voice_engine = MarieVoice()
rvc_engine = None


def _resolve_rvc_device() -> str:
    # NOTE: CPU conversion takes 5-10 seconds; GPU takes ~0.5s.
    return "cuda:0" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu"


def _ensure_rvc_engine() -> None:
    global rvc_engine
    if not RVC_AVAILABLE:
        rvc_engine = None
        return
    if rvc_engine is None:
        rvc_engine = RVCInference(device=_resolve_rvc_device())


def _release_rvc_engine() -> None:
    global rvc_engine
    rvc_engine = None
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            if hasattr(torch.cuda, "ipc_collect"):
                torch.cuda.ipc_collect()
    except Exception:
        pass


if RVC_AVAILABLE:
    _ensure_rvc_engine()


def _get_wav_duration_ms(path):
    if not path or not os.path.exists(path):
        return 0
    try:
        with wave.open(path, "rb") as wav_file:
            frame_count = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
            if sample_rate <= 0:
                return 0
            return int((frame_count / float(sample_rate)) * 1000)
    except Exception:
        return 0


def _extract_emotion_tag(text):
    match = re.search(r"\[([a-zA-Z]+)\]", str(text or ""))
    if not match:
        return ""
    return match.group(1).strip().lower()

@app.post("/speak")
def speak_endpoint(payload: dict = Body(...)):
    text = payload.get("text")
    char_id = payload.get("character", "tachyon").lower()
    async_play = payload.get("async_play", True)

    if not text:
        return {"status": "empty", "file": None, "duration_ms": 0, "async": bool(async_play)}
    

    char_data, _ = get_character_data(char_id)
    use_rvc = char_data.get("rvc_enable", False) and RVC_AVAILABLE
    emotion_tag = _extract_emotion_tag(text)
    emotion_cfg = (char_data.get("emotions") or {}).get(emotion_tag, {})
    emotion_pitch = emotion_cfg.get("pitch_shift", char_data.get("pitch_shift", 0))
    
    if voice_engine.current_name.lower() != char_data["name"].lower():
        voice_engine.set_voice(char_id)


    raw_audio_path = voice_engine.generate_only(text)

    final_path = raw_audio_path

    if use_rvc and raw_audio_path:
        _ensure_rvc_engine()
        model_name = char_data["rvc_model"]
        index_name = char_data.get("rvc_index", "")
        pitch = emotion_pitch

        model_path = os.path.join(RVC_DIR, model_name)
        index_path = os.path.join(RVC_DIR, index_name) if index_name else None
        
        output_rvc_path = raw_audio_path.replace(".wav", "_rvc.wav")
        
        print(f"[RVC] Converting using {model_name}...")
        
        print(f"[RVC] Converting on GTX 1080 (Legacy Mode)...")
        
        try:
            rvc_engine.load_model(model_path)
            rvc_engine.infer_file(
                input_path=raw_audio_path,
                output_path=output_rvc_path,
                index_path=index_path,
                f0_up_key=pitch, 
                # Legacy mode for GTX 1080
                f0_method="rmvpe", 
                version="v2",
                is_half=True
            )
            final_path = output_rvc_path
        except Exception as e:
            print(f"[RVC ERROR] {e}")
            final_path = raw_audio_path

    duration_ms = _get_wav_duration_ms(final_path)

    if async_play and final_path:
        threading.Thread(target=voice_engine.play_file, args=(final_path,), daemon=True).start()
    else:
        voice_engine.play_file(final_path)

    return {
        "status": "speaking",
        "file": final_path,
        "duration_ms": duration_ms,
        "async": bool(async_play),
    }


@app.post("/rvc/unload")
def rvc_unload_endpoint(payload: dict = Body(None)):
    _ = payload or {}
    if not RVC_AVAILABLE:
        return {"status": "disabled"}
    _release_rvc_engine()
    return {"status": "unloaded"}


@app.post("/rvc/load")
def rvc_load_endpoint(payload: dict = Body(None)):
    _ = payload or {}
    if not RVC_AVAILABLE:
        return {"status": "disabled"}
    _ensure_rvc_engine()
    return {"status": "loaded"}


@app.post("/stop")
def stop_endpoint(payload: dict = Body(None)):
    _ = payload or {}
    try:
        voice_engine.stop()
        return {"status": "stopped"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=CONFIG["servers"]["voice_host"],
        port=int(CONFIG["servers"]["voice_port"]),
    )