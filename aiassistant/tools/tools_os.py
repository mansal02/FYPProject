"""
Crash-safe OS tools for local autonomous actions.

Every public function returns a dict with at least:
- success: bool
- message: user-friendly status
- data or error: details for the agent

This contract prevents tool failures from crashing the main loop.
"""

from __future__ import annotations

import csv
import difflib
import fnmatch
import mimetypes
import os
import re
import shutil
import smtplib
import subprocess
import time
import webbrowser
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

try:
    import pyautogui

    pyautogui.FAILSAFE = True
except Exception:  # pragma: no cover - optional dependency
    pyautogui = None

try:
    import PyPDF2
except Exception:  # pragma: no cover - optional dependency
    PyPDF2 = None

try:
    import docx
except Exception:  # pragma: no cover - optional dependency
    docx = None

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dependency
    np = None

_SENTENCE_TRANSFORMER_CLASS = None
_SENTENCE_TRANSFORMER_UNAVAILABLE = False


def _get_sentence_transformer_class():
    global _SENTENCE_TRANSFORMER_CLASS, _SENTENCE_TRANSFORMER_UNAVAILABLE

    if _SENTENCE_TRANSFORMER_CLASS is not None:
        return _SENTENCE_TRANSFORMER_CLASS
    if _SENTENCE_TRANSFORMER_UNAVAILABLE:
        return None

    try:
        from sentence_transformers import SentenceTransformer as _SentenceTransformer

        _SENTENCE_TRANSFORMER_CLASS = _SentenceTransformer
        return _SENTENCE_TRANSFORMER_CLASS
    except BaseException:  # pragma: no cover - optional dependency and defensive import guard
        _SENTENCE_TRANSFORMER_UNAVAILABLE = True
        return None

try:
    from duckduckgo_search import DDGS
except Exception:  # pragma: no cover - optional dependency
    DDGS = None

try:
    import win32com.client as win32  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    win32 = None

try:
    import requests
except Exception:  # pragma: no cover - optional dependency
    requests = None

try:
    import pywhatkit
except Exception:  # pragma: no cover - optional dependency
    pywhatkit = None

try:
    from send2trash import send2trash
except Exception:  # pragma: no cover - optional dependency
    send2trash = None

try:
    from AppOpener import close as appopener_close
    from AppOpener import give_appnames as appopener_list
    from AppOpener import open as appopener_open

    APPOPENER_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    appopener_open = None
    appopener_close = None
    appopener_list = None
    APPOPENER_AVAILABLE = False


def _ok(message: str, data: Any = None) -> Dict[str, Any]:
    return {"success": True, "message": message, "data": data}


def _fail(message: str, error: str) -> Dict[str, Any]:
    return {"success": False, "message": message, "error": error}


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)

    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def _to_str_list(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    text = str(value).strip()
    if not text:
        return []

    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def _safe_resolve_path(raw_path: str) -> Path:
    path = Path(str(raw_path or "").strip()).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    return path


def _list_drive_roots() -> List[Path]:
    if os.name != "nt":
        return [Path("/")]

    roots: List[Path] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:/")
        try:
            if drive.exists():
                roots.append(drive)
        except Exception:
            continue

    if not roots:
        roots.append(Path("C:/"))
    return roots


TEXT_SEARCH_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".csv",
    ".log",
    ".xml",
    ".html",
    ".htm",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".ps1",
    ".bat",
    ".cmd",
    ".sql",
    ".toml",
    ".rtf",
}


WINDOWS_APP_ALIASES = {
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "visual studio": "devenv",
    "excel": "excel",
    "word": "winword",
    "powerpoint": "powerpnt",
    "outlook": "outlook",
    "notepad": "notepad",
}

WINDOWS_WEB_APP_ALIASES = {
    "youtube": "https://www.youtube.com",
    "yt": "https://www.youtube.com",
    "youtube music": "https://music.youtube.com",
    "youtube studio": "https://studio.youtube.com",
}


SMART_SEARCH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "did",
    "do",
    "for",
    "from",
    "get",
    "have",
    "i",
    "in",
    "is",
    "it",
    "last",
    "me",
    "my",
    "of",
    "on",
    "open",
    "please",
    "recent",
    "search",
    "that",
    "the",
    "this",
    "to",
    "want",
    "with",
}


SMART_SEARCH_SKIP_DIR_NAMES = {
    "$recycle.bin",
    "system volume information",
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "models",
    "piper",
    "rvc_models",
    "checkpoints",
    "chroma",
    "cache",
    "pkgconfig",
}


def default_search_roots() -> List[Path]:
    """Workspace-first roots for local search to keep vague matching relevant and fast."""
    cwd = Path.cwd()
    roots = [cwd]

    # Only add broad user folders when the current working directory is home itself.
    try:
        if cwd.resolve() == Path.home().resolve():
            roots.extend([Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"])
    except Exception:
        pass

    unique_existing: List[Path] = []
    seen = set()
    for item in roots:
        resolved = item.resolve()
        if resolved.exists() and resolved not in seen:
            unique_existing.append(resolved)
            seen.add(resolved)
    return unique_existing


class ConnectivityModule:
    """Online query module backed by duckduckgo-search."""

    def online_query(self, query: str, max_results: int = 5) -> Dict[str, Any]:
        clean = (query or "").strip()
        if not clean:
            return _fail("Online query text was empty.", "empty_online_query")

        if DDGS is None:
            return _fail(
                "duckduckgo-search is unavailable.",
                "missing_dependency: duckduckgo-search",
            )

        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(clean, max_results=max(1, int(max_results))))
        except Exception as exc:
            return _fail("Online query failed.", str(exc))

        compact = []
        for item in results:
            if not isinstance(item, dict):
                continue
            compact.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "url": str(item.get("href", "")).strip(),
                    "snippet": str(item.get("body", "")).strip(),
                }
            )

        return _ok(f"Found {len(compact)} online result(s).", data=compact)


