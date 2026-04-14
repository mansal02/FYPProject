import ollama
from app_config import CONFIG


OLLAMA_MODEL = CONFIG["ollama"]["model"]
OLLAMA_NUM_PREDICT = int(CONFIG["ollama"]["num_predict"])
OLLAMA_NUM_CTX = int(CONFIG["ollama"]["num_ctx"])
OLLAMA_TEMPERATURE = float(CONFIG["ollama"].get("temperature", 0.2))
OLLAMA_SYSTEM_PROMPT = CONFIG["ollama"].get("system_prompt", "You are MARIE, a friendly assistant.")


def _truncate_memory_context(memory_context, max_lines=60, max_chars=7000):
    if not memory_context:
        return ""

    lines = [line for line in memory_context.splitlines() if line.strip()]
    recent = lines[-max_lines:]
    joined = "\n".join(recent)
    if len(joined) <= max_chars:
        return joined
    return joined[-max_chars:]

def get_marie_response_stream(prompt, memory_context=""):
    """Streams responses from Ollama with memory context injected."""
    try:
        if not prompt: return iter([""])
        
        system_instructions = OLLAMA_SYSTEM_PROMPT
        system_instructions += (
            "\nIf desktop control is required, output exactly one JSON object using this schema: "
            "{\"action\":\"open|close|search_web|open_website|volume|write_note\","
            "\"target\":\"string\",\"value\":\"optional\"}. "
            "Do not produce shell commands or unsafe code."
        )
        if memory_context:
            # Keep only recent context to reduce prompt size and prevent overflow.
            recent_facts = _truncate_memory_context(memory_context)
            system_instructions += f"\nFacts about the user you remember:\n{recent_facts}"

        stream = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {'role': 'system', 'content': system_instructions},
                {'role': 'user', 'content': prompt}
            ],
            options={
                "temperature": OLLAMA_TEMPERATURE,
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