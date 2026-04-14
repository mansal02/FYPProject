import re

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def drain_complete_sentences(buffer_text):
    """Returns (complete_sentences, leftover_fragment)."""
    if not buffer_text:
        return [], ""

    parts = _SENTENCE_SPLIT_RE.split(buffer_text)
    if len(parts) <= 1:
        return [], buffer_text

    complete = [p.strip() for p in parts[:-1] if p and p.strip()]
    leftover = parts[-1] if parts else ""
    return complete, leftover
