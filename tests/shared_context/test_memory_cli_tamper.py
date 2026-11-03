"""The human memory CLI refuses each altered server answer before it prints, confirms or accepts a receipt.

Each case edits ONE field of an otherwise real answer. Where the answer is a wire model, the edited answer must
still parse, so a refusal can only come from the CLI's own comparison with what the human chose. A case counts
only when its edit was actually applied to the answer (the original request succeeded first).
"""

import copy
import json

from shared_context import behavior_memory_cli as cli
from shared_context.memory_note_proposals import NoteProposalView
from shared_context.memory_note_synthesis import NoteSynthesisView
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_memory_bridge_settings import (
    SETTINGS, install_settings, settings_cli_arguments, settings_cli_environment, settings_command,
)
from test_memory_note_bridge import bridge_case, bridge_cli, cli_arguments
from test_memory_note_proposal_cli import arguments, environment
from test_memory_note_proposals import install_case, proposal_case
from test_memory_note_synthesis import synthesis_case
from test_memory_synthesis_cli import args as synthesis_arguments


class Tamper:
    """Wrap the CLI's one HTTP helper: the real answer for a selected request goes through ``self.change``."""

    def __init__(self, monkeypatch, selector):
        self.change, self.applied, self.invalid, self.seen = None, 0, [], []
        original = cli._request

        def request(http, token, method, path, body=None):
            value = original(http, token, method, path, body)
            self.seen.append((method, path))
            if self.change is not None and selector(method, path):
                self.applied += 1
                return self.change(value)
            return value

        monkeypatch.setattr(cli, "_request", request)

    def arm(self, change, model=None, name=""):
        self.applied, self.seen = 0, []

        def edited(value):
            value = change(value)
            if model is not None:
                try:
                    model.model_validate_json(json.dumps(value))
                except Exception as error:  # the edit alone broke the wire model: the case proves nothing
                    self.invalid.append((name, repr(error)[:200]))
            return value

        self.change = edited


def deep(root, dotted, new):
    root = copy.deepcopy(root)
    node = root
    *parents, last = dotted.split(".")
    for name in parents:
        node = node[int(name)] if isinstance(node, list) else node[name]
    if isinstance(node, list):
        node[int(last)] = new
    else:
        node[last] = new
    return root


def move_span(value, dotted):
    """Shift a byte span as a whole: the wire model keeps its length, only the position differs."""
    value = copy.deepcopy(value)
    node = value
    for name in dotted.split("."):
        node = node[int(name)] if isinstance(node, list) else node[name]
    step = -1 if node["start_byte"] > 0 else 1
    node["start_byte"] += step
    node["end_byte"] += step
    return value


def bump(value, dotted):
    return deep(value, dotted, _get(value, dotted) + 1)


def _get(value, dotted):
    for name in dotted.split("."):
        value = value[int(name)] if isinstance(value, list) else value[name]
    return value


def drop(value, key):
    return {k: x for k, x in value.items() if k != key}


def run_cases(cases, tamper, run, model=None):
    """Run ``run()`` once per case with that edit armed; collect the cases that were NOT refused as expected."""
    wrong = []
    for name, change in cases:
        tamper.arm(change, model, name)
        outcome = run()
        if tamper.applied != 1 or outcome is not True:
            wrong.append((name, tamper.applied, outcome))
    assert not tamper.invalid, [name for name, _ in tamper.invalid]
    return wrong


PREVIEW_EDITS = [
    ("binding_id", lambda v: deep(v, "binding_id", "other-binding")),
    ("request_id", lambda v: deep(v, "request_id", "other-request")),
    ("team_id", lambda v: deep(v, "team_id", "other-team")),
    ("work_id", lambda v: deep(v, "work_id", "other-work")),
    ("state", lambda v: deep(deep(v, "proposal_id", "unexpected-proposal"), "state", "pending_review")),
    ("conversation_id", lambda v: deep(v, "source.conversation_id", "other-conversation")),
    ("turn_number", lambda v: deep(v, "source.turn_number", 7)),
    ("speaker", lambda v: deep(v, "source.speaker", "assistant")),
    ("source_sha256", lambda v: deep(v, "source.source_sha256", "0" * 64)),
    ("span", lambda v: move_span(v, "source")),
]

