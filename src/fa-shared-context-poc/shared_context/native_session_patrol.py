"""ADR-0009 M1 2d-i daily patrol: ``get_session`` metadata only; never an invoke, a Session Files call or a stop.

An idle Agent has no turn end to start its switch, and the platform deletes a session after 30 days without use, so
once a day (plus jitter; the first pass 5-60 minutes after the API starts) the API reads the platform metadata of every served Agent's current SID that a v1 binding row
records (ADR M1 step 3 (b)). A SID read as 404 / ``failed`` / ``deleted`` / ``expired`` twice, at least
``RETIRED_CONFIRM_SECONDS`` and at most ``RETIRED_PAIR_SECONDS`` apart (§7a (m)), gets the §7a (g) abandon decision.
The patrol itself writes nothing else: a decided Agent (this run's, or one decided earlier and still not switched) is
handed to the background cut worker (P6, ADR M1 step 4(b) "背景工作…切走"), which carries the decision out to a fresh
session the way a turn end does, so an Agent whose owner never sends still leaves its retired SID. ``idle`` / ``active``
SIDs, v2 rows and retired Agents are left as they are (Neal decision 5); a failed read is read again the next day.
Reads are paced; a run that hits its time budget or repeated unavailable reads stops and defers the rest. Behind ``FA_NATIVE_SESSION_GUARD_ENFORCE`` (default off: no task at all).
"""

import asyncio
import itertools
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import logging
import random
from threading import Lock
import time

from .behavior_native_sessions import (
    _RETIRED, RETIRED_CONFIRM_SECONDS, RETIRED_PAIR_SECONDS, _MetadataUnconfirmed, _session_metadata, _target)
from .errors import Problem
from .native_binding import binding_store, session_guard_enforced
from .native_responses_wire import UNLABELED_STATE_PROFILE

PATROL_SECONDS = 24 * 3600
PATROL_JITTER_SECONDS = 3600
# The first pass after a start: dev restarts several times a day, so a first wait of a day would never end. Random, so
# a deploy's two replicas rarely read together (harmless if they do: metadata only, and the decision is a CAS).
PATROL_FIRST_SECONDS = (300, 3600)
# Before every platform read, so a run never bursts the project's read quota.
PATROL_PACE_SECONDS = 1
# A run stops starting reads after this long; the Agents it did not reach go first in the next run.
PATROL_BUDGET_SECONDS = 1800
# This many unavailable reads in a row (429 / 5xx / timeout) stop the run the same way.
PATROL_FAILURE_LIMIT = 3
_LOG = logging.getLogger(__name__)


def _code(error):
    return error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__


