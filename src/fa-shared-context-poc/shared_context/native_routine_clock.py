"""API-resident minute clock for owner-approved native routines (FA_NATIVE_ROUTINE_CLOCK=api; default off).

The behavior API already runs one always-on replica, so it ticks approved routines itself instead of a separate
Container Apps Job, managed identity or Foundry routine (Foundry routine creator dispatch accepts only a human
delegated token; dev probe 2026-10-07). Every occurrence still goes through NativeRoutineService.due/trigger:
the RID is fixed by tenant/Team/Agent/routine/scheduled UTC, the intent is a CAS row and Hosted N1 creates or
reads `rid:<RID>`, so a restart or an overlapping revision cannot start a second turn.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import time

from .behavior_team_registry import registered_agent_ids
from .errors import Problem
from .native_routine_auth import NativeRoutineApiClock
from .native_routine_wire import NativeRoutineTrigger

TICK_SECONDS = 60
# Wake just after each minute boundary so an HH:MM occurrence is sent within about one tick.
TICK_OFFSET_SECONDS = 2
# Stop starting new occurrences after this; the rest are due again on the next tick.
TICK_BUDGET_SECONDS = 45
_SETTLED = frozenset({"completed", "unknown", "busy_refused"})
_LOG = logging.getLogger(__name__)


def _code(error):
    return error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__


class NativeRoutineClock:
    def __init__(self, service, *, interval=TICK_SECONDS, offset=TICK_OFFSET_SECONDS, budget=TICK_BUDGET_SECONDS,
                 monotonic=time.monotonic, wall=time.time):
        self.service, self.owner = service, service.owner
        self.principal = NativeRoutineApiClock(service.owner.verifier.auth.tenant_id)
        self.interval, self.offset, self.budget = interval, offset, budget
        self.monotonic, self.wall = monotonic, wall
        self._executor = self._task = None
        self._stopping = False
        # Last logged value per agent / occurrence / issue; a steady state is logged once, not every minute.
        self._notes = {}

    def _note(self, key, value, message, *args, settled=False):
        if self._notes.get(key) != value:
            _LOG.warning(message, *args)
        if settled or value is None:
            self._notes.pop(key, None)
        else:
            self._notes[key] = value

    def agent_ids(self):
        config, store = self.owner.config, self.owner.runtime.store
        configured = tuple(target.agent_id for target in config.targets)
        return tuple(dict.fromkeys((*configured, *registered_agent_ids(store, config))))

    def tick(self):
        """One bounded pass over every served Agent; one failure never blocks another Agent or occurrence."""
        deadline = self.monotonic() + self.budget
        counts = {"agents": 0, "triggered": 0, "refused": 0, "deferred": 0, "agent_failures": 0}
        try:
            agents = self.agent_ids()
        except Exception as error:
            _LOG.warning("FA_NATIVE_ROUTINE_CLOCK_ROSTER exception_type=%s", type(error).__name__)
            agents = tuple(dict.fromkeys(target.agent_id for target in self.owner.config.targets))
        for agent_id in agents:
            if self._stopping or self.monotonic() >= deadline:
                counts["deferred"] += 1
                continue
            counts["agents"] += 1
            try:
                self._visit(agent_id, deadline, counts)
            except Exception as error:
                counts["agent_failures"] += 1
                code = _code(error)
                self._note(("agent", agent_id), code, "FA_NATIVE_ROUTINE_CLOCK_AGENT agent=%s code=%s",
                           agent_id[:8], code)
            else:
                if self._notes.get(("agent", agent_id)) is not None:
                    self._note(("agent", agent_id), None, "FA_NATIVE_ROUTINE_CLOCK_AGENT agent=%s code=ok", agent_id[:8])
        return counts

    def _visit(self, agent_id, deadline, counts):
        after = None
        while True:
            page = self.service.due(self.principal, agent_id, after)
            for issue in page.issues:
                self._note(("issue", agent_id, issue.routine_id), issue.code,
                           "FA_NATIVE_ROUTINE_CLOCK_ISSUE agent=%s routine=%s code=%s",
                           agent_id[:8], issue.routine_id[:16], issue.code)
            for item in page.occurrences:
                if self._stopping or self.monotonic() >= deadline:
                    counts["deferred"] += 1
                    continue
                key = ("rid", item.request_id)
                try:
                    receipt = self.service.trigger(self.principal, agent_id, NativeRoutineTrigger(
                        routine_id=item.routine_id, scheduled_for=item.scheduled_for))
                except Exception as error:
                    counts["refused"] += 1
                    code = _code(error)
                    self._note(key, code, "FA_NATIVE_ROUTINE_CLOCK_REFUSED agent=%s rid=%s code=%s",
                               agent_id[:8], item.request_id[:8], code)
                    continue
                counts["triggered"] += 1
                self._note(key, receipt.status, "FA_NATIVE_ROUTINE_CLOCK_DISPATCH agent=%s rid=%s status=%s",
                           agent_id[:8], receipt.request_id[:8], receipt.status, settled=receipt.status in _SETTLED)
            if (page.next_after is None or after is not None and page.next_after <= after
                    or self._stopping or self.monotonic() >= deadline):
                return
            after = page.next_after

    def _delay(self):
        return self.interval - self.wall() % self.interval + self.offset

    async def _run(self):
        loop = asyncio.get_running_loop()
        while not self._stopping:
            try:
                # Its own single worker: a tick never uses request threads and never overlaps another tick.
                counts = await loop.run_in_executor(self._executor, self.tick)
                if counts["triggered"] or counts["refused"] or counts["deferred"]:
                    _LOG.info("FA_NATIVE_ROUTINE_CLOCK_TICK %s", " ".join(f"{k}={v}" for k, v in counts.items()))
            except Exception as error:
                _LOG.error("FA_NATIVE_ROUTINE_CLOCK_TICK_FAILED exception_type=%s", type(error).__name__)
            await asyncio.sleep(self._delay())

    def start(self):
        """First tick runs at once: the latest due occurrence missed during a restart is sent with its fixed RID."""
        if self._task is not None:
            return
        self._stopping = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fa-routine-clock")
        self._task = asyncio.get_running_loop().create_task(self._run())
        _LOG.warning("FA_NATIVE_ROUTINE_CLOCK_STARTED interval=%s budget=%s", self.interval, self.budget)

    async def stop(self):
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        executor, self._executor = self._executor, None
        if executor is not None:
            # An in-flight trigger finishes on its own; its RID is reconciled by the next process.
            executor.shutdown(wait=False, cancel_futures=True)
