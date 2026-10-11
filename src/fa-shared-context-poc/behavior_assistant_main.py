"""Explicit Hosted entry; legacy M2/M3/M4 defaults remain unchanged.

Deploy with importlib.import_module("behavior_assistant_main").main(). Preserve
normal module identity for Pydantic annotations; do not execute via runpy.
This entry never bootstraps authority records or extends RuntimePolicy deadlines.
"""

import os

from shared_context.behavior_assistant_hosted import AssistantHostedRuntime
from shared_context.behavior_desktop_view import install_desktop_view
from shared_context.behavior_hosted import create_host
from shared_context.behavior_process_hardening import disable_dumpable


def create_assistant_host():
    factory = None
    if os.environ.get("FA_RESPONSES_ENABLED") == "1" and not os.environ.get("FA_N6_RESPONSES_PROBE"):
        from shared_context.native_responses import native_responses_handler_factory

        factory = native_responses_handler_factory
    return create_host(AssistantHostedRuntime.from_environment(), responses_handler_factory=factory)


def main():
    # Same-UID desktop or tool children must not read this process's environment or memory.
    disable_dumpable()
    app = create_assistant_host()
    install_desktop_view(app)
    app.run()


if __name__ == "__main__":
    main()