RECEIPT_EDITS = [
    ("extra-envelope", lambda v: {**v, "extra": 1}),
    ("missing-digest", lambda v: drop(v, "control_request_digest")),
    ("short-digest", lambda v: {**v, "control_request_digest": "abc"}),
    ("result-not-object", lambda v: {**v, "result": "pending_review"}),
    ("extra-result-field", lambda v: {**v, "result": {**v["result"], "extra": 1}}),
    ("other-request", lambda v: deep(v, "result.request_id", "other-request")),
    ("not-pending", lambda v: deep(v, "result.state", "not_submitted")),
    ("effective", lambda v: deep(v, "result.effective", True)),
    ("bridge-available", lambda v: deep(v, "result.bridge_available", True)),
    ("bad-proposal-id", lambda v: deep(v, "result.proposal_id", 5)),
    ("other-control-id", lambda v: {**v, "control_request_id": "other-control"}),
]


def proposal_runner(case, monkeypatch, tmp_path, capsys, command, synthesis):
    original = tmp_path / "proposal.json"
    original.write_text(json.dumps(command), encoding="utf-8")
    marker = original.with_name(original.name + (".synthesis" if synthesis else "") + ".attempt.json")
    argv = (synthesis_arguments(case, "propose", original) if synthesis else arguments(case, "propose", original))

    def refused_before_confirmation():
        marker.unlink(missing_ok=True)
        code = cli.main(argv)
        out = capsys.readouterr()
        return code == 1 and not marker.exists() and out.out.count("\n") <= 1 and "MEMORY_NOTE_CLI_UNCONFIRMED" in out.err

    def refused_after_receipt():
        marker.unlink(missing_ok=True)
        code = cli.main(argv)
        out = capsys.readouterr()
        # the durable attempt was written before the send, so only the original receipt may be looked up
        return code == 1 and marker.exists() and '"confirmed"' not in out.out and "MEMORY_NOTE_CLI_UNCONFIRMED" in out.err

    return refused_before_confirmation, refused_after_receipt


def test_a_proposal_preview_or_receipt_that_differs_from_the_sharing_command_is_refused(
        proposal_case, monkeypatch, tmp_path, capsys):
    path, command = install_case(proposal_case)
    environment(proposal_case, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "share-note")
    before_confirmation, after_receipt = proposal_runner(proposal_case, monkeypatch, tmp_path, capsys, command, False)
    preview = Tamper(monkeypatch, lambda method, route: route == path + "/preview")
    assert run_cases(PREVIEW_EDITS, preview, before_confirmation, NoteProposalView) == []
    assert preview.seen == [("POST", path + "/preview")]  # nothing but the preview was sent
    preview.change = None
    receipt = Tamper(monkeypatch, lambda method, route: method == "POST" and route == path)
    assert run_cases(RECEIPT_EDITS, receipt, after_receipt) == []


def other_conversation(value):
    """The summary and every supporting original in another conversation: the wire model needs them to agree."""
    value = deep(value, "summary_source.conversation_id", "other-conversation")
    for index in range(len(value["evidence"]["fragments"])):
        value = deep(value, f"evidence.fragments.{index}.source.conversation_id", "other-conversation")
    return value


SYNTHESIS_PREVIEW_EDITS = [
    ("conversation", other_conversation),
    ("summary-turn", lambda v: bump(v, "summary_source.turn_number")),
    ("summary-sha", lambda v: deep(v, "summary_source.source_sha256", "0" * 64)),
    ("first-sha", lambda v: deep(v, "evidence.fragments.0.source.source_sha256", "0" * 64)),
    ("second-span", lambda v: move_span(v, "evidence.fragments.1.source")),
    ("binding", lambda v: deep(v, "binding_id", "other-binding")),
    ("state", lambda v: deep(deep(v, "proposal_id", "unexpected-proposal"), "state", "pending_review")),
]


