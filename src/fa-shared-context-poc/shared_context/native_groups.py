"""Original group state on the existing Team repository; no execution queue or model loop."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .native_group_wire import (
    MAX_GROUP_POSTS, MAX_GROUP_ROUNDS, MAX_MEMBER_ROUND_POSTS, NativeGroupAdvance,
    NativeGroupDelivery, NativeGroupList, NativeGroupOpen, NativeGroupPost, NativeGroupPostReference, NativeGroupPostView,
    NativeGroupRoundReceipt, NativeGroupSignal, NativeGroupSignalReceipt, NativeGroupView,
)
from .source_materials import digest
from .storage import Repository, serialize

_DIGEST = TypeAdapter(Digest)


def _identifier(value):
    try:
        return _DIGEST.validate_python(value, strict=True)
    except ValueError:
        raise Problem(422, "NATIVE_GROUP_INVALID", "an original group or post identifier is required") from None


def group_post_id(group_id, speaker_agent_id, request_id):
    return digest({"kind": "fa-native-group-post-v1", "group_id": group_id,
                   "speaker_agent_id": speaker_agent_id, "request_id": request_id})


def _unconfirmed(error):
    """A conflicting or failed write may still have landed, so its outcome is read back."""
    return error.status in {409, 412} or error.status >= 500


def _find_post(posts, post_id):
    return next((post for post in posts if post.post_id == post_id), None)


def _check_speaker(view, speaker_agent_id, command):
    if speaker_agent_id not in view.member_agent_ids:
        raise Problem(403, "NATIVE_GROUP_MEMBER_REQUIRED", "only an original group member may publish")
    if command.recipient_agent_id is not None and command.recipient_agent_id not in view.member_agent_ids:
        raise Problem(403, "NATIVE_GROUP_MEMBER_REQUIRED", "the published addressee must be a group member")
    if ((command.turn is None) != (command.result is None) and command.kind != "blocked"
            or command.turn is not None and command.turn.agent_id != speaker_agent_id):
        raise Problem(409, "NATIVE_GROUP_CONFLICT", "a saved result must retain its original speaking Agent")


def _check_open_round(view, speaker_agent_id, command):
    if view.state != "open":
        raise Problem(409, "NATIVE_GROUP_CLOSED", "the original group is already closed")
    if command.round != view.round:
        raise Problem(409, "NATIVE_GROUP_ROUND_CHANGED", "read the original group's current round")
    if command.kind == "final" and speaker_agent_id != view.orchestrator_agent_id:
        raise Problem(403, "NATIVE_GROUP_ORCHESTRATOR_REQUIRED", "only the original orchestrator may conclude")


def _settle(pending, settles, speaker_agent_id, command):
    """Remove and return the awaiting delivery this post answers, if it names one."""
    if settles is None:
        return None
    completed = next((item for item in pending if item.message_id == settles), None)
    if (completed is None or completed.recipient_agent_id != speaker_agent_id
            or command.kind not in {"result", "blocked"} or command.round != completed.round):
        raise Problem(409, "NATIVE_GROUP_CONFLICT", "the result must settle its original reserved message")
    pending.remove(completed)
    return completed


def _deferred(final, pending, command):
    deferred = final and bool(pending)
    if deferred and (command.turn is None or command.result is None):
        raise Problem(409, "NATIVE_GROUP_ORIGINAL_RESULT_REQUIRED", "only a verified saved final can wait for results")
    return deferred


def _reserve(view, delivery, speaker_agent_id, command):
    if delivery is None:
        return None
    delivery = NativeGroupDelivery.model_validate_json(delivery.model_dump_json())
    if (delivery.sender_agent_id != speaker_agent_id or delivery.round != command.round
            or delivery.recipient_agent_id != command.recipient_agent_id
            or delivery.recipient_agent_id == speaker_agent_id
            or delivery.status != "awaiting" or command.kind not in {"request", "handoff"}
            or any(item.message_id == delivery.message_id for item in view.deliveries)):
        raise Problem(409, "NATIVE_GROUP_CONFLICT", "one original request must reserve one original result")
    return delivery


def _round_count(view, pending, agent_id):
    """Published posts plus awaiting results that hold this member's slots in the current round."""
    return (sum(post.speaker_agent_id == agent_id and post.round == view.round for post in view.posts)
            + sum(item.recipient_agent_id == agent_id and item.round == view.round for item in pending))


