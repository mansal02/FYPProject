import os
import ollama


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


OLLAMA_MODEL = os.environ.get("MARIE_OLLAMA_MODEL", "llama3")
OLLAMA_NUM_PREDICT = _env_int("MARIE_NUM_PREDICT", 180)
OLLAMA_NUM_CTX = _env_int("MARIE_NUM_CTX", 2048)

def get_marie_response_stream(prompt, memory_context=""):
    """Streams responses from Ollama with memory context injected."""
    try:
        if not prompt: return iter([""])
        
        system_instructions = (
            "You are MARIE, a friendly assistant. "
            "Reply with only the important points and keep answers short. "
            "Use clear alignment with tool/action output when available. "
            "Default to 2-5 concise bullet points when explaining. "
            "Stay warm and natural, not robotic."
        )
        if memory_context:
            # Keep only recent facts to reduce prompt size and speed up token generation.
            recent_facts = "\n".join(memory_context.splitlines()[-30:])
            system_instructions += f"\nFacts about the user you remember:\n{recent_facts}"

        stream = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {'role': 'system', 'content': system_instructions},
                {'role': 'user', 'content': prompt}
            ],
            options={
                "temperature": 0.2,
                "num_predict": OLLAMA_NUM_PREDICT,
                "num_ctx": OLLAMA_NUM_CTX,
            },
            stream=True
        )
        
        for chunk in stream:
            yield chunk['message']['content']
            
    except Exception as e:
        print(f"Ollama Error: {e}")
        yield "I am having trouble connecting to my brain."