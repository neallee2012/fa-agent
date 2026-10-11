"""SDK-owned message queueing with a local send/idle fence, not another model or tool loop."""

import asyncio
import logging

from .errors import Problem


class SdkSteeringChannel:
    def __init__(self, stop_requested):
        self.ready = False
        self.closed = False
        self.stop_requested = stop_requested
        self._session = None
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._idle = False
        self._pending = set()
        self.delivered = {}
        self._submitted = {}

    def observe(self, event):
        from copilot.session_events import AssistantTurnStartData, SessionIdleData, SessionMode, UserMessageData

        if isinstance(event.data, UserMessageData):
            value = event.data
            if value.message_id is not None and value.delivery is not None:
                self.delivered[value.message_id] = value.delivery.value
                self._pending.discard(value.message_id)
            self._idle = False
        elif isinstance(event.data, AssistantTurnStartData):
            self._idle = False
        elif isinstance(event.data, SessionIdleData) and event.data.mode != SessionMode.AUTOPILOT:
            self._idle = True
        else:
            return
        self._changed.set()

    def open(self, session):
        self._session = session
        self.ready = True
        self._changed.set()

    async def send(self, content):
        async with self._lock:
            if not self.ready or self.closed or self.stop_requested():
                return None
            self._idle = False
            try:
                # Official queueing: the original work finishes its turn first, then the SDK takes this as the next
                # round. Steering (`immediate`) let a mid-tool-loop append replace the unfinished work (stage A).
                identifier = await self._session.send(content, mode="enqueue")
            except Exception as error:
                logging.getLogger(__name__).warning("NATIVE_APPEND_UNKNOWN exception_type=%s", type(error).__name__)
                raise Problem(503, "NATIVE_APPEND_UNKNOWN", "read the original append; the SDK handoff is unconfirmed") from None
            if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
                logging.getLogger(__name__).warning("NATIVE_APPEND_UNKNOWN reason=invalid_message_id")
                raise Problem(503, "NATIVE_APPEND_UNKNOWN", "the SDK did not return an original message ID")
            if identifier not in self.delivered:
                self._pending.add(identifier)
            self._submitted[identifier] = content
            self._changed.set()
            return identifier

    def human_inputs(self):
        """Only SDK-observed original appends, as request context; never an approval decision."""
        return tuple(content for identifier, content in self._submitted.items() if identifier in self.delivered)

    async def wait_idle(self):
        while True:
            await self._changed.wait()
            self._changed.clear()
            async with self._lock:
                if self._idle and not self._pending:
                    self.close()
                    return

    def close(self):
        self.closed = True
        self.ready = False
        self._changed.set()
