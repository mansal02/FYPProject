"""Legacy compatibility for code that still imports index.get_marie_response_stream."""

from aiassistant.backend.server_reasoning import get_marie_response_stream


__all__ = ["get_marie_response_stream"]