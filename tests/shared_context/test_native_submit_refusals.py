"""``NativeTurnGateway._submit``'s decision table: what each refusal becomes, and what ran on the way.

Every collaborator is a recording stand-in, so a case states one branch: the integration through real Hosted transport
is in test_native_turn_api.py and the test_native_send_*.py files."""

from types import SimpleNamespace

import pytest

from shared_context import behavior_native_turn_api as api
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_native_turn_api import _BeforeAdmission, NativeTurnGateway
from shared_context.errors import Problem

CHANGED, FOLLOWING = "NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING"
UNAVAILABLE, PENDING, RID_CONFLICT = "NATIVE_ENVIRONMENT_UNAVAILABLE", "NATIVE_ADMISSION_PENDING", "NATIVE_RID_CONFLICT"
ORIGINAL = SimpleNamespace(status="accepted")
SID = "session-one"


class Chosen(Exception):
    """What ``_follow_before_admission`` / ``_bootstrap`` returned for the caller to raise."""

    def __init__(self, kind, **details):
        super().__init__(kind)
        self.kind, self.details = kind, details


class Harness:
    def __init__(self, monkeypatch, *, enforce=False, current=None, candidate=None, prepared=False, admission=None,
                 existing=(None, None), hosted=None, older=False, row=None):
        self.calls, self.older_args = [], None
        self.client = SimpleNamespace(target=SimpleNamespace(session_id=SID), _binding_row=row)
        self.current, self.admission_error, self.hosted = current, admission, hosted
        self.existing, self.older, self.prepared_value = existing, older, prepared
        self.sessions = SimpleNamespace(
            guard=SimpleNamespace(enforce=enforce), following_candidate=candidate, current=self._current,
            admission=self._admission, moved=lambda error: self.calls.append("moved"),
            forget=lambda: self.calls.append("forget"), admission_stuck=self._stuck,
            observed_binding=lambda client, version: self.calls.append(f"observed:{version}"))
        gateway = NativeTurnGateway.__new__(NativeTurnGateway)
        gateway.composition = "composition"
        gateway._context = self._context
        gateway._legacy = lambda control, cid: None
        gateway._prepare = lambda *_args, **_kwargs: ("capture", "previous")
        gateway._prefetch = lambda *_args: None
        gateway._prepared_before = self._prepared_before
        gateway._existing = self._existing
        gateway._follow_before_admission = self._follow
        gateway._bootstrap = self._bootstrap
        gateway._keep_admitted = lambda client, value: None
        gateway._accounted = lambda control, value: value
        gateway._bound = lambda _sessions, client, result, *_notices: ("bound", client, result)
        gateway._retained = lambda *args, **kwargs: ("retained", args[3], kwargs["response"])
        gateway._after_refusal = lambda *_args, **_kwargs: (self.client, "after-refusal")
        self.gateway = gateway
        monkeypatch.setattr("shared_context.behavior_native_sessions.native_sessions", lambda _: self.sessions)
        monkeypatch.setattr("shared_context.behavior_native_responses_api.submit_native_turn", self._hosted)
        monkeypatch.setattr(api, "older_image", self._older)

    def _context(self, _principal, action, *, prepared):
        self.calls.append(f"context:{action}:{prepared}")
        return "control", "reader"

    def _current(self, reader, checked_since):
        assert reader == "reader" and isinstance(checked_since, float)
        if self.current is not None:
            raise self.current
        return self.client

    def _admission(self, client, *, request_id, resolved):
        if self.admission_error is not None:
            raise self.admission_error

    def _prepared_before(self, _sessions, reader, located):
        self.calls.append(f"prepared_before:{located}")
        if isinstance(self.prepared_value, Exception):
            raise self.prepared_value
        return self.prepared_value

    def _existing(self, client, rid):
        self.calls.append("existing")
        if isinstance(self.existing, Exception):
            raise self.existing
        return self.existing

    def _follow(self, _principal, _reader, _sessions, error, rid, *, refused_session_id=None):
        self.calls.append("follow")
        return Chosen("follow", error=error, rid=rid, refused=refused_session_id)

    def _bootstrap(self, _principal, _reader, _sessions, error, rid):
        self.calls.append("bootstrap")
        return Chosen("bootstrap", error=error, rid=rid)

    def _stuck(self, client, *, before_send):
        self.calls.append("stuck")
        before_send()

    def _older(self, composition, control, client, conflict, started, *, cut):
        self.calls.append(f"older:{cut}")
        self.older_args = composition, control, client, conflict, started
        return self.older

    def _hosted(self, composition, client, command, capture, *, conversation, defer):
        if self.hosted is not None:
            raise self.hosted
        return ("receipt", None)

    def submit(self, *, original=None, response=None, early=True):
        command = InstructionSubmit(request_id="native-rid", content="text")
        return self.gateway._submit("principal", command, original=original, response=response, early=early)


