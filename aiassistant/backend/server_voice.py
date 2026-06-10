"""Voice server handling Text-to-Speech via Piper engine."""

import uvicorn
from fastapi import FastAPI
from aiassistant.infra.voice.voice import MarieVoice

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
    uvicorn.run(app, host="127.0.0.1", port=8002)