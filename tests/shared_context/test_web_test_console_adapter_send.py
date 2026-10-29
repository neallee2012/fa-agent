"""Pin ``PullAdapter.send``: request guards, response limits, accepted statuses and error mapping (offline)."""

import json
from types import SimpleNamespace

import httpx
import pytest

from web_test_console import adapter as adapter_module
from web_test_console.adapter import ConsoleError, PullAdapter

ORIGIN = "http://api.test"
TOKEN = "tok-secret"
DENIED = ("ORIGIN_DENIED", 403, None, False, False, False)
FALLBACK = ("BACKEND_UNAVAILABLE", 409, 500, False, False, False)


def make(handler, prefix=""):
    seen = []

    def route(request):
        seen.append(request)
        return handler(request)

    return PullAdapter(SimpleNamespace(backend_origin=ORIGIN), httpx.Client(transport=httpx.MockTransport(route)),
                       api_prefix=prefix), seen


def outcome(method, path, *, status=200, content=b"{}", headers=None, body=None, params=None, if_match=None,
            prefix="", **kwargs):
    adapter, seen = make(lambda request: httpx.Response(status, headers=headers, content=content), prefix)
    try:
        return "ok", adapter.send(method, path, body, TOKEN, params=params, if_match=if_match, **kwargs), seen
    except ConsoleError as error:
        return "err", (error.code, error.status, error.upstream_status, error.not_dispatched,
                       error.server_following, error.automatic_retry), seen


def failure(method, path, *, error=None, status=500, raw=None, **kwargs):
    content = raw if raw is not None else json.dumps({"error": error}).encode()
    kind, value, _ = outcome(method, path, status=status, content=content, **kwargs)
    assert kind == "err"
    return value


def code_error(code, **extra):
    return {"code": code, **extra}


# ---------------------------------------------------------------- request guards

@pytest.mark.parametrize("path", ["x", "//evil.test/x", "/a?b=1", "/a#b", "@evil.test/x"])
def test_path_must_stay_on_the_backend_origin(path):
    kind, value, seen = outcome("GET", path)
    assert (kind, value, seen) == ("err", DENIED, [])


def test_a_path_that_would_move_the_host_is_denied():
    # An origin without a host ("https:/") lets the path name one; the host comparison must catch it.
    seen = []
    adapter = PullAdapter(SimpleNamespace(backend_origin="https:/"), httpx.Client(transport=httpx.MockTransport(
        lambda request: (seen.append(request), httpx.Response(200, content=b"{}"))[1])))
    with pytest.raises(ConsoleError) as denied:
        adapter.send("GET", "/evil.test/x", None, TOKEN)
    assert (denied.value.code, denied.value.status, seen) == ("ORIGIN_DENIED", 403, [])


@pytest.mark.parametrize("prefix", ["/other/a1", "/agents/", "/agents/bad id", "/agents/" + "a" * 200])
def test_api_prefix_must_be_an_agent_route(prefix):
    with pytest.raises(ConsoleError) as denied:
        PullAdapter(SimpleNamespace(backend_origin=ORIGIN), None, api_prefix=prefix)
    assert (denied.value.code, denied.value.status) == ("ORIGIN_DENIED", 403)


def test_api_prefix_and_fixed_headers_reach_the_backend():
    kind, value, seen = outcome("POST", "/behavior/v1/x", body={"b": "ü", "a": 1}, prefix="/agents/a1",
                                content=b"data")
    assert kind == "ok" and value[0] == b"data" and value[1]["content-length"] == "4"
    request = seen[0]
    assert str(request.url) == "http://api.test/agents/a1/behavior/v1/x"
    assert request.content == '{"a":1,"b":"ü"}'.encode()
    assert request.headers["authorization"] == "Bearer " + TOKEN
    assert request.headers["content-type"] == "application/json" and request.headers["accept-encoding"] == "identity"
    from shared_context.behavior_native_turn_wire import (
        NOTICE_KINDS_HEADER, NOTICE_KINDS_VERSION, ROLLBACK_NOTICE_HEADER, ROLLBACK_NOTICE_VERSION,
        UPGRADE_VETOED_HEADER, UPGRADE_VETOED_VERSION, UPGRADE_WAITING_HEADER, UPGRADE_WAITING_VERSION)
    for name, version in ((NOTICE_KINDS_HEADER, NOTICE_KINDS_VERSION), (UPGRADE_WAITING_HEADER, UPGRADE_WAITING_VERSION),
                          (ROLLBACK_NOTICE_HEADER, ROLLBACK_NOTICE_VERSION),
                          (UPGRADE_VETOED_HEADER, UPGRADE_VETOED_VERSION)):
        assert request.headers[name] == version
    assert "if-match" not in request.headers


def test_a_get_without_a_body_sends_no_content():
    _, _, seen = outcome("GET", "/behavior/v1/x")
    assert seen[0].content == b""


SKILL_PATH = "/team-skills/v1/skills/s1/decisions"


