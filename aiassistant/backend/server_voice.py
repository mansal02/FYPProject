import os
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

if RVC_AVAILABLE:
    # NOTE: CPU conversion takes 5-10 seconds! GPU takes 0.5s.
    device = "cuda:0" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu"
    rvc_engine = RVCInference(device=device)


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

@app.post("/speak")
def speak_endpoint(payload: dict = Body(...)):
    text = payload.get("text")
    char_id = payload.get("character", "tachyon").lower()
    async_play = payload.get("async_play", True)

    if not text:
        return {"status": "empty", "file": None, "duration_ms": 0, "async": bool(async_play)}
    

    char_data, _ = get_character_data(char_id)
    use_rvc = char_data.get("rvc_enable", False) and RVC_AVAILABLE
    
    if voice_engine.current_name.lower() != char_data["name"].lower():
        voice_engine.set_voice(char_id)


    raw_audio_path = voice_engine.generate_only(text)

    final_path = raw_audio_path

    if use_rvc and raw_audio_path:
        model_name = char_data["rvc_model"]
        index_name = char_data.get("rvc_index", "")
        pitch = char_data.get("pitch_shift", 0)

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