from fastapi import FastAPI, Body
import uvicorn
from index import get_marie_response_stream #
from database import MarieDB #

app = FastAPI()
db = MarieDB()

@app.post("/chat")
def chat_endpoint(payload: dict = Body(...)):
    user_text = payload.get("text")
    rag_context = db.get_all_rad_data() 
    

    full_response = ""
    for token in get_marie_response_stream(user_text, memory_context=rag_context):
        full_response += token
        
        
    db.log_chat(payload.get("user_id"), "marie", full_response) 
    
    return {"response": full_response}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)