def test_a_synthesis_preview_or_receipt_that_differs_from_the_selected_originals_is_refused(
        synthesis_case, monkeypatch, tmp_path, capsys):
    c = synthesis_case
    environment(c, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "share-synthesis")
    before_confirmation, after_receipt = proposal_runner(c, monkeypatch, tmp_path, capsys, c.command, True)
    preview = Tamper(monkeypatch, lambda method, route: route == c.path + "/preview")
    assert run_cases(SYNTHESIS_PREVIEW_EDITS, preview, before_confirmation, NoteSynthesisView) == []
    assert preview.seen == [("POST", c.path + "/preview")]
    preview.change = None
    receipt = Tamper(monkeypatch, lambda method, route: method == "POST" and route == c.path)
    synthesis_receipts = [
        # a synthesis receipt carries no bridge flag: one that does is a different receipt shape
        ("bridge-flag", lambda v: deep(v, "result.bridge_available", False)),
        ("extra-envelope", lambda v: {**v, "extra": 1}),
        ("other-control-id", lambda v: {**v, "control_request_id": "other-control"}),
        ("not-pending", lambda v: deep(v, "result.state", "not_submitted")),
    ]
    assert run_cases(synthesis_receipts, receipt, after_receipt) == []


SETTINGS_PREVIEW_EDITS = [
    ("extra-key", lambda v: {**v, "extra": 1}),
    ("other-command", lambda v: deep(v, "command.request_id", "other-request")),
    ("applies-to", lambda v: {**v, "applies_to": "all-conversations"}),
    ("scope-unchanged", lambda v: {**v, "data_scope_change": False}),
    ("already-submitted", lambda v: {**v, "submitted": True}),
    ("note-missing", lambda v: {**v, "note": None}),
    ("note-not-object", lambda v: {**v, "note": "text"}),
    ("note-extra-key", lambda v: deep(v, "note.extra", 1)),
    ("note-other-origin", lambda v: deep(v, "note.origin_binding_id", "other-binding")),
    ("note-usage", lambda v: deep(v, "note.usage", "instruction")),
    ("note-other-claim", lambda v: deep(v, "note.source.claim_id", "other-claim")),
    ("note-other-version", lambda v: bump(v, "note.source.claim_version")),
    ("note-other-claim-hash", lambda v: deep(v, "note.source.claim_sha256", "0" * 64)),
    ("note-other-length", lambda v: bump(v, "note.source.text_utf8_bytes")),
    ("note-other-text-hash", lambda v: deep(v, "note.source.text_sha256", "0" * 64)),
]

SETTINGS_RECEIPT_EDITS = [
    ("extra-envelope", lambda v: {**v, "extra": 1}),
    ("missing-digest", lambda v: drop(v, "control_request_digest")),
    ("short-digest", lambda v: {**v, "control_request_digest": "abc"}),
    ("no-settings-id", lambda v: deep(v, "result.settings_id", None)),
    ("disabled", lambda v: deep(v, "result.enabled", False)),
    ("no-recall", lambda v: deep(v, "result.recall", None)),
    ("other-recall", lambda v: deep(v, "result.recall.version_policy", "pinned")),
    ("other-control-id", lambda v: {**v, "control_request_id": "other-control"}),
]


def test_a_settings_preview_or_receipt_that_differs_from_the_human_choice_is_refused(
        proposal_case, monkeypatch, tmp_path, capsys):
    install_settings(proposal_case)
    command = settings_command(proposal_case, recall=True)
    settings_cli_environment(proposal_case, monkeypatch)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(command), encoding="utf-8")
    marker = path.with_name(path.name + ".memory-settings.attempt.json")
    select = settings_cli_arguments("select-memory-settings", path)

    def before_confirmation():
        code = cli.main(select)
        out = capsys.readouterr()
        return code == 1 and not marker.exists() and out.out == "" and "MEMORY_NOTE_CLI_UNCONFIRMED" in out.err

    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("confirmation asked")))
    preview = Tamper(monkeypatch, lambda method, route: route == SETTINGS + "/preview")
    assert run_cases(SETTINGS_PREVIEW_EDITS, preview, before_confirmation) == []
    assert preview.seen == [("POST", SETTINGS + "/preview")]
    # One real selection (the setting can be made once), then the read-only reconcile answers are altered.
    preview.change = None
    monkeypatch.setattr("builtins.input", lambda _: "select-memory")
    assert cli.main(select) == 0 and marker.exists()
    capsys.readouterr()
    saved = marker.read_bytes()
    reconcile = settings_cli_arguments("memory-settings-reconcile", path)

    def after_receipt():
        code = cli.main(reconcile)
        out = capsys.readouterr()
        return code == 1 and marker.read_bytes() == saved and '"confirmed"' not in out.out and (
            "MEMORY_NOTE_CLI_UNCONFIRMED" in out.err)

    receipt = Tamper(monkeypatch, lambda method, route: route == SETTINGS + "/reconcile")
    assert run_cases(SETTINGS_RECEIPT_EDITS, receipt, after_receipt) == []
    receipt.change = None
    assert cli.main(reconcile) == 0  # the same request, unaltered, is accepted


