"""Read the current approved routine version through the existing Agent-authenticated API client."""

from .errors import Problem
from .native_capability_tools import NativeCapabilityClient
from .native_routine_wire import NativeReminderQuery, NativeRoutineCheck, NativeRoutineCurrent, NativeWorkReminders


class NativeRoutineClient(NativeCapabilityClient):
    async def reminders(self, conversation_id, after=None):
        value = await self._post(
            "/routine/reminders", NativeReminderQuery(conversation_id=conversation_id, after=after), NativeWorkReminders)
        if (any(item.conversation_id != conversation_id for item in (*value.reminders, *value.routine_turns))
                or value.next_after is not None and after is not None and value.next_after <= after):
            raise Problem(403, "NATIVE_ROUTINE_SCOPE", "the reminders belong to another conversation or cursor")
        return value

    async def check(self, source):
        query = NativeRoutineCheck(
            request_id=source.request_id, conversation_id=source.conversation_id,
            definition_sha256=source.occurrence.definition_sha256)
        try:
            value = await self._post("/routine/check", query, NativeRoutineCurrent)
        except Problem as error:
            refused = error.status in {403, 404, 409, 410, 422}
            raise Problem(
                403 if refused else 503,
                "NATIVE_ROUTINE_INACTIVE" if refused else "NATIVE_ROUTINE_UNCONFIRMED",
                "the original routine is inactive or its entry could not be checked; retain the original RID") from None
        if value.model_dump(include=set(NativeRoutineCheck.model_fields)) != query.model_dump():
            raise Problem(502, "NATIVE_ROUTINE_UNCONFIRMED", "the current routine reply names another original")
        return value
