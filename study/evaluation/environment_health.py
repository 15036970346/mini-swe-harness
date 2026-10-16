"""对冻结任务镜像进行无模型调用的工具健康检查；不读取隐藏裁判。"""

import json
import logging
import shlex
import time
from pathlib import Path

import typer
from harness_support import ROOT, environment_config, setup_environment

PROBES = [
    (["grep", "--version"], 0, "grep"),
    (["grep", "-n", "^ID=", "/etc/os-release"], 0, "ID="),
    (["grep", "MINI_HEALTH_NO_MATCH_20261006", "/etc/os-release"], 1, ""),
    (["grep", "[", "/etc/os-release"], 2, ""),
    (["git", "rev-parse", "HEAD"], 0, ""),
    (
        [
            "python",
            "-c",
            "import platform, subprocess; print(platform.machine()); subprocess.run(['git', 'status', '--porcelain'], check=True)",
        ],
        0,
        "x86_64",
    ),
]

PROGRAM = """
import json,resource,subprocess,sys
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
rows=[]
for repeat in range(int(sys.argv[1])):
    for argv,code,needle in json.loads(sys.argv[2]):
        r=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=10)
        output=r.stdout.decode('utf-8','replace')
        rows.append(dict(repeat=repeat,argv=argv,returncode=r.returncode,
                         passed=r.returncode==code and needle in output and 'QEMU internal' not in output,
                         output=output[:1000]))
print(json.dumps(rows))
"""


def check_task(task: dict, repeats: int) -> dict:
    from minisweagent.environments.managed_docker import ManagedDockerEnvironment

    started = time.monotonic()
    env = ManagedDockerEnvironment(**environment_config(task))
    try:
        result = env.execute(
            {"command": shlex.join(["python", "-c", PROGRAM, str(repeats), json.dumps(PROBES)])},
            timeout=180,
        )
        probes = json.loads(result["output"]) if result["returncode"] == 0 else []
        return {
            "instance_id": task["instance_id"],
            "image": task["image_name"],
            "passed": len(probes) == len(PROBES) * repeats and all(p["passed"] for p in probes),
            "seconds": time.monotonic() - started,
            "probes": probes,
            "execution": result,
        }
    finally:
        env.cleanup()


def main(output: Path, repeats: int = typer.Option(10, min=1, max=100)) -> None:
    setup_environment()
    logging.getLogger("minisweagent.environment").setLevel(logging.WARNING)
    output.mkdir(parents=True, exist_ok=False)
    tasks = json.loads((ROOT / "manifest-v1.json").read_text())["tasks"]
    rows = []
    for task in tasks:
        result = check_task(task, repeats)
        (output / f"{task['instance_id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        rows.append({k: result[k] for k in ("instance_id", "image", "passed", "seconds")})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    passed = all(r["passed"] for r in rows)
    (output / "summary.json").write_text(
        json.dumps(
            {
                "passed": passed,
                "repeats": repeats,
                "checks_per_image": repeats * len(PROBES),
                "model_calls": 0,
                "tasks": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    raise typer.Exit(0 if passed else 1)


if __name__ == "__main__":
    typer.run(main)
