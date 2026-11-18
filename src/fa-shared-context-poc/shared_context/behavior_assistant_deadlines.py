"""Assistant-only call lifetimes; cancellation is not thread/provider stop."""
import asyncio
from contextvars import ContextVar, copy_context
from concurrent.futures import ThreadPoolExecutor



SERVER_MODEL_SECONDS = 75.0
CLIENT_MODEL_SECONDS = 90.0
SDK_TURN_SECONDS = 240.0
_server_call = ContextVar("assistant_server_model_call", default=None)
_hosted_turn = ContextVar("assistant_hosted_turn", default=None)


def check_server_model():
    call = _server_call.get()
    if call is not None:
        call.check()


def check_hosted_turn():
    call = _hosted_turn.get()
    if call is not None:
        call.check()


async def hosted_sync(function, *args, **kwargs):
    # asyncio.run joins its default executor. An abandoned credential/HTTP wait
    # must not delay the turn's return, nor be mistaken for a stopped thread.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fa-assistant-call")
    context = copy_context()
    def invoke():
        check_hosted_turn()
        return function(*args, **kwargs)
    try:
        return await asyncio.get_running_loop().run_in_executor(executor, context.run, invoke)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


async def model_sync(function, *args, **kwargs):
    """Abandon the wait, never reuse a late result as fresh dispatch permission."""
    def invoke():
        check_server_model()
        return function(*args, **kwargs)
    check_server_model()
    result = await asyncio.to_thread(invoke)
    check_server_model()
    return result