def test_if_match_is_sent_only_with_a_team_skill_post():
    kind, _, seen = outcome("POST", SKILL_PATH, body={}, if_match="W/\"1\"")
    assert kind == "ok" and seen[0].headers["if-match"] == "W/\"1\""
    kind, _, seen = outcome("POST", SKILL_PATH, body={}, if_match="e" * 256)
    assert kind == "ok" and seen[0].headers["if-match"] == "e" * 256


@pytest.mark.parametrize("method,path,if_match", [
    ("GET", SKILL_PATH, "e"), ("POST", "/behavior/v1/x", "e"), ("POST", SKILL_PATH, ""), ("POST", SKILL_PATH, "e" * 257),
    ("POST", SKILL_PATH, "a\nb"), ("POST", SKILL_PATH, 123), ("PUT", SKILL_PATH, "e")])
def test_if_match_elsewhere_or_malformed_is_denied(method, path, if_match):
    kind, value, seen = outcome(method, path, body={}, if_match=if_match)
    assert (kind, value, seen) == ("err", DENIED, [])


# ---------------------------------------------------------------- query parameters

HISTORY = "/work-policy/v1/responses-turns/conversations/c1/history"
NATIVE = "/work-policy/v1/native-turns/"
ROUTINES = "/native-routines/v1/agents/a1/definitions"

ALLOWED_PARAMS = [
    ("GET", "/native-hitl/v1/attention", {"cursor": "abc_-9", "limit": 20}),
    ("GET", "/native-hitl/v1/attention", {"limit": 1}),
    ("GET", "/team-skills/v1/reviews", {"after": "a" + "b" * 63}),
    ("GET", ROUTINES, {"after": "x1"}),
    ("GET", HISTORY, {"after": "x" * 128, "limit": 100}),
    ("GET", "/work-policy/v1/responses-turns/conversations/" + "c" * 128 + "/history", {"limit": 1}),
    ("GET", "/work-policy/v1/responses-turns/responses/r_1/history", {"limit": 1}),
    ("GET", "/work-policy/v1/native-turns/received", {"after": "c" * 2048}),
    ("GET", NATIVE + "t1/history", {"after_turn": 0, "limit": 5}),
    ("GET", NATIVE + "t1/result", {"part": 0}),
]


@pytest.mark.parametrize("method,path,params", ALLOWED_PARAMS)
def test_allowed_query_parameters_pass(method, path, params):
    kind, _, seen = outcome(method, path, params=params)
    assert kind == "ok" and dict(seen[0].url.params) == {k: str(v) for k, v in params.items()}


DENIED_PARAMS = [
    ("POST", "/native-hitl/v1/attention", {"limit": 1}, None),
    ("GET", "/native-hitl/v1/attention", {"limit": 1}, {}),
    ("GET", "/native-hitl/v1/attention", {"other": 1}, None),
    ("GET", "/native-hitl/v1/attention", {"limit": 0}, None),
    ("GET", "/native-hitl/v1/attention", {"limit": 21}, None),
    ("GET", "/native-hitl/v1/attention", {"limit": True}, None),
    ("GET", "/native-hitl/v1/attention", {"cursor": "bad cursor"}, None),
    ("GET", "/native-hitl/v1/attention", {"cursor": 5}, None),
    ("GET", "/native-hitl/v1/attention", {"cursor": b"abc"}, None),
    ("GET", "/native-hitl/v1/attention", {"x": 1}, None),
    ("GET", "/team-skills/v1/reviews/x", {"after": "a"}, None),
    ("GET", HISTORY, {"x": 1}, None),
    ("GET", "/work-policy/v1/responses-turns/conversations/" + "c" * 129 + "/history", {"limit": 1}, None),
    ("GET", NATIVE + "t1/result", {"x": 1}, None),
    ("GET", "/behavior/v1/y/result", {"part": 0}, None),
    ("GET", "/behavior/v1/y/history", {"limit": 1}, None),
    ("GET", "/team-skills/v1/reviews", {"after": "-a"}, None),
    ("GET", "/team-skills/v1/reviews", {"after": 5}, None),
    ("GET", "/team-skills/v1/reviews", {"after": "a" * 65}, None),
    ("GET", "/team-skills/v1/reviews", {"after": "a", "limit": 1}, None),
    ("GET", "/team-skills/v1/skills", {"after": "a"}, None),
    ("POST", "/team-skills/v1/reviews", {"after": "a"}, None),
    ("GET", ROUTINES + "/d1", {"after": "a"}, None),
    ("GET", ROUTINES, {"after": "-a"}, None),
    ("GET", ROUTINES, {"after": "a", "limit": 1}, None),
    ("POST", ROUTINES, {"after": "a"}, None),
    ("GET", ROUTINES, {"after": "a"}, {}),
    ("GET", HISTORY, {"limit": 101}, None),
    ("GET", HISTORY, {"limit": 0}, None),
    ("GET", HISTORY, {"limit": True}, None),
    ("GET", HISTORY, {"after": ""}, None),
    ("GET", HISTORY, {"after": 5}, None),
    ("GET", HISTORY, {"after": "x" * 129}, None),
    ("GET", HISTORY, {"after_turn": 1}, None),
    ("POST", HISTORY, {"limit": 1}, None),
    ("GET", HISTORY, {"limit": 1}, {}),
    ("GET", "/work-policy/v1/responses-turns/submit", {"limit": 1}, None),
    ("GET", "/work-policy/v1/responses-turns/other/c1/history", {"limit": 1}, None),
    ("GET", "/work-policy/v1/native-turns/received", {"after": ""}, None),
    ("GET", "/work-policy/v1/native-turns/received", {"after": "c" * 2049}, None),
    ("GET", "/work-policy/v1/native-turns/received", {"after": "a\nb"}, None),
    ("GET", "/work-policy/v1/native-turns/received", {"after": 5}, None),
    ("GET", "/work-policy/v1/native-turns/received", {"after": "a", "limit": 1}, None),
    ("POST", "/work-policy/v1/native-turns/received", {"after": "a"}, None),
    ("GET", "/work-policy/v1/native-turns/received", {"after": "a"}, {}),
    ("GET", NATIVE + "t1/history", {"part": 1}, None),
    ("GET", NATIVE + "t1/history", {"after_turn": -1}, None),
    ("GET", NATIVE + "t1/history", {"limit": True}, None),
    ("GET", NATIVE + "t1/history", {"limit": "5"}, None),
    ("GET", NATIVE + "t1/result", {"after_turn": 0}, None),
    ("GET", NATIVE + "t1/other", {"part": 0}, None),
    ("POST", NATIVE + "t1/result", {"part": 0}, None),
    ("GET", NATIVE + "t1/result", {"part": 0}, {}),
    ("GET", "/behavior/v1/x", {"part": 0}, None),
]