def test_a_settings_preview_with_a_note_for_a_note_free_setting_is_refused(
        proposal_case, monkeypatch, tmp_path, capsys):
    install_settings(proposal_case)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings_command(proposal_case, recall=False)), encoding="utf-8")
    settings_cli_environment(proposal_case, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("confirmation asked")))
    note = {"origin_binding_id": "x", "text": "x", "source": {}, "usage": "knowledge-data-not-permission"}
    tamper = Tamper(monkeypatch, lambda method, route: route == SETTINGS + "/preview")
    tamper.arm(lambda v: {**v, "note": note})
    assert cli.main(settings_cli_arguments("select-memory-settings", path)) == 1
    assert tamper.applied == 1
    out = capsys.readouterr()
    assert out.out == "" and not path.with_name(path.name + ".memory-settings.attempt.json").exists()


BRIDGE_PREVIEW_EDITS = [
    ("other-bridge", lambda v: deep(v, "bridge_id", "0" * 64)),
    ("other-proposal", lambda v: deep(v, "proposal_id", "other-proposal")),
    ("other-binding", lambda v: deep(v, "binding_id", "other-binding")),
    ("other-mapping", lambda v: deep(v, "mapping.request_id", "other-bridge-request")),
    ("other-text", lambda v: deep(v, "text", "another selected text")),
    ("effective", lambda v: deep(v, "effective", True)),
    ("other-source-id", lambda v: deep(v, "source.source_id", "other-source")),
    ("other-source-version", lambda v: deep(v, "source.version", 2)),
    ("other-source-hash", lambda v: deep(v, "source.sha256", "0" * 64)),
    ("other-source-length", lambda v: bump(v, "source.byte_length")),
    ("other-claim-id", lambda v: deep(v, "claim.claim_id", "other-claim")),
    ("other-claim-version", lambda v: bump(v, "claim.version")),
    ("other-visibility", lambda v: deep(deep(v, "claim.scope.visibility", "private"), "claim.scope.owner_actor", "someone")),
    ("other-team", lambda v: deep(v, "claim.scope.team_id", "other-team")),
    ("other-tenant", lambda v: deep(v, "claim.scope.tenant_id", "other-tenant")),
    ("other-claim-hash", lambda v: deep(v, "claim.sha256", "0" * 64)),
]


def test_a_bridge_preview_that_differs_from_the_original_proposal_is_not_shown(
        bridge_case, monkeypatch, tmp_path, capsys):
    c = bridge_case
    bridge_cli(c, monkeypatch)
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(c.mapping), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "prepare-note")
    assert cli.main(cli_arguments(c, "prepare", path)) == 0
    capsys.readouterr()
    tamper = Tamper(monkeypatch, lambda method, route: route.endswith("/bridge/preview"))

    def parsed(name, change):
        def edited(value):
            value = change(value)
            for model, field in ((cli.ExactSourceRef, "source"), (cli.ClaimReference, "claim"),
                                 (cli.NoteBridgeCommand, "mapping")):
                try:
                    model.model_validate_json(cli._canonical(value[field]))
                except Exception as error:  # the edit alone broke a wire model: the case proves nothing
                    tamper.invalid.append((name, repr(error)[:200]))
            return value
        return edited

    def not_shown():
        code = cli.main(cli_arguments(c, "preview", path))
        out = capsys.readouterr()
        shown = code == 1 and '"proposal_id"' not in out.out and "MEMORY_NOTE_CLI_UNCONFIRMED" in out.err
        return shown or (code, out.err[:300])

    assert run_cases([(name, parsed(name, change)) for name, change in BRIDGE_PREVIEW_EDITS], tamper, not_shown) == []
