"""ADR-0005 on the native lane: one Agent VM keeps up to two released SDKs between turns.

The SDK's own guidance keeps one long-lived CLI server per user and does not stop it after each task
(``docs/features/session-persistence.md``, "Pattern 1: one CLI server per user"); FA has one Agent per VM.
A kept SDK continues a conversation only when its files are exactly the checkpoint the next turn was given;
anything else stops it and restores from that checkpoint as before. Every turn of this VM runs one at a time
under ``execution_slot``, on the agent server's single event loop.
"""
from collections import OrderedDict
import asyncio
import logging

from .backup_models import MAX_BACKUP_BYTES
from .behavior_sdk_files import LARGE_RESULT_RESERVE_BYTES
from .errors import Problem
from .m2_copilot import ResidentHolder

log = logging.getLogger(__name__)
LIVE_LIMIT = 2


class NativeResidentPool:
    def __init__(self, limit=LIVE_LIMIT):
        self.limit = limit
        self._holders = OrderedDict()  # sdk_session_id -> ResidentHolder, least recently used first
        self._stopping = set()

    def holder(self, sdk_session_id):
        """This conversation's slot, now the most recently used; slots without a live SDK are dropped."""
        holder = self._holders.pop(sdk_session_id, None) or ResidentHolder()
        for key in [key for key, value in self._holders.items() if value.live is None]:
            del self._holders[key]
        self._holders[sdk_session_id] = holder
        return holder

    async def continue_live(self, holder, previous, *, checkpoint_profile, model_provider, model, needed_bytes=0):
        """The live files this turn continues on, or None after stopping a kept SDK that no longer matches.

        ``model`` is this turn's deployment: a kept SDK runs only the model it was started with, and its live files
        keep that model's private events, so a model switch restores the published checkpoint cold instead
        (never the SDK's experimental ``set_model``). ``needed_bytes`` is room this turn reserves up front (its
        attached images' base64)."""
        live = holder.live
        if live is None:
            self._evict(holder, room=1)
            return None
        files = live.files
        if previous is None or live.released_sha256 != previous.sha256:
            reason = "checkpoint_changed"
        elif not live.alive():
            reason = "exited"
        elif not live.reusable():
            reason = "files_moved"
            # Only the fixed code and a hash prefix: never file names or content.
            log.warning("RESIDENT_FILES_MOVED released=%s", live.released_sha256[:12])
        elif (files.checkpoint_profile, files.model_provider) != (checkpoint_profile, model_provider):
            reason = "settings_changed"
        elif live.model != model:
            reason = "model_changed"
        elif (size := sum(len(text.encode("utf-8")) for text in files.files.values())) > MAX_BACKUP_BYTES // 2 or (
                size + needed_bytes + LARGE_RESULT_RESERVE_BYTES > MAX_BACKUP_BYTES):
            # Live files keep the private events a checkpoint drops; a large state restores the smaller copy.
            reason = "state_size"
        else:
            try:
                files.continue_from(previous)
            except Problem:
                # A runtime filesystem call failed while idle: restore the published checkpoint instead.
                reason = "files_unusable"
            else:
                log.info("RESIDENT_SDK_CONTINUED released=%s", live.released_sha256[:12])
                return files
        log.warning("RESIDENT_SDK_DISCARDED reason=%s", reason)
        await self.discard(holder)
        self._evict(holder, room=1)
        return None

    def released(self, holder, state, files_digest):
        """The turn's saved state is the release, with the files it was taken from; older kept SDKs over the
        limit stop."""
        holder.live.mark_released(state.sha256, files_digest)
        self._evict(holder, room=0)

    def _evict(self, current, *, room):
        kept = [holder for holder in self._holders.values() if holder.live is not None and holder is not current]
        over = len(kept) + (1 if current.live is not None else room) - self.limit
        for holder in kept[:max(0, over)]:
            # Turns run one at a time, so another conversation's SDK is idle and bound to no turn here.
            if holder.live.turn is None:
                self._stop_later(holder)

    def _stop_later(self, holder):
        live, holder.live = holder.live, None
        # Warning like FA_TIMING: that conversation's next turn starts its SDK cold.
        log.warning("RESIDENT_SDK_EVICTED")
        return self._track(live)

    def _track(self, live):
        """The stop runs as its own task, so a cancelled caller never leaves the CLI process running."""
        task = asyncio.ensure_future(self._stop(live))
        self._stopping.add(task)
        task.add_done_callback(self._stopping.discard)
        return task

    @staticmethod
    async def _stop(live):
        from .behavior_timing import phase_timing
        try:
            with phase_timing("resident_sdk_stop"):
                await live.stop()
        except Exception:
            log.warning("RESIDENT_SDK_STOP_UNCONFIRMED")

    async def discard(self, holder):
        live, holder.live = holder.live, None
        if live is not None:
            await asyncio.shield(self._track(live))

    async def aclose(self):
        """Best effort at VM shutdown: every published checkpoint is already saved.

        A turn still bound to its SDK stops it itself on the way out."""
        for holder in list(self._holders.values()):
            if holder.live is not None and holder.live.turn is None:
                self._stop_later(holder)
        if self._stopping:
            await asyncio.gather(*self._stopping, return_exceptions=True)
