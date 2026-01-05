import ollama

def get_marie_response_stream(prompt):
    """
    Returns a GENERATOR that yields text chunks one by one.
    """
    try:
        if not prompt: return iter([""])

        # Create a stream
        stream = ollama.chat(
            model='llama3', 
            messages=[
                {'role': 'system', 'content': "You are MARIE. Be concise. Use emotions like [happy]."},
                {'role': 'user', 'content': prompt}
            ],
            stream=True  # <--- ENABLE STREAMING
        )
        
        # Yield clean content chunks
        for chunk in stream:
            yield chunk['message']['content']
            
    except Exception as e:
        print(f"\n[ERROR] Ollama Stream failed: {e}")
        yield "I am having connection issues."