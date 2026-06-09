from chromadb.utils import embedding_functions
import torch

def get_marie_embedding_function():
    """
    Loads the BGE model directly into the GTX 1080's VRAM.
    This bypasses Ollama entirely for lightning-fast RAG vectorization.
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        # BAAI/bge-base-en-v1.5 is extremely accurate and lightweight (~400MB VRAM)
        model_name="BAAI/bge-base-en-v1.5", 
        device=device
    )