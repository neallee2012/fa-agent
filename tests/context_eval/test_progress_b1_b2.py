import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "controller" / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


progress = module("progress_b1_b2")


def table(scores=None):
    scores = scores or {}
    rows = []
    for (batch, kind), identities in progress.EXPECTED.items():
        for identity in identities:
            score = scores.get(identity, 0)
            rows.append({"id": identity, "batch": batch, "kind": kind, "judgment": "C", "title": identity,
                         "score": score, "stage": progress.STAGES[score], "evidence": [f"ledger:{identity}"],
                         "pending": None, "checked_commit": "a" * 7})
    return {"schema_version": "fa-progress-b1b2-v1", "checked_main": "a" * 40, "deployed_main": "b" * 40, "rows": rows}


def write(tmp_path, value):
    path = tmp_path / "progress.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_readings_average_scores_per_batch_and_combined(tmp_path):
    result = progress.compute(progress.load(write(tmp_path, table(
        {"TC-11": 100, "SK-06": 100, "PA-02": 50, "SP-00": 25, "E-09": 25, "TURN-01": 75, "TURN-06": 50, "TOOL-01": 25, "CAP-05": 25}))))
    assert result["B1"]["weighted_progress_pct"] == round(275 / 76, 2)
    assert (result["B1"]["accepted"], result["B1"]["accepted_pct"]) == (2, round(200 / 76, 2))
    assert result["B1"]["scenario_progress_pct"] == round(25 / 12, 2)
    assert result["B2"]["weighted_progress_pct"] == round(150 / 34, 2)
    assert result["B2"]["scenario_progress_pct"] == round(25 / 6, 2)
    assert result["B1+B2"]["weighted_progress_pct"] == round(425 / 110, 2)
    assert result["B1+B2"]["scenario_progress_pct"] == round(50 / 18, 2)
    assert result["B1+B2"]["stage_counts"] == {"not_started": 103, "partial_merged": 2, "main_merged_offline": 2,
                                               "deployed_live": 1, "accepted": 2}


@pytest.mark.parametrize("change", [
    lambda value: value["rows"].pop(0),
    lambda value: value["rows"].append(dict(value["rows"][1])),
    lambda value: value["rows"][0].update(score=30),
    lambda value: value["rows"][0].update(score=25),
    lambda value: value["rows"][0].update(score=True, stage="not_started"),
    lambda value: value["rows"][0].update(evidence=[]),
    lambda value: value["rows"][0].update(checked_commit="main"),
    lambda value: value["rows"][0].update(batch="B2"),
    lambda value: value.update(schema_version="other"),
])
def test_incomplete_or_inconsistent_tables_are_rejected(tmp_path, change):
    value = table()
    change(value)
    with pytest.raises(ValueError):
        progress.load(write(tmp_path, value))


def test_ledger_cross_check_reports_only_b1_accepted_differences(tmp_path):
    states = {identity: {"acceptance": "not_evaluated"} for identity in progress.B1_TARGETS + progress.B1_SCENARIOS}
    states["TC-11"]["acceptance"] = "accepted"
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"targets": {k: v for k, v in states.items() if not k.startswith("E-")},
                                  "scenarios": {k: v for k, v in states.items() if k.startswith("E-")}}), encoding="utf-8")
    assert progress.ledger_mismatches(table({"TC-11": 100, "TURN-01": 100}), ledger) == []
    assert progress.ledger_mismatches(table({"SK-06": 100}), ledger) == [
        "TC-11: table score 0 vs ledger acceptance accepted", "SK-06: table score 100 vs ledger acceptance not_evaluated"]


def test_markdown_has_chart_links_and_escaped_cells(tmp_path):
    value = table({"TC-11": 100})
    value["rows"][0].update(evidence=["pr:#103（原接受）", "commit:fe5ab6b", "run:tc11-x（note）", "doc:a|b"], pending="x|y")
    loaded = progress.load(write(tmp_path, value))
    text = progress.summary(progress.compute(loaded)) + progress.rows_markdown(loaded)
    assert "```mermaid\n%%{init" in text and "xychart-beta" in text
    assert 'x-axis "批次與讀數種類"' in text and 'y-axis "完成百分比（%）" 0 --> 100' in text
    pr, commit = ((f"[#103]({progress.REPO}/pull/103)", f"[`fe5ab6b`]({progress.REPO}/commit/fe5ab6b)")
                  if progress.REPO else ("#103", "`fe5ab6b`"))
    assert f"{pr}（原接受）" in text and commit in text
    assert "run `tc11-x`（note）" in text and "doc:a\\|b" in text and "x\\|y" in text


def test_links_become_plain_text_without_a_repo(monkeypatch):
    monkeypatch.setattr(progress, "REPO", "")
    assert progress.link("pr:#103（原接受）") == "#103（原接受）"
    assert progress.link("commit:fe5ab6b") == "`fe5ab6b`"
    assert progress.link("run:tc11-x") == "run `tc11-x`"