@pytest.mark.parametrize("method,path,params,body", DENIED_PARAMS)
def test_other_query_parameters_are_denied_before_any_request(method, path, params, body):
    kind, value, seen = outcome(method, path, params=params, body=body)
    assert (kind, value, seen) == ("err", DENIED, [])


def test_empty_params_skip_the_check():
    kind, _, seen = outcome("POST", "/behavior/v1/x", params={}, body={})
    assert kind == "ok" and len(seen) == 1


# ---------------------------------------------------------------- response limits and accepted statuses

def test_response_is_capped_at_the_limit():
    assert outcome("GET", "/behavior/v1/x", content=b"x" * 10, limit=10)[0] == "ok"
    kind, value, _ = outcome("GET", "/behavior/v1/x", content=b"x" * 11, limit=10)
    assert (kind, value) == ("err", ("RESPONSE_LIMIT", 502, None, False, False, False))
    assert outcome("GET", "/behavior/v1/x", content=b"x" * 131072)[0] == "ok"
    assert outcome("GET", "/behavior/v1/x", content=b"x" * 131073)[1][0] == "RESPONSE_LIMIT"


@pytest.mark.parametrize("elapsed,expected", [(119.0, "ok"), (120.0, "err")])
def test_response_is_capped_at_120_seconds(monkeypatch, elapsed, expected):
    ticks = iter([0.0, elapsed, elapsed])
    monkeypatch.setattr(adapter_module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    kind, value, _ = outcome("GET", "/behavior/v1/x", content=b"abc")
    assert kind == expected
    if expected == "err":
        assert value == ("RESPONSE_LIMIT", 502, None, False, False, False)


@pytest.mark.parametrize("status", [200, 500])
def test_a_response_echoing_the_token_is_rejected(status):
    kind, value, _ = outcome("GET", "/behavior/v1/x", status=status, content=("a" + TOKEN + "b").encode())
    assert (kind, value) == ("err", ("RESULT_INVALID", 502, None, False, False, False))


DELEGATION = "/work-policy/v1/conversations/c1/turns/3/delegation"
NO_STORE = {"cache-control": "no-store"}
INVALID_DELEGATION = ("DELEGATION_RESPONSE_INVALID", 502, None, False, False, False)


def test_delegation_read_accepts_an_empty_no_store_204():
    kind, value, _ = outcome("GET", DELEGATION, status=204, content=b"", headers=NO_STORE)
    assert kind == "ok" and value[0] is None and value[1]["cache-control"] == "no-store"


@pytest.mark.parametrize("headers,content", [({}, b""), ({"cache-control": "no-cache"}, b"")])
def test_delegation_204_must_be_empty_and_no_store(headers, content):
    assert outcome("GET", DELEGATION, status=204, content=content, headers=headers)[:2] == ("err", INVALID_DELEGATION)


def test_delegation_204_with_content_is_invalid():
    # httpx drops a 204 body on the wire, so feed the bytes through a streamed response.
    adapter, _ = make(lambda request: httpx.Response(204, headers=NO_STORE, stream=httpx.ByteStream(b"x")))
    with pytest.raises(ConsoleError) as bad:
        adapter.send("GET", DELEGATION, None, TOKEN)
    assert (bad.value.code, bad.value.status) == ("DELEGATION_RESPONSE_INVALID", 502)


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/work-policy/v1/conversations/c1/turns/0/delegation", None),
    ("GET", "/work-policy/v1/conversations/c1/turns/3/other", None),
    ("GET", "/work-policy/v1/conversations//turns/3/delegation", None),
    ("GET", DELEGATION, {}),
    ("POST", DELEGATION, None),
    ("GET", "/behavior/v1/x", None)])
