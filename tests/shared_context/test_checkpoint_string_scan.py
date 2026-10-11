"""Bounded scalar-scan work checks; no SDK, model, timing SLA or long regex runs."""
import itertools
import json
import random
import re

import pytest

from shared_context import behavior_checkpoint_privacy as privacy
from shared_context.errors import Problem


class ObservedText(str):
    def __new__(cls, text):
        value = super().__new__(cls, text)
        value.visits = 0
        value.slices = []
        return value

    def __iter__(self):
        for char in super().__iter__():
            self.visits += 1
            assert self.visits <= len(self), "scanner revisited the input"
            yield char

    def __getitem__(self, key):
        assert isinstance(key, slice), "scanner must consume characters through one forward iterator"
        self.slices.append((key.start, key.stop))
        return super().__getitem__(key)


@pytest.mark.parametrize("value", [
    "", "plain text", 'quote " and slash \\', "\b\f\n\r\t/", "中文\u2028\u2029\u0085",
    "\U0001d11e", "\ud800",
])
@pytest.mark.parametrize("ascii_only", [False, True])
def test_valid_json_scalars_decode_exactly_once(value, ascii_only):
    encoded = json.dumps(value, ensure_ascii=ascii_only)
    assert list(privacy.json_string_values(encoded)) == [value]


@pytest.mark.parametrize("text", [
    '"unterminated', '"dangling\\', '"bad\\xescape"', '"bad\\u123"', '"bad\\u12x4"',
    '"literal\ncontrol"', '"literal\x00control"', "null true false 123 [1, 2]",
])
def test_invalid_scalars_and_non_string_values_are_not_decoded(text):
    assert list(privacy.json_string_values(text)) == []


def test_invalid_unicode_quote_can_start_a_later_valid_scalar():
    assert list(privacy.json_string_values('"bad\\u12"valid"')) == ["valid"]


def test_bounded_fragment_semantics_match_the_previous_pattern():
    # Only short inputs use the old oracle; never the long escape regression.
    previous = re.compile(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')
    alphabet = '"\\u0x\n'
    samples = ["".join(chars) for size in range(5) for chars in itertools.product(alphabet, repeat=size)]
    rng = random.Random(83013)
    samples.extend("".join(rng.choice(alphabet + "abf1") for _ in range(32)) for _ in range(256))
    samples.extend(['prefix "one" {"two":"three"} suffix', r'"bad\u12"valid"', r'"\u005c" "\/"'])
    for text in samples:
        expected = [json.loads(match[0]) for match in previous.finditer(text)]
        assert list(privacy.json_string_values(text)) == expected


@pytest.mark.parametrize("repetitions", [1024, 4096, 16384, 32768, 65536])
@pytest.mark.parametrize("tail", ["\n", "\\x", "\\u12x4", ""])
def test_malformed_escape_runs_have_one_forward_pass(repetitions, tail):
    text = ObservedText(('\\"' * repetitions) + tail)
    assert list(privacy.json_string_values(text)) == []
    assert text.visits == len(text)
    assert text.slices == []


def test_successful_scalar_slices_never_overlap_or_exceed_input():
    parts = [json.dumps("value " + str(index) + ' \\" ' * 8) for index in range(256)]
    text = ObservedText(" ".join(parts))
    result = list(privacy.json_string_values(text))
    assert result == [json.loads(part) for part in parts]
    assert text.visits == len(text)
    assert len(text.slices) == len(parts)
    assert all(end <= start for (_, end), (start, _) in zip(text.slices, text.slices[1:]))
    assert sum(end - start for start, end in text.slices) <= len(text)


@pytest.mark.parametrize("layers", [0, 1, 2, 4, 8])
def test_known_private_values_after_malformed_runs_still_reject(layers):
    marker = 'PRIVATE_SCAN_"秘密"'
    encoded = '"' + "".join(f"\\u{ord(char):04x}" for char in marker) + '"'
    for _ in range(layers):
        encoded = json.dumps(encoded, ensure_ascii=True)
    text = ('\\"' * 8192) + "\n" + encoded
    with pytest.raises(Problem) as error:
        privacy.reject_private_copies({"temp/opaque.txt": text}, [], [marker], nested_json=True)
    assert error.value.body["error"]["code"] == "SDK_STATE_PRIVATE_MATERIAL"


def test_public_malformed_content_is_not_reinterpreted_as_private_structure():
    text = ('\\"' * 32768) + '\n{"systemPrompt":"Public example"}'
    files = {"events.jsonl": json.dumps({"type": "assistant.message", "data": {"content": text}}) + "\n"}
    before = dict(files)
    privacy.reject_private_copies(files, [], ['ABSENT_PRIVATE_"值"'], nested_json=True)
    assert files == before
