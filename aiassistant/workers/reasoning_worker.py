import json
import threading
import uuid

import requests
from PyQt5.QtCore import QThread, pyqtSignal


class ReasoningStreamWorker(QThread):
    """Streams LLM output from the reasoning server without blocking the UI thread."""

    token_received = pyqtSignal(str)
    sentence_ready = pyqtSignal(str)
    completed = pyqtSignal(str, str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    stream_started = pyqtSignal(str)

    def __init__(self, stream_url, stop_url, payload, timeout_sec=140):
        super().__init__()
        self.stream_url = stream_url
        self.stop_url = stop_url
        self.payload = dict(payload or {})
        self.timeout_sec = timeout_sec
        self.request_id = self.payload.get("request_id") or str(uuid.uuid4())
        self.payload["request_id"] = self.request_id
        self._cancel_event = threading.Event()
        self._active_response = None
        self._last_sentiment = "neutral"

    def cancel(self):
        self._cancel_event.set()

        try:
            requests.post(
                self.stop_url,
                json={"request_id": self.request_id},
                timeout=2,
            )
        except Exception:
            pass

        try:
            if self._active_response is not None:
                self._active_response.close()
        except Exception:
            pass

    def run(self):
        full_text = ""
        self.stream_started.emit(self.request_id)

        try:
            response = requests.post(
                self.stream_url,
                json=self.payload,
                stream=True,
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
            self._active_response = response

            for raw_line in response.iter_lines(decode_unicode=True):
                if self._cancel_event.is_set():
                    self.cancelled.emit()
                    return

                if not raw_line:
                    continue

                try:
                    packet = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                packet_type = packet.get("type")
                content = packet.get("content", "")

                if packet_type == "token":
                    full_text += content
                    self.token_received.emit(content)
                elif packet_type == "sentence":
                    if content:
                        self.sentence_ready.emit(content)
                elif packet_type == "done":
                    response_text = packet.get("full_response", full_text)
                    self._last_sentiment = packet.get("sentiment") or self._last_sentiment
                    self.completed.emit(response_text, self._last_sentiment)
                    return
                elif packet_type == "error":
                    self.failed.emit(content or "Reasoning stream failed.")
                    return

            # If server closes stream cleanly without explicit done packet.
            if self._cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.completed.emit(full_text, self._last_sentiment)

        except Exception as e:
            if self._cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.failed.emit(str(e))
        finally:
            self._active_response = None