def test_a_204_elsewhere_is_an_ordinary_failure(method, path, body):
    kind, value, _ = outcome(method, path, status=204, content=b"", headers=NO_STORE, body=body)
    assert (kind, value) == ("err", ("BACKEND_UNAVAILABLE", 409, 204, False, False, False))


@pytest.mark.parametrize("method,path", [
    ("POST", NATIVE + "submit"), ("POST", NATIVE + "t1/submit"), ("POST", NATIVE + "append"),
    ("POST", "/work-policy/v1/responses-turns/submit"), ("POST", "/work-policy/v1/responses-turns/reconcile")])
def test_a_202_post_is_accepted_on_the_submit_paths(method, path):
    kind, value, _ = outcome(method, path, status=202, body={})
    assert kind == "ok" and value[0] == b"{}"


@pytest.mark.parametrize("method,path", [
    ("GET", NATIVE + "submit"), ("PUT", NATIVE + "submit"), ("POST", NATIVE + "cancel"), ("POST", NATIVE + "t1/append"),
    ("POST", "/work-policy/v1/responses-turns/cancel"), ("POST", "/work-policy/v1/responses-turns/t1/submit"),
    ("POST", "/work-policy/v1/other/submit"), ("POST", "/behavior/v1/work/submit")])
def test_a_202_anywhere_else_is_a_failure(method, path):
    kind, value, _ = outcome(method, path, status=202, body={})
    assert (kind, value) == ("err", ("BACKEND_UNAVAILABLE", 409, 202, False, False, False))


def test_a_redirect_is_not_followed():
    adapter, seen = make(lambda request: httpx.Response(302, headers={"location": "http://api.test/elsewhere"}))
    with pytest.raises(ConsoleError) as redirected:
        adapter.send("GET", "/behavior/v1/x", None, TOKEN)
    assert (redirected.value.code, redirected.value.upstream_status, len(seen)) == ("BACKEND_UNAVAILABLE", 302, 1)


def test_201_is_accepted_only_for_creating_a_group():
    assert outcome("POST", "/native-groups/v1/groups", status=201, body={})[0] == "ok"
    for method, path in (("GET", "/native-groups/v1/groups"), ("POST", "/native-groups/v1/groups/g1"),
                         ("POST", "/behavior/v1/x")):
        kind, value, _ = outcome(method, path, status=201, body={})
        assert (kind, value) == ("err", ("BACKEND_UNAVAILABLE", 409, 201, False, False, False))


@pytest.mark.parametrize("status,code", [(401, "LOGIN_REQUIRED"), (403, "BACKEND_DENIED"), (404, "NO_RESULT"),
                                         (409, "RESULT_UNKNOWN"), (412, "RESULT_UNKNOWN"), (429, "BACKEND_LIMIT"),
                                         (400, "BACKEND_UNAVAILABLE"), (500, "BACKEND_UNAVAILABLE"),
                                         (302, "BACKEND_UNAVAILABLE")])
def test_plain_failures_map_the_upstream_status(status, code):
    assert failure("GET", "/behavior/v1/x", status=status, raw=b"not json") == (code, 409, status, False, False, False)


@pytest.mark.parametrize("method,path", [("POST", "/behavior/v1/bindings/b/work/submit"), ("PUT", "/behavior/v1/x")])
def test_a_write_outside_the_service_routes_never_parses_the_error_body(method, path, monkeypatch):
    def parsed(_raw):
        raise AssertionError("the error body was parsed")
    monkeypatch.setattr(adapter_module, "strict_json", parsed)
    assert failure(method, path, error=code_error("ETAG_CONFLICT"), body={}) == FALLBACK


@pytest.mark.parametrize("path,code", [
    ("/team-skills/v1/reviews", "ETAG_CONFLICT"), ("/team-mcp/v1/servers", "TEAM_CONNECTOR_PRIVATE_NETWORK"),
    ("/team-connectors/v1/connectors", "TEAM_CONNECTOR_PRIVATE_NETWORK"), ("/native-groups/v1/groups", "NATIVE_GROUP_CLOSED"),
    ("/native-hitl/v1/rules", "NATIVE_HITL_ACTION_CHANGED"), (ROUTINES, "NATIVE_ROUTINE_CHANGED"),
    ("/work-policy/v1/agent-connectors", "AGENT_CONNECTORS_CHANGED")])
def test_a_write_on_a_service_route_still_reads_its_error_code(path, code):
    assert failure("POST", path, error=code_error(code), status=409, body={}) == (code, 409, 409, False, False, False)


@pytest.mark.parametrize("method,path,body,status,expected", [
    ("GET", "/behavior/v1/x", None, 500, FALLBACK),
    ("GET", "/behavior/v1/x", None, 409, ("RESULT_UNKNOWN", 409, 409, False, False, False)),
    ("POST", "/team-skills/v1/reviews", {}, 500, FALLBACK), ("POST", ROUTINES, {}, 500, FALLBACK)])
