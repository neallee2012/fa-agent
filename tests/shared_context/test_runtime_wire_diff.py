"""Self-check of runtime_wire_diff's diff on two synthetic recordings; recording itself needs real runtimes."""
import copy

from runtime_wire_diff import diff


def test_the_diff_names_each_changed_field_once_per_request_and_ignores_per_session_values():
    sent = {"max_tokens": 128000, "prompt_cache_key": "a" * 32, "messages": [{"content": "x" * 500}]}
    old = {"cases": {"case": {"error": None, "requests": [{"runtime": sent, "gateway": sent, "sent": {"store": False}}]}}}
    assert diff(old, copy.deepcopy(old)) == []

    shaped = {"max_completion_tokens": 128000, "prompt_cache_key": "b" * 32, "text": {"verbosity": "medium"},
              "messages": [{"content": "y" * 501}]}
    new = {"cases": {"case": {"error": "TimeoutError: late", "failure_events": [{"type": "session.error"}], "requests": [
        {"runtime": {**sent, "prompt_cache_key": "c" * 32, "messages": [{"content": "y" * 501}]},
         "gateway": shaped, "refused": "CODE"}, {}]}}}
    assert diff(old, new) == [
        'case error: None -> TimeoutError: late',
        "case failure_events: [] -> ['session.error']",
        'case requests: 1 -> 2',
        'case request#1 runtime,gateway messages[0].content: "<text len=500>" -> "<text len=501>"',
        'case request#1 gateway max_completion_tokens: "<absent>" -> 128000',
        'case request#1 gateway max_tokens: 128000 -> "<absent>"',
        'case request#1 gateway text.verbosity: "<absent>" -> "medium"',
        'case request#1 sent store: false -> "<absent>"',
        'case request#1 refused .: "<absent>" -> "CODE"',
    ]
