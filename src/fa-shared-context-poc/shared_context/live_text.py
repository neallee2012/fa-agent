"""Live text: the display-only preview of the reply while the SDK streams it (RTT B).

Not the 2026-10-05 saved "reply preview" (``native_progress.save_preview``), which is written once before the SDK stops.

Copilot SDK ``assistant.message_delta`` events are ephemeral and never persisted to the session log
(https://github.com/github/copilot-sdk/blob/f13e4a2cc7e4e220974d2333142234e162a3252e/docs/features/streaming-events.md),
so reading them changes neither the checkpoint nor the resident SDK. Only the parent agent's user-facing messages
are previewed: the phase comes from ``assistant.message_start`` (a delta carries none) and must be ``None`` (chat
completions) or ``final_answer``. Reasoning events, sub-agent events and any other or unseen phase are never shown.
The text gets the same exact-secret ``redact`` as the saved reply; a tail that could still become a known secret
is held back until it can be decided. A preview is never a result: the saved reply replaces it.
"""

import logging

LIVE_PHASES = (None, "final_answer")
MAX_OPEN_MESSAGES = 64


def _secret_prefix(text, secrets):
    """Length of the longest end of ``text`` that is a proper start of any secret (0 when none)."""
    held = 0
    for secret in secrets:
        if not secret:
            continue
        tail = text[-(len(secret) - 1):] if len(secret) > 1 else ""
        start = tail.find(secret[0])
        while start != -1:
            if secret.startswith(tail[start:]):
                held = max(held, len(tail) - start)
                break
            start = tail.find(secret[0], start + 1)
    return held


class LiveText:
    def __init__(self, *, redact, secrets, sdk_session_id):
        self.redact, self.secrets, self.sdk_session_id = redact, secrets, sdk_session_id
        self.messages = {}
        self.shown = False

    def observe(self, value):
        """Return the new display text for one redacted SDK event value, or None."""
        kind = value.get("type")
        if kind not in {"assistant.message_start", "assistant.message_delta", "assistant.message"}:
            return None
        if value.get("agent_id") not in (None, self.sdk_session_id):
            return None
        data = value.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("message_id"), str):
            return None
        message = data["message_id"]
        if kind == "assistant.message_start":
            if data.get("phase") in LIVE_PHASES and message not in self.messages:
                if len(self.messages) >= MAX_OPEN_MESSAGES:
                    return None
                self.messages[message] = {"raw": "", "sent": ""}
            return None
        state = self.messages.get(message)
        if state is None:
            return None
        if kind == "assistant.message_delta":
            delta = data.get("delta_content")
            if not isinstance(delta, str) or not delta:
                return None
            state["raw"] += delta
            safe = self.redact(state["raw"])
            safe = safe[:len(safe) - _secret_prefix(safe, tuple(self.secrets()))]
        else:
            del self.messages[message]
            content = data.get("content")
            if data.get("phase") not in LIVE_PHASES or not isinstance(content, str):
                return None
            safe = self.redact(content)
        if not safe.startswith(state["sent"]):
            # The complete message differs from what was streamed; stop this message's preview, never rewrite it.
            self.messages.pop(message, None)
            logging.getLogger(__name__).warning("FA_LIVE_TEXT_STOPPED reason=diverged")
            return None
        text = safe[len(state["sent"]):]
        state["sent"] = safe
        if not text:
            return None
        if not state.get("started"):
            state["started"] = True
            if self.shown:
                text = "\n\n" + text
            self.shown = True
        return text