def test_a_parsed_error_body_too_deep_to_parse_falls_back(method, path, body, status, expected):
    # Under the size limit but deeper than the JSON parser's recursion limit: treated like any unreadable body.
    assert failure(method, path, raw=b"[" * 50000, status=status, body=body) == expected


@pytest.mark.parametrize("error", [httpx.ConnectError("down"), OSError("down")])
def test_transport_failure_is_offline(error):
    def refuse(request):
        raise error
    adapter, _ = make(refuse)
    with pytest.raises(ConsoleError) as offline:
        adapter.send("GET", "/behavior/v1/x", None, TOKEN)
    assert (offline.value.code, offline.value.status, offline.value.upstream_status) == ("BACKEND_OFFLINE", 503, None)
    assert offline.value.__cause__ is None and offline.value.__suppress_context__


# ---------------------------------------------------------------- automatic retry hint

@pytest.mark.parametrize("method,path,raw,expected", [
    ("GET", "/behavior/v1/x", {"error": {"automatic_retry": True}}, True),
    ("GET", "/behavior/v1/x", {"error": {"automatic_retry": "true"}}, False),
    ("GET", "/behavior/v1/x", {"error": {"automatic_retry": 1}}, False),
    ("GET", "/behavior/v1/x", {"error": []}, False),
    ("GET", "/behavior/v1/x", [1], False),
    ("POST", "/behavior/v1/x", {"error": {"automatic_retry": True}}, False),
    ("GET", "/team-skills/v1/x", {"error": {"automatic_retry": True, "code": "ETAG_CONFLICT"}}, True),
    ("GET", "/work-policy/v1/x", {"error": {"automatic_retry": True, "code": "CONVERSATION_ARCHIVED"}}, True),
    ("POST", "/work-policy/v1/x", {"error": {"automatic_retry": True, "code": "CONVERSATION_ARCHIVED"}}, False)])
def test_automatic_retry_is_taken_from_a_failed_read(method, path, raw, expected):
    value = failure(method, path, raw=json.dumps(raw).encode(), body={} if method == "POST" else None)
    assert value[5] is expected


def test_automatic_retry_survives_invalid_json_and_duplicates():
    assert failure("GET", "/behavior/v1/x", raw=b"{")[5] is False
    assert failure("GET", "/behavior/v1/x", raw=b'{"error":{"automatic_retry":true},"error":{}}')[5] is False


# ---------------------------------------------------------------- error families

def mapped(method, path, error, status, *, body=None):
    return failure(method, path, error=error, status=status, body=body)


@pytest.mark.parametrize("status,expected", [(404, 404), (412, 412), (499, 499), (500, 502), (399, 502), (302, 502)])
def test_team_skill_codes_keep_a_client_status_and_hide_a_server_one(status, expected):
    assert mapped("GET", "/team-skills/v1/reviews", code_error("ETAG_CONFLICT"), status) == (
        "ETAG_CONFLICT", expected, status, False, False, False)


def test_team_skill_unknown_or_malformed_codes_fall_back():
    for error in (code_error("NOPE"), code_error(5), code_error(["ETAG_CONFLICT"]), [1], {"code": "ETAG_CONFLICT"}["code"]):
        assert mapped("GET", "/team-skills/v1/reviews", error, 404) == ("NO_RESULT", 409, 404, False, False, False)
    assert failure("GET", "/team-skills/v1/reviews", raw=b"{", status=404)[0] == "NO_RESULT"
    assert mapped("GET", "/behavior/v1/x", code_error("ETAG_CONFLICT"), 404)[0] == "NO_RESULT"


@pytest.mark.parametrize("path", ["/team-mcp/v1/servers", "/team-connectors/v1/connectors"])
@pytest.mark.parametrize("status,expected", [(422, 422), (400, 400), (500, 502)])
def test_team_connector_codes_follow_the_same_status_rule(path, status, expected):
    assert mapped("GET", path, code_error("TEAM_CONNECTOR_PRIVATE_NETWORK"), status) == (
        "TEAM_CONNECTOR_PRIVATE_NETWORK", expected, status, False, False, False)


def test_team_connector_unknown_code_and_other_prefix_fall_back():
    assert mapped("GET", "/team-mcp/v1/servers", code_error("NOPE"), 500) == FALLBACK
    assert mapped("GET", "/team-mcp/servers", code_error("TEAM_CONNECTOR_PRIVATE_NETWORK"), 500) == FALLBACK
    assert mapped("GET", "/team-skills/v1/x", code_error("TEAM_CONNECTOR_PRIVATE_NETWORK"), 500) == FALLBACK


@pytest.mark.parametrize("status", [404, 409, 500, 503])
def test_native_group_codes_keep_their_status(status):
    assert mapped("POST", "/native-groups/v1/groups/g1", code_error("NATIVE_GROUP_CLOSED"), status, body={}) == (
        "NATIVE_GROUP_CLOSED", status, status, False, False, False)


