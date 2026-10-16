"""冻结、预检、执行和汇总 Harness 对照；第一版显式登记每题公开合同。"""

import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import typer
from harness_support import DOCKER, REPO, ROOT, RUNTIME, setup_environment, task_info

app = typer.Typer(pretty_exceptions_show_locals=False)
COMMIT = "04d809ceab9df28f9adaed044884180159172930"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(path.read_text())


def save(path: Path, value) -> None:
    from minisweagent.harness.contracts import save_json

    save_json(path, value)


def verify(run: Path) -> dict:
    lock = read(run / "run-manifest.json")
    for name, expected in lock["sha256"].items():
        if sha(run / name) != expected:
            raise ValueError(f"冻结文件发生变化：{name}")
    # 单题入口目前仍复用这两个项目配置；任何变化均要求新建实验。
    for name in ("pilot.yaml", "manifest-v1.json", "judge.py", "batch.py", "compare.py"):
        if sha(ROOT / name) != lock["sha256"][f"frozen/{name}"]:
            raise ValueError(f"公共运行配置发生变化：{name}")
    return lock


@app.command()
def freeze(
    output: Path,
    contract: list[Path] = typer.Option(..., "--contract"),
    request_recovery: bool = False,
    wrapup: bool = False,
    test_feedback: bool = False,
) -> None:
    """新建运行目录；每个 --contract 对应一题，拒绝覆盖。"""
    setup_environment()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / "frozen"
    frozen.mkdir()
    data = subprocess.check_output(["git", "archive", COMMIT, "src/minisweagent", "LICENSE.md"], cwd=REPO)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for item in archive:
            if item.isfile():
                path = frozen / item.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(item).read())
    additions = list((REPO / "src/minisweagent/harness").glob("*.py")) + [
        REPO / "src/minisweagent/agents/verified.py",
        REPO / "src/minisweagent/agents/recovering.py",
        REPO / "src/minisweagent/agents/wrapup.py",
        REPO / "src/minisweagent/environments/managed_docker.py",
    ]
    for path in additions:
        destination = frozen / path.relative_to(REPO)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    for name in (
        "run_verified.py",
        "harness_support.py",
        "initial_state.py",
        "pilot.yaml",
        "manifest-v1.json",
        "judge.py",
        "batch.py",
        "compare.py",
        "compare_history.py",
        "comparison-public-selection-v1.json",
    ):
        shutil.copy2(ROOT / name, frozen / name)
    save(
        frozen / "agent-dependencies.json", {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    )
    tasks = []
    for path in contract:
        spec = read(path)
        task = task_info(spec["task_id"])
        if task["instance_id"] in [t["instance_id"] for t in tasks]:
            raise ValueError("同一题不可重复登记")
        packet = next(
            r
            for r in map(json.loads, (RUNTIME / f"inputs/{task['group']}/test.jsonl").read_text().splitlines())
            if r["instance_id"] == task["instance_id"]
        )
        save(frozen / f"{task['instance_id']}.packet.json", packet)
        shutil.copy2(path, frozen / f"{task['instance_id']}.contract.json")
        tasks.append({"instance_id": task["instance_id"], "split": task["group"]})
    save(
        output / "run-manifest.json",
        {
            "version": 1,
            "upstream_commit": COMMIT,
            "tasks": tasks,
            "attempts_per_task_variant": 1,
            "request_recovery": request_recovery,
            "test_feedback": test_feedback,
            "variants": ["B3", "B4"] if wrapup else ["B0", "B1", "B2", "B3"],
            "experiment": "wrapup-v1.1" if wrapup else "harness-v1",
            "budget_usd_soft": 24,
            "sha256": {str(p.relative_to(output)): sha(p) for p in frozen.rglob("*") if p.is_file()},
        },
    )
    print(f"已冻结 {len(tasks)} 题：{output}")


@app.command()
def preflight(run: Path) -> None:
    """无模型调用；通过 empty/gold 后才允许本轮推理。"""
    setup_environment()
    lock = verify(run)
    for task in lock["tasks"]:
        directory = run / "controls" / task["instance_id"]
        directory.mkdir(parents=True, exist_ok=True)
        for mode in ("empty", "gold"):
            target = directory / mode
            if not target.exists():
                with (directory / f"{mode}.log").open("w") as log:
                    subprocess.run(
                        [
                            str(RUNTIME / "judge-venv/bin/python"),
                            str(ROOT / "judge.py"),
                            task["instance_id"],
                            mode,
                            str(target),
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        timeout=1100,
                    )
            result = read(target / "result.json")
            if result["status"] != "graded" or result["resolved"] is not (mode == "gold"):
                raise RuntimeError(f"{task['instance_id']} {mode} 控制失败，不启动模型")
    save(run / "readiness.json", {"ready": True, "manifest_sha256": sha(run / "run-manifest.json")})
    print("所有登记任务的裁判控制通过")


@app.command("run")
def run_variants(run: Path, variants: list[str] = typer.Option(..., "--variant"), limit: int = 1) -> None:
    """串行单次尝试；已开始未完成不自动重试；超时留证并停止。"""
    setup_environment()
    run = run.resolve()
    lock = verify(run)
    if limit <= 0:
        raise ValueError("limit 必须为正数")
    for previous in run.glob("attempts/*/*/task-result.json"):
        prior = read(previous)
        if prior.get("usage_complete") is False and not (
            lock.get("request_recovery") and prior.get("accounting_complete")
        ):
            raise RuntimeError("本轮存在未知计费或未完成请求，先审计，禁止继续付费调用")
    if any(v not in lock["variants"] for v in variants) or len(set(variants)) != len(variants):
        raise ValueError("变体非法或重复")
    if read(run / "readiness.json") != {"ready": True, "manifest_sha256": sha(run / "run-manifest.json")}:
        raise ValueError("先运行本轮 preflight")
    for task in lock["tasks"][:limit]:
        for variant in variants:
            verify(run)
            target = run / "attempts" / task["instance_id"] / variant
            if (target / "task-result.json").exists():
                continue
            if target.exists():
                raise RuntimeError(f"已有未完成尝试，需检查后新建运行：{target}")
            total = sum(
                read(p).get("cost_usd_peak_estimate", 0)
                + (read(p).get("request_recovery") or {}).get("unknown_cost_reserve_usd", 0)
                for p in run.glob("attempts/*/*/agent/result.json")
            )
            if total >= lock["budget_usd_soft"]:
                raise RuntimeError("累计费用已达软阈值")
            target.mkdir(parents=True)
            command = [
                str(REPO / ".venv/bin/python"),
                str(run / "frozen/run_verified.py"),
                task["instance_id"],
                str(target / "agent"),
                str(run / f"frozen/{task['instance_id']}.contract.json"),
                str(run / "frozen/src"),
                str(run / f"frozen/{task['instance_id']}.packet.json"),
                "--variant",
                variant,
            ]
            environment = os.environ | {"MINI_HARNESS_REPO": str(REPO)}
            if lock.get("request_recovery"):
                command.append("--request-recovery")
            if lock.get("test_feedback"):
                command.append("--test-feedback")
            try:
                with (target / "agent.log").open("w") as log:
                    process = subprocess.run(
                        command, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=1830
                    )
                if process.returncode:
                    raise RuntimeError(f"模型运行异常，检查 {target / 'agent.log'}")
            except subprocess.TimeoutExpired:
                save(
                    target / "task-result.json",
                    {
                        "variant": variant,
                        "status": "watchdog_timeout",
                        "resolved": None,
                        "usage_complete": False,
                        "unknown_requests": 1,
                    },
                )
                raise
            finally:
                container_file = target / "agent/container-id.txt"
                if container_file.exists():
                    container = container_file.read_text().strip()
                    if len(container) != 64 or any(c not in "0123456789abcdef" for c in container):
                        raise ValueError("容器 ID 非法")
                    subprocess.run([str(DOCKER), "rm", "-f", container], capture_output=True, timeout=30)
                label = hashlib.sha256(str((target / "agent").resolve()).encode()).hexdigest()
                remaining = subprocess.check_output(
                    [str(DOCKER), "ps", "-aq", "--filter", f"label=mini-harness-run={label}"], text=True, timeout=15
                ).split()
                for container in remaining:
                    if not all(c in "0123456789abcdef" for c in container):
                        raise ValueError("待清理容器标识非法")
                    subprocess.run([str(DOCKER), "rm", "-f", container], capture_output=True, check=True, timeout=30)
            from batch import inference_metrics

            usage = inference_metrics(target / "agent/trajectory.json")
            trace = read(target / "agent/trajectory.json")
            responses = [
                m["extra"]["response"]
                for m in trace["messages"]
                if isinstance(m.get("extra", {}).get("response"), dict)
            ]
            usage["usage_complete"] = usage["usage_complete"] and all(
                isinstance(r.get("usage"), dict)
                and all(isinstance(r["usage"].get(key), int) for key in ("prompt_tokens", "completion_tokens"))
                for r in responses
            )
            usage["accounting_complete"] = usage["usage_complete"] or (
                bool(lock.get("request_recovery")) and recovery_accounted(trace, target / "agent/events.jsonl")
            )
            usage["unknown_requests"] = max(0, usage["api_calls_recorded"] - usage["responses_with_usage"])
            save(target / "usage.json", usage)
            if not usage["accounting_complete"]:
                save(
                    target / "task-result.json",
                    {"variant": variant, "status": "usage_incomplete", "resolved": None, "usage_complete": False},
                )
                raise RuntimeError("模型用量不完整，停止新增付费调用")
            # 官方裁判独立进程，只有它读取隐藏材料。
            with (target / "judge.log").open("w") as log:
                subprocess.run(
                    [
                        str(RUNTIME / "judge-venv/bin/python"),
                        str(ROOT / "judge.py"),
                        task["instance_id"],
                        "prediction",
                        str(target / "judge"),
                        "--prediction",
                        str(target / "agent/prediction.json"),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=1100,
                    check=True,
                )
            result = read(target / "judge/result.json")
            save(
                target / "task-result.json",
                {
                    "instance_id": task["instance_id"],
                    "variant": variant,
                    "status": result["status"],
                    "resolved": result["resolved"],
                    "usage_complete": usage["usage_complete"],
                    "accounting_complete": usage["accounting_complete"],
                    "unknown_requests": usage["unknown_requests"],
                },
            )
            report(run)


def recovery_accounted(trace: dict, event_file: Path) -> bool:
    """只允许有逐请求证据的未知费用；不把审计完整偷换成用量完整。"""
    recovery = trace["info"].get("request_recovery", {})
    if recovery.get("version") != "request-recovery-v1" or not event_file.is_file():
        return False
    events = [json.loads(line) for line in event_file.read_text().splitlines()]
    failed = [e["call"] for e in events if e["event"] == "request_attempt_failed" and e.get("usage_unknown") is True]
    received = [
        e["call"] for e in events if e["event"] in ("request_attempt_succeeded", "request_response_format_error")
    ]
    calls = trace["info"]["model_stats"]["api_calls"]
    responses = [
        m["extra"]["response"] for m in trace["messages"] if isinstance(m.get("extra", {}).get("response"), dict)
    ]
    return (
        failed == recovery.get("unknown_calls")
        and len(failed) == len(set(failed))
        and len(received) == len(set(received)) == len(responses)
        and not set(failed).intersection(received)
        and set(failed + received) == set(range(1, calls + 1))
        and all(
            isinstance(r.get("usage"), dict)
            and all(isinstance(r["usage"].get(k), int) for k in ("prompt_tokens", "completion_tokens"))
            for r in responses
        )
    )


@app.command()
def report(run: Path) -> None:
    """按冻结任务和所有变体展示分母，包括未运行、失败和未知。"""
    lock = verify(run)
    rows = []
    for task in lock["tasks"]:
        for variant in lock["variants"]:
            target = run / "attempts" / task["instance_id"] / variant
            result = read(target / "task-result.json") if (target / "task-result.json").exists() else {}
            agent = read(target / "agent/result.json") if (target / "agent/result.json").exists() else {}
            event_file = target / "agent/events.jsonl"
            validation_seconds = agent.get("validation_seconds")
            if validation_seconds is None and event_file.is_file():
                validation_seconds = sum(
                    event["elapsed_seconds"]
                    for event in map(json.loads, event_file.read_text().splitlines())
                    if event["event"] == "validation_finished"
                )
            rows.append(
                {
                    **task,
                    "variant": variant,
                    "status": result.get("status", "interrupted" if target.exists() else "not_run"),
                    "resolved": result.get("resolved"),
                    "termination_reason": agent.get("termination_reason", agent.get("exit_status")),
                    "api_calls": agent.get("api_calls"),
                    "cost_usd_peak_estimate": agent.get("cost_usd_peak_estimate"),
                    "seconds": agent.get("inference_seconds"),
                    "validations": agent.get("validation_runs"),
                    "validation_seconds": validation_seconds,
                    "wrapup_trigger": agent.get("wrapup_trigger"),
                    "submission_origin": agent.get("submission_origin"),
                    "nonempty_submission": bool(agent.get("submission")) if agent else None,
                }
            )
    save(run / "summary.json", {"registered_tasks": len(lock["tasks"]), "attempts": rows})
    lines = [
        "# Harness 第一版对照结果",
        "",
        "仅统计本轮明确登记的任务；未运行不算已判分失败。",
        "",
        "|任务|组别|状态|解决|调用|验证|",
        "|---|---|---|---|---:|---:|",
    ]
    lines += [
        f"|{r['instance_id']}|{r['variant']}|{r['status']}|{r['resolved']}|{r['api_calls']}|{r['validations']}|"
        for r in rows
    ]
    (run / "report.md").write_text("\n".join(lines) + "\n")
    print(f"报告：{run / 'report.md'}")


if __name__ == "__main__":
    app()
