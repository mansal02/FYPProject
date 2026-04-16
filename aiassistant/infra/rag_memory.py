import os
import threading
from pathlib import Path

from aiassistant.infra.config.app_config import CONFIG

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
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self.persist_dir))
            self.collection = client.get_or_create_collection("marie_knowledge")

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


def get_rag_context(question, top_k=4):
    return RAG.query(question, top_k=top_k)
