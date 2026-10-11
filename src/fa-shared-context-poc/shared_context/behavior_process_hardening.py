"""Keep the Hosted main process's environment and memory from same-UID children.

Desktop apps run as the Hosted UID, so a file dialog or browser could otherwise open
``/proc/<main pid>/environ`` or ``mem``. A non-dumpable process makes those files
root-owned; same-UID readers get EACCES.

Linux resets the flag to dumpable on every ordinary exec, so call ``disable_dumpable`` in
the final Hosted process after bootstrap's exec. Children that exec (the SDK runtime, GUI
apps) become dumpable again and must keep receiving only allowlisted environments. The
hardened process itself can no longer read its own ``/proc/self/environ``; ``os.environ``
is unaffected.
"""

from __future__ import annotations

import ctypes
import os
import sys

PR_GET_DUMPABLE, PR_SET_DUMPABLE = 3, 4


def _prctl():
    function = ctypes.CDLL(None, use_errno=True).prctl
    function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    function.restype = ctypes.c_int
    return function


def is_dumpable() -> bool | None:
    """Current flag on Linux; None where the flag does not exist."""
    if not sys.platform.startswith("linux"):
        return None
    value = _prctl()(PR_GET_DUMPABLE, 0, 0, 0, 0)
    if value < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return value != 0


def disable_dumpable() -> bool:
    """Make this process non-dumpable; False only on platforms without the flag.

    A Linux failure raises, so callers that need the protection fail closed.
    """
    if not sys.platform.startswith("linux"):
        return False
    if _prctl()(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if is_dumpable() is not False:
        raise OSError("process is still dumpable after PR_SET_DUMPABLE")
    return True