def problem(status, code, **details):
    return Problem(status, code, "private detail", **details)


def before(raised, code, *, following=False):
    assert type(raised) is _BeforeAdmission
    assert raised.body["error"]["code"] == code and raised.request_id == "native-rid"
    assert raised.server_following is following


def chosen(raised, kind, error, **details):
    assert type(raised) is Chosen and raised.kind == kind and raised.details["error"] is error
    assert raised.details["rid"] == "native-rid"
    assert {key: raised.details[key] for key in details} == details


@pytest.fixture
def harness(monkeypatch):
    return lambda **options: Harness(monkeypatch, **options)


@pytest.mark.parametrize("status,code", [(409, CHANGED), (503, FOLLOWING), (503, "NATIVE_TURN_UNKNOWN")])
def test_a_reconcile_of_an_accepted_rid_keeps_a_location_refusal_as_it_is(harness, status, code):
    error = problem(status, code, not_dispatched=True)
    case = harness(current=error, enforce=True)
    with pytest.raises(Problem) as raised:
        case.submit(original=ORIGINAL, response="response")
    assert raised.value is error and case.calls == ["context:submit:False"]


def test_an_unrelated_location_refusal_stays_as_it_is(harness):
    error = problem(503, "NATIVE_TURN_UNKNOWN", not_dispatched=True)
    case = harness(current=error, enforce=True)
    with pytest.raises(Problem) as raised:
        case.submit()
    assert raised.value is error and case.calls == ["context:submit:False"]


def test_a_known_candidate_follows_without_listing_sessions(harness):
    error = problem(409, CHANGED)
    case = harness(current=error, candidate="candidate", prepared=False, enforce=True)
    with pytest.raises(Chosen) as raised:
        case.submit()
    chosen(raised.value, "follow", error, refused=None)
    assert case.calls == ["context:submit:False", "follow"]


@pytest.mark.parametrize("enforce", [False, True])
def test_an_agent_prepared_earlier_follows(harness, enforce):
    error = problem(409, CHANGED)
    error.located = "located-listing"
    case = harness(current=error, prepared=True, enforce=enforce)
    with pytest.raises(Chosen) as raised:
        case.submit()
    chosen(raised.value, "follow", error, refused=None)
    assert case.calls == ["context:submit:False", "prepared_before:located-listing", "follow"]


def test_an_unprepared_agent_bootstraps_only_with_the_guard_enforced(harness):
    error = problem(409, CHANGED)
    case = harness(current=error, enforce=True)
    with pytest.raises(Chosen) as raised:
        case.submit()
    chosen(raised.value, "bootstrap", error)
    assert case.calls == ["context:submit:False", "prepared_before:None", "bootstrap"]


def test_an_unprepared_agent_without_the_guard_sends_nothing(harness):
    case = harness(current=problem(409, CHANGED))
    with pytest.raises(Problem) as raised:
        case.submit()
    before(raised.value, CHANGED)
    assert case.calls == ["context:submit:False", "prepared_before:None"]


@pytest.mark.parametrize("unread,code", [
    (problem(503, FOLLOWING), FOLLOWING), (problem(409, FOLLOWING), FOLLOWING), (problem(503, UNAVAILABLE), UNAVAILABLE),
])
@pytest.mark.parametrize("enforce", [False, True])
def test_a_listing_that_could_not_be_read_answers_its_own_refusal(harness, unread, code, enforce):
    case = harness(current=problem(409, CHANGED), prepared=unread, enforce=enforce)
    with pytest.raises(Problem) as raised:
        case.submit()
    before(raised.value, code)
    assert case.calls == ["context:submit:False", "prepared_before:None"]