def _check_allowance(view, pending, speaker_agent_id, final, delivery):
    member_limit = MAX_MEMBER_ROUND_POSTS - int(
        speaker_agent_id == view.orchestrator_agent_id and not final)
    if (len(view.posts) + len(pending) >= MAX_GROUP_POSTS - int(not final)
            or _round_count(view, pending, speaker_agent_id) >= member_limit):
        raise Problem(409, "NATIVE_GROUP_ALLOWANCE", "the original publication allowance is exhausted")
    if delivery is not None:
        recipient = delivery.recipient_agent_id
        if _round_count(view, pending, recipient) > MAX_MEMBER_ROUND_POSTS - int(recipient == view.orchestrator_agent_id):
            raise Problem(409, "NATIVE_GROUP_ALLOWANCE", "the recipient has no result publication slot")


def _reference(post_id, speaker_agent_id, command):
    return NativeGroupPostReference(
        post_id=post_id, speaker_agent_id=speaker_agent_id, round=command.round, kind=command.kind,
        recipient_agent_id=command.recipient_agent_id,
        turn=command.turn, result=command.result,
        content_sha256=hashlib.sha256(command.content.encode("utf-8")).hexdigest())


def _settled_deliveries(view, completed_delivery, kind, delivery):
    deliveries = tuple(item.model_copy(update={"status": "blocked" if kind == "blocked" else "completed"})
                       if item == completed_delivery else item for item in view.deliveries)
    return deliveries if delivery is None else (*deliveries, delivery)


def _published(group, command, reference, completed_delivery, delivery, pending, final):
    """The root after this post; the last settled result also publishes a waiting final and closes."""
    view = group.view
    publications = (*view.posts, reference)
    finish_deferred = group.deferred_final is not None and not pending and completed_delivery is not None
    if finish_deferred:
        publications = (*publications, group.deferred_final)
    closed = final or finish_deferred
    updated_view = NativeGroupView.model_validate_json(view.model_copy(update={
        "posts": publications,
        "deliveries": _settled_deliveries(view, completed_delivery, command.kind, delivery),
        "state": "closed" if closed else "open", "awaiting_final": not closed,
    }).model_dump_json())
    return group.model_copy(update={"view": updated_view, "deferred_final": None if closed else group.deferred_final})


class _Group(WireModel):
    kind: Literal["fa-native-group-state-v1"] = "fa-native-group-state-v1"
    tenant_id: Identifier
    team_id: Identifier
    opening: NativeGroupOpen
    view: NativeGroupView
    advances: Annotated[tuple[NativeGroupAdvance, ...], Field(max_length=MAX_GROUP_ROUNDS - 1)] = ()
    deferred_final: NativeGroupPostReference | None = None


class _Post(WireModel):
    kind: Literal["fa-native-group-post-v1"] = "fa-native-group-post-v1"
    group_id: Digest
    speaker_agent_id: Identifier
    command: NativeGroupPost


class _Signal(WireModel):
    kind: Literal["fa-native-group-signal-v1"] = "fa-native-group-signal-v1"
    sender_agent_id: Identifier
    command: NativeGroupSignal
    receipt: NativeGroupSignalReceipt


