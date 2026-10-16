"""只读审计既有尝试并生成汇总；不调用模型、不读取保留组的推理或工具内容。"""

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from unidiff import PatchSet


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wilson(successes, total):
    if not total:
        return None
    z = 1.95996398454
    p = successes / total
    center = (p + z*z/(2*total)) / (1 + z*z/total)
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / (1+z*z/total)
    return [center-half, center+half]


def patch_audit(patch, repo):
    roots = {"psf/requests": "requests/", "pytest-dev/pytest": "src/_pytest/",
             "sphinx-doc/sphinx": "sphinx/"}
    files = [change.path for change in PatchSet(patch)]
    unexpected = []
    for name in files:
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or ".git" in path.parts
                or not name.startswith(roots[repo]) or path.name.startswith("test_")):
            unexpected.append(name)
    return {"changed_files": files, "unexpected_paths": unexpected,
            "source_only_paths": bool(files) and not unexpected,
            "note": "仅检查补丁路径，不代替代码语义/测试绕过审查；可疑路径需人工判定，不静默修改补丁。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    lock = read(run / "run-manifest.json")
    manifest = read(run / "frozen/manifest-v1.json")
    assert all(sha(run / "frozen" / name) == digest for name, digest in lock["files"].items())
    lineage = read(run / "continuation.json")
    reused = lineage.get("reused_completed_attempts", {})
    if reused:
        previous = (run / lineage["source_run"]).resolve()
        old_lock = read(previous / "run-manifest.json")
        for key in ("model", "reasoning_effort", "attempts_per_task", "mini_commit", "evaluator", "tasks"):
            assert old_lock[key] == lock[key]
        for name in ("manifest-v1.json", "pilot.yaml"):
            assert old_lock["files"][name] == lock["files"][name]
        for name, digest in reused.items():
            assert sha(run / name / "task-result.json") == sha(previous / name / "task-result.json") == digest
            # 确认整份既有证据的副本相同，不仅核对成绩数字。
            for original in (previous / name).rglob("*"):
                if original.is_file():
                    assert sha(original) == sha(run / name / original.relative_to(previous / name))
    rows = []
    for task in manifest["tasks"]:
        folder = run / task["instance_id"]
        row = read(folder / "task-result.json") or {
            "instance_id": task["instance_id"], "status": "not_run", "resolved": None}
        row.update(group=task["group"], repo=task["repo"],
                   actual_run=lineage["source_run"] if task["instance_id"] in reused else ".")
        if "agent" in row and (row["agent"].get("timeout") or row["agent"].get("returncode") != 0):
            # 强制终止可能发生在两次轨迹落盘之间；调用计数相等不能排除尚未返回的请求。
            row["model"]["usage_complete"] = False
            row["model"]["unrecorded_request_cost_may_exist"] = True
        pred = read(folder / "agent/prediction.json")
        judge = read(folder / "prediction/result.json")
        if pred:
            patch = pred["model_patch"]
            row["patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
            if judge:
                assert judge["patch_sha256"] == row["patch_sha256"]
            row["patch_audit"] = patch_audit(patch, task["repo"])
        trace = read(folder / "agent/trajectory.json")
        if trace:
            config = read(folder / "agent/config.json")
            assert config["model"]["model_name"] == lock["model"]
            assert config["model"]["model_kwargs"]["extra_body"]["reasoning_effort"] == lock["reasoning_effort"]
            assert config["model"]["model_kwargs"]["thinking"] == {"type": "enabled"}
            assert config["agent"]["step_limit"] == 60
            assert config["agent"]["cost_limit"] == 2
            assert config["agent"]["wall_time_limit_seconds"] == 1800
            assert read(folder / "empty/result.json")["resolved"] is False
            assert read(folder / "gold/result.json")["resolved"] is True
            row["controls_and_requested_configuration_verified"] = True
            # 不查看 messages 的 content/actions/choices，保留组策略反馈不进入设计。
            responses = [m.get("extra", {}).get("response") for m in trace["messages"]]
            responses = [r for r in responses if isinstance(r, dict)]
            row["response_models"] = sorted({r["model"] for r in responses})
            row["response_fingerprints"] = sorted({r.get("system_fingerprint") for r in responses
                                                    if r.get("system_fingerprint")})
            times = [r["created"] for r in responses if r.get("created")]
            row["first_last_api_response_utc"] = [datetime.fromtimestamp(t, timezone.utc).isoformat()
                                                    for t in (min(times), max(times))] if times else None
        row["submitted"] = row.get("model", {}).get("exit_status") == "Submitted"
        row["incorrect_submission"] = row["submitted"] and row.get("resolved") is False
        row["regression"] = (row["p2p_passed"] < row["p2p_total"]
                              if row.get("p2p_passed") is not None else None)
        row["matched_final_validation_evidence"] = None
        row["agent_internal_timing"] = {k: v for k, v in read(folder / "agent/result.json").items()
                                         if k in ("environment_seconds", "inference_seconds")}
        rows.append(row)
    assessed = [r for r in rows if r["resolved"] is not None]
    solved = sum(r["resolved"] is True for r in rows)
    submitted = sum(r["submitted"] for r in rows)
    incorrect = sum(r["incorrect_submission"] for r in rows)
    costs = {key: sum(r.get("model", {}).get(key, 0) for r in rows)
             for key in ("cny_peak_estimate", "cny_offpeak_estimate", "usd_peak_estimate",
                         "api_calls_recorded", "prompt_tokens", "cached_tokens", "completion_tokens")}
    times = [r["agent"]["elapsed_seconds"] for r in rows if "agent" in r]
    summary = {"planned": len(rows), "assessed": len(assessed), "resolved": solved,
               "not_run": sum(r["status"] == "not_run" for r in rows),
               "unresolved_assessed": len(assessed)-solved,
               "infrastructure_or_unknown": sum(r["status"] != "not_run" and r["resolved"] is None for r in rows),
               "submitted": submitted, "incorrect_submissions": incorrect,
               "incorrect_over_submitted": incorrect/submitted if submitted else None,
               "incorrect_over_planned": incorrect/len(rows),
               "regression_tasks": sum(r["regression"] is True for r in rows),
               "regression_assessed_tasks": sum(r["regression"] is not None for r in rows),
               "test_entries": {key: sum(r.get(key) or 0 for r in rows)
                                for key in ("f2p_passed", "f2p_total", "p2p_passed", "p2p_total")},
               "completed_model_usage_is_complete": all(r.get("model", {}).get("usage_complete", False)
                                                         for r in rows if "agent" in r),
               "patch_path_review_required": [r["instance_id"] for r in rows
                    if r.get("patch_audit", {}).get("unexpected_paths")],
               "wilson95_assessed_descriptive_only": wilson(solved, len(assessed)),
               "totals": costs,
               "cny_peak_per_resolved": costs["cny_peak_estimate"]/solved if solved else None,
               "cny_offpeak_per_resolved": costs["cny_offpeak_estimate"]/solved if solved else None,
               "agent_seconds": {"sum": sum(times), "median": statistics.median(times) if times else None,
                                 "min": min(times) if times else None, "max": max(times) if times else None},
               "phase_seconds_sum": {phase: sum(r.get(phase, {}).get("elapsed_seconds", 0) for r in rows)
                                     for phase in ("pull", "empty", "gold", "agent", "judge")},
               "validation_coverage": "未测量：原版无绑定最终源码、测试和环境摘要的可信验证凭证；不能推断没有运行测试。",
               "timestamp_scope": "记录实际响应时间范围和阶段耗时；未提前采集所有阶段精确起止时刻，不用文件 mtime 伪造。",
               "timing_scope": "镜像 pull 含缓存命中；不含此前工具安装、失败预检和人工等待，不能当作首次从零部署总耗时。",
               "task_groups": {group: {"planned": sum(r["group"] == group for r in rows),
                                       "assessed": sum(r["group"] == group for r in assessed),
                                       "resolved": sum(r["group"] == group and r["resolved"] is True for r in rows)}
                               for group in ("dev", "holdout")},
               "interpretation": "12 题便利样本、每题一次；Wilson 仅描述小样本不确定性，不满足随机独立抽样，不能推广全量榜单。",
               "lineage": lineage, "tasks": rows}
    (run / "audit-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("tasks", "lineage")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