@pytest.mark.parametrize("unread", [problem(409, UNAVAILABLE), problem(503, "OTHER_CODE"), problem(404, "OTHER_CODE")])
@pytest.mark.parametrize("enforce", [False, True])
def test_any_other_listing_refusal_is_neither_prepared_nor_unprepared(harness, unread, enforce):
    error = problem(409, CHANGED)
    case = harness(current=error, prepared=unread, enforce=enforce)
    with pytest.raises(Problem) as raised:
        case.submit()
    before(raised.value, CHANGED)
    assert case.calls == ["context:submit:False", "prepared_before:None"]


def test_a_guard_refusal_that_sent_nothing_runs_the_switch(harness):
    error = problem(503, FOLLOWING, not_dispatched=True)
    case = harness(current=error, enforce=True)
    with pytest.raises(Chosen) as raised:
        case.submit()
    chosen(raised.value, "follow", error, refused=None)


@pytest.mark.parametrize("enforce,details", [(False, {"not_dispatched": True}), (True, {}), (False, {}),
                                             (True, {"not_dispatched": False})])
def test_a_version_this_build_does_not_serve_is_not_followed(harness, enforce, details):
    case = harness(current=problem(503, FOLLOWING, **details), enforce=enforce)
    with pytest.raises(Problem) as raised:
        case.submit()
    before(raised.value, FOLLOWING)
    assert case.calls == ["context:submit:False"]


@pytest.mark.parametrize("status", [500, 503])
def test_an_unknown_admission_refusal_is_raised_without_reading_the_rid(harness, status):
    error = problem(status, "NATIVE_STATE_STORE_UNKNOWN")
    case = harness(admission=error)
    with pytest.raises(Problem) as raised:
        case.submit()
    assert raised.value is error and "existing" not in case.calls


@pytest.mark.parametrize("status,code", [(409, "NATIVE_TURN_BUSY"), (499, "NATIVE_TURN_BUSY"), (503, FOLLOWING)])
def test_an_accepted_rid_continues_past_an_admission_refusal(harness, status, code):
    case = harness(admission=problem(status, code), existing=(ORIGINAL, "saved"))
    assert case.submit() == ("bound", case.client, ("retained", ORIGINAL, "saved"))


def test_an_unread_rid_keeps_the_admission_refusal(harness):
    error = problem(409, "NATIVE_TURN_BUSY")
    case = harness(admission=error, existing=problem(503, "NATIVE_STATE_STORE_UNKNOWN"))
    with pytest.raises(Problem) as raised:
        case.submit()
    assert raised.value is error


def test_an_admission_refusal_of_a_new_rid_may_be_sent_again(harness):
    case = harness(admission=problem(409, "NATIVE_TURN_BUSY"))
    with pytest.raises(Problem) as raised:
        case.submit()
    before(raised.value, "NATIVE_TURN_BUSY")
    assert "follow" not in case.calls


def test_an_agent_following_at_admission_runs_the_switch_for_a_new_rid(harness):
    error = problem(503, FOLLOWING)
    case = harness(admission=error)
    with pytest.raises(Chosen) as raised:
        case.submit()
    chosen(raised.value, "follow", error, refused=None)


def hosted_case(harness, error, **options):
    case = harness(hosted=error, **options)
    with pytest.raises(BaseException) as raised:
        case.submit()
    return case, raised.value


def test_a_rid_conflict_with_an_original_retains_it(harness):
    case = harness(hosted=problem(409, RID_CONFLICT), existing=(ORIGINAL, "saved"))
    assert case.submit() == ("bound", case.client, ("retained", ORIGINAL, "saved"))
    assert case.calls == ["context:submit:False", "existing"]


def test_a_rid_conflict_without_an_original_stays_a_conflict(harness):
    error = problem(409, RID_CONFLICT)
    case, raised = hosted_case(harness, error)
    assert raised is error and case.calls == ["context:submit:False", "existing"]


def test_a_reported_version_that_sent_nothing_is_recorded_and_resent_by_the_client(harness):
    error = problem(409, CHANGED, actual_version="42", reason="version", not_dispatched=True)
    case, raised = hosted_case(harness, error, enforce=True)
    before(raised, CHANGED, following=True)
    assert case.calls == ["context:submit:False", "observed:42", "forget"]


@pytest.mark.parametrize("details", [{"reason": "version"}, {"reason": "version", "not_dispatched": False}])
def test_a_reported_version_that_may_have_dispatched_is_recorded_and_stays_a_refusal(harness, details):
    error = problem(409, CHANGED, actual_version="42", **details)
    case, raised = hosted_case(harness, error)
    assert raised is error
    assert case.calls == ["context:submit:False", "observed:42", "moved"]


