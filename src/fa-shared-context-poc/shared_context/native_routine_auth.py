"""Only the API's own in-process clock may trigger approved occurrences; never their owner or a request."""

from dataclasses import dataclass
import os

from .errors import Problem
from .native_routine_wire import native_routines_enabled

ROUTINE_CLOCK_ENV = "FA_NATIVE_ROUTINE_CLOCK"


def routine_clock_enabled(environ=None):
    """`FA_NATIVE_ROUTINE_CLOCK=api` lets the always-on API tick approved routines itself (default off)."""
    environ = os.environ if environ is None else environ
    value = environ.get(ROUTINE_CLOCK_ENV)
    if value not in (None, "0", "api"):
        raise Problem(503, "NATIVE_ROUTINES_DISABLED", "FA_NATIVE_ROUTINE_CLOCK must be api or 0")
    if value != "api":
        return False
    if not native_routines_enabled(environ):
        raise Problem(503, "NATIVE_ROUTINES_DISABLED", "the API clock also requires native routines to be enabled")
    return True


@dataclass(frozen=True)
class NativeRoutineApiClock:
    """The API process's own minute tick. Only built in-process; no request or token can produce it."""
    tenant_id: str
