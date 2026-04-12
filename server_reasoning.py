from fastapi import FastAPI, Body
import uvicorn
from index import get_marie_response_stream #
from database import MarieDB #

app = FastAPI()
db = MarieDB()

@app.post("/chat")
def chat_endpoint(payload: dict = Body(...)):
    user_text = payload.get("text")
    rag_context = payload.get("memory_context")
    if not rag_context:
        rag_context = db.get_all_rad_data()

    action_result = payload.get("action_result", "")
    if action_result:
        rag_context = f"{rag_context}\n\nLatest action/tool output:\n{action_result}".strip()
    

    full_response = ""
    for token in get_marie_response_stream(user_text, memory_context=rag_context):
        full_response += token
    
    return {"response": full_response}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)