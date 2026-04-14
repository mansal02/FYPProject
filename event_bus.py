import threading
from collections import defaultdict


class EventBus:
    """Simple thread-safe publish/subscribe event bus."""

    def __init__(self):
        self._handlers = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event_name, callback):
        with self._lock:
            self._handlers[event_name].append(callback)

    def unsubscribe(self, event_name, callback):
        with self._lock:
            callbacks = self._handlers.get(event_name, [])
            if callback in callbacks:
                callbacks.remove(callback)

    def emit(self, event_name, payload=None):
        with self._lock:
            callbacks = list(self._handlers.get(event_name, []))

        for callback in callbacks:
            try:
                callback(payload)
            except Exception as e:
                print(f"[EVENT BUS] Handler error for {event_name}: {e}")


class Events:
    USER_SPOKE = "user_spoke"
    AI_TOKEN = "ai_token"
    AI_SENTENCE_READY = "ai_sentence_ready"
    AI_COMPLETED = "ai_completed"
    AUDIO_READY = "audio_ready"
    BARGE_IN = "barge_in"
    ERROR = "error"