def test_delta_cells_basis_and_comparison_against_the_previous_table(tmp_path):
    assert progress.delta_cell(24.26, 25.0) == "24.26% → 25.00%（Δ **+0.74 pt**）"
    assert progress.delta_cell(2.63, 2.63) == "2.63% → 2.63%（Δ ±0.00 pt）"
    assert progress.delta_cell(10.0, 9.5) == "10.00% → 9.50%（Δ **-0.50 pt**）"
    assert progress.delta_cell(None, 5.0) == "5.00%"
    assert progress.basis(["pr:#138"], ["pr:#138（loader）", "pr:#150（capture）", "doc:x"]) == "#150"
    assert progress.basis(None, ["pr:#8", "pr:#137（transfer）", "run:r1"]) == "#137"
    assert progress.basis(["pr:#8"], ["pr:#8", "run:new-run"]) == "new-run"
    before = table({"TURN-06": 50})
    after = table({"TURN-06": 75})
    after["rows"][[row["id"] for row in after["rows"]].index("TURN-06")]["evidence"] = ["ledger:TURN-06", "pr:#137"]
    comparison = progress.compare(before, after)
    assert comparison["changes"] == [{"id": "TURN-06", "batch": "B2", "before": 50, "after": 75, "basis": "#137"}]
    text = progress.summary(progress.compute(after), comparison)
    assert "**對比基準**" in text and "Δ **+0.74 pt**" in text and "TURN-06（B2）50→75（+25，依據 #137）" in text
    assert "本次無改分" in progress.summary(progress.compute(after), progress.compare(after, after))


def test_cli_is_read_only_and_signals_invalid_input(tmp_path):
    path = write(tmp_path, table())
    before = path.read_bytes()
    ok = subprocess.run([sys.executable, str(ROOT / "controller" / "progress_b1_b2.py"), "--table", str(path)],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8")
    assert ok.returncode == 0 and json.loads(ok.stdout)["B1+B2"]["ids"] == 110
    assert path.read_bytes() == before
    bad = write(tmp_path, {"schema_version": "fa-progress-b1b2-v1", "rows": []})
    failed = subprocess.run([sys.executable, str(ROOT / "controller" / "progress_b1_b2.py"), "--table", str(bad)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8")
    assert failed.returncode == 2 and "INVALID_INPUT" in failed.stderr


def test_builder_curates_every_b2_row_once_and_replaces_only_the_generated_block(tmp_path):
    build = module("progress_b1_b2_build")
    assert [row[0] for row in build.B2_ROWS] == list(progress.B2_TARGETS)
    assert [row[0] for row in build.CAP_ROWS] == list(progress.B2_SCENARIOS)
    assert all(row[3] in progress.STAGES and row[4] for row in build.B2_ROWS)
    rows = build.curated(build.B2_ROWS, "target", "a" * 40) + build.curated(build.CAP_ROWS, "scenario", "a" * 40)
    assert all(row["pending"].startswith(build.B2_JUDGED) for row in rows if row["score"] != 100)
    assert {row["id"] for row in rows if row["score"] == 100} == set(build.B2_ACCEPTED)
    assert all(row["pending"] is None and row["evidence"][-1] for row in rows if row["score"] == 100)
    assert set(build.OFFICIAL) <= {identity for identities in progress.EXPECTED.values() for identity in identities}
    noted = build.with_official([{"id": "SP-17", "pending": None}, {"id": "CAP-06", "pending": "x"}, {"id": "SP-00", "pending": None}])
    assert [row.get("official") for row in noted] == [build.OFFICIAL["SP-17"], build.OFFICIAL["CAP-06"], None]
    assert [row["pending"] for row in noted] == [None, "x", None]
    notes_table = table()
    notes_table["rows"][0]["official"] = "官方能力：[SDK](https://example.test)"
    assert progress.compute(notes_table) == progress.compute(table())
    assert "| 官方能力：[SDK](https://example.test) |" in progress.rows_markdown(notes_table)
    assert [build.pr_number(subject) for subject in (
        "Merge pull request #139 from neallee2012/branch", "Merge PR #12: approved skill source access",
        "merge: bring latest main into the branch", "docs: mention Merge PR #9 later")] == ["139", "12", None, None]
    page = tmp_path / "page.md"
    page.write_text(f"head\n{build.BEGIN}\nold\n{build.END}\ntail\n", encoding="utf-8")
    build.write_markdown(page, progress.load(write(tmp_path, table())))
    text = page.read_text(encoding="utf-8")
    assert text.startswith(f"head\n{build.BEGIN}\n\n| 讀數 |") and text.endswith(f"{build.END}\ntail\n") and "old" not in text
    page.write_text("no markers\n", encoding="utf-8")
    with pytest.raises(ValueError):
        build.write_markdown(page, table())
