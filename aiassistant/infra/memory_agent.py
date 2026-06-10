from __future__ import annotations
import argparse, hashlib, threading, time, queue
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from aiassistant.infra.embeddings import get_marie_embedding_function
from aiassistant.infra.config.app_config import CONFIG

try:
    import chromadb
except Exception as e:  
    print(f"[Warning] ChromaDB import failed: {e}")
    chromadb = None

try:
    import ollama
except Exception:  
    ollama = None

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except Exception:  
    FileSystemEventHandler = object  
    Observer = None
    WATCHDOG_AVAILABLE = False


class MarieMemoryAgent:
    def __init__(self) -> None:
        cfg = CONFIG.get("memory_agent", {})
        self.enabled = bool(cfg.get("enabled", True))
        self.watch_dir = Path(str(cfg.get("watch_dir", "./knowledge/memory_agent")))
        self.persist_dir = Path(str(cfg.get("persist_dir", "./cache/chroma_db")))
        self.chunk_size = int(cfg.get("chunk_size", 850))
        self.chunk_overlap = int(cfg.get("chunk_overlap", 120))
        self.watch_extensions = set(cfg.get("watch_extensions", [".txt", ".md"]))
        self.embedding_model = str(cfg.get("embedding_model", "llama3.2:3b"))
        self.collection_name = str(cfg.get("collection", "marie_knowledge"))

        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file_signatures: Dict[str, str] = {}
        
        self._client = None
        self._collection = None
        if chromadb is not None:
            try:
                self._client = chromadb.PersistentClient(path=str(self.persist_dir))
                self._collection = self._client.get_or_create_collection(
                    name=self.collection_name,
                    embedding_function=get_marie_embedding_function()
                )
            except Exception:
                self._collection = None

        self._ollama_client = None
        if ollama is not None:
            try:
                self._ollama_client = ollama.Client(host="http://127.0.0.1:11434")
            except Exception:
                self._ollama_client = None 


    @property
    def is_ready(self) -> bool:
        return bool(self.enabled and self._collection is not None and ollama is not None)

    def _iter_files(self) -> List[Path]:
        if not self.watch_dir.exists():
            return []
        files: List[Path] = []
        for path in self.watch_dir.rglob("*"):
            if path.is_file() and path.suffix.lower() in self.watch_extensions:
                files.append(path)
        return files

    @staticmethod
    def _signature(path_obj: Path) -> str:
        stat = path_obj.stat()
        return f"{path_obj.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"

    @staticmethod
    def _collapse_ws(text: str) -> str:
        return " ".join((text or "").split()).strip()

    def _chunk(self, text: str) -> List[str]:
        clean = self._collapse_ws(text)
        if not clean:
            return []

        parts: List[str] = []
        start = 0
        while start < len(clean):
            end = min(len(clean), start + self.chunk_size)
            piece = clean[start:end].strip()
            if piece:
                parts.append(piece)
            if end >= len(clean):
                break
            start = max(0, end - self.chunk_overlap)
        return parts

    @staticmethod
    def _extract_embeddings(payload) -> List[List[float]]:
        if payload is None:
            return []

        if hasattr(payload, "model_dump"):
            try:
                payload = payload.model_dump()
            except Exception:
                pass

        values = None
        single = None

        if isinstance(payload, dict):
            values = payload.get("embeddings")
            single = payload.get("embedding")
        else:
            values = getattr(payload, "embeddings", None)
            single = getattr(payload, "embedding", None)

        if isinstance(values, list) and values and isinstance(values[0], list):
            return [[float(v) for v in vec] for vec in values if isinstance(vec, list)]

        if isinstance(single, list) and single:
            return [[float(v) for v in single]]

        return []

    def _get_embedding_payload(self, text_or_list):
        """Unified embedding call replacing redundant blocks."""
        client = self._ollama_client or ollama
        if not client: return None
        
        for method_name in ("embed", "embeddings"):
            if hasattr(client, method_name):
                try:
                    method = getattr(client, method_name)
                    # 'embeddings' strictly wants string prompt, 'embed' wants list or string input
                    if method_name == "embeddings" and isinstance(text_or_list, list):
                        if len(text_or_list) == 1:
                            return method(model=self.embedding_model, prompt=text_or_list[0])
                        return None
                    
                    kwargs = {"model": self.embedding_model}
                    if method_name == "embed": kwargs["input"] = text_or_list
                    else: kwargs["prompt"] = text_or_list
                    return method(**kwargs)
                except Exception:
                    pass
        return None

    def _embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        """Batches embeddings, utilizing the unified helper to prevent repetitive loops."""
        if not texts or ollama is None: return []

        payload = self._get_embedding_payload(list(texts))
        vectors = self._extract_embeddings(payload)
        if len(vectors) == len(texts):
            return vectors

        # Fallback one-by-one if batching fails
        one_by_one = []
        for text in texts:
            single = self._get_embedding_payload(text)
            single_vec = self._extract_embeddings(single)
            if not single_vec: return []
            one_by_one.append(single_vec[0])

        return one_by_one

    @staticmethod
    def _make_chunk_id(source: str, idx: int, chunk: str) -> str:
        source_hash = hashlib.sha1(source.encode("utf-8", errors="ignore")).hexdigest()[:12]
        chunk_hash = hashlib.sha1(chunk.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"{source_hash}:{idx}:{chunk_hash}"

    def _delete_source_chunks(self, source_path: str) -> None:
        if self._collection is None:
            return
        try:
            existing = self._collection.get(where={"source": source_path})
            ids = existing.get("ids") if isinstance(existing, dict) else None
            if ids:
                self._collection.delete(ids=ids)
        except Exception:
            pass

    def remove_file(self, file_path: Path) -> None:
        source = str(file_path.resolve())
        with self._lock:
            self._delete_source_chunks(source)
            self._file_signatures.pop(source, None)

    def process_file(self, file_path: Path) -> bool:
        if self._collection is None:
            return False

        path_obj = Path(file_path).resolve()
        source = str(path_obj)

        if not path_obj.exists() or not path_obj.is_file():
            self.remove_file(path_obj)
            return True

        if path_obj.suffix.lower() not in self.watch_extensions:
            return False

        try:
            signature = self._signature(path_obj)
        except Exception:
            return False

        with self._lock:
            if self._file_signatures.get(source) == signature:
                return False

        try:
            text = path_obj.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            text = ""

        chunks = self._chunk(text)

        with self._lock:
            self._delete_source_chunks(source)
            if not chunks:
                self._file_signatures[source] = signature
                return True

            embeddings = self._embed_batch(chunks)
            if len(embeddings) != len(chunks):
                return False

            ids: List[str] = []
            docs: List[str] = []
            metas: List[Dict[str, object]] = []
            rel_source = str(path_obj)
            try:
                rel_source = str(path_obj.relative_to(self.watch_dir))
            except Exception:
                rel_source = str(path_obj)

            for idx, chunk in enumerate(chunks):
                ids.append(self._make_chunk_id(source, idx, chunk))
                docs.append(chunk)
                metas.append(
                    {
                        "source": source,
                        "relative_source": rel_source,
                        "chunk": idx,
                    }
                )

            try:
                self._collection.upsert(
                    ids=ids,
                    documents=docs,
                    embeddings=embeddings,
                    metadatas=metas,
                )
                self._file_signatures[source] = signature
                return True
            except Exception:
                return False

    def refresh_all(self) -> int:
        processed = 0
        for path in self._iter_files():
            if self.process_file(path):
                processed += 1
        return processed

    def query(self, question: str, top_k: Optional[int] = None) -> str:
        if self._collection is None:
            return ""

        prompt = self._collapse_ws(question)
        if not prompt:
            return ""

        query_vectors = self._embed_batch([prompt])
        if not query_vectors:
            return ""

        k = max(1, int(top_k or self.default_top_k))
        try:
            result = self._collection.query(query_embeddings=[query_vectors[0]], n_results=k)
        except Exception:
            return ""

        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        if not docs:
            return ""

        lines: List[str] = []
        for idx, doc in enumerate(docs):
            snippet = self._collapse_ws(str(doc))
            if not snippet:
                continue
            source = "memory"
            if idx < len(metas) and isinstance(metas[idx], dict):
                source = str(metas[idx].get("relative_source") or metas[idx].get("source") or "memory")
            lines.append(f"- [{source}] {snippet[:320]}")

        return "\n".join(lines)


_MEMORY_AGENT_LOCK = threading.RLock()
_MEMORY_AGENT: Optional[MarieMemoryAgent] = None


def get_memory_agent() -> MarieMemoryAgent:
    global _MEMORY_AGENT
    with _MEMORY_AGENT_LOCK:
        if _MEMORY_AGENT is None:
            try:
                _MEMORY_AGENT = MarieMemoryAgent()
            except Exception as e:
                print(f"[Warning] Memory agent initialization failed: {e}")
                agent = MarieMemoryAgent.__new__(MarieMemoryAgent)
                agent.is_ready = False
                _MEMORY_AGENT = agent
        return _MEMORY_AGENT


def get_memory_agent_context(question: str, top_k: Optional[int] = None) -> str:
    agent = get_memory_agent()
    if not agent.is_ready:
        return ""
    return agent.query(question, top_k=top_k)


def warm_memory_agent_index() -> int:
    agent = get_memory_agent()
    if not agent.is_ready:
        return 0
    return agent.refresh_all()


class _MemoryEventHandler(FileSystemEventHandler):
    def __init__(self, agent: MarieMemoryAgent) -> None:
        super().__init__()
        self.agent = agent

    def _try_process(self, path_text: str) -> None:
        try:
            self.agent.process_file(Path(path_text))
        except Exception:
            pass

    def on_created(self, event) -> None: 
        if getattr(event, "is_directory", False):
            return
        self._try_process(str(event.src_path))

    def on_modified(self, event) -> None: 
        if getattr(event, "is_directory", False):
            return
        self._try_process(str(event.src_path))

    def on_moved(self, event) -> None:  
        if getattr(event, "is_directory", False):
            return
        self._try_process(str(event.dest_path))

    def on_deleted(self, event) -> None:  
        if getattr(event, "is_directory", False):
            return
        try:
            self.agent.remove_file(Path(str(event.src_path)))
        except Exception:
            pass


def run_memory_agent_watcher() -> None:
    agent = get_memory_agent()

    if not agent.enabled:
        print("[MEMORY] Disabled by config (memory_agent.enabled=false).")
        return

    if chromadb is None:
        print("[MEMORY] Missing dependency: chromadb")
        return

    if ollama is None:
        print("[MEMORY] Missing dependency: ollama")
        return

    if not WATCHDOG_AVAILABLE or Observer is None:
        print("[MEMORY] Missing dependency: watchdog")
        return

    indexed = agent.refresh_all()
    print(f"[MEMORY] Initial index complete. Processed files: {indexed}")
    print(f"[MEMORY] Watching folder: {agent.watch_dir}")
    print(f"[MEMORY] Collection: {agent.collection_name}")
    print(f"[MEMORY] Embedding model: {agent.embedding_model}")

    observer = Observer()
    handler = _MemoryEventHandler(agent)
    observer.schedule(handler, str(agent.watch_dir), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("[MEMORY] Stopping watcher...")
    finally:
        observer.stop()
        observer.join(timeout=5)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MARIE memory agent watcher")
    parser.add_argument("--once", action="store_true", help="Index current files once and exit")
    parser.add_argument("--query", type=str, default="", help="Query the memory store and print matches")
    parser.add_argument("--top-k", type=int, default=4, help="Number of query results")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    agent = get_memory_agent()

    if args.query.strip():
        warm_memory_agent_index()
        print(get_memory_agent_context(args.query, top_k=args.top_k) or "")
        return

    if args.once:
        count = warm_memory_agent_index()
        print(f"[MEMORY] Indexed files: {count}")
        return

    run_memory_agent_watcher()


if __name__ == "__main__":
    main()
