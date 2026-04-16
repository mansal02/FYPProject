"""
Crash-safe OS tools for local autonomous actions.

Every public function returns a dict with at least:
- success: bool
- message: user-friendly status
- data or error: details for the agent

This contract prevents tool failures from crashing the main loop.
"""

from __future__ import annotations

import mimetypes
import os
import re
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

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

try:
    from sentence_transformers import SentenceTransformer
except Exception:  # pragma: no cover - optional dependency
    SentenceTransformer = None

try:
    from duckduckgo_search import DDGS
except Exception:  # pragma: no cover - optional dependency
    DDGS = None

try:
    import win32com.client as win32  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    win32 = None


def _ok(message: str, data: Any = None) -> Dict[str, Any]:
    return {"success": True, "message": message, "data": data}


def _fail(message: str, error: str) -> Dict[str, Any]:
    return {"success": False, "message": message, "error": error}


def default_search_roots() -> List[Path]:
    """Conservative roots for local search to reduce unnecessary disk scanning."""
    roots = [Path.cwd(), Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"]
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
        if SentenceTransformer is None or np is None:
            return None
        if self._semantic_model is None:
            # CPU pin avoids stealing VRAM from Ollama on low-end GPUs.
            self._semantic_model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
        return self._semantic_model

    @staticmethod
    def _iter_candidate_files(search_roots: List[Path], max_files: int = 1400) -> List[Path]:
        files: List[Path] = []
        for root in search_roots:
            try:
                if not root.exists():
                    continue
                for path in root.rglob("*"):
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
        if self._is_vague_hint(hint):
            return self.semantic_search_files(hint, roots=roots, max_results=max_results)
        return self.lexical_search_files(hint, roots=roots, max_results=max_results)

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

    def open_file(self, file_path: str) -> Dict[str, Any]:
        try:
            path = Path(file_path).expanduser().resolve()
            if not path.exists():
                return _fail(
                    "Cannot open file because it does not exist.",
                    f"missing_file: {path}",
                )

            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif os.name == "posix":
                subprocess.Popen(["xdg-open", str(path)])
            else:
                subprocess.Popen(["open", str(path)])

            return _ok(f"Opened file: {path.name}", data=str(path))
        except Exception as exc:
            return _fail("Failed to open file.", str(exc))

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
        matches = self.search_files_by_hint(file_hint, roots=roots, max_results=5)
        data = matches.get("data") if isinstance(matches, dict) else []
        if not data:
            return _fail("No file found to attach.", f"no_attachment_match: {file_hint}")

        attachment_path = str(data[0])
        if os.name == "nt" and win32 is not None:
            try:
                outlook = win32.Dispatch("Outlook.Application")
                mail = outlook.CreateItem(0)
                mail.To = (to_email or "").strip()
                mail.Subject = (subject or "").strip()
                mail.Body = (body or "").strip()
                mail.Attachments.Add(attachment_path)
                mail.Save()
                return _ok(
                    "Outlook draft created with attachment.",
                    data={"attachment": attachment_path},
                )
            except Exception as exc:
                return _fail("Outlook draft creation failed.", str(exc))

        # SMTP fallback path: optionally send directly, otherwise create local .eml draft.
        try:
            msg = EmailMessage()
            sender = (from_email or smtp_user or "").strip()
            if sender:
                msg["From"] = sender
            msg["To"] = (to_email or "").strip()
            msg["Subject"] = (subject or "").strip()
            msg.set_content((body or "").strip())

            file_path = Path(attachment_path)
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if not mime_type:
                mime_type = "application/octet-stream"
            maintype, subtype = mime_type.split("/", 1)
            msg.add_attachment(
                file_path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=file_path.name,
            )

            if send_now and smtp_host.strip() and sender:
                with smtplib.SMTP(smtp_host.strip(), int(smtp_port), timeout=15) as smtp:
                    smtp.starttls()
                    if smtp_user.strip() and smtp_password:
                        smtp.login(smtp_user.strip(), smtp_password)
                    smtp.send_message(msg)
                return _ok(
                    "Email sent with attachment via SMTP.",
                    data={"attachment": attachment_path, "smtp_host": smtp_host.strip()},
                )

            draft_dir = Path.cwd() / "cache"
            draft_dir.mkdir(parents=True, exist_ok=True)
            draft_path = draft_dir / f"email_draft_{int(Path(attachment_path).stat().st_mtime)}.eml"
            draft_path.write_bytes(bytes(msg))
            return _ok(
                "Email draft file created with attachment.",
                data={"draft": str(draft_path.resolve()), "attachment": attachment_path},
            )
        except Exception as exc:
            return _fail("Email draft fallback failed.", str(exc))

    def execute_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        try:
            action_name = str(action.get("action", "")).strip().lower()

            if action_name == "search_file":
                return self.search_files_by_hint(
                    hint=str(action.get("hint", "")),
                    roots=action.get("roots"),
                    max_results=int(action.get("max_results", 20)),
                )

            if action_name == "semantic_search_file":
                return self.semantic_search_files(
                    hint=str(action.get("hint", "")),
                    roots=action.get("roots"),
                    max_results=int(action.get("max_results", 20)),
                )

            if action_name == "read_file":
                return self.read_file_text(
                    file_path=str(action.get("path", "")),
                    max_chars=int(action.get("max_chars", 12000)),
                )

            if action_name == "open_file":
                return self.open_file(str(action.get("path", "")))

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
                keys = action.get("keys", [])
                if not isinstance(keys, list):
                    return _fail("Hotkey keys must be a list.", "invalid_hotkey_payload")
                return self.hotkey([str(k) for k in keys])

            if action_name == "run_command":
                return self.run_system_command(
                    command=str(action.get("command", "")),
                    timeout=int(action.get("timeout", 25)),
                )

            if action_name == "toggle_dark_mode":
                enable = action.get("enable")
                parsed_enable = None if enable is None else bool(enable)
                return self.toggle_dark_mode(parsed_enable)

            if action_name == "draft_email_attachment":
                return self.draft_email_attachment(
                    to_email=str(action.get("to", "")),
                    subject=str(action.get("subject", "")),
                    body=str(action.get("body", "")),
                    file_hint=str(action.get("file_hint", "")),
                    roots=action.get("roots"),
                    smtp_host=str(action.get("smtp_host", "")),
                    smtp_port=int(action.get("smtp_port", 587)),
                    smtp_user=str(action.get("smtp_user", "")),
                    smtp_password=str(action.get("smtp_password", "")),
                    from_email=str(action.get("from", "")),
                    send_now=bool(action.get("send_now", False)),
                )

            if action_name == "online_query":
                return self.connectivity.online_query(
                    query=str(action.get("query", "")),
                    max_results=int(action.get("max_results", 5)),
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
