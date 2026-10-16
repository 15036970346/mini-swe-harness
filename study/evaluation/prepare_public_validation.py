"""从初始仓库公开测试准备合同，两次独立执行后冻结；不接触官方答案。"""

import json
import shlex
from pathlib import Path

import typer
from harness_support import environment_config, setup_environment, task_info
from initial_state import inspect_initial_state

app = typer.Typer()


def collect(task: dict, command: list[str]) -> tuple[dict, dict, dict]:
    from minisweagent.environments.managed_docker import ManagedDockerEnvironment
    from minisweagent.harness import pytest_reporter
    from minisweagent.harness.contracts import digest
    from minisweagent.harness.snapshot import protected, tree_hash

    env = ManagedDockerEnvironment(**environment_config(task))
    try:

        def execute(command):
            output = env.execute({"command": command})
            return output["returncode"], output["output"]

        inspect_initial_state(execute, task)
        initial = env.initial_files()
        env.put_bytes(Path(pytest_reporter.__file__).read_bytes(), "/tmp/mini_public_reporter.py")
        output = env.execute(
            {
                "command": "PYTHONPATH=/tmp${PYTHONPATH:+:$PYTHONPATH} "
                "MINI_PUBLIC_REPORT=/tmp/mini-cases.json "
                + shlex.join([*command, "-p", "mini_public_reporter", "--junitxml=/tmp/mini-junit.xml"])
            }
        )
        if output["returncode"] != 0:
            raise RuntimeError("公开基线测试不通过，先检查命令及依赖：\n" + output["output"][-4000:])
        report = json.loads(env.raw("cat /tmp/mini-cases.json"))
        metadata = {
            "initial_tree_hash": tree_hash(initial),
            "protected_hashes": {n: digest(v[0]) for n, v in initial.items() if protected(n)},
            "runner_hash": digest(Path(pytest_reporter.__file__).read_bytes()),
        }
        return metadata, report, output
    finally:
        env.cleanup()


@app.command()
def main(instance_id: str, output: Path, selector: list[str] = typer.Option(..., "--selector")) -> None:
    setup_environment()
    from minisweagent.harness.contracts import PublicContract, save_json
    from minisweagent.harness.validation import classify

    if output.exists():
        raise FileExistsError(f"已冻结合同不覆盖：{output}")
    command = ["python", "-m", "pytest", "-q", *selector]
    task = task_info(instance_id)
    first, report, _ = collect(task, command)
    second, again, _ = collect(task, command)
    if first != second or report["collected"] != again["collected"]:
        raise ValueError("公开测试收集或文件树不稳定")
    contract = PublicContract(
        task_id=instance_id,
        image=task["image_name"],
        command=command,
        expected_nodeids=report["collected"],
        source="固定镜像原始仓库中的公开测试，无官方测试补丁",
        source_prefixes={
            "psf/requests": ["requests/"],
            "pytest-dev/pytest": ["src/"],
            "sphinx-doc/sphinx": ["sphinx/"],
        }[task["repo"]],
        allowed_skips=sorted(
            {r["nodeid"] for r in report["reports"] if r["outcome"] == "skipped" and not r.get("wasxfail")}
        ),
        allowed_xfails=sorted(
            {r["nodeid"] for r in report["reports"] if r["outcome"] == "skipped" and r.get("wasxfail")}
        ),
        **first,
    )
    first_status, _, outcomes, _ = classify(report, contract, 0)
    if first_status != "passed" or classify(again, contract, 0)[2] != outcomes:
        raise ValueError("公开测试基线有未声明跳过、异常或状态漂移")
    contract.baseline_outcomes = outcomes
    save_json(output, contract.model_dump())
    print(
        json.dumps(
            {"contract": str(output), "tests": len(contract.expected_nodeids), "sha256": contract.sha256},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    app()
