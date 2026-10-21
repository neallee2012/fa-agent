"""Shared Responses routing contracts, independent of the Hosted SDK."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import Digest, Identifier, WireModel


# ADR-0009 §3: the one profile whose material may be unlabeled. It is fixed, not ``NATIVE_STATE_PROFILE``: a v1 digest
# (``submit_digest``) is over the unlabeled form, so v1 stays byte-identical, while a later profile always labels its own.
# Every DTO default is this profile, so an unlabeled stored item keeps reading as v1 after 2d-ii.
UNLABELED_STATE_PROFILE = "foundry-native-v1"
NATIVE_V2_PROFILE = "foundry-native-v2"
# ADR-0009 M1 2d-ii: the profile the API sends (and labels new v2 material with) is v2.
NATIVE_STATE_PROFILE = NATIVE_V2_PROFILE
NATIVE_RESPONSES_PROFILE = "foundry-responses-v1"
# What a Hosted runtime may declare (FA_NATIVE_TURNS): H2d images keep the v1 alias until the first image after 2d-ii.
ACCEPTED_STATE_PROFILES = frozenset({UNLABELED_STATE_PROFILE, NATIVE_V2_PROFILE})
NativeStateProfile = Literal["foundry-native-v1", "foundry-responses-v1", "foundry-native-v2"]


def channel(profile) -> Literal["invocations", "responses"]:
    """ADR-0009 §3: conversations and receipts are compared by channel, never by profile version."""
    return "responses" if profile == NATIVE_RESPONSES_PROFILE else "invocations"


def unlabeled_profile(value) -> bool:
    """``exclude_if`` of a digest-bearing ``state_profile``: only the v1-era profile is left out."""
    return value == UNLABELED_STATE_PROFILE


NativeResponseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
NATIVE_RESPONSE_RID_HEADER = "x-client-fa-rid"
NATIVE_RESPONSE_CID_HEADER = "x-client-fa-cid"
NATIVE_RESPONSE_DIGEST_HEADER = "x-client-fa-submit-digest"
RESPONSES_ENVIRONMENT_MODE: Literal["retain-cid"] | None = "retain-cid"


def responses_environment_blocker():
    """D1/D2 select whether the original Responses CID may continue in a new explicit session."""
    return None if RESPONSES_ENVIRONMENT_MODE == "retain-cid" else "NATIVE_ENVIRONMENT_UNAVAILABLE"


class NativeResponseHeaders(WireModel):
    request_id: Identifier
    conversation_id: Identifier
    submit_digest: Digest


class NativeResponseDispatch(WireModel):
    """A read-time transport instruction, not a stored execution or acceptance authority."""

    state: Literal["needs_response", "already_queued", "not_taken"]
    inflight_request_id: Identifier | None = None
    previous_response_id: NativeResponseId | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def owned_slot(self):
        if self.state != "not_taken" and self.inflight_request_id is None:
            raise ValueError("Dispatch requires the original CID slot")
        return self


def check_response_state(profile, status, rid, response_id, dispatch):
    if channel(profile) == "invocations":
        if response_id is not None or dispatch is not None:
            raise ValueError("An Invocations receipt cannot acquire Responses transport state")
        return
    if status in {"started", "completed"} and response_id is None:
        raise ValueError("A started Responses turn retains its official response ID")
    if status == "accepted" and response_id is not None:
        raise ValueError("The official response ID is persisted with the started CAS")
    if dispatch is not None and (
            status != "accepted"
            or dispatch.state != "not_taken" and dispatch.inflight_request_id != rid):
        raise ValueError("Only the original accepted RID may request its second transport step")