class NativeGroupStore:
    def __init__(self, repository: Repository, *, tenant_id: str, team_id: str):
        self.repository, self.tenant_id, self.team_id = repository, tenant_id, team_id
        self.partition_key = "native-groups:" + digest({"tenant": tenant_id, "team": team_id})

    def _identifier(self, request_id):
        return digest({"kind": "fa-native-group-v1", "tenant": self.tenant_id,
                       "team": self.team_id, "request_id": request_id})

    def _read(self, group_id):
        group_id = _identifier(group_id)
        try:
            stored = self.repository.get(self.partition_key, "group:" + group_id)
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                raise Problem(404, "NATIVE_GROUP_NOT_FOUND", "the original group is unavailable") from None
            raise
        try:
            value = _Group.model_validate_json(serialize(stored.payload))
            if ((value.tenant_id, value.team_id, value.view.group_id) != (self.tenant_id, self.team_id, group_id)
                    or self._identifier(value.opening.request_id) != group_id
                    or value.view.opening_request_id != value.opening.request_id
                    or value.view.question != value.opening.content
                    or (value.view.orchestrator_agent_id, value.view.member_agent_ids)
                    != (value.opening.orchestrator_agent_id, value.opening.member_agent_ids)
                    or value.view.round != len(value.advances) + 1
                    or len({item.request_id for item in value.advances}) != len(value.advances)
                    or any(item.expected_round != index for index, item in enumerate(value.advances, 1))
                    or value.deferred_final is not None and (
                        value.view.state != "open" or value.deferred_final.kind != "final"
                        or value.deferred_final.speaker_agent_id != value.view.orchestrator_agent_id
                        or value.deferred_final.turn is None or value.deferred_final.result is None
                        or value.deferred_final.round != value.view.round)
                    or not stored.etag or stored.etag == "*"):
                raise ValueError()
        except ValueError:
            raise Problem(409, "NATIVE_GROUP_STATE_INVALID", "the saved group differs from its original scope") from None
        return value, stored

    def read(self, group_id: str) -> NativeGroupView:
        return self._read(group_id)[0].view

    def opening(self, request_id):
        return self.read(self._identifier(request_id))

    def list(self, after=None):
        after = None if after is None else "group:" + _identifier(after)
        rows, more = self.repository.list_prefix(self.partition_key, "group:", 20, after=after)
        groups = tuple(self.read(key.removeprefix("group:")) for key, _record in rows)
        return NativeGroupList(groups=groups, next_after=groups[-1].group_id if more and groups else None)

    def read_post(self, group_id, post_id) -> NativeGroupPostView:
        post_id = _identifier(post_id)
        group, _ = self._read(group_id)
        reference = next((post for post in group.view.posts if post.post_id == post_id), None)
        if reference is None:
            raise Problem(404, "NATIVE_GROUP_POST_NOT_FOUND", "the original group has no published reference")
        record = self.repository.get(self.partition_key, "post:" + post_id)
        try:
            value = _Post.model_validate_json(serialize(record.payload))
            original_id = group_post_id(group_id, value.speaker_agent_id, value.command.request_id)
            if (value.group_id != group_id or original_id != post_id
                    or (value.speaker_agent_id, value.command.round, value.command.kind) != (
                        reference.speaker_agent_id, reference.round, reference.kind)
                    or (value.command.recipient_agent_id, value.command.turn, value.command.result)
                    != (reference.recipient_agent_id, reference.turn, reference.result)
                    or hashlib.sha256(value.command.content.encode("utf-8")).hexdigest() != reference.content_sha256):
                raise ValueError()
        except ValueError:
            raise Problem(409, "NATIVE_GROUP_STATE_INVALID", "the original published content differs from its reference") from None
        return NativeGroupPostView(reference=reference, content=value.command.content)

    @staticmethod
    def _same_opening(value, command):
        if value.opening != command:
            raise Problem(409, "NATIVE_GROUP_CONFLICT", "the group opening ID already names different material")
        return value.view

    def open(self, command: NativeGroupOpen) -> NativeGroupView:
        command = NativeGroupOpen.model_validate_json(command.model_dump_json())
        group_id = self._identifier(command.request_id)
        try:
            value, _ = self._read(group_id)
        except Problem as error:
            if error.status != 404 or error.body["error"]["code"] != "NATIVE_GROUP_NOT_FOUND":
                raise
        else:
            return self._same_opening(value, command)
        value = _Group(tenant_id=self.tenant_id, team_id=self.team_id, opening=command, view=NativeGroupView(
            group_id=group_id, opening_request_id=command.request_id,
            question=command.content,
            orchestrator_agent_id=command.orchestrator_agent_id, member_agent_ids=command.member_agent_ids,
        ))
        try:
            self.repository.create(self.partition_key, "group:" + group_id, value.model_dump(mode="json"))
        except Problem as error:
            if error.status not in {409, 412} and error.status < 500:
                raise
            try:
                original, _ = self._read(group_id)
            except Problem as read_error:
                if read_error.status == 404 and read_error.body["error"]["code"] == "NATIVE_GROUP_NOT_FOUND":
                    raise Problem(503, "NATIVE_GROUP_UNKNOWN", "the original group write remains unconfirmed") from None
                raise
            return self._same_opening(original, command)
        return value.view

    def _post_original(self, post_id, expected):
        record = self.repository.get(self.partition_key, "post:" + post_id)
        try:
            value = _Post.model_validate_json(serialize(record.payload))
        except ValueError:
            raise Problem(409, "NATIVE_GROUP_STATE_INVALID", "the original published post is invalid") from None
        if value != expected:
            raise Problem(409, "NATIVE_GROUP_CONFLICT", "the original post ID names different material")
        return value

    def post(self, group_id, speaker_agent_id, command: NativeGroupPost, *, delivery=None,
             settles=None) -> NativeGroupPostReference:
        command = NativeGroupPost.model_validate_json(command.model_dump_json())
        group, record = self._read(group_id)
        view = group.view
        _check_speaker(view, speaker_agent_id, command)
        post_id = group_post_id(group_id, speaker_agent_id, command.request_id)
        candidate = _Post(group_id=group_id, speaker_agent_id=speaker_agent_id, command=command)
        previous = _find_post(view.posts, post_id)
        if previous is not None:
            self._post_original(post_id, candidate)
            return previous
        _check_open_round(view, speaker_agent_id, command)
        final = command.kind == "final"
        pending = [item for item in view.deliveries if item.status == "awaiting"]
        completed_delivery = _settle(pending, settles, speaker_agent_id, command)
        deferred = _deferred(final, pending, command)
        delivery = _reserve(view, delivery, speaker_agent_id, command)
        if delivery is not None:
            pending.append(delivery)
        _check_allowance(view, pending, speaker_agent_id, final, delivery)
        reference = _reference(post_id, speaker_agent_id, command)
        # Immutable content is prepared first; only the root CAS publishes its reference and consumes quota.
        self._save_post_content(post_id, candidate)
        if deferred:
            self._defer_final(group, record, group_id, reference)
        updated = _published(group, command, reference, completed_delivery, delivery, pending, final)
        return self._publish(group_id, record, updated, reference, candidate)

    def _save_post_content(self, post_id, candidate):
        try:
            self.repository.create(self.partition_key, "post:" + post_id, candidate.model_dump(mode="json"))
        except Problem as error:
            if not _unconfirmed(error):
                raise
            try:
                self._post_original(post_id, candidate)
            except Problem as read_error:
                if read_error.status == 404:
                    raise Problem(503, "NATIVE_GROUP_UNKNOWN", "the original post content remains unconfirmed") from None
                raise

    def _defer_final(self, group, record, group_id, reference):
        previous_final = group.deferred_final
        if previous_final is not None and (
                previous_final == reference
                or previous_final.result.turn_number > reference.result.turn_number):
            raise Problem(409, "NATIVE_GROUP_PENDING", "the saved final still awaits original member results")
        updated = group.model_copy(update={"deferred_final": reference})
        try:
            self.repository.replace(
                self.partition_key, "group:" + group_id, updated.model_dump(mode="json"), record.etag)
        except Problem as error:
            if not _unconfirmed(error):
                raise
            observed, _ = self._read(group_id)
            if observed.deferred_final != reference and reference not in observed.view.posts:
                raise Problem(503 if error.status >= 500 else 409, "NATIVE_GROUP_UNKNOWN",
                              "the original deferred final is unconfirmed") from None
        raise Problem(409, "NATIVE_GROUP_PENDING", "the saved final awaits the original member result publications")

    def _publish(self, group_id, record, updated, reference, candidate):
        try:
            self.repository.replace(
                self.partition_key, "group:" + group_id, updated.model_dump(mode="json"), record.etag)
        except Problem as error:
            if not _unconfirmed(error):
                raise
            observed, _ = self._read(group_id)
            saved = _find_post(observed.view.posts, reference.post_id)
            if saved is not None:
                if saved != reference:
                    raise Problem(409, "NATIVE_GROUP_STATE_INVALID", "the published post reference differs") from None
                self._post_original(reference.post_id, candidate)
                return saved
            raise Problem(503 if error.status >= 500 else 409, "NATIVE_GROUP_UNKNOWN",
                          "the original post publication is unconfirmed; read the same post ID") from None
        return reference

    def advance(self, group_id, actor_agent_id, command: NativeGroupAdvance) -> NativeGroupRoundReceipt:
        command = NativeGroupAdvance.model_validate_json(command.model_dump_json())
        group, record = self._read(group_id)
        if actor_agent_id != group.view.orchestrator_agent_id:
            raise Problem(403, "NATIVE_GROUP_ORCHESTRATOR_REQUIRED", "only the original orchestrator may advance")
        original = next((item for item in group.advances if item.request_id == command.request_id), None)
        if original is not None:
            if original != command:
                raise Problem(409, "NATIVE_GROUP_CONFLICT", "the original round request names different material")
            return NativeGroupRoundReceipt(
                group_id=group_id, request_id=command.request_id, to_round=command.expected_round + 1)

        if group.view.state != "open":
            raise Problem(409, "NATIVE_GROUP_CLOSED", "the original group is already closed")
        if command.expected_round != group.view.round:
            raise Problem(409, "NATIVE_GROUP_ROUND_CHANGED", "the original round has already changed")
        if any(item.status == "awaiting" for item in group.view.deliveries):
            raise Problem(409, "NATIVE_GROUP_PENDING", "original results must settle before the next group round")
        if group.view.round >= MAX_GROUP_ROUNDS or len(group.view.posts) >= MAX_GROUP_POSTS - 1:
            raise Problem(409, "NATIVE_GROUP_ALLOWANCE", "the group must preserve its remaining final allowance")
        updated = group.model_copy(update={
            "view": group.view.model_copy(update={"round": group.view.round + 1}),
            "advances": (*group.advances, command),
        })
        try:
            self.repository.replace(
                self.partition_key, "group:" + group_id, updated.model_dump(mode="json"), record.etag)
        except Problem as error:
            if error.status not in {409, 412} and error.status < 500:
                raise
            observed, _ = self._read(group_id)
            retained = next((item for item in observed.advances if item.request_id == command.request_id), None)
            if retained != command:
                raise Problem(503 if error.status >= 500 else 409, "NATIVE_GROUP_UNKNOWN",
                              "the original round change is unconfirmed; read its original request") from None
        return NativeGroupRoundReceipt(
            group_id=group_id, request_id=command.request_id, to_round=command.expected_round + 1)

    def route(self, group_id, sender_agent_id, command: NativeGroupSignal) -> NativeGroupSignalReceipt:
        command = NativeGroupSignal.model_validate_json(command.model_dump_json())
        group, _ = self._read(group_id)
        if sender_agent_id not in group.view.member_agent_ids or command.recipient_agent_id not in group.view.member_agent_ids:
            raise Problem(403, "NATIVE_GROUP_MEMBER_REQUIRED", "the signal must retain its original group members")
        key = "signal:" + digest({"group_id": group_id, "sender_agent_id": sender_agent_id,
                                  "recipient_agent_id": command.recipient_agent_id, "event_id": command.event_id})
        expected = _Signal(sender_agent_id=sender_agent_id, command=command, receipt=NativeGroupSignalReceipt(
            group_id=group_id, event_id=command.event_id, recipient_agent_id=command.recipient_agent_id,
            reason="transport_receipt" if command.kind == "receipt" else "fyi_no_reply_requested"))

        def original():
            record = self.repository.get(self.partition_key, key)
            try:
                value = _Signal.model_validate_json(serialize(record.payload))
            except ValueError:
                raise Problem(409, "NATIVE_GROUP_STATE_INVALID", "the original signal receipt is invalid") from None
            if value != expected:
                raise Problem(409, "NATIVE_GROUP_CONFLICT", "the original signal ID names different material")
            return value.receipt

        try:
            return original()
        except Problem as error:
            if error.status != 404 or error.body["error"]["code"] != "NOT_FOUND":
                raise
        try:
            self.repository.create(self.partition_key, key, expected.model_dump(mode="json"))
        except Problem as error:
            if error.status not in {409, 412} and error.status < 500:
                raise
            try:
                return original()
            except Problem as read_error:
                if read_error.status == 404:
                    raise Problem(503, "NATIVE_GROUP_UNKNOWN", "the original signal outcome remains unconfirmed") from None
                raise
        return expected.receipt

    def relay_request(self, message):
        source = message.source
        try:
            group = self.read(source.root.request_id)
        except Problem as error:
            if error.body["error"]["code"] in {"NATIVE_GROUP_NOT_FOUND", "NATIVE_GROUP_INVALID"}:
                return None
            raise
        if source.root.agent_id != group.orchestrator_agent_id:
            raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "the relay root must name this group's orchestrator")
        if any(item.message_id == source.message_id and item.refusal_code is not None for item in group.deliveries):
            raise Problem(409, "NATIVE_GROUP_REQUEST_REJECTED", "the original group request was explicitly refused")
        if source.recipient_agent_id == group.orchestrator_agent_id:
            raise Problem(409, "NATIVE_GROUP_REPLY_REQUIRED",
                          "return the original result to the orchestrator instead of creating a circular request")
        if message.capture.template.settings.state_profile != "foundry-responses-v1":
            raise Problem(409, "NATIVE_GROUP_ROUTE_UNAVAILABLE", "new group requests require the recipient's Responses lane")
        speaker = source.sender.agent_id
        identifier = group_post_id(group.group_id, speaker, source.message_id)
        existing = next((post for post in group.posts if post.post_id == identifier), None)
        return self.post(group.group_id, speaker, NativeGroupPost(
            request_id=source.message_id, round=group.round if existing is None else existing.round,
            kind=source.message_kind, content=message.command.content, recipient_agent_id=source.recipient_agent_id),
            delivery=NativeGroupDelivery(
                message_id=source.message_id, sender_agent_id=speaker, recipient_agent_id=source.recipient_agent_id,
                round=group.round if existing is None else existing.round))

    def refused_request(self, message, code):
        """The caller verified the exact pre-admission refusal and re-read no original peer RID."""
        source = message.source
        try:
            group, record = self._read(source.root.request_id)
        except Problem as error:
            if error.body["error"]["code"] in {"NATIVE_GROUP_NOT_FOUND", "NATIVE_GROUP_INVALID"}:
                return
            raise
        original = next((item for item in group.view.deliveries if item.message_id == source.message_id), None)
        if original is None or original.status != "awaiting":
            return
        if (original.sender_agent_id, original.recipient_agent_id) != (
                source.sender.agent_id, source.recipient_agent_id):
            raise Problem(409, "NATIVE_GROUP_CONFLICT", "the refusal must retain its original group request")
        declined = NativeGroupDelivery.model_validate_json(original.model_copy(
            update={"status": "blocked", "refusal_code": code}).model_dump_json())
        deliveries = tuple(declined if item == original else item for item in group.view.deliveries)
        finish = group.deferred_final is not None and not any(item.status == "awaiting" for item in deliveries)
        posts = (*group.view.posts, group.deferred_final) if finish else group.view.posts
        view = NativeGroupView.model_validate_json(group.view.model_copy(update={
            "deliveries": deliveries, "posts": posts, "state": "closed" if finish else group.view.state,
            "awaiting_final": False if finish else group.view.awaiting_final,
        }).model_dump_json())
        updated = group.model_copy(update={"view": view, "deferred_final": None if finish else group.deferred_final})
        try:
            self.repository.replace(
                self.partition_key, "group:" + group.view.group_id, updated.model_dump(mode="json"), record.etag)
        except Problem as error:
            if error.status not in {409, 412} and error.status < 500:
                raise
            observed, _ = self._read(group.view.group_id)
            if declined not in observed.view.deliveries:
                raise Problem(503, "NATIVE_GROUP_UNKNOWN",
                              "the original group refusal observation is unconfirmed") from None
