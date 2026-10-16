"""串行、可续跑的基线调度与量化汇总；不自动重试已经开始的模型尝试。"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from preflight import validate_packets
from harness_support import DOCKER, setup_environment

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
RUNTIME = ROOT.parent / ".runtime/evaluation"
JUDGE_PYTHON = RUNTIME / "judge-venv/bin/python"
AGENT_PYTHON = REPO / ".venv/bin/python"


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def execute(command: list[str], log: Path, timeout: int, environment: dict) -> dict:
    start = time.time()
    log.parent.mkdir(parents=True, exist_ok=True)
    # 超时必须成为可量化的状态，不能被静默丢弃或无限重试。
    with log.open("w") as stream:
        try:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                    env=environment, cwd=REPO, timeout=timeout)
            status = {"returncode": result.returncode, "timeout": False}
        except subprocess.TimeoutExpired:
            status = {"returncode": None, "timeout": True}
    return status | {"elapsed_seconds": time.time() - start}


def inference_metrics(path: Path) -> dict:
    if not path.exists():
        return {"api_calls_recorded": 0, "usd_peak_estimate": 0, "usage_complete": False}
    trace = json.loads(path.read_text())
    usage_rows = [m.get("extra", {}).get("response", {}).get("usage") or {} for m in trace["messages"]
                  if isinstance(m.get("extra", {}).get("response"), dict)]
    prompt = sum(r.get("prompt_tokens", 0) for r in usage_rows)
    output = sum(r.get("completion_tokens", 0) for r in usage_rows)
    cache = sum(r.get("prompt_cache_hit_tokens", (r.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
                for r in usage_rows)
    stats = trace["info"]["model_stats"]
    peak_cny = ((prompt - cache) * 2 + cache * .04 + output * 8) / 1_000_000
    return {"api_calls_recorded": stats["api_calls"], "responses_with_usage": len(usage_rows),
            "prompt_tokens": prompt, "cached_tokens": cache, "completion_tokens": output,
            "usd_peak_estimate": stats["instance_cost"], "cny_peak_estimate": peak_cny,
            "cny_offpeak_estimate": peak_cny / 2, "exit_status": trace["info"]["exit_status"],
            "usage_complete": stats["api_calls"] == len(usage_rows)}


def summarize(run: Path, tasks: list[dict]) -> dict:
    rows = []
    for task in tasks:
        path = run / task["instance_id"] / "task-result.json"
        row = json.loads(path.read_text()) if path.exists() else {
            "instance_id": task["instance_id"], "status": "not_run", "resolved": None}
        rows.append(row | {"group": task["group"]})
    resolved = sum(r.get("resolved") is True for r in rows)
    assessed = sum(r.get("resolved") is not None for r in rows)
    summary = {"planned_tasks": len(rows), "assessed_tasks": assessed, "resolved": resolved,
               "resolved_over_planned": resolved / len(rows),
               "resolved_over_assessed": resolved / assessed if assessed else None,
               "not_run": sum(r["status"] == "not_run" for r in rows),
               "usd_peak_estimate": sum(r.get("model", {}).get("usd_peak_estimate", 0) for r in rows),
               "cny_offpeak_estimate": sum(r.get("model", {}).get("cny_offpeak_estimate", 0) for r in rows),
               "cny_peak_estimate": sum(r.get("model", {}).get("cny_peak_estimate", 0) for r in rows),
               "cost_note": "按响应 usage 估算；未返回 usage 的超时请求费用未知，不是供应商账单。",
               "tasks": rows}
    save(run / "summary.json", summary)
    lines = ["# 原生 mini + Flash max 基线运行报告", "", "未判分任务不等于模型修复失败；本报告不是全量 SWE-bench 成绩。", "",
             f"预定 {len(rows)} 题；已有任务结论 {assessed} 题；解决 {resolved} 题。", "",
             "| 任务 | 分组 | 状态 | 修复 | 调用数 | 推理秒 | 美元估算 |",
             "|---|---|---|---|---:|---:|---:|"]
    for row in rows:
        model = row.get("model", {})
        lines.append(f"| {row['instance_id']} | {row['group']} | {row['status']} | {row.get('resolved')} | "
                     f"{model.get('api_calls_recorded', 0)} | {row.get('agent', {}).get('elapsed_seconds', 0):.1f} | "
                     f"{model.get('usd_peak_estimate', 0):.5f} |")
    (run / "report.md").write_text("\n".join(lines) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--limit", type=int, default=12, help="本次最多推进到第几题，包含已完成任务")
    args = parser.parse_args()
    run = args.run.resolve()
    manifest = json.loads((ROOT / "manifest-v1.json").read_text())
    assert validate_packets(manifest) == []
    tasks = sorted(manifest["tasks"], key=lambda t: (not t["pilot"], t["group"] != "dev"))
    frozen = {"model": "deepseek/deepseek-flash", "reasoning_effort": "max", "attempts_per_task": 1,
              "mini_commit": manifest["mini_commit"], "evaluator": "4.1.0",
              "tasks": [t["instance_id"] for t in tasks],
              "files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ("manifest-v1.json", "pilot.yaml", "run_native.py", "judge.py", "batch.py", "initial_state.py")}}
    run.mkdir(parents=True, exist_ok=True)
    lock = run / "run-manifest.json"
    if lock.exists():
        assert json.loads(lock.read_text()) == frozen, "执行脚本或配置发生变化，不能混为同一轮实验"
    else:
        save(lock, frozen)
        (run / "frozen").mkdir()
        for name in frozen["files"]:
            shutil.copy2(ROOT / name, run / "frozen" / name)
        for label, executable in (("agent", AGENT_PYTHON), ("judge", JUDGE_PYTHON)):
            versions = subprocess.check_output([str(executable), "-c",
                "import importlib.metadata,json; print(json.dumps({d.metadata['Name']:d.version for d in importlib.metadata.distributions()},sort_keys=True))"], text=True)
            (run / "frozen" / f"{label}-dependencies.json").write_text(versions)
    setup_environment()
    environment = dict(os.environ)
    environment.update(PYTHON_DOTENV_DISABLED="1", HF_HUB_DISABLE_TELEMETRY="1")
    subprocess.run([str(DOCKER), "info", "--format", "{{.ServerVersion}}"], env=environment, check=True, timeout=15)
    for task in tasks[:args.limit]:
        summary = summarize(run, tasks)
        if summary["usd_peak_estimate"] >= 24:
            raise SystemExit("批次软预算已达到 24 美元，停止启动新题")
        target = run / task["instance_id"]
        if (target / "task-result.json").exists():
            continue
        assert not target.exists(), f"任务已开始但未完成，需人工审计，禁止自动重试：{target}"
        target.mkdir()
        result = {"instance_id": task["instance_id"], "status": "preparing", "resolved": None}
        print(f"开始 {task['instance_id']}", flush=True)
        result["pull"] = execute([str(DOCKER), "pull", "--platform", "linux/amd64", task["image_name"]],
                                 target / "pull.log", 1800, environment)
        if result["pull"]["returncode"] != 0:
            result["status"] = "image_pull_failed"
            save(target / "task-result.json", result)
            summarize(run, tasks)
            raise SystemExit("镜像下载失败；停止批次，未调用模型")
        for mode in ("empty", "gold"):
            phase = target / mode
            control = execute([str(JUDGE_PYTHON), str(ROOT / "judge.py"), task["instance_id"], mode, str(phase)],
                              target / f"{mode}.log", 1100, environment)
            report = json.loads((phase / "result.json").read_text()) if (phase / "result.json").exists() else {}
            result[mode] = control | {"status": report.get("status"), "resolved": report.get("resolved")}
            correct = report.get("resolved") is (mode == "gold")
            if control["returncode"] != 0 or not correct or report.get("status") != "graded":
                result["status"] = f"{mode}_control_failed"
                save(target / "task-result.json", result)
                summarize(run, tasks)
                raise SystemExit("裁判控制实验未通过；停止批次，未开始本题模型推理")
        print(f"控制实验通过，开始模型解题：{task['instance_id']}", flush=True)
        agent_dir = target / "agent"
        result["agent"] = execute([str(AGENT_PYTHON), str(ROOT / "run_native.py"), task["instance_id"], str(agent_dir)],
                                  target / "agent.log", 2250, environment)
        result["model"] = inference_metrics(agent_dir / "trajectory.json")
        # 外部超时后只清理该任务明确记录的容器；不进行全局 prune。
        cid_file = agent_dir / "container-id.txt"
        if cid_file.exists():
            cid = cid_file.read_text().strip()
            assert re.fullmatch(r"[a-f0-9]{64}", cid)
            subprocess.run([str(DOCKER), "rm", "-f", cid], capture_output=True, env=environment, timeout=30)
        prediction = agent_dir / "prediction.json"
        if not prediction.exists():
            result.update(status="agent_runtime_error", resolved=None)
        else:
            judge_dir = target / "prediction"
            result["judge"] = execute([str(JUDGE_PYTHON), str(ROOT / "judge.py"), task["instance_id"], "prediction",
                                       str(judge_dir), "--prediction", str(prediction)],
                                      target / "prediction.log", 1100, environment)
            report = json.loads((judge_dir / "result.json").read_text()) if (judge_dir / "result.json").exists() else {}
            result.update(status=report.get("status", "judge_error"), resolved=report.get("resolved"),
                          f2p_passed=report.get("f2p_passed"), f2p_total=report.get("f2p_total"),
                          p2p_passed=report.get("p2p_passed"), p2p_total=report.get("p2p_total"))
        save(target / "task-result.json", result)
        summarize(run, tasks)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["status"] == "agent_runtime_error" or not result["model"].get("usage_complete"):
            raise SystemExit("运行异常或 usage 不完整；停止新增付费调用，先审计而非自动重试")
    summary = summarize(run, tasks)
    print(json.dumps({k: v for k, v in summary.items() if k != "tasks"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
