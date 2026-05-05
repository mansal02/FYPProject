"""
Optional CrewAI adapter.

If CrewAI is unavailable or not configured, this module falls back to the
existing local multi-agent chain.
"""

from __future__ import annotations

from typing import Dict, Optional

from aiassistant.core.multi_agent_orchestrator import run_multi_agent_round


def _normalize_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"assist", "replace"}:
        return "assist"
    return mode


def _truncate_context(text: str, max_chars: int) -> str:
    if not text:
        return ""
    limit = max(200, int(max_chars))
    if len(text) <= limit:
        return text
    return text[-limit:]


def run_crew_assist(
    question: str,
    memory_context: str = "",
    config: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Run CrewAI if available, otherwise fallback to local multi-agent chain."""
    settings = config or {}
    provider = str(settings.get("provider", "fallback")).strip().lower()
    context_max_chars = int(settings.get("context_max_chars", 900) or 900)
    memory_context = _truncate_context(str(memory_context or ""), context_max_chars)

    if provider not in {"crewai", "fallback"}:
        provider = "fallback"

    if provider == "crewai":
        try:
            from crewai import Agent, Crew, Process, Task  # type: ignore
        except Exception as exc:
            return {
                "ok": False,
                "provider": "crewai",
                "mode": _normalize_mode(settings.get("mode", "assist")),
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
        )
        implementer = Agent(
            role="Implementer",
            goal="Translate research into concrete implementation notes.",
            backstory="You output practical steps and checks only.",
            allow_delegation=False,
        )
        synthesizer = Agent(
            role="Synthesizer",
            goal="Deliver a final, direct answer for the user.",
            backstory="You keep responses short, accurate, and outcome-focused.",
            allow_delegation=False,
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
            verbose=bool(settings.get("verbose", False)),
        )

        try:
            result = crew.kickoff()
        except Exception as exc:
            return {
                "ok": False,
                "provider": "crewai",
                "mode": _normalize_mode(settings.get("mode", "assist")),
                "error": f"CrewAI kickoff failed: {exc}",
            }

        return {
            "ok": True,
            "provider": "crewai",
            "mode": _normalize_mode(settings.get("mode", "assist")),
            "summary": str(result or "").strip(),
        }

    result = run_multi_agent_round(question, memory_context=memory_context or "")
    summary = str(result.get("final") or "").strip()
    return {
        "ok": True,
        "provider": "fallback",
        "mode": _normalize_mode(settings.get("mode", "assist")),
        "summary": summary,
        "details": result,
    }
