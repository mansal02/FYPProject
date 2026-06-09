"""Stub: voice server is disabled in offline mode."""

import uvicorn
from fastapi import FastAPI

from aiassistant.infra.voice.voice import MarieVoice

app = FastAPI()

voice_engine = MarieVoice()

@app.post("/speak")
def speak(payload: dict):
    text = payload.get("text")
    voice_engine.play_file(voice_engine.generate_speech(text))
    return {"status": "speaking"}


if __name__ == "__main__":
    print("Voice server is disabled in offline mode.")
    uvicorn.run(app, host="127.0.0.1", port=8002)
