"""ADR-0009 §12 phase 6a: the API's backup upgrade trigger, (a) and (b) only. Off unless both
``FA_NATIVE_UPGRADE_RECONCILER=1`` and ``FA_NATIVE_SESSION_GUARD_ENFORCE=1`` (the cut worker is 2d-i's): no task at all.

A turn end is the main trigger of an Agent's switch (``native_cut``). Every 15 minutes plus jitter this pass reads the
binding row of every served Agent and hands two kinds to that same background cut worker, nothing more:
(a) a row whose recorded profile is not supported, or whose abandon decision is not carried out yet (no platform read);
(b) a supported row whose V_rec is not the selected version and whose current SID reads ``active`` on ``get_session``:
a turn end the API missed, e.g. a restart mid-turn. The selector is read first, so a row with nothing due (``cut``'s
``current``) costs no ``get_session``; ``cut`` never wakes an ``idle`` source. A compatible ``idle`` or retired SID is
left as it is: it moves at its next turn end (Neal decision 5), and the daily patrol decides retired ones. Here only the
selector and ``get_session`` are read: never an invoke, a Session Files call or a stop.
Replicas need no lease (every cut step is re-entrant and CAS-guarded). The binding row has no attempt field until
phase 4b (which also brings (c) and (a)'s abandon decision), so the dedupe is in process: an Agent handed is not handed
again for ``RECONCILE_RETRY_SECONDS``, and one with nothing due, left alone or whose selected version is not ready is not read again for
``RECONCILE_BACKOFF_SECONDS`` while its binding row stays the same. A restart forgets both: its first pass reads all.
"""

import logging
import os

from .errors import Problem
from .native_binding import binding_store, session_guard_enforced
from .native_session_patrol import PATROL_SECONDS, NativeSessionPatrol, _code

RECONCILER_ENV = "FA_NATIVE_UPGRADE_RECONCILER"
RECONCILE_SECONDS = 15 * 60
RECONCILE_JITTER_SECONDS = 5 * 60
# An Agent this process handed is not handed again sooner (a busy or idle source answers the same until a turn ends).
RECONCILE_RETRY_SECONDS = 3600
# A row with nothing due or left alone is read again after the patrol's day, or as soon as the row changes.
RECONCILE_BACKOFF_SECONDS = PATROL_SECONDS
# Shorter than the interval, so a slow pass leaves room for the next one.
RECONCILE_BUDGET_SECONDS = 600
_LOG = logging.getLogger(__name__)


def reconciler_enabled(environ=None) -> bool:
    """``FA_NATIVE_UPGRADE_RECONCILER=1``; any other value than ``0``/``1`` keeps it off and is logged."""
    value = (os.environ if environ is None else environ).get(RECONCILER_ENV)
    if value not in (None, "", "0", "1"):
        _LOG.warning("NATIVE_UPGRADE_RECONCILER_SETTING_INVALID enabled=False")
    return value == "1"


class NativeUpgradeReconciler(NativeSessionPatrol):
    """The patrol's roster, pacing, budget, hand-off, loop and lifespan; its own pass and schedule."""
    name, thread = "NATIVE_UPGRADE_RECONCILER", "fa-upgrade-reconciler"

    def __init__(self, composition, *, enabled=None, interval=RECONCILE_SECONDS, jitter=RECONCILE_JITTER_SECONDS,
                 retry=RECONCILE_RETRY_SECONDS, backoff=RECONCILE_BACKOFF_SECONDS, budget=RECONCILE_BUDGET_SECONDS,
                 **options):
        if enabled is None:
            enabled = reconciler_enabled() and session_guard_enforced()
        # Never at the start itself, and jittered, so two replicas rarely read together.
        super().__init__(composition, enabled=enabled, interval=interval, jitter=jitter, budget=budget,
                         first=(interval, interval + jitter), **options)
        self.retry, self.backoff = retry, backoff
        # agent ID -> (this process's clock at its last outcome, the binding row then, how long that outcome stands)
        self._attempted = {}

    def selected(self, source):
        """The selected version of ``source``'s Agent, read as ``cut`` reads it first; paced like ``status``."""
        from .behavior_agent_versions import SelectorUnreadable, current_hosted_version

        self.sleep(self.pace)
        try:
            return current_hosted_version(self.composition.runtime, source.target)
        except SelectorUnreadable:  # 429, 5xx or a timeout: counts toward stopping the pass, as a metadata read does
            # Only a get_session answer resets the count: a readable selector says nothing about session reads.
            self._failures += 1
            raise

    def _run(self):
        from .behavior_agent_versions import SelectorUnreadable

        counts = dict.fromkeys(("agents", "unbound", "recent", "incompatible", "current", "active", "left", "unread",
                                "handed", "deferred"), 0)
        store, started = binding_store(self.composition), self.clock()
        served = self.agents()
        if self._resume in served:  # the first Agent a stopped pass did not reach goes first
            start = served.index(self._resume)
            served = served[start:] + served[:start]
        self._resume, self._failures = None, 0
        # An outcome past its window, or of an Agent no longer served, is forgotten (an expired one would be read anyway).
        self._attempted = {agent_id: last for agent_id, last in self._attempted.items()
                           if agent_id in served and started - last[0] < last[2]}
        for index, agent_id in enumerate(served):
            reason = self._halted(started)
            if reason is not None:
                self._resume, counts["deferred"] = agent_id, len(served) - index
                _LOG.warning("NATIVE_UPGRADE_RECONCILER_STOPPED reason=%s deferred=%s", reason, counts["deferred"])
                break
            counts["agents"] += 1
            try:
                row = store.read(agent_id)
                if row is None:
                    counts["unbound"] += 1
                    continue
                last = self._attempted.get(agent_id)
                if last is not None and last[1] == row and self.clock() - last[0] < last[2]:
                    counts["recent"] += 1
                    continue
                if row.abandon is None and row.supported():
                    source = self.source(agent_id, row)
                    try:
                        chosen = self.selected(source)
                    except SelectorUnreadable:
                        raise
                    except Problem as error:  # e.g. the selected version is not ready: it stays so until the row moves
                        kind = "unread"
                        _LOG.warning("NATIVE_UPGRADE_RECONCILER_UNREAD agent=%s code=%s", agent_id[:8], _code(error))
                    else:
                        if chosen == row.observed.agent_version:
                            kind = "current"  # nothing due: ``cut`` would answer ``current``
                        else:
                            kind = "active" if self.status(source) == "active" else "left"  # (b), or left alone
                else:
                    # (a). shortcut: ``cut`` carries a written decision out but makes none, so an unsupported row
                    # nobody decided yet comes back "unbound"; the coordinator keeps it that way until phase 4b.
                    kind = "incompatible"
            except Exception as error:  # noqa: BLE001 - 5xx, timeout, a bad registration, an unread selector: next pass
                counts["unread"] += 1
                _LOG.warning("NATIVE_UPGRADE_RECONCILER_UNREAD agent=%s code=%s", agent_id[:8], _code(error))
                continue
            counts[kind] += 1
            handed = kind in ("active", "incompatible")
            self._attempted[agent_id] = (self.clock(), row, self.retry if handed else self.backoff)
            if handed:
                self._hand(agent_id, counts)  # a failure is handed again after the retry window
        return counts
