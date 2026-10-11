"""Recompute B1/B2 progress readings from PROGRESS-B1-B2.json; read-only, never a deploy or merge gate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

B1_TARGETS = tuple(
    f"{prefix}-{index:02}"
    for prefix, start, stop in (("SP", 0, 32), ("PA", 1, 17), ("TC", 1, 17), ("SK", 1, 13))
    for index in range(start, stop)
)
B1_SCENARIOS = tuple(f"E-{index:02}" for index in range(1, 13))
B2_TARGETS = tuple(
    f"{prefix}-{index:02}"
    for prefix, count in (("TOOL", 7), ("WAKE", 7), ("TURN", 7), ("MEM", 7), ("DELEG", 6))
    for index in range(1, count + 1)
)
B2_SCENARIOS = tuple(f"CAP-{index:02}" for index in range(1, 7))
EXPECTED = {("B1", "target"): B1_TARGETS, ("B1", "scenario"): B1_SCENARIOS,
            ("B2", "target"): B2_TARGETS, ("B2", "scenario"): B2_SCENARIOS}
STAGES = {0: "not_started", 25: "partial_merged", 50: "main_merged_offline", 75: "deployed_live", 100: "accepted"}
LABELS = {0: "未開始", 25: "部分合併", 50: "主要實作＋離線測試", 75: "已部署＋live smoke", 100: "已驗收"}
BATCHES = ("B1", "B2", "B1+B2")
READINGS = (("加權進度", "weighted_progress_pct"), ("accepted", "accepted_pct"), ("情境進度", "scenario_progress_pct"))
# The public copy sets this to "" (scripts/public_export rule this-repo-progress-constant); links then become text.
REPO = ""


def load(path: Path) -> dict:
    table = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(table, dict) or table.get("schema_version") != "fa-progress-b1b2-v1" or not isinstance(table.get("rows"), list):
        raise ValueError("unsupported progress table")
    seen = {}
    for row in table["rows"]:
        if not isinstance(row, dict):
            raise ValueError("row must be an object")
        key = (row.get("batch"), row.get("kind"))
        if key not in EXPECTED or row.get("id") not in EXPECTED[key]:
            raise ValueError(f"unknown row {row.get('id')!r} in {key}")
        if row["id"] in seen:
            raise ValueError(f"duplicate row {row['id']}")
        if type(row.get("score")) is not int or row["score"] not in STAGES or row.get("stage") != STAGES[row["score"]]:
            raise ValueError(f"{row['id']}: score/stage must be one of {STAGES}")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item.strip() for item in evidence):
            raise ValueError(f"{row['id']}: evidence is required")
        if not re.fullmatch(r"[0-9a-f]{7,40}", str(row.get("checked_commit", ""))):
            raise ValueError(f"{row['id']}: checked_commit must be a git SHA")
        seen[row["id"]] = row
    for key, identities in EXPECTED.items():
        missing = [identity for identity in identities if identity not in seen]
        if missing:
            raise ValueError(f"{key} is missing rows: {', '.join(missing)}")
    return table


def percent(total: int, count: int) -> float:
    return round(total / count, 2) if count else 0.0


def compute(table: dict) -> dict:
    rows = table["rows"]

    def reading(batch: str | None) -> dict:
        targets = [row for row in rows if row["kind"] == "target" and batch in (None, row["batch"])]
        scenarios = [row for row in rows if row["kind"] == "scenario" and batch in (None, row["batch"])]
        accepted = sum(row["score"] == 100 for row in targets)
        return {
            "ids": len(targets),
            "weighted_progress_pct": percent(sum(row["score"] for row in targets), len(targets)),
            "accepted": accepted,
            "accepted_pct": percent(100 * accepted, len(targets)),
            "scenarios": len(scenarios),
            "scenario_progress_pct": percent(sum(row["score"] for row in scenarios), len(scenarios)),
            "scenarios_accepted": sum(row["score"] == 100 for row in scenarios),
            "stage_counts": {STAGES[score]: sum(row["score"] == score for row in targets) for score in STAGES},
            "pending_checks": sum(bool(row.get("pending")) for row in targets + scenarios),
        }

    return {"checked_main": table.get("checked_main"), "deployed_main": table.get("deployed_main"),
            "B1": reading("B1"), "B2": reading("B2"), "B1+B2": reading(None)}


def ledger_mismatches(table: dict, ledger_path: Path) -> list[str]:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
    states = {**ledger["targets"], **ledger["scenarios"]}
    return [f"{row['id']}: table score {row['score']} vs ledger acceptance {states[row['id']]['acceptance']}"
            for row in table["rows"]
            if row["batch"] == "B1" and (states[row["id"]]["acceptance"] == "accepted") != (row["score"] == 100)]


def chart(result: dict) -> str:
    kinds = (("加權", "weighted_progress_pct"), ("驗收", "accepted_pct"), ("情境", "scenario_progress_pct"))
    names = {"B1": "B1", "B2": "B2", "B1+B2": "合計"}
    labels = [f'"{names[batch]} {name}"' for batch in BATCHES for name, _ in kinds]
    values = [f"{result[batch][key]:.2f}" for batch in BATCHES for _, key in kinds]
    return "\n".join([
        "```mermaid", '%%{init: {"xyChart": {"width": 900, "height": 420}}}%%', "xychart-beta",
        '    title "B1／B2 進度讀數"', f'    x-axis "批次與讀數種類" [{", ".join(labels)}]',
        '    y-axis "完成百分比（%）" 0 --> 100', f"    bar [{', '.join(values)}]", "```", "",
        "- **X 軸**：批次（B1、B2、合計＝B1+B2）× 讀數種類。**加權**＝各 ID 階段分數的平均；"
        "**驗收**＝accepted 的 ID 占比；**情境**＝情境分數平均（B1 看 E-01～12，B2 看 CAP-01～06）。",
        "- **Y 軸**：完成百分比，0～100%。",
    ])


def delta_cell(before: float | None, after: float) -> str:
    if before is None:
        return f"{after:.2f}%"
    change = round(after - before, 2)
    text = "±0.00 pt" if change == 0 else f"**{'+' if change > 0 else '-'}{abs(change):.2f} pt**"
    return f"{before:.2f}% → {after:.2f}%（Δ {text}）"


def evidence_heads(evidence: list[str]) -> list[str]:
    return [item.split("（", 1)[0] for item in evidence if item.split(":", 1)[0] in {"pr", "commit", "run", "deploy"}]


def basis(before: list[str] | None, after: list[str]) -> str:
    """Name the evidence that is new since the previous reading; fall back to the newest PR cited."""
    heads = evidence_heads(after)
    new = [head for head in heads if before is not None and head not in before]
    picked = [head for head in new if head.startswith("pr:")] or new
    if not picked:
        prs = sorted((int(head[4:]) for head in heads if re.fullmatch(r"pr:#\d+", head)), reverse=True)
        picked = [f"pr:#{prs[0]}"] if prs else heads[-1:]
    return "、".join(dict.fromkeys(head.split(":", 1)[1] for head in picked[:3])) or "—"


def compare(previous: dict, table: dict) -> dict:
    """Readings and per-ID score changes of table against an earlier table."""
    before = {row["id"]: row for row in previous["rows"]}
    old = compute(previous)
    changes = [{"id": row["id"], "batch": row["batch"], "before": before[row["id"]]["score"], "after": row["score"],
                "basis": basis(evidence_heads(before[row["id"]]["evidence"]), row["evidence"])}
               for row in table["rows"] if row["id"] in before and before[row["id"]]["score"] != row["score"]]
    return {"checked_main": previous.get("checked_main"), "as_of": previous.get("as_of"),
            "readings": {batch: {key: old[batch][key] for _, key in READINGS} for batch in BATCHES}, "changes": changes}


def delta_section(result: dict, comparison: dict, current_main: str | None, previous_label: str) -> str:
    lines = [f"**對比基準**：上一次＝{previous_label}，main `{(comparison.get('checked_main') or '?')[:7]}`；"
             f"這一次＝main `{(current_main or '?')[:7]}`。", "",
             "| 讀數（上次 → 這次） | B1 | B2 | B1+B2 |", "|---|---|---|---|"]
    for label, key in READINGS:
        lines.append(f"| {label} | " + " | ".join(
            delta_cell(comparison["readings"][batch][key], result[batch][key]) for batch in BATCHES) + " |")
    lines.append("")
    if comparison["changes"]:
        lines.append("**改分明細**：")
        lines += [f"- {change['id']}（{change['batch']}）{change['before']}→{change['after']}"
                  f"（{change['after'] - change['before']:+d}，依據 {change['basis']}）" for change in comparison["changes"]]
    else:
        lines.append("**改分明細**：本次無改分。")
    return "\n".join(lines)


def summary(result: dict, comparison: dict | None = None) -> str:
    lines = []
    if comparison is not None:
        label = f"main 上已發布的 dashboard（as_of {comparison.get('as_of') or '未記'}）"
        lines += [delta_section(result, comparison, result.get("checked_main"), label), ""]
    lines += ["| 讀數 | B1 | B2 | B1+B2 |", "|---|---:|---:|---:|"]
    for label, key in READINGS:
        lines.append(f"| {label} | " + " | ".join(f"{result[batch][key]:.2f}%" for batch in BATCHES) + " |")
    lines.append("| ID／情境數 | " + " | ".join(f"{result[batch]['ids']}／{result[batch]['scenarios']}" for batch in BATCHES) + " |")
    lines.append("| accepted ID | " + " | ".join(str(result[batch]["accepted"]) for batch in BATCHES) + " |")
    lines.append("| 有待核註記的列 | " + " | ".join(str(result[batch]["pending_checks"]) for batch in BATCHES) + " |")
    lines += ["", "| 階段 | 分數 | B1 ID | B2 ID |", "|---|---:|---:|---:|"]
    for score, stage in STAGES.items():
        lines.append(f"| {LABELS[score]}（`{stage}`） | {score}% | {result['B1']['stage_counts'][stage]} | {result['B2']['stage_counts'][stage]} |")
    return "\n".join(lines) + "\n\n" + chart(result) + "\n"


def link(item: str) -> str:
    kind, _, value = item.partition(":")
    head, note = (value.split("（", 1) + [""])[:2]
    note = f"（{note}" if note else ""
    pr = re.fullmatch(r"#(\d+)", head)
    commit = re.fullmatch(r"[0-9a-f]{7,40}", head)
    if kind == "pr" and pr:
        return (f"[#{pr.group(1)}]({REPO}/pull/{pr.group(1)})" if REPO else f"#{pr.group(1)}") + note
    if kind == "commit" and commit:
        return (f"[`{head}`]({REPO}/commit/{head})" if REPO else f"`{head}`") + note
    if kind in {"run", "test"}:
        return f"{kind} `{head}`{note}"
    return item


def cell(text: str | None) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def rows_markdown(table: dict) -> str:
    lines = []
    for batch, kind, heading in (("B1", "target", "B1 目標（76）"), ("B1", "scenario", "B1 情境 E-01～12"),
                                 ("B2", "target", "B2 評測切面（34）"), ("B2", "scenario", "B2 能力情境 CAP-01～06")):
        lines += [f"### {heading}", "", "| ID | 判定 | 主題 | 分數 | 證據 | 待核／註記 | 核對 |", "|---|---|---|---:|---|---|---|"]
        for row in (item for item in table["rows"] if item["batch"] == batch and item["kind"] == kind):
            evidence = "；".join(cell(link(item)) for item in row["evidence"])
            notes = " ".join(text for text in (row.get("pending"), row.get("official")) if text)
            lines.append(f"| {row['id']} | {row['judgment']} | {cell(row['title'])} | {row['score']}% | {evidence} "
                         f"| {cell(notes)} | `{row['checked_commit']}` |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", type=Path, default=Path("docs/status/PROGRESS-B1-B2.json"))
    parser.add_argument("--ledger", type=Path, help="Optional read-only cross-check of B1 accepted rows")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--summary", action="store_true", help="Print the readings and a Mermaid chart as Markdown")
    output.add_argument("--markdown", action="store_true", help="Print the readings, chart and per-ID tables as Markdown")
    args = parser.parse_args()
    try:
        table = load(args.table)
        result = compute(table)
        if args.ledger is not None:
            result["ledger_mismatches"] = ledger_mismatches(table, args.ledger)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "INVALID_INPUT", "error": str(error)}), file=sys.stderr)
        return 2
    sys.stdout.reconfigure(encoding="utf-8")
    if args.summary or args.markdown:
        print(summary(result, table.get("compared_to")) + ("\n" + rows_markdown(table) if args.markdown else ""), end="")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
