"""Stub: voice server is disabled in offline mode."""

import uvicorn
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "voice server disabled"}


if __name__ == "__main__":
    print("Voice server is disabled in offline mode.")
    uvicorn.run(app, host="127.0.0.1", port=8002)
