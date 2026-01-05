import ollama

def get_marie_response_stream(prompt, memory_context=""):
    """Streams responses from Ollama with memory context injected."""
    try:
        if not prompt: return iter([""])
        
        system_instructions = "You are MARIE. Be concise. Use emotions like [happy]."
        if memory_context:
            system_instructions += f"\nFacts about the user you remember:\n{memory_context}"

        stream = ollama.chat(
            model='llama3', 
            messages=[
                {'role': 'system', 'content': system_instructions},
                {'role': 'user', 'content': prompt}
            ],
            stream=True 
        )
        
        for chunk in stream:
            yield chunk['message']['content']
            
    except Exception as e:
        print(f"Ollama Error: {e}")
        yield "I am having trouble connecting to my brain."