def test_a_reported_version_for_another_reason_is_recorded_and_followed(harness):
    error = problem(409, CHANGED, actual_version="42", reason="session", not_dispatched=True)
    case, raised = hosted_case(harness, error)
    chosen(raised, "follow", error, refused=None)
    assert case.calls == ["context:submit:False", "observed:42", "moved", "follow"]


def test_a_changed_session_without_a_version_is_not_recorded(harness):
    error = problem(409, CHANGED, reason="version", not_dispatched=True)
    case, raised = hosted_case(harness, error)
    chosen(raised, "follow", error, refused=None)
    assert case.calls == ["context:submit:False", "moved", "follow"]


def test_only_a_changed_session_records_a_version(harness):
    error = problem(409, "NATIVE_TURN_BUSY", actual_version="42", reason="version", not_dispatched=True)
    case, raised = hosted_case(harness, error)
    before(raised, "NATIVE_TURN_BUSY")
    assert case.calls == ["context:submit:False", "moved", "older:True"]


def test_a_following_agent_names_the_refusing_session(harness):
    error = problem(503, FOLLOWING, not_dispatched=True)
    case, raised = hosted_case(harness, error)
    chosen(raised, "follow", error, refused=SID)
    assert case.calls == ["context:submit:False", "moved", "follow"]


def test_a_stuck_admission_runs_its_bound_then_the_client_resends(harness):
    error = problem(503, PENDING)
    case, raised = hosted_case(harness, error, enforce=True)
    before(raised, PENDING, following=True)
    assert case.calls == ["context:submit:False", "stuck", "context:submit:False"]


def test_a_pending_admission_without_the_guard_is_an_ordinary_refusal(harness):
    error = problem(503, PENDING)
    case, raised = hosted_case(harness, error)
    assert raised is error and case.calls == ["context:submit:False", "moved"]


def test_only_a_503_pending_admission_is_a_stuck_one(harness):
    error = problem(409, PENDING)
    case, raised = hosted_case(harness, error, enforce=True)
    assert raised is error and case.calls == ["context:submit:False", "moved"]


@pytest.mark.parametrize("details", [{}, {"not_dispatched": False}])
def test_a_refusal_that_may_have_dispatched_stays_as_it_is(harness, details):
    error = problem(503, "NATIVE_TURN_UNKNOWN", **details)
    case, raised = hosted_case(harness, error)
    assert raised is error and case.calls == ["context:submit:False", "moved"]


@pytest.mark.parametrize("early", [True, False])
def test_an_older_image_moves_the_agent_and_the_client_resends(harness, early):
    error = problem(409, "NATIVE_TURN_BUSY", not_dispatched=True)
    case = harness(hosted=error, older=True)
    with pytest.raises(Problem) as raised:
        case.submit(early=early)
    before(raised.value, FOLLOWING, following=True)
    assert raised.value.body["error"]["not_dispatched"] is True
    assert case.calls == ["context:submit:False", "moved", f"older:{early}"]
    composition, control, client, conflict, started = case.older_args
    assert (composition, control, client, conflict) == ("composition", "control", case.client, error)
    assert isinstance(started, float)


def test_a_definite_refusal_with_no_older_image_may_be_sent_again(harness):
    error = problem(409, "NATIVE_TURN_BUSY", not_dispatched=True)
    case, raised = hosted_case(harness, error)
    before(raised, "NATIVE_TURN_BUSY")


def test_a_refusal_the_binding_row_located_is_resolved_there_first(harness):
    error = problem(409, CHANGED, actual_version="42", not_dispatched=True)
    case = harness(hosted=error, row="row")
    assert case.submit() == ("bound", case.client, "after-refusal")
    assert case.calls == ["context:submit:False"]


def test_a_row_that_does_not_cover_this_refusal_continues_as_any_other(harness):
    error = problem(409, RID_CONFLICT)
    case, raised = hosted_case(harness, error, row="row")
    assert raised is error and case.calls == ["context:submit:False", "existing"]


def test_a_clean_send_binds_its_answer(harness):
    case = harness()
    assert case.submit() == ("bound", case.client, ("receipt", None))
    assert case.calls == ["context:submit:False"]
