# One error type for tool failures, so the agent can catch it, retry once, then
# mark the gap instead of guessing (see README "Safety rules").
from __future__ import annotations


class ToolError(Exception):
    def __init__(self, message: str, *, source: str | None = None):
        super().__init__(message)
        self.message = message
        self.source = source
