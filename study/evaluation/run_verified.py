"""第一版单题入口；基准裁判在此进程结束后单独执行。"""

import json
import os
import sys
import time
from pathlib import Path

import typer
import yaml
from harness_support import ROOT, environment_config, setup_environment, task_info
from initial_state import inspect_initial_state

app = typer.Typer(pretty_exceptions_show_locals=False)


@app.command()
def main(
    instance_id: str,
    output: Path,
    contract: Path,
    source: Path,
    packet: Path,
    variant: str = "B3",
    request_recovery: bool = False,
    test_feedback: bool = False,
) -> None:
    if variant not in ("B0", "B1", "B2", "B3", "B4"):
        raise ValueError("变体必须是 B0/B1/B2/B3/B4")
    setup_environment()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise ValueError("请在宿主进程设置 DEEPSEEK_API_KEY；不要把密钥写入源码或配置。")
    sys.path.insert(0, str(source.resolve()))
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.agents.recovering import RecoveringDefaultAgent, RecoveringVerifiedAgent
    from minisweagent.agents.verified import VerifiedAgent
    from minisweagent.agents.wrapup import RecoveringWrapupAgent, WrapupAgent
    from minisweagent.environments.managed_docker import ManagedDockerEnvironment
    from minisweagent.harness.contracts import PublicContract, digest, save_json
    from minisweagent.harness.events import Events
    from minisweagent.harness.snapshot import Snapshotter
    from minisweagent.harness.validation import PublicValidator
    from minisweagent.models import get_model
    from minisweagent.utils.serialize import recursive_merge

    output.mkdir(parents=True, exist_ok=False)
    spec = PublicContract.model_validate_json(contract.read_text())
    row = json.loads(packet.read_text())
    if set(row) != {"instance_id", "repo", "base_commit", "problem_statement", "version", "image_name"}:
        raise ValueError("模型输入必须严格满足白名单")
    task = task_info(instance_id)
    if spec.task_id != instance_id or row["instance_id"] != instance_id or spec.image != task["image_name"]:
        raise ValueError("题面、公开合同和镜像不匹配")
    base = yaml.safe_load((source / "minisweagent/config/benchmarks/swebench.yaml").read_text())
    config = recursive_merge(base, yaml.safe_load((ROOT / "pilot.yaml").read_text()))
    config["environment"] = environment_config(task)
    if test_feedback:
        config["environment"].update(test_feedback=True, test_feedback_dir=str(output.resolve() / "tool-tests"))
        config["agent"]["instance_template"] += (
            "\n测试请优先使用 mini-test -q tests/相关文件.py；它在当前任务目录直接运行 pytest，"
            "返回真实 pytest 退出码、通过/失败/跳过数量、失败详情和完整日志路径。"
            "这是 Bash 工具中的专用入口，只接受 pytest 参数，不要加 cd、管道或重定向。"
            "普通 Bash 仍可用，但 pytest | tail 的返回码可能只代表 tail 成功。"
            "测试观察不等于独立验收，也不证明 Issue 已修复。\n"
        )
    config["environment"]["run_args"] += ["--label", f"mini-harness-run={digest(str(output.resolve()).encode())}"]
    config["agent"]["output_path"] = str(output / "trajectory.json")
    config["agent"]["instance_template"] += (
        "\n<harness_contract>\nThe host freezes all allowed source changes at submission, including new files. "
        "The patch printed by the command is retained for audit; delivery uses the frozen source changes. "
        "If a submission returns public test failure feedback instead of ending, continue repairing. "
        "Public validation command: " + json.dumps(spec.command) + "\n</harness_contract>\n"
    )
    if variant == "B2":
        config["agent"]["instance_template"] += "Run the public validation command before submitting.\n"
    if variant == "B4":
        config["agent"]["instance_template"] += (
            "\n宿主会在预算接近上限时主动验证当前非空补丁，通过就交付；失败只允许在原预算内重修。"
            "容器离线，依赖已预装，不要尝试下载或远程查询。\n"
        )
        config["model"]["observation_template"] += "\n<宿主预算>{{ wrapup_budget | tojson }}</宿主预算>\n"
    config["request_recovery"] = request_recovery
    config["test_feedback"] = test_feedback
    save_json(output / "config.json", config)
    save_json(
        output / "imports.json",
        {
            "default_agent": sys.modules[DefaultAgent.__module__].__file__,
            "verified_agent": sys.modules[VerifiedAgent.__module__].__file__,
            "wrapup_agent": sys.modules[WrapupAgent.__module__].__file__,
        },
    )
    events = Events(output / "events.jsonl", f"{output.parent.name}/{instance_id}/{variant}")
    env = ManagedDockerEnvironment(**config["environment"])
    (output / "container-id.txt").write_text(env.container_id)
    agent = None
    try:

        def execute_check(command):
            result = env.execute({"command": command})
            return result["returncode"], result["output"]

        save_json(output / "initial-state.json", inspect_initial_state(execute_check, task))
        initial = env.initial_files()
        snapshotter = Snapshotter(initial, lambda: env.workspace_files(list(initial)), spec, output / "candidates")
        env.capture_candidate = snapshotter.capture
        model = get_model(config=config["model"])
        recovery_kwargs = {"request_events": events} if request_recovery else {}
        if variant in ("B1", "B3", "B4"):
            agent_class = RecoveringVerifiedAgent if request_recovery else VerifiedAgent
            if variant == "B4":
                agent_class = RecoveringWrapupAgent if request_recovery else WrapupAgent
            agent = agent_class(
                model,
                env,
                snapshotter=snapshotter,
                validator=PublicValidator(spec, config["environment"], output / "validations"),
                events=events,
                max_repair_cycles=0 if variant == "B1" else 2,
                **recovery_kwargs,
                **config["agent"],
            )
        else:
            agent = (RecoveringDefaultAgent if request_recovery else DefaultAgent)(
                model, env, **recovery_kwargs, **config["agent"]
            )
        start = time.monotonic()
        try:
            info = agent.run(row["problem_statement"])
        except Exception:
            save_json(
                output / "result.json",
                {
                    "instance_id": instance_id,
                    "variant": variant,
                    "exit_status": agent.messages[-1].get("extra", {}).get("exit_status", "runtime_error"),
                    "api_calls": agent.n_calls,
                    "cost_usd_peak_estimate": agent.cost,
                    "inference_seconds": time.monotonic() - start,
                    "request_recovery": agent.recovery_info() if request_recovery else None,
                    "runtime_error": True,
                },
            )
            raise
        patch = info.get("submission", "")
        save_json(
            output / "prediction.json",
            {"instance_id": instance_id, "model_name_or_path": f"harness-{variant}", "model_patch": patch},
        )
        (output / "patch.diff").write_text(patch)
        save_json(
            output / "result.json",
            {
                **info,
                "instance_id": instance_id,
                "variant": variant,
                "api_calls": agent.n_calls,
                "cost_usd_peak_estimate": agent.cost,
                "inference_seconds": time.monotonic() - start,
                "agent_loop_unmodified": variant in ("B0", "B2"),
                "runtime_version": ("harness-v1.1" if variant == "B4" else "harness-v1")
                + ("-request-recovery-v1" if request_recovery else ""),
                "request_recovery": agent.recovery_info() if request_recovery else None,
                "contract_hash": spec.sha256,
                "test_feedback": test_feedback,
            },
        )
        print(json.dumps({"variant": variant, "exit_status": info.get("exit_status"), "api_calls": agent.n_calls}))
    finally:
        env.cleanup()


if __name__ == "__main__":
    app()
