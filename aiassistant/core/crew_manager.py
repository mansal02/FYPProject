"""CrewAI manager that routes tasks across multiple Ollama models."""

from __future__ import annotations

import os
from typing import Dict, Optional

from aiassistant.infra.config.app_config import CONFIG


def _normalize_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"assist", "replace"}:
        return "assist"
    return mode


def _normalize_router(value: str) -> str:
    router = str(value or "").strip().lower()
    if router not in {"always", "complex_only", "never"}:
        return "complex_only"
    return router


def _format_ollama_model(model: str) -> str:
    model_name = str(model or "").strip()
    if not model_name:
        return ""
    if "/" in model_name:
        return model_name
    return f"ollama/{model_name}"


def _strip_provider(model: str) -> str:
    clean = str(model or "").strip()
    if not clean:
        return ""
    if "/" not in clean:
        return clean
    return clean.split("/", 1)[-1]


class CrewManager:
    def __init__(self, settings: Optional[Dict[str, object]] = None) -> None:
        self.settings = settings or {}
        self.mode = _normalize_mode(self.settings.get("mode", "assist"))
        self.router = _normalize_router(self.settings.get("router", "complex_only"))
        self.verbose = bool(self.settings.get("verbose", False))
        self.base_model = str(CONFIG.get("ollama", {}).get("model", "qwen2.5-coder:7b")).strip()
        self.ollama_host = str(CONFIG.get("ollama", {}).get("host", "http://127.0.0.1:11434"))
        os.environ.setdefault("OLLAMA_HOST", self.ollama_host)
        self.available_models = self._list_ollama_models()
        self.models = self._resolve_models(self.settings)

    def _list_ollama_models(self) -> set:
        try:
            import ollama
        except Exception:
            return set()

        try:
            response = ollama.list()
            models = response.get("models", []) if isinstance(response, dict) else []
            names = {str(item.get("name", "")).strip() for item in models if item.get("name")}
            return {name for name in names if name}
        except Exception:
            return set()

    def _resolve_models(self, settings: Dict[str, object]) -> Dict[str, str]:
        defaults = {
            "researcher": self.base_model,
            "coder": self.base_model,
            "synthesizer": self.base_model,
        }
        models = settings.get("models")
        if isinstance(models, dict):
            for key in list(defaults.keys()):
                value = models.get(key)
                if value:
                    defaults[key] = str(value).strip()
        return {
            key: self._select_model(value)
            for key, value in defaults.items()
        }

    def _select_model(self, model_name: str) -> str:
        clean = _strip_provider(model_name)
        if not clean:
            return self.base_model
        if not self.available_models:
            return self.base_model
        if clean in self.available_models:
            return clean
        return self.base_model

    def run(self, question: str, memory_context: str = "") -> Dict[str, object]:
        try:
            from crewai import Agent, Crew, Process, Task, LLM  # type: ignore
        except Exception as exc:
            return {
                "ok": False,
                "provider": "crewai",
                "mode": self.mode,
                "error": f"CrewAI import failed: {exc}",
            }

        question_text = str(question or "").strip()
        memory_text = str(memory_context or "").strip()
        if memory_text:
            question_text = f"{question_text}\n\nContext:\n{memory_text}"

        researcher = Agent(
            role="Researcher",
            goal="Summarize key facts and constraints for the task.",
            backstory="You are a concise analyst focused on actionable points.",
            allow_delegation=False,
            llm=LLM(model=_format_ollama_model(self.models["researcher"])),
        )
        implementer = Agent(
            role="Implementer",
            goal="Translate research into concrete implementation notes.",
            backstory="You output practical steps and checks only.",
            allow_delegation=False,
            llm=LLM(model=_format_ollama_model(self.models["coder"])),
        )
        synthesizer = Agent(
            role="Synthesizer",
            goal="Deliver a final, direct answer for the user.",
            backstory="You keep responses short, accurate, and outcome-focused.",
            allow_delegation=False,
            llm=LLM(model=_format_ollama_model(self.models["synthesizer"])),
        )

        tasks = [
            Task(
                description=f"Question: {question_text}",
                expected_output="Short bullet list of key points.",
                agent=researcher,
            ),
            Task(
                description=(
                    "Use the research output to produce implementation notes. "
                    "Avoid tutorials; keep it concise."
                ),
                expected_output="Implementation notes and checks.",
                agent=implementer,
            ),
            Task(
                description=(
                    "Produce the final response for the user. "
                    "Keep it short and direct."
                ),
                expected_output="Final assistant response.",
                agent=synthesizer,
            ),
        ]

        crew = Crew(
            agents=[researcher, implementer, synthesizer],
            tasks=tasks,
            process=Process.sequential,
            verbose=self.verbose,
        )

        try:
            result = crew.kickoff()
        except Exception as exc:
            return {
                "ok": False,
                "provider": "crewai",
                "mode": self.mode,
                "error": f"CrewAI kickoff failed: {exc}",
            }

        return {
            "ok": True,
            "provider": "crewai",
            "mode": self.mode,
            "summary": str(result or "").strip(),
        }
