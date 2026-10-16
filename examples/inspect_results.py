"""离线复算已发布的 10 题汇总；不运行 Agent、不调用模型。"""

import json
from pathlib import Path


def main() -> None:
    directory = Path(__file__).resolve().parents[1] / "docs/harness/evaluation-20261009"
    report = json.loads((directory / "summary.json").read_text())
    audit = json.loads((directory / "audit-summary.json").read_text())
    rows = report["attempts"]
    assert len(rows) == 20 and len({(r["instance_id"], r["variant"]) for r in rows}) == 20
    for variant in ("B3", "B4"):
        selected = [r for r in rows if r["variant"] == variant]
        assert len(selected) == 10 and all(isinstance(r["resolved"], bool) for r in selected)
        totals = {
            "resolved": sum(r["resolved"] for r in selected),
            "nonempty": sum(r["nonempty_submission"] for r in selected),
            "recorded_calls": sum(r["api_calls"] for r in selected),
        }
        assert all(audit["totals"][variant][key] == value for key, value in totals.items())
        print(
            f"{variant}：解决 {totals['resolved']}/10，非空交付 {totals['nonempty']}/10，"
            f"物理请求 {totals['recorded_calls']}"
        )
    print("这是历史汇总的一致性检查，不是重新运行，也不是独立复核完整原始轨迹。")


if __name__ == "__main__":
    main()
