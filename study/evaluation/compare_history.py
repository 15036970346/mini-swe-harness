"""把首轮历史、同运行层B0和第一版B3放在同一固定任务表；不读取推理内容。"""

import json
from pathlib import Path

import typer
from harness_support import REPO, ROOT


def read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def current(run: Path, task_id: str, variant: str, registered: set[str]) -> dict:
    target = run / "attempts" / task_id / variant
    result = read(target / "task-result.json")
    agent = read(target / "agent/result.json")
    usage = read(target / "usage.json")
    verdict = read(target / "judge/result.json")
    models, fingerprints = set(), set()
    for message in read(target / "agent/trajectory.json").get("messages", []):
        response = message.get("extra", {}).get("response")
        if isinstance(response, dict):
            models.add(response.get("model"))
            fingerprints.add(response.get("system_fingerprint"))
    return {
        "status": result.get(
            "status",
            "running_or_interrupted"
            if target.exists()
            else "not_run"
            if task_id in registered
            else "historical_environment_exclusion",
        ),
        "resolved": result.get("resolved"),
        "calls": agent.get("api_calls"),
        "seconds": agent.get("inference_seconds"),
        "cny_peak": usage.get("cny_peak_estimate"),
        "usage_complete": usage.get("usage_complete"),
        "termination": agent.get("termination_reason", agent.get("exit_status")),
        "validations": agent.get("validation_runs"),
        "repairs": agent.get("repair_cycles_started"),
        "f2p": [verdict.get("f2p_passed"), verdict.get("f2p_total")],
        "p2p": [verdict.get("p2p_passed"), verdict.get("p2p_total")],
        "response_models": sorted(x for x in models if x),
        "response_fingerprints": sorted(x for x in fingerprints if x),
    }


def totals(rows: list[dict], variant: str) -> dict:
    data = [r[variant] for r in rows]
    assessed = sum(isinstance(r["resolved"], bool) for r in data)
    resolved = sum(r["resolved"] is True for r in data)
    return {
        "planned": len(data),
        "assessed": assessed,
        "resolved": resolved,
        "rate_assessed": resolved / assessed if assessed else None,
        "calls_recorded": sum(r.get("calls") or 0 for r in data),
        "agent_seconds_recorded": sum(r.get("seconds") or 0 for r in data),
        "cny_peak_recorded": sum(r.get("cny_peak") or 0 for r in data),
        "unknown_or_pending": sum(r["resolved"] is None for r in data),
        "incomplete_paid_usage": sum(
            r.get("calls", 0) not in (0, None) and r.get("usage_complete") is not True for r in data
        ),
    }


def paired(rows: list[dict], left: str, right: str) -> dict:
    pairs = [r for r in rows if isinstance(r[left]["resolved"], bool) and isinstance(r[right]["resolved"], bool)]
    return {
        "paired_tasks": len(pairs),
        "both_resolved": sum(r[left]["resolved"] and r[right]["resolved"] for r in pairs),
        "gained": [r["instance_id"] for r in pairs if not r[left]["resolved"] and r[right]["resolved"]],
        "lost": [r["instance_id"] for r in pairs if r[left]["resolved"] and not r[right]["resolved"]],
        "both_failed": sum(not r[left]["resolved"] and not r[right]["resolved"] for r in pairs),
    }


def build(run: Path, historical: Path) -> dict:
    history = {r["instance_id"]: r for r in read(historical / "audit-summary.json")["tasks"]}
    registered = {r["instance_id"] for r in read(run / "run-manifest.json")["tasks"]}
    rows = []
    for task in read(ROOT / "manifest-v1.json")["tasks"]:
        old = history[task["instance_id"]]
        model = old.get("model", {})
        rows.append(
            {
                "instance_id": task["instance_id"],
                "group": task["group"],
                "historical": {
                    "status": old["status"],
                    "resolved": old["resolved"],
                    "calls": model.get("api_calls_recorded", 0),
                    "seconds": old.get("agent_internal_timing", {}).get("inference_seconds"),
                    "cny_peak": model.get("cny_peak_estimate", 0),
                    "usage_complete": model.get("usage_complete"),
                    "response_models": old.get("response_models", []),
                    "response_fingerprints": old.get("response_fingerprints", []),
                },
                **{v: current(run, task["instance_id"], v, registered) for v in ("B0", "B3")},
            }
        )
    return {
        "historical_run": str(historical),
        "current_run": str(run),
        "tasks": rows,
        "totals": {v: totals(rows, v) for v in ("historical", "B0", "B3")},
        "historical_vs_B3": paired(rows, "historical", "B3"),
        "B0_vs_B3": paired(rows, "B0", "B3"),
        "notes": [
            "12题便利样本，每题每组一次，不代表全榜或显著性。",
            "历史原版无共同快照运行层和公开测试提示；历史比较测整套版本差异，B0/B3比较隔离门禁与重修。",
            "原始模型别名相同不保证权重不漂移；逐题保存实际响应model和fingerprint。",
            "时间为Agent内部区间，B3含公开验证；历史无法回填同精度全流程时间。",
            "费用使用首轮固定价格口径的usage估算，不是实际账单；缺用量不计为免费。",
            "两道历史环境失败题未进入本轮付费尝试，仍保留在12题总分母。",
        ],
    }


def main(run: Path, historical: Path = REPO / "study/artifacts/native-flash-max-v4") -> None:
    result = build(run.resolve(), historical.resolve())
    (run / "historical-comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    lines = [
        "# 第一版与原生首轮对照",
        "",
        "费用为已记录用量估算；未完成和环境排除不当作模型失败。",
        "",
        "|组别|已判分|解决|调用数|Agent分钟|API高峰估算（元）|",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, row in result["totals"].items():
        lines.append(
            f"|{name}|{row['assessed']}/12|{row['resolved']}|{row['calls_recorded']}|"
            f"{row['agent_seconds_recorded'] / 60:.2f}|{row['cny_peak_recorded']:.4f}|"
        )
    lines += ["", "|任务|首轮原版|本轮B0|本轮B3|B3退出原因|", "|---|---|---|---|---|"]
    for row in result["tasks"]:
        labels = [
            "成功" if row[v]["resolved"] is True else "失败" if row[v]["resolved"] is False else row[v]["status"]
            for v in ("historical", "B0", "B3")
        ]
        lines.append(f"|{row['instance_id']}|{'|'.join(labels)}|{row['B3'].get('termination') or '—'}|")
    for label in ("historical_vs_B3", "B0_vs_B3"):
        p = result[label]
        lines += [
            "",
            f"{label}：共同可判分 {p['paired_tasks']} 题；新增解决 {len(p['gained'])}，"
            f"丢失解决 {len(p['lost'])}，均成功 {p['both_resolved']}，均失败 {p['both_failed']}。",
        ]
    lines += ["", "## 解读边界", "", *[f"- {s}" for s in result["notes"]], ""]
    (run / "historical-comparison.md").write_text("\n".join(lines))
    print(json.dumps(result["totals"], ensure_ascii=False))


if __name__ == "__main__":
    typer.run(main)
