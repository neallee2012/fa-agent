"""The stop shared by the API lifespan's background loops; stdlib only, so any image can import it."""

import asyncio


class BackgroundLoop:
    """One asyncio task over one single-thread executor; the owner sets ``_stopping``, ``_task`` and ``_executor``."""

    async def stop(self):
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        executor, self._executor = self._executor, None
        if executor is not None:
            # An in-flight run finishes on its own; a routine trigger's RID is reconciled by the next process.
            executor.shutdown(wait=False, cancel_futures=True)