def test_native_group_unknown_code_and_other_prefix_fall_back():
    assert mapped("GET", "/native-groups/v1/groups", code_error("NOPE"), 500) == FALLBACK
    assert mapped("GET", "/native-groups/v1/groups", code_error(7), 500) == FALLBACK
    assert mapped("GET", "/native-group/v1/groups", code_error("NATIVE_GROUP_CLOSED"), 500) == FALLBACK


@pytest.mark.parametrize("code", ["NATIVE_HITL_ACTION_CHANGED", "NATIVE_ROUTINE_CHANGED"])
def test_native_hitl_path_takes_hitl_and_routine_codes(code):
    assert mapped("POST", "/native-hitl/v1/rules", code_error(code), 422, body={}) == (code, 422, 422, False, False, False)


def test_routine_definitions_take_routine_codes_only():
    assert mapped("GET", ROUTINES, code_error("NATIVE_ROUTINE_CHANGED"), 409) == (
        "NATIVE_ROUTINE_CHANGED", 409, 409, False, False, False)
    assert mapped("GET", ROUTINES + "/d1", code_error("NATIVE_ROUTINE_CHANGED"), 500) == (
        "NATIVE_ROUTINE_CHANGED", 500, 500, False, False, False)
    assert mapped("GET", ROUTINES, code_error("NATIVE_HITL_ACTION_CHANGED"), 500) == FALLBACK
    assert mapped("GET", "/native-hitl/v1/x", code_error("NOPE"), 500) == FALLBACK
    assert mapped("GET", "/native-hitl/v1/x", code_error(["NATIVE_ROUTINE_CHANGED"]), 500) == FALLBACK
    assert mapped("GET", "/native-routines/v1/agents/a1/other", code_error("NATIVE_ROUTINE_CHANGED"), 500) == FALLBACK


# ---------------------------------------------------------------- work-policy

WP = "/work-policy/v1/"


@pytest.mark.parametrize("code", ["REALTIME_DISABLED", "REALTIME_UNAVAILABLE"])
def test_realtime_negotiation_failures_become_503(code):
    assert mapped("GET", WP + "realtime/negotiate", code_error(code), 404) == (code, 503, 404, False, False, False)
    assert mapped("POST", WP + "realtime/negotiate", code_error(code), 404, body={}) == (
        "NO_RESULT", 409, 404, False, False, False)
    assert mapped("GET", WP + "realtime/negotiate", code_error("OTHER"), 404)[0] == "NO_RESULT"
    assert mapped("GET", WP + "realtime/other", code_error(code), 404)[0] == "NO_RESULT"


@pytest.mark.parametrize("code", [["REALTIME_DISABLED"], {"x": 1}])
def test_realtime_negotiation_with_an_unhashable_code_falls_back(code):
    assert mapped("GET", WP + "realtime/negotiate", code_error(code), 500) == FALLBACK


@pytest.mark.parametrize("status,expected", [(400, True), (404, True), (499, True), (399, False), (500, False)])
def test_agent_connector_codes_only_map_client_statuses(status, expected):
    value = mapped("GET", WP + "agent-connectors", code_error("AGENT_CONNECTORS_CHANGED"), status)
    assert value == (("AGENT_CONNECTORS_CHANGED", status, status, False, False, False) if expected
                     else ("BACKEND_UNAVAILABLE", 409, status, False, False, False))


def test_agent_connector_unknown_code_or_path_fall_back():
    assert mapped("GET", WP + "agent-connectors", code_error("NOPE"), 404)[0] == "NO_RESULT"
    assert mapped("GET", WP + "agent-connectors/x", code_error("AGENT_CONNECTORS_CHANGED"), 404)[0] == "NO_RESULT"
    assert mapped("GET", WP + "agent-connectors", code_error([1]), 404)[0] == "NO_RESULT"


EXTRA_NATIVE_CODES = ["FOUNDRY_INVOCATION_UNKNOWN", "FOUNDRY_RESPONSE_INVALID", "FOUNDRY_NATIVE_BINDING_MISMATCH",
                      "BEHAVIOR_COMMIT_UNKNOWN", "AGENT_USAGE_CONFLICT", "INSTRUCTION_ROUTE_CHANGED"]


@pytest.mark.parametrize("path", [NATIVE + "history", "/work-policy/v1/responses-turns/x"])
@pytest.mark.parametrize("code", ["AGENT_RETIRED", "AGENT_VERSION_FOLLOWING"] + EXTRA_NATIVE_CODES)
@pytest.mark.parametrize("status", [409, 500])
def test_native_and_responses_turn_codes_are_kept(path, code, status):
    assert mapped("GET", path, code_error(code), status) == (code, status, status, False, False, False)


def test_native_codes_do_not_apply_to_other_work_policy_paths():
    assert mapped("GET", WP + "conversations", code_error("AGENT_RETIRED"), 500) == FALLBACK
    assert mapped("GET", NATIVE + "history", code_error("NOPE"), 500) == FALLBACK
    assert mapped("GET", NATIVE + "history", code_error(["AGENT_RETIRED"]), 500) == FALLBACK
    assert mapped("GET", "/work-policy/v1/native-turn/x", code_error("AGENT_RETIRED"), 500) == FALLBACK


