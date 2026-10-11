"""Typing prewarm (README#typing-prewarm): while the owner types, ask the API to wake this Agent's Hosted VM.

Off unless ``FA_CONSOLE_PREWARM=1``. Then the bridge flips one line of the page (like ``realtime.page``) and forwards
``POST /api/instructions/prewarm`` to the API's ``/work-policy/v1/native-turns/prewarm``. It is only a hint: no
journal entry, message lock or local state, and the page ignores the answer, so a send never waits for it.
"""

import os

from .adapter import require, strict_json
from .native import PREFIX

ENV = "FA_CONSOLE_PREWARM"
PAGE_MARKER = "const PREWARM_CONFIGURED=false;"
API_PATH = PREFIX + "/prewarm"
MESSAGES = {"PREWARM_DISABLED": "這個工作台沒有開啟打字預熱。", "PREWARM_INVALID": "工作台頁面的打字預熱設定不符。"}


def configured(environ=None):
    """Exactly ``1`` turns it on; anything else (unset, ``0``, ``true``) leaves it off."""
    return (os.environ if environ is None else environ).get(ENV) == "1"


def page(html, enabled):
    require(html.count(PAGE_MARKER) == 1, "PREWARM_INVALID", 500)
    return html.replace(PAGE_MARKER, "const PREWARM_CONFIGURED=true;") if enabled else html


def forward(console, enabled):
    require(enabled, "PREWARM_DISABLED", 404)
    require(console.native_instructions is not None, "ACTION_NOT_ALLOWED", 403)
    console.active()
    from shared_context.behavior_native_turn_wire import NativePrewarmResult

    raw, _ = console.native_instructions.adapter.send("POST", API_PATH, {}, console.vault.get())
    try:
        strict_json(raw)
        value = NativePrewarmResult.model_validate_json(raw)
    except (ValueError, TypeError):
        # Nothing depends on the answer; an unexpected shape is just "not confirmed".
        return {"prewarm": "unconfirmed"}
    return {"prewarm": value.state}