class NativeSessionPatrol:
    # The log prefix and thread name; the upgrade reconciler reuses this loop under its own.
    name, thread = "NATIVE_SESSION_PATROL", "fa-session-patrol"

    def __init__(self, composition, *, enabled=None, interval=PATROL_SECONDS, jitter=PATROL_JITTER_SECONDS,
                 confirm=RETIRED_CONFIRM_SECONDS, pace=PATROL_PACE_SECONDS, budget=PATROL_BUDGET_SECONDS,
                 sleep=time.sleep, clock=time.monotonic, switch=None, first=PATROL_FIRST_SECONDS):
        self.composition = composition
        self.enabled = session_guard_enforced() if enabled is None else enabled
        # A decided Agent's switch (``hand_off``: the background cut worker); True when it was handed.
        self.switch = self.hand_off if switch is None else switch
        self._hand_offs = itertools.count()
        self.interval, self.jitter, self.confirm, self.sleep, self.clock = interval, jitter, confirm, sleep, clock
        self.pace, self.budget, self.first = pace, budget, first
        # One API replica in dev/stage; this keeps two runs of one process from overlapping (a slow day, a test).
        self._running = Lock()
        self._executor = self._task = None
        self._stopping = False
        self._resume, self._failures = None, 0

    def agents(self):
        """Served Agent IDs: the configured ones, then the Team's registered, not retired ones (as the routine clock).
        Each is resolved in its own read (``source``), so one bad registration skips only that Agent."""
        from .behavior_team_registry import registered_agent_ids

        config = self.composition.config
        configured = (target.agent_id for target in config.targets)
        return tuple(dict.fromkeys((*configured, *registered_agent_ids(self.composition.runtime.store, config))))

    def source(self, agent_id, row):
        """The row's current SID as a Foundry client; building it calls nothing (as ``follow_agent``)."""
        from .behavior_team_registry import resolve_agent_scope
        from .native_agent_api import NativeAgentRelayGateway

        scope = resolve_agent_scope(self.composition.runtime.store, self.composition.config, agent_id)
        _, client = NativeAgentRelayGateway(self.composition, enabled=True)._client(scope, prepared=False)
        return client.for_target(_target(client.target, session_id=row.hosted_session_id,
                                         agent_version=row.observed.agent_version))

    def hand_off(self, agent_id):
        """The decided Agent goes to its composition's background cut worker (``native_cut``), which reads, creates
        and switches in its own thread; here only the hand-off happens. False when an attempt is already queued."""
        from .behavior_team_registry import resolve_agent_scope
        from .native_agent_api import NativeAgentRelayGateway
        from .native_cut import native_cut_driver

        scope = resolve_agent_scope(self.composition.runtime.store, self.composition.config, agent_id)
        peer, _ = NativeAgentRelayGateway(self.composition, enabled=True)._client(scope, prepared=False)
        # A new trigger ID per hand-off: an attempt that did not switch is tried again by the next run.
        return native_cut_driver(peer).turn_ended(peer.runtime.work_policy, agent_id,
                                                  "patrol-%d-%d" % (time.time_ns(), next(self._hand_offs)))

    def _hand(self, agent_id, counts):
        try:
            if self.switch(agent_id):
                counts["handed"] += 1
        except Exception as error:  # noqa: BLE001 - the next run hands it again
            _LOG.warning("%s_UNHANDED agent=%s code=%s", self.name, agent_id[:8], _code(error))

    def status(self, source):
        self.sleep(self.pace)
        try:
            metadata = _session_metadata(source)
        except _MetadataUnconfirmed:  # 429, 5xx or a timeout, after azure-core's own retries
            self._failures += 1
            raise
        self._failures = 0
        return "missing" if metadata is None else metadata.status

    def _halted(self, started):
        if self._stopping:
            return "stopping"
        if self._failures >= PATROL_FAILURE_LIMIT:
            return "unavailable"
        return "budget" if self.clock() - started > self.budget else None

    def run(self):
        """One pass; None when another pass of this process is still running."""
        if not self._running.acquire(blocking=False):
            _LOG.warning("%s overlap=skipped", self.name)
            return None
        try:
            counts = self._run()
        finally:
            self._running.release()
        _LOG.warning("%s %s", self.name, " ".join(f"{key}={value}" for key, value in counts.items()))
        return counts

    def _run(self):
        counts = dict.fromkeys(("agents", "unbound", "skipped", "idle_or_active", "unread",
                                "unconfirmed", "abandoned", "handed", "deferred"), 0)
        store, pending, started = binding_store(self.composition), deque(), self.clock()
        served = self.agents()
        if self._resume in served:  # the first Agent a stopped run did not reach goes first
            start = served.index(self._resume)
            served = served[start:] + served[:start]
        self._resume, self._failures = None, 0
        for index, agent_id in enumerate(served):
            # Confirm the first reads that are due before the next one, so a long pass never pushes a pair out of
            # its window (§7a (m)); first reads are in time order.
            while pending and self.clock() - pending[0][-1] >= self.confirm and not self._halted(started):
                self._confirm(store, pending.popleft(), counts)
            reason = self._halted(started)
            if reason is not None:
                self._resume, counts["deferred"] = agent_id, len(served) - index
                _LOG.warning("NATIVE_SESSION_PATROL_STOPPED reason=%s deferred=%s", reason, counts["deferred"])
                break
            counts["agents"] += 1
            try:
                row = store.read(agent_id)
                if row is None:
                    counts["unbound"] += 1
                    continue
                # A decided row is on its way out (handed to the cut worker again, unread); v2 rows run the new image.
                if row.abandon is not None or row.state_profile != UNLABELED_STATE_PROFILE:
                    counts["skipped"] += 1
                    if row.abandon is not None:
                        self._hand(agent_id, counts)
                    continue
                source = self.source(agent_id, row)
                status = self.status(source)
            except Exception as error:  # noqa: BLE001 - 5xx, timeout, 401/403, a bad registration: read again tomorrow
                counts["unread"] += 1
                _LOG.warning("NATIVE_SESSION_PATROL_UNREAD agent=%s code=%s", agent_id[:8], _code(error))
                continue
            if status in _RETIRED:
                pending.append((agent_id, row, source, status, self.clock()))
            else:
                counts["idle_or_active"] += 1
        while pending and self._halted(started) is None:
            self._confirm(store, pending.popleft(), counts)
        counts["unconfirmed"] += len(pending)
        return counts

    def _confirm(self, store, candidate, counts):
        """The second read of a SID first read as retired at ``at``, and the decision when it reads retired again
        at least ``confirm`` and at most ``RETIRED_PAIR_SECONDS`` later (the send path's window); else the next run
        starts a new pair."""
        agent_id, row, source, first, at = candidate
        try:
            if self.clock() - at > RETIRED_PAIR_SECONDS:
                counts["unconfirmed"] += 1
                return
            self.sleep(max(0, self.confirm - (self.clock() - at)))
            status = self.status(source)
            if status not in _RETIRED or self.clock() - at > RETIRED_PAIR_SECONDS or self._stopping:
                counts["unconfirmed"] += 1
                return
            # The row moved to another SID or was decided meanwhile: nothing is written over it.
            current = store.read(agent_id)
            if current is None or current.hosted_session_id != row.hosted_session_id or current.abandon is not None:
                counts["skipped"] += 1
                return
            # shortcut: decide_abandon returns a concurrent send-path decision like its own, so one landing between
            # this read and the write is counted and logged as this run's; tell them apart only if that matters.
            decided = store.decide_abandon(
                agent_id, hosted_session_id=row.hosted_session_id, basis="retired", source_status=status,
                created_agent_version=row.created_agent_version, state_profile=row.state_profile)
        except Exception as error:  # noqa: BLE001 - one Agent never blocks another
            counts["unread"] += 1
            _LOG.warning("NATIVE_SESSION_PATROL_UNREAD agent=%s code=%s", agent_id[:8], _code(error))
            return
        if decided is None:
            counts["skipped"] += 1
            return
        counts["abandoned"] += 1
        _LOG.warning("NATIVE_SESSION_PATROL_RETIRED agent=%s session_id=%s first=%s second=%s",
                     agent_id[:8], row.hosted_session_id, first, status)
        self._hand(agent_id, counts)

    def run_once(self):
        """Operator one-shot (``agent-versions patrol --once``): the daily pass now, with the same rules and flag. A
        decided Agent is not handed to a cut worker here (this process ends); the API's next pass or turn end hands it."""
        if not self.enabled:
            raise Problem(409, "NATIVE_SESSION_GUARD_DISABLED", "the patrol needs FA_NATIVE_SESSION_GUARD_ENFORCE=1")
        return self.run()

    async def _loop(self):
        loop = asyncio.get_running_loop()
        wait = random.uniform(*self.first)  # never at the start itself: a deploy adds no burst of reads
        while not self._stopping:
            await asyncio.sleep(wait)
            try:
                await loop.run_in_executor(self._executor, self.run)
            except Exception as error:  # noqa: BLE001 - the next day runs again
                _LOG.error("%s_FAILED code=%s", self.name, _code(error))
            wait = self.interval + random.uniform(0, self.jitter)

    def start(self):
        if not self.enabled or self._task is not None:
            return
        self._stopping = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self.thread)
        self._task = asyncio.get_running_loop().create_task(self._loop())
        _LOG.warning("%s_STARTED interval=%s jitter=%s", self.name, self.interval, self.jitter)

    async def stop(self):
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
