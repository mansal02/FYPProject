import os
import shutil
import threading
import time
from pathlib import Path

from aiassistant.infra.config.app_config import CONFIG
from aiassistant.infra.db.database import MarieDB

try:
    from aiassistant.infra.memory_agent import get_memory_agent_context, warm_memory_agent_index
except Exception:
    get_memory_agent_context = None
    warm_memory_agent_index = None

try:
    import chromadb
    CHROMA_AVAILABLE = True
except ImportError:
    chromadb = None
    CHROMA_AVAILABLE = False

try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except ImportError:
    PdfReader = None
    PDF_AVAILABLE = False


class LocalRAG:
    def __init__(self, knowledge_dir, persist_dir):
        self.knowledge_dir = Path(knowledge_dir)
        self.persist_dir = Path(persist_dir)
        self._lock = threading.RLock()
        self._file_fingerprints = {}

        self.collection = None
        if CHROMA_AVAILABLE:
            try:
                self.persist_dir.mkdir(parents=True, exist_ok=True)
                client = chromadb.PersistentClient(path=str(self.persist_dir))
                self.collection = client.get_or_create_collection("marie_knowledge")
            except BaseException as exc:
                self.collection = None
                print(f"[RAG] Chroma init failed, disabling local RAG: {exc}")
                self._attempt_reset()

    def _attempt_reset(self):
        if not self.persist_dir.exists():
            return
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = self.persist_dir.parent / f"chroma_corrupt_{timestamp}"
        try:
            shutil.move(str(self.persist_dir), str(backup_dir))
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            if not CHROMA_AVAILABLE:
                return
            client = chromadb.PersistentClient(path=str(self.persist_dir))
            self.collection = client.get_or_create_collection("marie_knowledge")
            print(f"[RAG] Reset corrupt Chroma store to {backup_dir}")
        except Exception as exc:
            self.collection = None
            print(f"[RAG] Chroma reset failed: {exc}")

    def _iter_docs(self):
        if not self.knowledge_dir.exists():
            return []
        doc_ext = {".txt", ".md", ".csv", ".json", ".py", ".pdf"}
        return [p for p in self.knowledge_dir.rglob("*") if p.is_file() and p.suffix.lower() in doc_ext]

    def _file_signature(self, path_obj):
        stat = path_obj.stat()
        return f"{path_obj}:{stat.st_size}:{int(stat.st_mtime)}"

    def _read_file_text(self, path_obj):
        ext = path_obj.suffix.lower()
        if ext == ".pdf":
            if not PDF_AVAILABLE:
                return ""
            try:
                reader = PdfReader(str(path_obj))
                pages = [(page.extract_text() or "") for page in reader.pages]
                return "\n".join(pages)
            except Exception:
                return ""

        try:
            return path_obj.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    def _chunk(self, text, chunk_size=750, overlap=120):
        cleaned = " ".join(text.split())
        if not cleaned:
            return []

        chunks = []
        start = 0
        while start < len(cleaned):
            end = min(len(cleaned), start + chunk_size)
            chunks.append(cleaned[start:end])
            if end == len(cleaned):
                break
            start = max(0, end - overlap)
        return chunks

    def refresh_index(self):
        if not self.collection:
            return

        with self._lock:
            for path_obj in self._iter_docs():
                signature = self._file_signature(path_obj)
                key = str(path_obj)
                if self._file_fingerprints.get(key) == signature:
                    continue

                try:
                    previous = self.collection.get(where={"source": key})
                    if previous and previous.get("ids"):
                        self.collection.delete(ids=previous["ids"])
                except Exception:
                    pass

                text = self._read_file_text(path_obj)
                chunks = self._chunk(text)
                if not chunks:
                    self._file_fingerprints[key] = signature
                    continue

                ids = []
                metadatas = []
                documents = []
                for idx, chunk in enumerate(chunks):
                    ids.append(f"{path_obj.name}:{idx}:{abs(hash(chunk))}")
                    metadatas.append({"source": key, "chunk": idx})
                    documents.append(chunk)

                try:
                    self.collection.add(ids=ids, documents=documents, metadatas=metadatas)
                    self._file_fingerprints[key] = signature
                except Exception as e:
                    print(f"[RAG] Failed to index {path_obj}: {e}")

    def query(self, question, top_k=4):
        if not self.collection or not question:
            return ""

        try:
            self.refresh_index()
            result = self.collection.query(query_texts=[question], n_results=top_k)
            docs = (result.get("documents") or [[]])[0]
            if not docs:
                return ""
            lines = []
            for doc in docs:
                snippet = doc.strip()
                if not snippet:
                    continue
                lines.append(f"- {snippet[:320]}")
            return "\n".join(lines)
        except Exception as e:
            print(f"[RAG] Query failed: {e}")
            return ""


_knowledge_dir = CONFIG["paths"].get("knowledge_dir")
_persist_dir = str((Path(CONFIG["paths"]["db_path"]).resolve().parent / "chroma").resolve())

RAG = LocalRAG(_knowledge_dir, _persist_dir)
_MEMORY_AGENT_WARMED = False


def _query_memory_agent(question, top_k):
    global _MEMORY_AGENT_WARMED

    if get_memory_agent_context is None:
        return ""

    try:
        if not _MEMORY_AGENT_WARMED and warm_memory_agent_index is not None:
            warm_memory_agent_index()
            _MEMORY_AGENT_WARMED = True
        return (get_memory_agent_context(question, top_k=top_k) or "").strip()
    except Exception:
        return ""


def _query_searchable_mirror(question, top_k):
    clean = str(question or "").strip()
    if not clean:
        return ""
    try:
        db = MarieDB()
        results = db.search_searchable_mirror(clean, limit=max(1, int(top_k)))
    except Exception:
        return ""

    if not results:
        return ""

    lines = []
    for item in results:
        path = str(item.get("file_path", "")).strip()
        snippet = str(item.get("snippet", "")).strip()
        if not path:
            continue
        if snippet:
            lines.append(f"- {path} :: {snippet[:240]}")
        else:
            lines.append(f"- {path}")

    return "\n".join(lines)


def get_rag_context(question, top_k=4):
    """Retrieve RAG context with mid-tier optimization.
    
    Respects device-class top_k and lazy loading settings.
    """
    from aiassistant.infra.optimization import get_device_capabilities
    
    # Auto-adjust top_k for mid-tier devices
    caps = get_device_capabilities()
    profile = caps.optimization_profile
    optimized_top_k = profile.get("top_k", 4)
    if top_k > optimized_top_k:
        top_k = optimized_top_k
    
    memory_snippets = _query_memory_agent(question, top_k=max(1, int(top_k)))
    local_snippets = RAG.query(question, top_k=top_k)
    mirror_snippets = _query_searchable_mirror(question, top_k=max(1, int(top_k)))

    sections = []
    if memory_snippets:
        sections.append("Memory agent snippets:\n" + memory_snippets)
    if local_snippets:
        sections.append("Knowledge folder snippets:\n" + local_snippets)
    if mirror_snippets:
        sections.append("Searchable mirror snippets:\n" + mirror_snippets)

    if sections:
        return "\n\n".join(sections).strip()
    return ""