SUBMIT_PATHS = [NATIVE + "submit", WP + "responses-turns/submit", NATIVE + "cancel", WP + "responses-turns/cancel",
                NATIVE + "delivered", NATIVE + "append"]


def not_dispatched(path="submit", method="POST", status=409, code="CUSTOM_CODE", body=None, **extra):
    error = code_error(code, not_dispatched=True, request_id="r1", **extra)
    full = path if path.startswith("/") else NATIVE + path
    return mapped(method, full, error, status, body={"request_id": "r1"} if body is None else body)


@pytest.mark.parametrize("path", SUBMIT_PATHS)
def test_a_certain_non_dispatch_is_reported_with_its_own_code(path):
    assert not_dispatched(path) == ("CUSTOM_CODE", 409, 409, True, False, False)


@pytest.mark.parametrize("kwargs", [
    {"method": "GET"}, {"path": "received"}, {"status": 399}, {"status": 600}, {"code": "lower"},
    {"code": "A" * 81}, {"code": "1ABC"}, {"body": {"request_id": "other"}}, {"body": {}}])
def test_non_dispatch_needs_every_condition(kwargs):
    value = not_dispatched(status=kwargs.pop("status", 500), **kwargs)
    assert value[3] is False and value[0] != "CUSTOM_CODE"


def test_non_dispatch_boundaries_and_flag_values():
    assert not_dispatched(status=400)[3:] == (True, False, False)
    assert not_dispatched(status=599)[3:] == (True, False, False)
    assert not_dispatched(code="A" * 80)[:1] == ("A" * 80,)
    assert not_dispatched(code="Z" + "9_" * 5)[3] is True
    assert mapped("POST", NATIVE + "submit", code_error("CUSTOM_CODE", not_dispatched="true", request_id="r1"), 500,
                  body={"request_id": "r1"}) == FALLBACK
    assert mapped("POST", NATIVE + "submit", code_error("CUSTOM_CODE", not_dispatched=True, request_id="r1"), 500,
                  body=[]) == FALLBACK


def test_non_dispatch_with_a_listed_code_without_the_flag_is_not_marked():
    assert mapped("POST", NATIVE + "submit", code_error("AGENT_RETIRED", request_id="r1"), 409, body={"request_id": "r1"}) == (
        "AGENT_RETIRED", 409, 409, False, False, False)
    assert mapped("POST", NATIVE + "submit", code_error("AGENT_RETIRED", request_id="r2", not_dispatched=True), 409,
                  body={"request_id": "r1"}) == ("AGENT_RETIRED", 409, 409, False, False, False)


@pytest.mark.parametrize("status,code,expected", [
    (409, "NATIVE_SESSION_CHANGED", True), (503, "AGENT_VERSION_FOLLOWING", True),
    (503, "NATIVE_EXECUTION_UNAVAILABLE", True), (409, "AGENT_VERSION_FOLLOWING", False),
    (503, "NATIVE_SESSION_CHANGED", False), (409, "NATIVE_EXECUTION_UNAVAILABLE", False),
    (500, "AGENT_VERSION_FOLLOWING", False)])
def test_server_following_needs_an_exact_status_and_code(status, code, expected):
    assert not_dispatched(status=status, code=code, server_following=True)[3:] == (True, expected, False)
    assert not_dispatched(status=status, code=code)[4] is False
    assert not_dispatched(status=status, code=code, server_following="true")[4] is False


def test_server_following_is_never_set_without_non_dispatch():
    value = mapped("POST", NATIVE + "submit", code_error("NATIVE_SESSION_CHANGED", server_following=True,
                                                          request_id="other"), 409, body={"request_id": "r1"})
    assert value[3:] == (False, False, False)


DELEGATION_CODES = ["DELEGATION_UNKNOWN", "DELEGATION_UNAVAILABLE", "DELEGATION_DISABLED", "DELEGATION_SESSION_READ_ONLY",
                    "DELEGATION_PARENT_REQUIRED", "DELEGATION_SCOPE_CHANGED", "DELEGATION_RECORD_INVALID",
                    "DELEGATION_RESULT_INVALID"]


@pytest.mark.parametrize("code", DELEGATION_CODES)
def test_delegation_read_codes_are_conflicts(code):
    assert mapped("GET", DELEGATION, code_error(code), 404) == (code, 409, 404, False, False, False)
    assert mapped("GET", DELEGATION, code_error(code), 500) == (code, 409, 500, False, False, False)


def test_delegation_codes_need_a_delegation_read():
    code = code_error("DELEGATION_UNKNOWN")
    assert mapped("POST", DELEGATION, code, 500, body={}) == FALLBACK
    assert mapped("GET", WP + "conversations/c1/turns/0/delegation", code, 500) == FALLBACK
    assert mapped("GET", WP + "conversations/c1/turns/3/delegation", code_error(["DELEGATION_UNKNOWN"]), 500) == FALLBACK


