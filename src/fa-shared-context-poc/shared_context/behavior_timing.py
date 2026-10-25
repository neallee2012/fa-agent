"""Content-free phase timings; elapsed intervals never imply business success."""
from asyncio import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
from threading import Lock
from time import perf_counter

_timing_work = ContextVar("behavior_timing_work", default=None)
_table_counts = ContextVar("behavior_timing_table_counts", default=())
# Routes RouteTiming logs: the original Hosted->API job routes, plus the native turn routes an owner turn crosses
# (submit, reads, skill catalog, usage report, memory relay) so API, Hosted and platform times line up per turn.
TIMED_ROUTES = ("/assistant/", "/hosted/", "/work-policy/v1/native-turns/", "/work-policy/v1/responses-turns/",
                "/work-policy/v1/native-capabilities/", "/work-policy/v1/turn-usage",
                "/work-policy/v1/agent-template", "/team-data/v1/memory/")


def timing_id(rid):
    """Correlates one turn's FA_TIMING phases (API and Hosted) without logging its RID."""
    return hashlib.sha256(rid.encode("utf-8")).hexdigest()[:12]


class _TableCounts:
    """Physical Table GETs and the sequential rounds they took; numbers only, never keys."""

    def __init__(self):
        self.lock, self.gets, self.batches = Lock(), 0, 0


def count_table_reads(gets):
    """One sequential round of ``gets`` physical point reads, counted for every open phase."""
    for counts in _table_counts.get():
        with counts.lock:
            counts.gets += gets
            counts.batches += 1


def elapsed_timing(phase, started, *, job_id=None, outcome="returned", table=None):
    logging.getLogger(__name__).warning("FA_TIMING %s", json.dumps({
        "phase": phase, "job_id": job_id or _timing_work.get(),
        "elapsed_ms": round((perf_counter() - started) * 1000, 3), "outcome": outcome,
        **({} if table is None else {"table_gets": table.gets, "batches": table.batches}),
    }, separators=(",", ":")))


def current_work():
    """The enclosing ``work_timing`` ID, kept by work that later runs on another task (a model stream)."""
    return _timing_work.get()


@contextmanager
def work_timing(job_id):
    token = _timing_work.set(job_id)
    try:
        yield
    finally:
        _timing_work.reset(token)


@contextmanager
def phase_timing(phase, *, job_id=None):
    started = perf_counter()
    outcome = "failed"
    table = _TableCounts()
    token = _table_counts.set((*_table_counts.get(), table))
    try:
        yield
        outcome = "returned"
    except CancelledError:
        outcome = "cancelled"  # stopped by its caller (e.g. an early read the turn no longer needs), not a failure
        raise
    finally:
        _table_counts.reset(token)
        elapsed_timing(phase,started,job_id=job_id,outcome=outcome,table=table)


def _route_name(template):
    """The last fixed segment of the route template (``/requests/{request_id}`` -> ``requests``); never a value."""
    fixed = [segment for segment in (template or "").split("/") if segment and not segment.startswith("{")]
    return fixed[-1] if fixed else "unmatched"


class RouteTiming:
    """ASGI timing for Hosted->API and native turn calls: route template, binding or hashed turn ID (from the path,
    else the body's validated ``request_id``), counts only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if not any(route in path for route in TIMED_ROUTES):
            return await self.app(scope, receive, send)
        started, table, outcome = perf_counter(), _TableCounts(), "failed"
        token = _table_counts.set((*_table_counts.get(), table))
        try:
            await self.app(scope, receive, send)
            outcome = "returned"
        finally:
            _table_counts.reset(token)
            params = scope.get("path_params") or {}
            # A body-only route (native capabilities, submit): the RID ``_body`` validated, hashed like a path RID.
            rid = params.get("request_id") or (scope.get("state") or {}).get("behavior_request_id")
            elapsed_timing("api_" + _route_name(getattr(scope.get("route"), "path", None)), started,
                           job_id=params.get("binding_id") or (timing_id(rid) if isinstance(rid, str) else None),
                           outcome=outcome, table=table)