class UnifiedTaskBridge:
    """
    Unified bridge for UI automation, semantic file search, and system commands.

    - UI actions use pyautogui.
    - System actions use subprocess.
    """

    def __init__(self) -> None:
        self.connectivity = ConnectivityModule()
        self._semantic_model = None
        self._appopener_name_map: Optional[Dict[str, str]] = None

    @staticmethod
    def _is_vague_hint(hint: str) -> bool:
        clean = (hint or "").strip().lower()
        if not clean:
            return False
        if len(clean.split()) >= 3:
            return True
        vague_tokens = {
            "something",
            "document",
            "notes",
            "file",
            "thing",
            "report",
            "project",
            "old",
            "latest",
        }
        return any(token in clean for token in vague_tokens)

    def _load_semantic_model(self):
        if np is None:
            return None
        sentence_transformer_cls = _get_sentence_transformer_class()
        if sentence_transformer_cls is None:
            return None
        if self._semantic_model is None:
            # CPU pin avoids stealing VRAM from Ollama on low-end GPUs.
            try:
                self._semantic_model = sentence_transformer_cls("all-MiniLM-L6-v2", device="cpu")
            except Exception:
                return None
        return self._semantic_model

    @staticmethod
    def _iter_candidate_files(search_roots: List[Path], max_files: int = 1400) -> List[Path]:
        files: List[Path] = []
        priority_dirs = {
            "aiassistant": 0,
            "knowledge": 1,
            "script": 2,
            "launchers": 3,
            "frontend": 4,
            "backend": 5,
            "core": 6,
            "tools": 7,
            "infra": 8,
        }

        for root in search_roots:
            try:
                if not root.exists():
                    continue

                for current_root, dirs, filenames in os.walk(root, topdown=True, onerror=lambda _e: None):
                    dirs[:] = [d for d in dirs if d.lower() not in SMART_SEARCH_SKIP_DIR_NAMES]
                    dirs.sort(key=lambda d: (priority_dirs.get(d.lower(), 99), d.lower()))

                    current_path = Path(current_root)
                    for filename in filenames:
                        path = current_path / filename
                        if len(files) >= max_files:
                            return files
                        if not path.is_file():
                            continue
                        # Skip very large files for responsive matching.
                        try:
                            if path.stat().st_size > 20 * 1024 * 1024:
                                continue
                        except Exception:
                            continue
                        files.append(path)
            except Exception:
                continue
        return files

    @staticmethod
    def _path_semantic_text(path: Path) -> str:
        stem_tokens = re.sub(r"[_\-\.]+", " ", path.stem)
        parent_tokens = re.sub(r"[_\-\.]+", " ", str(path.parent).replace("\\", " "))
        return f"{path.name} {stem_tokens} {parent_tokens}"

    @staticmethod
    def _normalize_search_text(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()

    @classmethod
    def _tokenize_search_hint(cls, hint: str) -> List[str]:
        tokens: List[str] = []
        seen = set()
        for token in re.findall(r"[a-z0-9]{2,}", cls._normalize_search_text(hint)):
            if token in SMART_SEARCH_STOP_WORDS:
                continue
            if token not in seen:
                tokens.append(token)
                seen.add(token)
        return tokens

    @classmethod
    def _score_path_hint_match(cls, hint: str, hint_tokens: List[str], path: Path) -> float:
        hint_norm = cls._normalize_search_text(hint)
        name_norm = cls._normalize_search_text(path.name)
        stem_norm = cls._normalize_search_text(path.stem)
        full_norm = cls._normalize_search_text(str(path))

        if not hint_norm:
            return 0.0

        ratio_name = difflib.SequenceMatcher(None, hint_norm, name_norm).ratio()
        ratio_stem = difflib.SequenceMatcher(None, hint_norm, stem_norm).ratio()
        ratio_full = difflib.SequenceMatcher(None, hint_norm, full_norm).ratio()

        token_hits = sum(1 for token in hint_tokens if token in full_norm)
        token_score = (token_hits / len(hint_tokens)) if hint_tokens else 0.0

        exact_bonus = 0.0
        if hint_norm in name_norm:
            exact_bonus = 0.45
        elif hint_norm in full_norm:
            exact_bonus = 0.30

        weighted = max(
            (ratio_name * 0.55) + (ratio_stem * 0.25) + (ratio_full * 0.20),
            token_score,
        )
        return min(1.0, weighted + exact_bonus)

    @classmethod
    def _content_grep_score(
        cls,
        path: Path,
        hint_tokens: List[str],
        hint: str,
        max_file_size_bytes: int = 3 * 1024 * 1024,
    ) -> tuple[float, str]:
        if not hint_tokens:
            return 0.0, ""

        if path.suffix.lower() not in TEXT_SEARCH_EXTENSIONS:
            return 0.0, ""

        try:
            if path.stat().st_size > max_file_size_bytes:
                return 0.0, ""
        except Exception:
            return 0.0, ""

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return 0.0, ""

        if not text:
            return 0.0, ""

        hint_norm = cls._normalize_search_text(hint)
        best_score = 0.0
        best_line = ""

        for raw_line in text.splitlines()[:1800]:
            line = raw_line.strip()
            if not line:
                continue

            line_norm = cls._normalize_search_text(line)
            if not line_norm:
                continue

            token_hits = sum(1 for token in hint_tokens if token in line_norm)
            if token_hits <= 0:
                continue

            coverage = token_hits / len(hint_tokens)
            fuzzy = difflib.SequenceMatcher(None, hint_norm, line_norm).ratio() if hint_norm else 0.0
            score = max(coverage, fuzzy * 0.85)

            if score > best_score:
                best_score = score
                best_line = line[:220]

                if score >= 0.95:
                    break

        return best_score, best_line

    def smart_heuristic_search_files(
        self,
        hint: str,
        roots: Optional[List[str]] = None,
        max_results: int = 20,
        include_content: bool = True,
        max_scan_files: int = 4200,
    ) -> Dict[str, Any]:
        clean_hint = str(hint or "").strip()
        if not clean_hint:
            return _fail("Search hint was empty.", "empty_search_hint")

        search_roots = self._resolve_search_roots(roots=roots, include_all_drives=False)
        if not search_roots:
            return _fail("No accessible roots were found for search.", "missing_roots")

        files = self._iter_candidate_files(search_roots, max_files=max(300, int(max_scan_files)))
        if not files:
            return _ok("No files available for smart heuristic search.", data=[])

        hint_tokens = self._tokenize_search_hint(clean_hint)
        cap_results = max(1, int(max_results))

        path_ranked: List[Dict[str, Any]] = []
        for file_path in files:
            try:
                if any(part.lower() in SMART_SEARCH_SKIP_DIR_NAMES for part in file_path.parts):
                    continue
            except Exception:
                pass

            path_score = self._score_path_hint_match(clean_hint, hint_tokens, file_path)
            if path_score < 0.18 and hint_tokens:
                continue

            path_ranked.append({
                "path": file_path,
                "path_score": path_score,
                "content_score": 0.0,
                "content_line": "",
            })

        if not path_ranked:
            return self.lexical_search_files(clean_hint, roots=roots, max_results=cap_results)

        path_ranked.sort(key=lambda item: float(item["path_score"]), reverse=True)
        shortlist_cap = min(max(len(path_ranked), cap_results * 8), 320)
        shortlist = path_ranked[:shortlist_cap]

        if include_content and hint_tokens:
            for item in shortlist:
                score, line = self._content_grep_score(
                    path=item["path"],
                    hint_tokens=hint_tokens,
                    hint=clean_hint,
                )
                item["content_score"] = score
                item["content_line"] = line

        scored: List[Dict[str, Any]] = []
        for item in shortlist:
            combined = (float(item["path_score"]) * 0.72) + (float(item["content_score"]) * 0.28)
            if combined < 0.20 and float(item["path_score"]) < 0.28:
                continue

            scored.append(
                {
                    "path": str(item["path"].resolve()),
                    "score": round(combined, 4),
                    "snippet": str(item["content_line"]),
                }
            )

        if not scored:
            return self.lexical_search_files(clean_hint, roots=roots, max_results=cap_results)

        scored.sort(key=lambda item: float(item["score"]), reverse=True)
        result_paths = [entry["path"] for entry in scored[:cap_results]]
        return _ok(
            f"Smart heuristic search found {len(result_paths)} match(es).",
            data=result_paths,
        )

    def lexical_search_files(
        self,
        hint: str,
        roots: Optional[List[str]] = None,
        max_results: int = 20,
    ) -> Dict[str, Any]:
        clean = (hint or "").strip()
        if not clean:
            return _fail("Search hint was empty.", "empty_search_hint")

        hint_lower = clean.lower()
        search_roots = [Path(r) for r in roots] if roots else default_search_roots()

        matches: List[str] = []
        for root in search_roots:
            try:
                if not root.exists():
                    continue
                for path in root.rglob("*"):
                    if len(matches) >= max_results:
                        break
                    if not path.is_file():
                        continue
                    if hint_lower in path.name.lower():
                        matches.append(str(path.resolve()))
                if len(matches) >= max_results:
                    break
            except Exception:
                continue

        return _ok(f"Found {len(matches)} lexical file match(es).", data=matches)

    def semantic_search_files(
        self,
        hint: str,
        roots: Optional[List[str]] = None,
        max_results: int = 20,
    ) -> Dict[str, Any]:
        clean = (hint or "").strip()
        if not clean:
            return _fail("Search hint was empty.", "empty_search_hint")

        model = self._load_semantic_model()
        if model is None:
            return self.lexical_search_files(clean, roots=roots, max_results=max_results)

        search_roots = [Path(r) for r in roots] if roots else default_search_roots()
        files = self._iter_candidate_files(search_roots)
        if not files:
            return _ok("No files available for semantic search.", data=[])

        corpus = [self._path_semantic_text(path) for path in files]

        try:
            query_vec = model.encode([clean], normalize_embeddings=True)
            corpus_vec = model.encode(corpus, normalize_embeddings=True)
            scores = np.dot(corpus_vec, query_vec[0])
            ranked_idx = np.argsort(scores)[::-1][: max(1, int(max_results))]
        except Exception as exc:
            return _fail("Semantic file search failed.", str(exc))

        results: List[str] = []
        for idx in ranked_idx:
            results.append(str(files[int(idx)].resolve()))

        return _ok(f"Found {len(results)} semantic file match(es).", data=results)

    def search_files_by_hint(
        self,
        hint: str,
        roots: Optional[List[str]] = None,
        max_results: int = 20,
    ) -> Dict[str, Any]:
        heuristic = self.smart_heuristic_search_files(
            hint=hint,
            roots=roots,
            max_results=max_results,
            include_content=True,
        )

        if isinstance(heuristic, dict) and heuristic.get("success") and heuristic.get("data"):
            return heuristic

        if self._is_vague_hint(hint):
            semantic = self.semantic_search_files(hint, roots=roots, max_results=max_results)
            if isinstance(semantic, dict) and semantic.get("success") and semantic.get("data"):
                return semantic

        return self.lexical_search_files(hint, roots=roots, max_results=max_results)

    def _resolve_search_roots(
        self,
        roots: Optional[List[str]] = None,
        include_all_drives: bool = False,
    ) -> List[Path]:
        candidates: List[Path] = []

        if roots:
            for root in roots:
                clean = str(root or "").strip()
                if not clean:
                    continue
                try:
                    candidates.append(_safe_resolve_path(clean))
                except Exception:
                    continue
        else:
            candidates.extend(default_search_roots())

        if include_all_drives:
            candidates.extend(_list_drive_roots())

        unique_existing: List[Path] = []
        seen = set()
        for item in candidates:
            try:
                resolved = item.resolve()
            except Exception:
                continue
            if resolved.exists() and resolved not in seen:
                unique_existing.append(resolved)
                seen.add(resolved)

        return unique_existing

    def list_system_roots(self) -> Dict[str, Any]:
        roots = self._resolve_search_roots(include_all_drives=True)
        if not roots:
            return _fail("No accessible roots were found.", "missing_roots")
        return _ok("Listed accessible storage roots.", data=[str(path) for path in roots])

    def list_directory(
        self,
        path: str,
        recursive: bool = False,
        max_depth: int = 2,
        max_results: int = 300,
        pattern: str = "",
        include_files: bool = True,
        include_dirs: bool = True,
    ) -> Dict[str, Any]:
        clean_path = str(path or "").strip()
        if not clean_path:
            return _fail("Directory path was empty.", "empty_path")

        try:
            target = _safe_resolve_path(clean_path)
            if not target.exists() or not target.is_dir():
                return _fail("Directory was not found.", f"missing_directory: {target}")

            max_items = max(1, int(max_results))
            depth_limit = max(0, int(max_depth))
            pattern_text = str(pattern or "").strip().lower()

            def _name_match(name: str) -> bool:
                if not pattern_text:
                    return True
                lowered = name.lower()
                return pattern_text in lowered or fnmatch.fnmatch(lowered, pattern_text)

            items: List[Dict[str, Any]] = []
            truncated = False

            if not recursive:
                for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if child.is_dir() and not include_dirs:
                        continue
                    if child.is_file() and not include_files:
                        continue
                    if not _name_match(child.name):
                        continue

                    entry: Dict[str, Any] = {
                        "path": str(child.resolve()),
                        "type": "directory" if child.is_dir() else "file",
                    }
                    if child.is_file():
                        try:
                            entry["size_bytes"] = child.stat().st_size
                        except Exception:
                            pass

                    items.append(entry)
                    if len(items) >= max_items:
                        truncated = True
                        break
            else:
                base_depth = len(target.parts)
                for current_root, dirs, files in os.walk(target, topdown=True, onerror=lambda _e: None):
                    current = Path(current_root)
                    current_depth = len(current.parts) - base_depth
                    if current_depth >= depth_limit:
                        dirs[:] = []

                    if include_dirs:
                        for dirname in sorted(dirs):
                            if not _name_match(dirname):
                                continue
                            items.append(
                                {
                                    "path": str((current / dirname).resolve()),
                                    "type": "directory",
                                }
                            )
                            if len(items) >= max_items:
                                truncated = True
                                break
                        if truncated:
                            break

                    if include_files:
                        for filename in sorted(files):
                            if not _name_match(filename):
                                continue
                            file_path = current / filename
                            entry = {
                                "path": str(file_path.resolve()),
                                "type": "file",
                            }
                            try:
                                entry["size_bytes"] = file_path.stat().st_size
                            except Exception:
                                pass
                            items.append(entry)
                            if len(items) >= max_items:
                                truncated = True
                                break
                        if truncated:
                            break

            return _ok(
                f"Listed {len(items)} item(s) from {target}.",
                data={
                    "path": str(target),
                    "items": items,
                    "truncated": truncated,
                    "recursive": bool(recursive),
                },
            )
        except Exception as exc:
            return _fail("Directory listing failed.", str(exc))

    def deep_search_paths(
        self,
        query: str,
        roots: Optional[List[str]] = None,
        max_results: int = 40,
        include_all_drives: bool = False,
        include_content: bool = False,
        case_sensitive: bool = False,
        use_regex: bool = False,
        file_extensions: Optional[List[str]] = None,
        max_scan_entries: int = 120000,
        max_file_size_mb: int = 5,
    ) -> Dict[str, Any]:
        clean_query = str(query or "").strip()
        if not clean_query:
            return _fail("Deep search query was empty.", "empty_query")

        search_roots = self._resolve_search_roots(roots=roots, include_all_drives=include_all_drives)
        if not search_roots:
            return _fail("No accessible roots were found for deep search.", "missing_roots")

        max_hits = max(1, int(max_results))
        max_scanned = max(1, int(max_scan_entries))
        max_size_bytes = max(1, int(max_file_size_mb)) * 1024 * 1024

        normalized_extensions = set()
        for ext in file_extensions or []:
            clean_ext = str(ext or "").strip().lower()
            if not clean_ext:
                continue
            if not clean_ext.startswith("."):
                clean_ext = "." + clean_ext
            normalized_extensions.add(clean_ext)

        regex = None
        if use_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(clean_query, flags=flags)
            except re.error as exc:
                return _fail("Deep search regex is invalid.", str(exc))

        needle = clean_query if case_sensitive else clean_query.lower()

        def _matches(text: str) -> bool:
            if regex is not None:
                return bool(regex.search(text))
            if case_sensitive:
                return needle in text
            return needle in text.lower()

        def _snippet(text: str) -> str:
            if not text:
                return ""

            if regex is not None:
                match = regex.search(text)
                if not match:
                    return ""
                start_idx, end_idx = match.start(), match.end()
            else:
                haystack = text if case_sensitive else text.lower()
                start_idx = haystack.find(needle)
                if start_idx < 0:
                    return ""
                end_idx = start_idx + len(needle)

            left = max(0, start_idx - 80)
            right = min(len(text), end_idx + 140)
            return re.sub(r"\s+", " ", text[left:right]).strip()

        scanned_entries = 0
        truncated = False
        results: List[Dict[str, Any]] = []

        skip_dir_names = {
            "$recycle.bin",
            "system volume information",
            ".git",
            ".venv",
            "node_modules",
            "__pycache__",
        }

        for root in search_roots:
            if len(results) >= max_hits or scanned_entries >= max_scanned:
                truncated = True
                break

            for current_root, dirs, files in os.walk(root, topdown=True, onerror=lambda _e: None):
                if len(results) >= max_hits or scanned_entries >= max_scanned:
                    truncated = True
                    break

                current_path = Path(current_root)

                kept_dirs = []
                for dirname in dirs:
                    if dirname.lower() in skip_dir_names:
                        continue

                    scanned_entries += 1
                    if scanned_entries > max_scanned:
                        truncated = True
                        break

                    dir_path = current_path / dirname
                    kept_dirs.append(dirname)
                    if _matches(str(dir_path)):
                        results.append(
                            {
                                "path": str(dir_path.resolve()),
                                "type": "directory",
                                "match": "name",
                            }
                        )
                        if len(results) >= max_hits:
                            truncated = True
                            break

                dirs[:] = kept_dirs
                if truncated and (len(results) >= max_hits or scanned_entries >= max_scanned):
                    break

                for filename in files:
                    scanned_entries += 1
                    if scanned_entries > max_scanned:
                        truncated = True
                        break

                    file_path = current_path / filename
                    if _matches(str(file_path)):
                        results.append(
                            {
                                "path": str(file_path.resolve()),
                                "type": "file",
                                "match": "name",
                            }
                        )
                        if len(results) >= max_hits:
                            truncated = True
                            break

                    if not include_content:
                        continue

                    suffix = file_path.suffix.lower()
                    if normalized_extensions and suffix not in normalized_extensions:
                        continue
                    if not normalized_extensions and suffix and suffix not in TEXT_SEARCH_EXTENSIONS:
                        continue

                    try:
                        if file_path.stat().st_size > max_size_bytes:
                            continue
                    except Exception:
                        continue

                    try:
                        file_text = file_path.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue

                    if _matches(file_text):
                        entry: Dict[str, Any] = {
                            "path": str(file_path.resolve()),
                            "type": "file",
                            "match": "content",
                        }
                        context = _snippet(file_text)
                        if context:
                            entry["snippet"] = context
                        results.append(entry)
                        if len(results) >= max_hits:
                            truncated = True
                            break

                if truncated and (len(results) >= max_hits or scanned_entries >= max_scanned):
                    break

        return _ok(
            f"Deep search found {len(results)} match(es).",
            data={
                "query": clean_query,
                "results": results,
                "scanned_entries": scanned_entries,
                "truncated": truncated,
                "roots": [str(root) for root in search_roots],
            },
        )

    def analyze_path(self, path: str, max_items: int = 3500) -> Dict[str, Any]:
        clean_path = str(path or "").strip()
        if not clean_path:
            return _fail("Analysis path was empty.", "empty_path")

        try:
            target = _safe_resolve_path(clean_path)
            if not target.exists():
                return _fail("Path was not found.", f"missing_path: {target}")

            if target.is_file():
                stat = target.stat()
                return _ok(
                    f"Analyzed file: {target.name}",
                    data={
                        "path": str(target),
                        "type": "file",
                        "size_bytes": stat.st_size,
                        "suffix": target.suffix.lower(),
                        "modified_epoch": stat.st_mtime,
                    },
                )

            cap = max(200, int(max_items))
            scanned_files = 0
            total_dirs = 0
            total_size = 0
            truncated = False

            extension_counts: Dict[str, int] = {}
            largest_files: List[Dict[str, Any]] = []

            for current_root, dirs, files in os.walk(target, topdown=True, onerror=lambda _e: None):
                total_dirs += len(dirs)

                for filename in files:
                    scanned_files += 1
                    if scanned_files > cap:
                        truncated = True
                        break

                    file_path = Path(current_root) / filename
                    try:
                        size = file_path.stat().st_size
                    except Exception:
                        continue

                    total_size += size
                    suffix = file_path.suffix.lower() or "<none>"
                    extension_counts[suffix] = extension_counts.get(suffix, 0) + 1

                    largest_files.append({"path": str(file_path.resolve()), "size_bytes": size})
                    largest_files.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
                    if len(largest_files) > 8:
                        largest_files = largest_files[:8]

                if truncated:
                    break

            top_extensions = sorted(
                extension_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:12]

            return _ok(
                f"Analyzed directory: {target}",
                data={
                    "path": str(target),
                    "type": "directory",
                    "total_files": scanned_files if not truncated else cap,
                    "total_dirs": total_dirs,
                    "total_size_bytes": total_size,
                    "top_extensions": top_extensions,
                    "largest_files": largest_files,
                    "truncated": truncated,
                    "max_items": cap,
                },
            )
        except Exception as exc:
            return _fail("Path analysis failed.", str(exc))

    def create_path(
        self,
        path: str,
        kind: str = "file",
        content: str = "",
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        clean_path = str(path or "").strip()
        if not clean_path:
            return _fail("Create path was empty.", "empty_path")

        clean_kind = str(kind or "file").strip().lower()

        try:
            target = _safe_resolve_path(clean_path)
            if clean_kind in {"dir", "folder", "directory"}:
                if target.exists() and not target.is_dir():
                    return _fail("Target exists as a file.", f"path_conflict: {target}")
                target.mkdir(parents=True, exist_ok=True)
                return _ok("Directory is ready.", data={"path": str(target), "type": "directory"})

            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                return _fail("File already exists. Use overwrite=true to replace it.", f"existing_file: {target}")

            target.write_text(str(content or ""), encoding="utf-8")
            return _ok("File created successfully.", data={"path": str(target), "type": "file"})
        except Exception as exc:
            return _fail("Create path failed.", str(exc))

    def move_path(self, src: str, dst: str, overwrite: bool = False) -> Dict[str, Any]:
        clean_src = str(src or "").strip()
        clean_dst = str(dst or "").strip()
        if not clean_src or not clean_dst:
            return _fail("Move source or destination was empty.", "empty_move_paths")

        try:
            src_path = _safe_resolve_path(clean_src)
            dst_path = _safe_resolve_path(clean_dst)

            if not src_path.exists():
                return _fail("Move source was not found.", f"missing_source: {src_path}")

            if dst_path.exists():
                if not overwrite:
                    return _fail(
                        "Move destination already exists. Use overwrite=true to replace it.",
                        f"existing_destination: {dst_path}",
                    )
                if dst_path.is_dir():
                    shutil.rmtree(dst_path)
                else:
                    dst_path.unlink()

            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
            return _ok(
                "Move completed.",
                data={"from": str(src_path), "to": str(dst_path)},
            )
        except Exception as exc:
            return _fail("Move operation failed.", str(exc))

    def copy_path(self, src: str, dst: str, overwrite: bool = False) -> Dict[str, Any]:
        clean_src = str(src or "").strip()
        clean_dst = str(dst or "").strip()
        if not clean_src or not clean_dst:
            return _fail("Copy source or destination was empty.", "empty_copy_paths")

        try:
            src_path = _safe_resolve_path(clean_src)
            dst_path = _safe_resolve_path(clean_dst)

            if not src_path.exists():
                return _fail("Copy source was not found.", f"missing_source: {src_path}")

            if dst_path.exists():
                if not overwrite:
                    return _fail(
                        "Copy destination already exists. Use overwrite=true to replace it.",
                        f"existing_destination: {dst_path}",
                    )
                if dst_path.is_dir():
                    shutil.rmtree(dst_path)
                else:
                    dst_path.unlink()

            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if src_path.is_dir():
                shutil.copytree(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)

            return _ok(
                "Copy completed.",
                data={"from": str(src_path), "to": str(dst_path)},
            )
        except Exception as exc:
            return _fail("Copy operation failed.", str(exc))

    def delete_path(
        self,
        path: str,
        recursive: bool = False,
        use_trash: bool = True,
    ) -> Dict[str, Any]:
        clean_path = str(path or "").strip()
        if not clean_path:
            return _fail("Delete path was empty.", "empty_path")

        try:
            target = _safe_resolve_path(clean_path)
            if not target.exists():
                return _fail("Delete target was not found.", f"missing_path: {target}")

            if _to_bool(use_trash, default=True) and send2trash is not None:
                send2trash(str(target))
                return _ok("Moved target to recycle bin.", data={"path": str(target)})

            if target.is_dir():
                if not _to_bool(recursive, default=False):
                    return _fail(
                        "Target is a directory. Set recursive=true to delete it permanently.",
                        "directory_requires_recursive",
                    )
                shutil.rmtree(target)
            else:
                target.unlink()

            return _ok("Delete completed.", data={"path": str(target)})
        except Exception as exc:
            return _fail("Delete operation failed.", str(exc))

    @staticmethod
    def _normalize_app_alias(name: str) -> str:
        clean = str(name or "").strip().lower()
        clean = re.sub(r"[^a-z0-9\s\-_.]+", " ", clean)
        clean = re.sub(r"\b(app|application|software|program)\b", " ", clean)
        return re.sub(r"\s+", " ", clean).strip()

    def _load_appopener_name_map(self) -> Dict[str, str]:
        if self._appopener_name_map is not None:
            return self._appopener_name_map

        name_map: Dict[str, str] = {}
        if APPOPENER_AVAILABLE and appopener_list is not None:
            try:
                raw_names = appopener_list()
                if isinstance(raw_names, dict):
                    iterable = raw_names.keys()
                else:
                    iterable = raw_names

                for item in iterable:
                    original = str(item or "").strip()
                    normalized = self._normalize_app_alias(original)
                    if normalized and normalized not in name_map:
                        name_map[normalized] = original
            except Exception:
                name_map = {}

        self._appopener_name_map = name_map
        return self._appopener_name_map

    def _resolve_appopener_app_name(self, app_name: str) -> Optional[str]:
        normalized_query = self._normalize_app_alias(app_name)
        if not normalized_query:
            return None

        name_map = self._load_appopener_name_map()
        if not name_map:
            return None

        if normalized_query in name_map:
            return name_map[normalized_query]

        alias = WINDOWS_APP_ALIASES.get(normalized_query)
        if alias:
            alias_key = self._normalize_app_alias(alias)
            if alias_key in name_map:
                return name_map[alias_key]

        query_tokens = [token for token in normalized_query.split() if token]
        if query_tokens:
            contains_all: List[tuple[int, int, str]] = []
            for normalized_name, original_name in name_map.items():
                token_set = set(normalized_name.split())
                if all(token in token_set for token in query_tokens):
                    contains_all.append((len(token_set), len(normalized_name), original_name))
            if contains_all:
                contains_all.sort(key=lambda item: (item[0], item[1], item[2]))
                return contains_all[0][2]

        close = difflib.get_close_matches(normalized_query, list(name_map.keys()), n=1, cutoff=0.82)
        if close:
            return name_map.get(close[0])

        return None

    def launch_application(
        self,
        app_name: str,
        args: Optional[List[str]] = None,
        target_path: str = "",
    ) -> Dict[str, Any]:
        clean_app = str(app_name or "").strip()
        clean_target = str(target_path or "").strip()
        clean_args = [str(item).strip() for item in (args or []) if str(item).strip()]

        if not clean_app and clean_target:
            return self.open_file(clean_target)
        if not clean_app:
            return _fail("Application name was empty.", "empty_app_name")

        normalized_app = self._normalize_app_alias(clean_app)

        if os.name == "nt":
            web_url = WINDOWS_WEB_APP_ALIASES.get(normalized_app)
            if web_url and not clean_target:
                try:
                    webbrowser.open(web_url)
                    return _ok(
                        f"Opened {clean_app} in browser.",
                        data={
                            "app": clean_app,
                            "url": web_url,
                        },
                    )
                except Exception as exc:
                    return _fail("Launch application failed.", str(exc))

            if normalized_app == "discord" and not clean_target:
                local_app_data = Path(os.environ.get("LOCALAPPDATA", "")).expanduser()
                discord_candidates = [
                    local_app_data / "Discord" / "Update.exe",
                    local_app_data / "DiscordCanary" / "Update.exe",
                    local_app_data / "DiscordPTB" / "Update.exe",
                ]
                for candidate in discord_candidates:
                    if not candidate.exists():
                        continue
                    try:
                        subprocess.Popen(
                            [
                                str(candidate),
                                "--processStart",
                                "Discord.exe",
                            ]
                            + clean_args
                        )
                        return _ok(
                            "Launched application: discord",
                            data={
                                "app": clean_app,
                                "executable": str(candidate),
                                "args": clean_args,
                            },
                        )
                    except Exception:
                        continue

                try:
                    fallback_url = "https://discord.com/app"
                    webbrowser.open(fallback_url)
                    return _ok(
                        "Opened Discord web (desktop app not found).",
                        data={
                            "app": clean_app,
                            "url": fallback_url,
                        },
                    )
                except Exception as exc:
                    return _fail("Launch application failed.", str(exc))

            if APPOPENER_AVAILABLE and appopener_open is not None and not clean_target:
                resolved_app_name = self._resolve_appopener_app_name(clean_app)
                if resolved_app_name:
                    try:
                        appopener_open(
                            resolved_app_name,
                            match_closest=False,
                            output=False,
                            throw_error=True,
                        )
                        return _ok(
                            f"Launched application: {clean_app}",
                            data={
                                "app": clean_app,
                                "resolved_app": resolved_app_name,
                                "args": clean_args,
                                "target": clean_target,
                                "launcher": "AppOpener",
                            },
                        )
                    except Exception:
                        pass

        try:
            executable = clean_app
            if os.name == "nt":
                alias = WINDOWS_APP_ALIASES.get(self._normalize_app_alias(clean_app))
                if alias:
                    executable = alias

            app_path = Path(clean_app).expanduser()
            if app_path.exists():
                command = [str(app_path.resolve())] + clean_args
                if clean_target:
                    command.append(str(_safe_resolve_path(clean_target)))
                subprocess.Popen(command)
            elif os.name == "nt":
                resolved_exec = shutil.which(executable)
                if not resolved_exec:
                    return _fail(
                        "Launch application failed.",
                        (
                            f"Unknown app '{clean_app}'. "
                            "AppOpener could not resolve this app name and no executable was found in PATH."
                        ),
                    )
                command = [resolved_exec] + clean_args
                if clean_target:
                    command.append(str(_safe_resolve_path(clean_target)))
                subprocess.Popen(command)
            elif os.name == "posix":
                command = [executable] + clean_args
                if clean_target:
                    command.append(str(_safe_resolve_path(clean_target)))
                subprocess.Popen(command)
            else:
                command = ["open", "-a", executable] + clean_args
                if clean_target:
                    command.append(str(_safe_resolve_path(clean_target)))
                subprocess.Popen(command)

            return _ok(
                f"Launched application: {clean_app}",
                data={
                    "app": clean_app,
                    "args": clean_args,
                    "target": clean_target,
                },
            )
        except Exception as exc:
            return _fail("Launch application failed.", str(exc))

    def close_application(self, app_name: str, force: bool = True) -> Dict[str, Any]:
        clean_app = str(app_name or "").strip()
        if not clean_app:
            return _fail("Application name was empty.", "empty_app_name")

        try:
            target = clean_app
            if os.name == "nt":
                if APPOPENER_AVAILABLE and appopener_close is not None:
                    resolved_app_name = self._resolve_appopener_app_name(clean_app)
                    if resolved_app_name:
                        try:
                            appopener_close(
                                resolved_app_name,
                                match_closest=False,
                                output=False,
                                throw_error=True,
                            )
                            return _ok(
                                f"Closed application: {clean_app}",
                                data={
                                    "app": clean_app,
                                    "resolved_app": resolved_app_name,
                                    "launcher": "AppOpener",
                                },
                            )
                        except Exception:
                            pass

                alias = WINDOWS_APP_ALIASES.get(self._normalize_app_alias(clean_app))
                if alias:
                    target = alias

                exe_name = Path(target).name
                if not exe_name.lower().endswith(".exe"):
                    exe_name += ".exe"

                command = ["taskkill", "/IM", exe_name]
                if _to_bool(force, default=True):
                    command.append("/F")

                completed = subprocess.run(command, capture_output=True, text=True, check=False)
                output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
                if completed.returncode == 0:
                    return _ok(
                        f"Closed application: {clean_app}",
                        data={"app": clean_app, "output": output[:1200]},
                    )
                return _fail("Close application failed.", output[:1200] or f"exit_code={completed.returncode}")

            command = ["pkill", "-f", target]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode in {0, 1}:
                return _ok(f"Close request sent for application: {clean_app}")
            output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
            return _fail("Close application failed.", output[:1200] or f"exit_code={completed.returncode}")
        except Exception as exc:
            return _fail("Close application failed.", str(exc))

    def list_running_applications(self, max_results: int = 120) -> Dict[str, Any]:
        cap = max(1, int(max_results))
        try:
            apps: List[str] = []
            if os.name == "nt":
                completed = subprocess.run(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    return _fail("Could not list running applications.", (completed.stderr or "").strip())

                reader = csv.reader((completed.stdout or "").splitlines())
                for row in reader:
                    if not row:
                        continue
                    name = str(row[0]).strip()
                    if name:
                        apps.append(name)
                    if len(apps) >= cap:
                        break
            else:
                completed = subprocess.run(
                    ["ps", "-e", "-o", "comm="],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    return _fail("Could not list running applications.", (completed.stderr or "").strip())

                for line in (completed.stdout or "").splitlines():
                    name = line.strip()
                    if name:
                        apps.append(name)
                    if len(apps) >= cap:
                        break

            return _ok(f"Found {len(apps)} running application(s).", data=apps)
        except Exception as exc:
            return _fail("Listing running applications failed.", str(exc))

    def open_service(self, service: str) -> Dict[str, Any]:
        clean = self._normalize_app_alias(service)
        if not clean:
            return _fail("Service name was empty.", "empty_service")

        service_urls = {
            "gmail": "https://mail.google.com",
            "outlook": "https://outlook.live.com/mail/",
            "whatsapp": "https://web.whatsapp.com",
            "telegram": "https://web.telegram.org",
        }

        if clean in service_urls:
            try:
                url = service_urls[clean]
                webbrowser.open(url)
                return _ok(f"Opened {clean}.", data={"url": url})
            except Exception as exc:
                return _fail(f"Failed to open {clean}.", str(exc))

        return self.launch_application(clean)

    def read_file_text(self, file_path: str, max_chars: int = 12000) -> Dict[str, Any]:
        try:
            path = Path(file_path).expanduser().resolve()
            if not path.exists() or not path.is_file():
                return _fail("File was not found.", f"missing_file: {path}")

            suffix = path.suffix.lower()
            if suffix == ".pdf":
                if PyPDF2 is None:
                    return _fail(
                        "PyPDF2 is not available to read PDF files.",
                        "missing_dependency: PyPDF2",
                    )
                try:
                    with path.open("rb") as fh:
                        reader = PyPDF2.PdfReader(fh)
                        pages_text = []
                        for page in reader.pages:
                            page_text = page.extract_text() or ""
                            pages_text.append(page_text)
                            if sum(len(t) for t in pages_text) >= max_chars:
                                break
                        text = "\n".join(pages_text)
                except Exception as exc:
                    return _fail("Could not parse PDF file.", str(exc))
            elif suffix == ".docx":
                if docx is None:
                    return _fail(
                        "python-docx is not available to read DOCX files.",
                        "missing_dependency: python-docx",
                    )
                try:
                    document = docx.Document(str(path))
                    text = "\n".join(p.text for p in document.paragraphs)
                except Exception as exc:
                    return _fail("Could not parse DOCX file.", str(exc))
            else:
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    text = path.read_bytes().decode("utf-8", errors="ignore")

            text = text[:max_chars]
            return _ok(
                f"Read file successfully: {path.name}",
                data={"path": str(path), "text": text},
            )
        except Exception as exc:
            return _fail("File read failed.", str(exc))

    def open_file(
        self,
        file_path: str,
        resolve_by_hint: bool = False,
        roots: Optional[List[str]] = None,
        include_content: bool = True,
    ) -> Dict[str, Any]:
        try:
            raw_target = str(file_path or "").strip()
            if not raw_target:
                return _fail("Cannot open path because it was empty.", "empty_path")

            path = Path(raw_target).expanduser().resolve()
            resolved_from_hint = False

            if not path.exists() and _to_bool(resolve_by_hint, default=False):
                search_result = self.smart_heuristic_search_files(
                    hint=raw_target,
                    roots=roots,
                    max_results=1,
                    include_content=_to_bool(include_content, default=True),
                )
                candidates = search_result.get("data") if isinstance(search_result, dict) else []
                if candidates:
                    path = Path(str(candidates[0])).expanduser().resolve()
                    resolved_from_hint = True

            if not path.exists():
                return _fail(
                    "Cannot open path because it does not exist.",
                    f"missing_file: {path}",
                )

            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif os.name == "posix":
                subprocess.Popen(["xdg-open", str(path)])
            else:
                subprocess.Popen(["open", str(path)])

            if resolved_from_hint:
                return _ok(
                    f"Opened path: {path.name} (resolved from hint).",
                    data=str(path),
                )
            return _ok(f"Opened path: {path.name}", data=str(path))
        except Exception as exc:
            return _fail("Failed to open path.", str(exc))

    def move_mouse(self, x: int, y: int, duration: float = 0.2) -> Dict[str, Any]:
        try:
            if pyautogui is None:
                return _fail("PyAutoGUI is unavailable.", "missing_dependency: pyautogui")
            pyautogui.moveTo(int(x), int(y), duration=max(0.0, duration))
            return _ok(f"Mouse moved to ({x}, {y}).")
        except Exception as exc:
            return _fail("Mouse move failed.", str(exc))

    def click(self, button: str = "left", clicks: int = 1) -> Dict[str, Any]:
        try:
            if pyautogui is None:
                return _fail("PyAutoGUI is unavailable.", "missing_dependency: pyautogui")
            pyautogui.click(button=button, clicks=max(1, int(clicks)))
            return _ok(f"Mouse click completed ({button}, {clicks}x).")
        except Exception as exc:
            return _fail("Mouse click failed.", str(exc))

    def type_text(self, text: str, interval: float = 0.01) -> Dict[str, Any]:
        try:
            if pyautogui is None:
                return _fail("PyAutoGUI is unavailable.", "missing_dependency: pyautogui")
            pyautogui.write(str(text), interval=max(0.0, float(interval)))
            return _ok("Text typing completed.")
        except Exception as exc:
            return _fail("Typing action failed.", str(exc))

    def press_key(self, key: str) -> Dict[str, Any]:
        try:
            if pyautogui is None:
                return _fail("PyAutoGUI is unavailable.", "missing_dependency: pyautogui")
            pyautogui.press(str(key))
            return _ok(f"Pressed key: {key}")
        except Exception as exc:
            return _fail("Key press failed.", str(exc))

    def hotkey(self, keys: List[str]) -> Dict[str, Any]:
        try:
            if pyautogui is None:
                return _fail("PyAutoGUI is unavailable.", "missing_dependency: pyautogui")
            if not keys:
                return _fail("No hotkey keys provided.", "empty_hotkey")
            pyautogui.hotkey(*[str(k) for k in keys])
            return _ok(f"Hotkey sent: {' + '.join(keys)}")
        except Exception as exc:
            return _fail("Hotkey action failed.", str(exc))

    def run_system_command(self, command: str, timeout: int = 25) -> Dict[str, Any]:
        clean = (command or "").strip()
        if not clean:
            return _fail("Command text was empty.", "empty_command")
        try:
            env = os.environ.copy()
            env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
            env.setdefault("OLLAMA_KV_CACHE_TYPE", "q4_0")
            completed = subprocess.run(
                clean,
                shell=True,
                capture_output=True,
                text=True,
                timeout=max(1, int(timeout)),
                env=env,
            )
            combined = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
            compact = combined.strip()[:4000]
            if completed.returncode == 0:
                return _ok("System command completed.", data={"output": compact})
            return _fail(
                "System command failed.",
                f"exit_code={completed.returncode}; output={compact}",
            )
        except Exception as exc:
            return _fail("System command execution failed.", str(exc))

    def toggle_dark_mode(self, enable: Optional[bool] = None) -> Dict[str, Any]:
        if os.name == "nt":
            return self._toggle_windows_dark_mode(enable)
        return self._toggle_linux_dark_mode(enable)

    def _toggle_windows_dark_mode(self, enable: Optional[bool]) -> Dict[str, Any]:
        key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        current_is_dark = False
        try:
            probe = subprocess.run(
                ["reg", "query", key, "/v", "AppsUseLightTheme"],
                capture_output=True,
                text=True,
            )
            current_is_dark = "0x0" in (probe.stdout or "")
        except Exception:
            current_is_dark = False

        target_is_dark = (not current_is_dark) if enable is None else bool(enable)
        value = "0" if target_is_dark else "1"

        try:
            subprocess.run(
                ["reg", "add", key, "/v", "AppsUseLightTheme", "/t", "REG_DWORD", "/d", value, "/f"],
                check=False,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["reg", "add", key, "/v", "SystemUsesLightTheme", "/t", "REG_DWORD", "/d", value, "/f"],
                check=False,
                capture_output=True,
                text=True,
            )
            mode = "dark" if target_is_dark else "light"
            return _ok(f"Windows theme toggled to {mode} mode.")
        except Exception as exc:
            return _fail("Failed to toggle Windows dark mode.", str(exc))

    def _toggle_linux_dark_mode(self, enable: Optional[bool]) -> Dict[str, Any]:
        target_is_dark = True if enable is None else bool(enable)
        color_scheme = "prefer-dark" if target_is_dark else "default"
        gtk_theme = "Adwaita-dark" if target_is_dark else "Adwaita"

        try:
            subprocess.run(
                ["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", color_scheme],
                check=False,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["gsettings", "set", "org.gnome.desktop.interface", "gtk-theme", gtk_theme],
                check=False,
                capture_output=True,
                text=True,
            )
            mode = "dark" if target_is_dark else "light"
            return _ok(f"Linux theme toggled to {mode} mode.")
        except Exception as exc:
            return _fail("Failed to toggle Linux dark mode.", str(exc))

    def _resolve_attachment_paths(
        self,
        attachments: Optional[List[str]],
        roots: Optional[List[str]] = None,
    ) -> tuple[List[str], List[str]]:
        resolved: List[str] = []
        unresolved: List[str] = []

        for item in attachments or []:
            hint = str(item or "").strip()
            if not hint:
                continue

            try:
                direct_path = _safe_resolve_path(hint)
                if direct_path.exists() and direct_path.is_file():
                    resolved.append(str(direct_path))
                    continue
            except Exception:
                pass

            found = self.search_files_by_hint(hint, roots=roots, max_results=1)
            candidates = found.get("data") if isinstance(found, dict) else []
            if candidates:
                resolved.append(str(candidates[0]))
            else:
                unresolved.append(hint)

        # Preserve order and remove duplicates.
        unique_resolved: List[str] = []
        seen = set()
        for path in resolved:
            if path not in seen:
                unique_resolved.append(path)
                seen.add(path)

        return unique_resolved, unresolved

    @staticmethod
    def _smtp_default(provider: str) -> tuple[str, int]:
        clean = str(provider or "").strip().lower()
        if clean == "gmail":
            return "smtp.gmail.com", 587
        if clean in {"outlook", "hotmail", "live"}:
            return "smtp.office365.com", 587
        return "", 587

    def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        provider: str = "gmail",
        attachments: Optional[List[str]] = None,
        roots: Optional[List[str]] = None,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        from_email: str = "",
        send_now: bool = False,
        use_outlook_desktop: bool = True,
    ) -> Dict[str, Any]:
        clean_to = str(to_email or "").strip()
        if not clean_to:
            return _fail("Recipient email is required.", "missing_to_email")

        clean_provider = str(provider or "gmail").strip().lower() or "gmail"
        resolved_attachments, unresolved_attachments = self._resolve_attachment_paths(attachments, roots=roots)

        # Outlook desktop route for Windows users.
        if (
            clean_provider == "outlook"
            and os.name == "nt"
            and win32 is not None
            and _to_bool(use_outlook_desktop, default=True)
        ):
            try:
                outlook = win32.Dispatch("Outlook.Application")
                mail = outlook.CreateItem(0)
                mail.To = clean_to
                mail.Subject = str(subject or "").strip()
                mail.Body = str(body or "").strip()
                for file_path in resolved_attachments:
                    mail.Attachments.Add(file_path)

                if _to_bool(send_now, default=False):
                    mail.Send()
                    status = "Outlook email sent."
                else:
                    mail.Save()
                    status = "Outlook draft created."

                payload = {
                    "provider": "outlook_desktop",
                    "to": clean_to,
                    "attachments": resolved_attachments,
                }
                if unresolved_attachments:
                    payload["unresolved_attachments"] = unresolved_attachments
                return _ok(status, data=payload)
            except Exception as exc:
                return _fail("Outlook desktop send failed.", str(exc))

        # SMTP route (Gmail, Outlook SMTP, or custom SMTP).
        default_host, default_port = self._smtp_default(clean_provider)
        final_host = str(smtp_host or default_host).strip()
        final_port = int(smtp_port or default_port or 587)
        sender = str(from_email or smtp_user or "").strip()

        if not final_host:
            return _fail(
                "SMTP host is required for this email provider.",
                "missing_smtp_host",
            )
        if not sender:
            return _fail(
                "Sender address is required (from_email or smtp_user).",
                "missing_sender",
            )

        try:
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = clean_to
            msg["Subject"] = str(subject or "").strip()
            msg.set_content(str(body or "").strip())

            for file_path in resolved_attachments:
                source = Path(file_path)
                mime_type, _ = mimetypes.guess_type(str(source))
                if not mime_type:
                    mime_type = "application/octet-stream"
                maintype, subtype = mime_type.split("/", 1)
                msg.add_attachment(
                    source.read_bytes(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=source.name,
                )

            if _to_bool(send_now, default=False):
                if not str(smtp_user or "").strip() or not str(smtp_password or ""):
                    return _fail(
                        "SMTP credentials are required to send now.",
                        "missing_smtp_credentials",
                    )

                with smtplib.SMTP(final_host, final_port, timeout=25) as smtp:
                    smtp.starttls()
                    smtp.login(str(smtp_user).strip(), str(smtp_password))
                    smtp.send_message(msg)

                payload = {
                    "provider": clean_provider,
                    "mode": "smtp_send",
                    "smtp_host": final_host,
                    "to": clean_to,
                    "attachments": resolved_attachments,
                }
                if unresolved_attachments:
                    payload["unresolved_attachments"] = unresolved_attachments
                return _ok("Email sent via SMTP.", data=payload)

            draft_dir = Path.cwd() / "cache" / "mail_drafts"
            draft_dir.mkdir(parents=True, exist_ok=True)
            draft_name = f"email_draft_{int(time.time())}.eml"
            draft_path = draft_dir / draft_name
            draft_path.write_bytes(bytes(msg))

            payload = {
                "provider": clean_provider,
                "mode": "draft_file",
                "draft": str(draft_path.resolve()),
                "to": clean_to,
                "attachments": resolved_attachments,
            }
            if unresolved_attachments:
                payload["unresolved_attachments"] = unresolved_attachments
            return _ok("Email draft file created.", data=payload)
        except Exception as exc:
            return _fail("Email operation failed.", str(exc))

    def draft_email_attachment(
        self,
        to_email: str,
        subject: str,
        body: str,
        file_hint: str,
        roots: Optional[List[str]] = None,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        from_email: str = "",
        send_now: bool = False,
    ) -> Dict[str, Any]:
        return self.send_email(
            to_email=to_email,
            subject=subject,
            body=body,
            provider="outlook" if os.name == "nt" else "custom",
            attachments=[str(file_hint or "")],
            roots=roots,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            from_email=from_email,
            send_now=send_now,
            use_outlook_desktop=True,
        )

    def send_telegram(
        self,
        bot_token: str,
        chat_id: str,
        message: str = "",
        file_path: str = "",
        file_hint: str = "",
        roots: Optional[List[str]] = None,
        disable_web_preview: bool = False,
    ) -> Dict[str, Any]:
        if requests is None:
            return _fail("requests is unavailable for Telegram messaging.", "missing_dependency: requests")

        clean_token = str(bot_token or "").strip()
        clean_chat = str(chat_id or "").strip()
        clean_message = str(message or "").strip()
        if not clean_token or not clean_chat:
            return _fail("Telegram bot token and chat_id are required.", "missing_telegram_credentials")

        base_url = f"https://api.telegram.org/bot{clean_token}"
        sent = []

        try:
            if clean_message:
                payload = {
                    "chat_id": clean_chat,
                    "text": clean_message,
                    "disable_web_page_preview": _to_bool(disable_web_preview, default=False),
                }
                response = requests.post(f"{base_url}/sendMessage", data=payload, timeout=20)
                if not response.ok:
                    return _fail("Telegram message send failed.", response.text[:1200])
                sent.append("message")

            file_candidate = str(file_path or file_hint or "").strip()
            if file_candidate:
                resolved, unresolved = self._resolve_attachment_paths([file_candidate], roots=roots)
                if not resolved:
                    return _fail("Telegram file was not found.", f"unresolved_file: {unresolved}")

                source = Path(resolved[0])
                with source.open("rb") as handle:
                    payload = {"chat_id": clean_chat}
                    if clean_message:
                        payload["caption"] = clean_message[:900]
                    response = requests.post(
                        f"{base_url}/sendDocument",
                        data=payload,
                        files={"document": handle},
                        timeout=40,
                    )
                if not response.ok:
                    return _fail("Telegram file send failed.", response.text[:1200])
                sent.append("document")

            if not sent:
                return _fail("Nothing to send to Telegram. Provide message or file_path.", "empty_telegram_payload")

            return _ok(
                "Telegram send completed.",
                data={"chat_id": clean_chat, "sent": sent},
            )
        except Exception as exc:
            return _fail("Telegram operation failed.", str(exc))

    def send_whatsapp(
        self,
        to_number: str,
        message: str,
        use_twilio: bool = False,
        twilio_account_sid: str = "",
        twilio_auth_token: str = "",
        twilio_from: str = "",
        media_url: str = "",
    ) -> Dict[str, Any]:
        clean_to = str(to_number or "").strip()
        clean_message = str(message or "").strip()
        clean_media_url = str(media_url or "").strip()

        if not clean_to:
            return _fail("WhatsApp recipient number is required.", "missing_whatsapp_number")

        wants_twilio = _to_bool(use_twilio, default=False) or bool(
            str(twilio_account_sid or "").strip()
            and str(twilio_auth_token or "").strip()
            and str(twilio_from or "").strip()
        )

        if wants_twilio:
            if requests is None:
                return _fail("requests is unavailable for Twilio WhatsApp.", "missing_dependency: requests")

            sid = str(twilio_account_sid or "").strip()
            token = str(twilio_auth_token or "").strip()
            from_number = str(twilio_from or "").strip()
            if not sid or not token or not from_number:
                return _fail(
                    "Twilio SID, token, and from number are required.",
                    "missing_twilio_credentials",
                )

            to_value = clean_to if clean_to.lower().startswith("whatsapp:") else f"whatsapp:{clean_to}"
            from_value = from_number if from_number.lower().startswith("whatsapp:") else f"whatsapp:{from_number}"
            payload = {"To": to_value, "From": from_value}
            if clean_message:
                payload["Body"] = clean_message
            if clean_media_url:
                payload["MediaUrl"] = clean_media_url

            try:
                response = requests.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                    data=payload,
                    auth=(sid, token),
                    timeout=25,
                )
                if response.status_code >= 400:
                    return _fail("Twilio WhatsApp send failed.", response.text[:1200])

                response_data = {}
                try:
                    response_data = response.json()
                except Exception:
                    response_data = {}

                return _ok(
                    "WhatsApp message sent via Twilio.",
                    data={
                        "to": to_value,
                        "sid": response_data.get("sid", ""),
                    },
                )
            except Exception as exc:
                return _fail("Twilio WhatsApp send failed.", str(exc))

        if not clean_message:
            return _fail(
                "WhatsApp web mode supports text messages only. Provide a message.",
                "missing_message",
            )

        if pywhatkit is not None:
            try:
                pywhatkit.sendwhatmsg_instantly(
                    phone_no=clean_to,
                    message=clean_message,
                    wait_time=15,
                    tab_close=True,
                    close_time=4,
                )
                return _ok(
                    "WhatsApp Web message queued. Ensure your browser is logged in to WhatsApp.",
                    data={"to": clean_to},
                )
            except Exception as exc:
                return _fail("WhatsApp Web send failed.", str(exc))

        try:
            url = f"https://web.whatsapp.com/send?phone={quote_plus(clean_to)}&text={quote_plus(clean_message)}"
            webbrowser.open(url)
            return _ok("Opened WhatsApp Web compose window.", data={"url": url})
        except Exception as exc:
            return _fail("Failed to open WhatsApp Web.", str(exc))

    def execute_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        try:
            action_name = str(action.get("action", "")).strip().lower()
            roots = _to_str_list(action.get("roots")) or None

            if action_name == "list_system_roots":
                return self.list_system_roots()

            if action_name == "list_directory":
                return self.list_directory(
                    path=str(action.get("path", "")),
                    recursive=_to_bool(action.get("recursive", False), default=False),
                    max_depth=int(action.get("max_depth", 2) or 2),
                    max_results=int(action.get("max_results", 300) or 300),
                    pattern=str(action.get("pattern", "")),
                    include_files=_to_bool(action.get("include_files", True), default=True),
                    include_dirs=_to_bool(action.get("include_dirs", True), default=True),
                )

            if action_name in {"deep_search", "deep_search_paths"}:
                return self.deep_search_paths(
                    query=str(action.get("query") or action.get("hint") or ""),
                    roots=roots,
                    max_results=int(action.get("max_results", 40) or 40),
                    include_all_drives=_to_bool(action.get("include_all_drives", False), default=False),
                    include_content=_to_bool(action.get("include_content", False), default=False),
                    case_sensitive=_to_bool(action.get("case_sensitive", False), default=False),
                    use_regex=_to_bool(action.get("use_regex", False), default=False),
                    file_extensions=_to_str_list(action.get("file_extensions")) or None,
                    max_scan_entries=int(action.get("max_scan_entries", 120000) or 120000),
                    max_file_size_mb=int(action.get("max_file_size_mb", 5) or 5),
                )

            if action_name == "analyze_path":
                return self.analyze_path(
                    path=str(action.get("path", "")),
                    max_items=int(action.get("max_items", 3500) or 3500),
                )

            if action_name == "create_path":
                return self.create_path(
                    path=str(action.get("path", "")),
                    kind=str(action.get("kind", "file")),
                    content=str(action.get("content", "")),
                    overwrite=_to_bool(action.get("overwrite", False), default=False),
                )

            if action_name == "move_path":
                return self.move_path(
                    src=str(action.get("src") or action.get("source") or ""),
                    dst=str(action.get("dst") or action.get("destination") or ""),
                    overwrite=_to_bool(action.get("overwrite", False), default=False),
                )

            if action_name == "copy_path":
                return self.copy_path(
                    src=str(action.get("src") or action.get("source") or ""),
                    dst=str(action.get("dst") or action.get("destination") or ""),
                    overwrite=_to_bool(action.get("overwrite", False), default=False),
                )

            if action_name == "delete_path":
                return self.delete_path(
                    path=str(action.get("path", "")),
                    recursive=_to_bool(action.get("recursive", False), default=False),
                    use_trash=_to_bool(action.get("use_trash", True), default=True),
                )

            if action_name == "launch_application":
                return self.launch_application(
                    app_name=str(action.get("app") or action.get("name") or ""),
                    args=_to_str_list(action.get("args")) or None,
                    target_path=str(action.get("path") or action.get("target_path") or ""),
                )

            if action_name == "close_application":
                return self.close_application(
                    app_name=str(action.get("app") or action.get("name") or ""),
                    force=_to_bool(action.get("force", True), default=True),
                )

            if action_name in {"list_running_apps", "list_running_applications"}:
                return self.list_running_applications(
                    max_results=int(action.get("max_results", 120) or 120),
                )

            if action_name == "open_service":
                return self.open_service(str(action.get("service", "")))

            if action_name == "search_file":
                return self.smart_heuristic_search_files(
                    hint=str(action.get("hint") or action.get("query") or ""),
                    roots=roots,
                    max_results=int(action.get("max_results", 20) or 20),
                    include_content=_to_bool(action.get("include_content", True), default=True),
                    max_scan_files=int(action.get("max_scan_files", 4200) or 4200),
                )

            if action_name == "semantic_search_file":
                return self.smart_heuristic_search_files(
                    hint=str(action.get("hint") or action.get("query") or ""),
                    roots=roots,
                    max_results=int(action.get("max_results", 20) or 20),
                    include_content=_to_bool(action.get("include_content", True), default=True),
                    max_scan_files=int(action.get("max_scan_files", 4200) or 4200),
                )

            if action_name == "read_file":
                return self.read_file_text(
                    file_path=str(action.get("path", "")),
                    max_chars=int(action.get("max_chars", 12000) or 12000),
                )

            if action_name in {"open_file", "open_path"}:
                return self.open_file(
                    file_path=str(action.get("path") or action.get("hint") or ""),
                    resolve_by_hint=_to_bool(action.get("resolve_by_hint", True), default=True),
                    roots=roots,
                    include_content=_to_bool(action.get("include_content", True), default=True),
                )

            if action_name == "move_mouse":
                return self.move_mouse(
                    x=int(action.get("x", 0)),
                    y=int(action.get("y", 0)),
                    duration=float(action.get("duration", 0.2)),
                )

            if action_name == "click":
                return self.click(
                    button=str(action.get("button", "left")),
                    clicks=int(action.get("clicks", 1)),
                )

            if action_name == "type_text":
                return self.type_text(
                    text=str(action.get("text", "")),
                    interval=float(action.get("interval", 0.01)),
                )

            if action_name == "press_key":
                return self.press_key(str(action.get("key", "enter")))

            if action_name == "hotkey":
                keys = _to_str_list(action.get("keys"))
                if not keys:
                    return _fail("Hotkey keys must be a non-empty list.", "invalid_hotkey_payload")
                return self.hotkey(keys)

            if action_name == "run_command":
                return self.run_system_command(
                    command=str(action.get("command", "")),
                    timeout=int(action.get("timeout", 25) or 25),
                )

            if action_name == "toggle_dark_mode":
                enable = action.get("enable")
                parsed_enable = None if enable is None else _to_bool(enable, default=False)
                return self.toggle_dark_mode(parsed_enable)

            if action_name == "send_email":
                attachments = _to_str_list(action.get("attachments"))
                attachments.extend(_to_str_list(action.get("attachment")))
                file_hint = str(action.get("file_hint", "")).strip()
                if file_hint:
                    attachments.append(file_hint)

                return self.send_email(
                    to_email=str(action.get("to") or action.get("to_email") or ""),
                    subject=str(action.get("subject", "")),
                    body=str(action.get("body") or action.get("message") or ""),
                    provider=str(action.get("provider", "gmail")),
                    attachments=attachments,
                    roots=roots,
                    smtp_host=str(action.get("smtp_host", "")),
                    smtp_port=int(action.get("smtp_port", 587) or 587),
                    smtp_user=str(action.get("smtp_user", "")),
                    smtp_password=str(action.get("smtp_password", "")),
                    from_email=str(action.get("from") or action.get("from_email") or ""),
                    send_now=_to_bool(action.get("send_now", False), default=False),
                    use_outlook_desktop=_to_bool(action.get("use_outlook_desktop", True), default=True),
                )

            if action_name == "draft_email_attachment":
                return self.draft_email_attachment(
                    to_email=str(action.get("to") or action.get("to_email") or ""),
                    subject=str(action.get("subject", "")),
                    body=str(action.get("body", "")),
                    file_hint=str(action.get("file_hint", "")),
                    roots=roots,
                    smtp_host=str(action.get("smtp_host", "")),
                    smtp_port=int(action.get("smtp_port", 587) or 587),
                    smtp_user=str(action.get("smtp_user", "")),
                    smtp_password=str(action.get("smtp_password", "")),
                    from_email=str(action.get("from") or action.get("from_email") or ""),
                    send_now=_to_bool(action.get("send_now", False), default=False),
                )

            if action_name == "send_telegram":
                return self.send_telegram(
                    bot_token=str(action.get("bot_token") or action.get("token") or ""),
                    chat_id=str(action.get("chat_id") or action.get("to") or ""),
                    message=str(action.get("message") or action.get("text") or ""),
                    file_path=str(action.get("file_path") or action.get("path") or ""),
                    file_hint=str(action.get("file_hint") or action.get("attachment") or ""),
                    roots=roots,
                    disable_web_preview=_to_bool(action.get("disable_web_preview", False), default=False),
                )

            if action_name == "send_whatsapp":
                return self.send_whatsapp(
                    to_number=str(action.get("to") or action.get("to_number") or ""),
                    message=str(action.get("message") or action.get("text") or ""),
                    use_twilio=_to_bool(action.get("use_twilio", False), default=False),
                    twilio_account_sid=str(action.get("twilio_account_sid") or action.get("account_sid") or ""),
                    twilio_auth_token=str(action.get("twilio_auth_token") or action.get("auth_token") or ""),
                    twilio_from=str(action.get("twilio_from") or action.get("from") or ""),
                    media_url=str(action.get("media_url", "")),
                )

            if action_name == "online_query":
                return self.connectivity.online_query(
                    query=str(action.get("query", "")),
                    max_results=int(action.get("max_results", 5) or 5),
                )

            return _fail("Unknown tool action.", f"unsupported_action: {action_name}")
        except Exception as exc:
            return _fail("Tool dispatcher failed.", str(exc))


def search_files_by_hint(
    hint: str,
    roots: Optional[List[str]] = None,
    max_results: int = 20,
) -> Dict[str, Any]:
    return _TASK_BRIDGE.search_files_by_hint(hint=hint, roots=roots, max_results=max_results)


def read_file_text(file_path: str, max_chars: int = 12000) -> Dict[str, Any]:
    return _TASK_BRIDGE.read_file_text(file_path=file_path, max_chars=max_chars)


def open_file(file_path: str) -> Dict[str, Any]:
    return _TASK_BRIDGE.open_file(file_path=file_path)


def move_mouse(x: int, y: int, duration: float = 0.2) -> Dict[str, Any]:
    return _TASK_BRIDGE.move_mouse(x=x, y=y, duration=duration)


def click(button: str = "left", clicks: int = 1) -> Dict[str, Any]:
    return _TASK_BRIDGE.click(button=button, clicks=clicks)


def type_text(text: str, interval: float = 0.01) -> Dict[str, Any]:
    return _TASK_BRIDGE.type_text(text=text, interval=interval)


def press_key(key: str) -> Dict[str, Any]:
    return _TASK_BRIDGE.press_key(key=key)


def hotkey(keys: List[str]) -> Dict[str, Any]:
    return _TASK_BRIDGE.hotkey(keys=keys)


_TASK_BRIDGE = UnifiedTaskBridge()


def run_tool_action(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dispatches tool actions produced by the agent.

    Expected schema example:
    {
        "action": "search_file",
        "hint": "report"
    }
    """
    return _TASK_BRIDGE.execute_action(action)