CONVERSATION_CODES = ["CONVERSATION_ARCHIVED", "CONVERSATION_SESSION_RESET_REQUIRED", "CONVERSATION_SESSION_UNAVAILABLE",
                      "CONVERSATION_SESSION_VERSION_CHANGED", "CONVERSATION_TURN_UNRESOLVED", "WORK_SUBMIT_UNKNOWN",
                      "CANCELLATION_RECEIPT_UNKNOWN", "CANCELLATION_RECEIPT_CHANGED", "CANCELLATION_RECEIPT_INVALID"]
TRANSFER_CODES = ["CONVERSATION_TRANSFER_NOT_READY", "CONVERSATION_TRANSFER_SCOPE", "CONVERSATION_TRANSFER_ARCHIVE_INVALID",
                  "CONVERSATION_TRANSFER_UNAVAILABLE", "CONVERSATION_TRANSFER_UNKNOWN", "CONVERSATION_TRANSFER_CHANGED",
                  "CONVERSATION_TRANSFER_CONFLICT", "CONVERSATION_TRANSFER_INVALID", "CONVERSATION_TRANSFER_BUDGET"]


@pytest.mark.parametrize("code", CONVERSATION_CODES + TRANSFER_CODES)
def test_conversation_and_transfer_codes_are_conflicts_on_any_work_policy_path(code):
    assert mapped("POST", WP + "conversations", code_error(code), 500, body={}) == (code, 409, 500, False, False, False)
    assert mapped("GET", WP + "x", code_error(code), 404) == (code, 409, 404, False, False, False)
    assert mapped("GET", "/behavior/v1/x", code_error(code), 500) == FALLBACK


def test_template_profile_unsupported_is_a_conflict_on_a_409_read():
    code = code_error("AGENT_TEMPLATE_PROFILE_UNSUPPORTED")
    path = WP + "agent-template"
    assert mapped("GET", path, code, 409) == ("AGENT_TEMPLATE_PROFILE_UNSUPPORTED", 409, 409, False, False, False)
    assert mapped("GET", path, code, 404)[0] == "NO_RESULT"
    assert mapped("POST", path, code, 409, body={})[0] == "RESULT_UNKNOWN"
    assert mapped("GET", path + "/x", code, 409)[0] == "RESULT_UNKNOWN"
    assert mapped("GET", path, code_error("OTHER"), 409)[0] == "RESULT_UNKNOWN"


@pytest.mark.parametrize("code", ["SYSTEM_PROMPT_UNRECONSTRUCTABLE", "AGENT_TEMPLATE_PROFILE_UNSUPPORTED",
                                  "AGENT_ROLE_PROMPT_INVALID", "AGENT_ROLE_PROMPT_MISSING", "CAPABILITY_SOURCES_CHANGED",
                                  "NATIVE_TURNS_DISABLED"])
def test_system_prompt_codes_are_conflicts_on_a_read(code):
    path = WP + "agent-template/system-prompt"
    assert mapped("GET", path, code_error(code), 500) == (code, 409, 500, False, False, False)
    assert mapped("POST", path, code_error(code), 500, body={}) == FALLBACK
    assert mapped("GET", path + "x", code_error(code), 500) == FALLBACK
    assert mapped("GET", path, code_error(code + "X"), 500) == FALLBACK


SELECTION_CODES = ["PROFILE_SELECTION_UNSUPPORTED", "PROFILE_SELECTION_DISABLED", "PROFILE_SELECTION_DENIED",
                   "PROFILE_SELECTION_CONFLICT", "PROFILE_SELECTION_SOURCES_CHANGED", "PROFILE_SELECTION_MISSING",
                   "PROFILE_SELECTION_INVALID", "PROFILE_SELECTION_UNKNOWN"]


@pytest.mark.parametrize("path", [WP + "agent-template/select", WP + "agent-template/selection",
                                  WP + "agent-template/selection-reconcile"])
@pytest.mark.parametrize("code", SELECTION_CODES)
def test_profile_selection_codes_are_conflicts_on_the_selection_paths(path, code):
    assert mapped("POST", path, code_error(code), 500, body={}) == (code, 409, 500, False, False, False)
    assert mapped("GET", path, code_error(code), 404) == (code, 409, 404, False, False, False)


def test_profile_selection_codes_need_a_selection_path_and_a_listed_code():
    code = code_error("PROFILE_SELECTION_DENIED")
    assert mapped("POST", WP + "agent-template/other", code, 500, body={}) == ("BACKEND_UNAVAILABLE", 409, 500, False, False, False)
    assert mapped("POST", WP + "agent-template/select", code_error("PROFILE_SELECTION_OTHER"), 500, body={}) == FALLBACK
    assert mapped("POST", WP + "agent-template/select", code_error(["PROFILE_SELECTION_DENIED"]), 500, body={}) == FALLBACK
    assert mapped("POST", WP + "agent-template/select", code, 500, body={}) == (
        "PROFILE_SELECTION_DENIED", 409, 500, False, False, False)


def test_unknown_work_policy_failures_fall_back():
    assert mapped("GET", WP + "x", code_error("NOPE"), 500) == FALLBACK
    assert mapped("GET", WP + "x", None, 500) == FALLBACK
    assert mapped("GET", WP + "x", "text", 500) == FALLBACK
    assert failure("GET", WP + "x", raw=b"[1]") == FALLBACK
