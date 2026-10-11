"""Read scheduled originals and annotate existing attention; no producer or approval writer."""

from shared_context.native_routine_wire import NativeReminderQuery, NativeRoutineIssue, NativeWorkReminders, native_routines_enabled
from .adapter import ConsoleError, PullAdapter, require
from .native import _decode


class NativeRoutineControl:
    def __init__(self, native):
        self.native, self.console = native, native.console
        self.enabled = native_routines_enabled()
        self.adapter = PullAdapter(self.console.config, self.console.backend)
        self.reminders, self.errors, self.issues = {}, {}, {}

    def refresh(self, thread, token):
        if not self.enabled:
            return
        try:
            cid = self.native._cid(thread)
            if cid is None:
                self.reminders.pop(thread, None)
                self.issues.pop(thread, None)
                return
            known = [value for rid, value in self.native.receipts.items()
                     if self.native.request_threads.get(rid) == thread and value.conversation_id == cid]
            agents = {value.agent_id for value in known}
            require(len(agents) == 1, "NATIVE_STATE_SCOPE", 502)
            agent_id = next(iter(agents))
            after, reminders, issues = None, [], []
            while True:
                query = NativeReminderQuery(conversation_id=cid, after=after)
                raw, headers = self.adapter.send(
                    "POST", "/native-routines/v1/agents/" + agent_id + "/reminders",
                    query.model_dump(mode="json"), token, limit=512 * 1024)
                self.native._current(token)
                require(headers.get("cache-control") == "no-store", "NATIVE_STATE_INVALID", 502)
                page = _decode(NativeWorkReminders, raw)
                issues.extend(page.issues)
                entries = (*page.reminders, *page.routine_turns)
                require(all(item.conversation_id == cid for item in entries), "NATIVE_STATE_SCOPE", 502)
                require(all(item.agent_id == agent_id for item in page.reminders), "NATIVE_STATE_SCOPE", 502)
                originals = {}
                for rid in {item.request_id for item in entries}:
                    try:
                        value = self.native._receipt(rid, token)
                    except ConsoleError as error:
                        if error.code != "NATIVE_RID_MISSING":
                            raise
                        reference = next(item for item in entries if item.request_id == rid)
                        issues.append(NativeRoutineIssue(
                            routine_id=reference.routine_id, request_id=rid, code="NATIVE_RID_MISSING"))
                        self.native.errors[rid] = error.code
                        continue
                    require((value.agent_id, value.conversation_id) == (agent_id, cid), "NATIVE_STATE_SCOPE", 502)
                    originals[rid] = self.native._remember(thread, value)
                    if self.native.errors.get(rid) == "NATIVE_RID_MISSING":
                        self.native.errors.pop(rid)
                for item in page.routine_turns:
                    value = originals.get(item.request_id)
                    if value is None:
                        continue
                    require(value.routine is not None and (
                        value.routine.occurrence.routine_id, value.routine.occurrence.scheduled_for
                    ) == (item.routine_id, item.scheduled_for), "NATIVE_STATE_SCOPE", 502)
                for item in page.reminders:
                    original = originals.get(item.request_id)
                    if original is None:
                        continue
                    if (not original.cancel_requested and original.failure_code != "NATIVE_TURN_CANCELLED"
                            and original.status not in {"completed", "busy_refused"}):
                        reminders.append(item.model_copy(update={
                            "status": original.status, "failure_code": original.failure_code}))
                if page.next_after is None:
                    break
                require(entries and (after is None or page.next_after > after), "NATIVE_STATE_INVALID", 502)
                after = page.next_after
            self.reminders[thread] = reminders
            self.issues[thread] = issues
            self.errors.pop(thread, None)
        except ConsoleError as error:
            self.reminders.pop(thread, None)
            self.issues.pop(thread, None)
            self.errors[thread] = error.code
            if error.status in {401, 403} or error.upstream_status in {401, 403}:
                raise

    def view(self, thread):
        authenticated = self.console.vault.state() == "AVAILABLE"
        return {
            "enabled": self.enabled,
            "reminders": [item.model_dump(mode="json") for item in self.reminders.get(thread, ())] if authenticated else [],
            "issues": [item.model_dump(mode="json") for item in self.issues.get(thread, ())] if authenticated else [],
            "error": {"code": self.errors[thread]} if authenticated and thread in self.errors else None,
        }

    def attention(self, entries):
        if not self.enabled or self.console.vault.state() != "AVAILABLE":
            return entries
        result = []
        for entry in entries:
            item = entry["item"]
            reference = item.get("approval") or item.get("question") or {}
            call = reference.get("call", {})
            reminder = next((value for value in self.reminders.get(entry["thread_id"], ())
                             if (value.agent_id, value.conversation_id, value.request_id) == (
                                 call.get("agent_id"), call.get("conversation_id"), call.get("request_id"))), None)
            result.append(entry if reminder is None else {
                **entry, "reminder": reminder.model_dump(mode="json")})
        return result
