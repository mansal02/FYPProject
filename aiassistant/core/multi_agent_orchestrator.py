import ollama

from aiassistant.infra.config.app_config import CONFIG


MODEL = CONFIG["ollama"]["model"]


def _ask_agent(system_prompt, user_prompt):
    response = ollama.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "temperature": 0.25,
            "num_predict": int(CONFIG["ollama"].get("num_predict", 180)),
            "num_ctx": int(CONFIG["ollama"].get("num_ctx", 2048)),
        },
        stream=False,
        keep_alive=0,
    )
    return (response.get("message") or {}).get("content", "").strip()


def run_multi_agent_round(question, memory_context=""):
    """Runs a simple local 3-agent cycle: researcher -> coder -> synthesizer."""
    context_block = f"\n\nContext:\n{memory_context}" if memory_context else ""

    researcher_output = _ask_agent(
        "You are a Researcher Agent. Return only concise findings needed to solve the request. No tutorial tone.",
        f"Question: {question}{context_block}",
    )

    coder_output = _ask_agent(
        "You are a Coder Agent. Return implementation actions and checks only, concise and practical.",
        f"Question: {question}\nResearcher notes:\n{researcher_output}{context_block}",
    )

    final_output = _ask_agent(
        "You are the Final Synthesizer Agent. Return one concise, execution-first answer with only what the user needs.",
        (
            f"Question: {question}\n\n"
            f"Researcher Agent:\n{researcher_output}\n\n"
            f"Coder Agent:\n{coder_output}\n\n"
            "Return only the final answer. Keep it short and direct. Explain details only if explicitly requested."
        ),
    )

    return {
        "researcher": researcher_output,
        "coder": coder_output,
        "final": final_output,
    }
