"""Native routine owner management; due occurrences are sent only by the API's own default-off clock."""

import logging

from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _principal_dependency, _response
from .behavior_native_capability_wire import NATIVE_CAPABILITY_PREFIX
from .errors import Problem
from .native_routine_auth import routine_clock_enabled
from .native_routine_wire import (
    NATIVE_ROUTINE_ERRORS, NativeReminderQuery, NativeRoutineChange, NativeRoutineCheck, native_routines_enabled,
)
from .native_routines import NativeRoutineService

PREFIX = "/native-routines/v1/agents/{agent_id}"


async def _reply(call):
    try:
        value = await run_in_threadpool(call)
        return _response(value, max_bytes=512 * 1024)
    except Problem as error:
        if error.body["error"]["code"] not in NATIVE_ROUTINE_ERRORS:
            raise
        return JSONResponse(error.body, status_code=error.status, headers={"Cache-Control": "no-store"})


def install_native_routine_routes(app, composition, *, enabled):
    clock = routine_clock_enabled()
    if not native_routines_enabled():
        return
    service = NativeRoutineService(composition, enabled=enabled)
    if clock and service.owner is composition:
        if not enabled:
            logging.getLogger(__name__).warning("FA_NATIVE_ROUTINE_CLOCK_DISABLED reason=native_turns_off")
        else:
            from .native_routine_clock import NativeRoutineClock
            # The server lifespan starts and stops it; peer apps share this owner and never get a second clock.
            app.state.native_routine_clock = NativeRoutineClock(service)
    identity = _principal_dependency(composition.verifier)

    @app.post(NATIVE_CAPABILITY_PREFIX + "/routine/check")
    async def check(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRoutineCheck, max_body_bytes=1024)
        return await _reply(lambda: service.check(principal, command))

    @app.post(NATIVE_CAPABILITY_PREFIX + "/routine/reminders")
    async def agent_reminders(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeReminderQuery, max_body_bytes=1024)
        return await _reply(lambda: service.agent_reminders(principal, command))

    @app.post(PREFIX + "/reminders")
    async def reminders(request: Request, agent_id: str, principal=Depends(identity)):
        command = await _body(request, NativeReminderQuery, max_body_bytes=1024)
        return await _reply(lambda: service.reminders(principal, agent_id, command))

    @app.post(PREFIX + "/definitions")
    async def publish(request: Request, agent_id: str, principal=Depends(identity)):
        command = await _body(request, NativeRoutineChange, max_body_bytes=24 * 1024)
        return await _reply(lambda: service.publish(principal, agent_id, command))

    @app.get(PREFIX + "/definitions/{routine_id}")
    async def read(agent_id: str, routine_id: str, principal=Depends(identity)):
        return await _reply(lambda: service.read(principal, agent_id, routine_id))

    @app.get(PREFIX + "/definitions")
    async def listing(agent_id: str, after: str | None = Query(default=None, min_length=1, max_length=128),
                      principal=Depends(identity)):
        return await _reply(lambda: service.list(principal, agent_id, after))
