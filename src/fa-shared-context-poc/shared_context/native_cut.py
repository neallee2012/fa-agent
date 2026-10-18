"""ADR-0009 M1 2d-i step 2: the API's background cut driver.

A turn end (``NativeTurnGateway._accounted`` sees a completed or unknown receipt) hands the Agent to an in-process
worker; the request that saw it reads, writes and waits for nothing more. The worker runs ``NativeSessions.cut`` at
most once at a time per Agent and logs one ``NATIVE_UPGRADE_CUT_API`` line. A busy store waits for the next turn end:
there is no timer and no poll. Off unless ``FA_NATIVE_SESSION_GUARD_ENFORCE=1`` (2d-i's one switch, Q9), so nothing
changes until P9 turns it on. SDK-independent: the API never imports the Hosted runtime.
"""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import logging
from threading import Event, Lock

from .errors import Problem

log = logging.getLogger(__name__)
# A cut is a few platform reads and session calls, a files fence at most ``CUT_FENCE_SECONDS``.
_WORKERS = 4


class CutDriver:
    def __init__(self, composition):
        self.composition = composition
        self._pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="fa-native-cut")
        self._lock, self._stop = Lock(), Event()
        # Agents with an attempt queued or running, and the RID that last triggered each: polls of the same terminal
        # receipt trigger once.
        self._running, self._seen = set(), {}

    def turn_ended(self, control, agent_id, request_id):
        """Schedule one attempt for ``agent_id``; False when one is already queued or running, or this RID already
        triggered one. Never reads or blocks."""
        with self._lock:
            if self._stop.is_set() or agent_id in self._running or self._seen.get(agent_id) == request_id:
                return False
            self._running.add(agent_id)
            self._seen[agent_id] = request_id
        scheduled = False
        try:
            self._pool.submit(copy_context().run, self._attempt, control, agent_id)
            scheduled = True
        except RuntimeError:  # shut down meanwhile
            return False
        finally:
            if not scheduled:
                with self._lock:
                    self._running.discard(agent_id)
                    if self._seen.get(agent_id) == request_id:
                        del self._seen[agent_id]  # never scheduled: the same RID may trigger again
        return True

    def _attempt(self, control, agent_id):
        from .behavior_native_sessions import _FenceOverrun, native_sessions
        from .behavior_native_turn_api import agent_client

        outcome, reason, fence = "error", None, None
        try:
            client = agent_client(self.composition, control)
            if client.target.agent_id != agent_id:
                outcome = "other_agent"
            else:
                outcome, reason, fence = native_sessions(self.composition).cut(client, stop=self._stop)
        except _FenceOverrun:  # the new destination was not ready within the cut's bound, or the API is stopping
            outcome, reason = "destination_unready", "shutdown" if self._stop.is_set() else "fence_overrun"
        except Problem as error:
            reason = error.body["error"]["code"]
        except Exception as error:  # noqa: BLE001 - a background attempt only logs; the next turn end tries again
            reason = type(error).__name__
        finally:
            with self._lock:
                self._running.discard(agent_id)
        log.warning("NATIVE_UPGRADE_CUT_API outcome=%s reason=%s fence_ms=%s agent_id=%s", outcome, reason,
                    None if fence is None else round(fence * 1000), agent_id)
        return outcome

    def close(self):
        """Lifespan end: a running files fence stops at its next step. Before FA's stop of the source is marked it is
        withdrawn; after it the pending stays, never withdrawn, for the next attempt to drive to its commit. Queued
        attempts are dropped."""
        self._stop.set()
        self._pool.shutdown(wait=True, cancel_futures=True)


def native_cut_driver(composition):
    value = getattr(composition, "_native_cut", None)
    if value is None:
        value = composition.__dict__.setdefault("_native_cut", CutDriver(composition))
    return value


def turn_ended(composition, control, receipt):
    """The turn-end hook: an in-memory flag check, then a hand-off (no Table, platform or session call here). It never
    raises into the user's turn: a failure only logs, and the next turn end tries again."""
    from .behavior_native_sessions import native_sessions

    try:
        if native_sessions(composition).guard.enforce:
            native_cut_driver(composition).turn_ended(control, receipt.agent_id, receipt.request_id)
    except Exception as error:  # noqa: BLE001 - see the docstring
        log.warning("NATIVE_UPGRADE_CUT_HOOK_FAILED code=%s agent_id=%s", type(error).__name__, receipt.agent_id)


def fence_command(composition, agent_id, *, clear=False):
    """Operator CLI (``agent-versions fence``, ADR-0009 2d-i P9 gate): one Agent's binding row as the fence policy sees
    it: its current SID, the files fence it records (``upgrade``: destination, RID, start, FA's stop marker) and the
    durable bounds running for it. Inspecting reads the row only (no session or platform call). ``clear`` runs the
    operator path (``NativeSessions.clear_fence``) once and reports the outcome and the row after it."""
    from .behavior_native_sessions import native_sessions
    from .behavior_team_registry import resolve_agent_scope
    from .native_agent_api import NativeAgentRelayGateway
    from .native_binding import binding_store

    scope = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
    peer, client = NativeAgentRelayGateway(composition, enabled=True)._client(scope, prepared=False)
    store = binding_store(peer)

    def view(row):
        return None if row is None else {
            "session_id": row.hosted_session_id, "version": row.observed.agent_version,
            "fence": None if row.upgrade is None else row.upgrade.model_dump(mode="json"),
            "clocks": [clock.model_dump(mode="json") for clock in row.clocks],
            "abandon": None if row.abandon is None else row.abandon.model_dump(mode="json")}

    result = {"agent_id": agent_id, "row": view(store.read(agent_id))}
    if clear:
        result["outcome"] = native_sessions(peer).clear_fence(client)
        result["after"] = view(store.read(agent_id))
        log.warning("NATIVE_UPGRADE_FENCE_OPERATOR agent_id=%s outcome=%s", agent_id, result["outcome"])
    return result


def close_cut_drivers(composition):
    for owner in (composition, *(getattr(composition, "_peers", None) or {}).values()):
        driver = getattr(owner, "_native_cut", None)
        if driver is not None:
            driver.close()
