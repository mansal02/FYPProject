"""Voice server handling Text-to-Speech via Piper engine."""

import uvicorn
from fastapi import FastAPI
from aiassistant.infra.voice.voice import MarieVoice
from aiassistant.infra.config.app_config import CONFIG

app = FastAPI()

print("[SYSTEM] Initializing TTS Voice Server...")
voice_engine = MarieVoice()

@app.post("/speak")
def speak(payload: dict):
    text = payload.get("text", "")
    if text.strip():
        # generate_only returns the path to the saved wav, which we then play safely
        filepath = voice_engine.generate_only(text)
        if filepath:
            voice_engine.play_file(filepath)
    return {"status": "speaking"}

if __name__ == "__main__":
    print("Voice server active and listening.")
    # Dynamically match config.yaml settings to prevent communication disconnects
    host = CONFIG.get("servers", {}).get("voice_host", "127.0.0.1")
    port = int(CONFIG.get("servers", {}).get("voice_port", 8001))
    uvicorn.run(app, host=host, port=port)