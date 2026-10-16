"""单题原生 mini 入口：不导入裁判数据，不改变 Agent 循环。"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import yaml

from initial_state import inspect_initial_state
from harness_support import DOCKER, setup_environment

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT.parent / ".runtime/evaluation"
REPO = ROOT.parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance_id")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    setup_environment()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise ValueError("请设置 DEEPSEEK_API_KEY 环境变量。")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((ROOT / "manifest-v1.json").read_text())
    upstream = RUNTIME / "upstream-04d809ce"
    archive_bytes = subprocess.check_output(
        ["git", "archive", manifest["mini_commit"], "src/minisweagent", "LICENSE.md"], cwd=REPO
    )
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
        for item in archive.getmembers():
            if item.isfile():
                assert (upstream / item.name).read_bytes() == archive.extractfile(item).read(), item.name
    packets = []
    for group in ("dev", "holdout"):
        raw = (RUNTIME / f"inputs/{group}/test.jsonl").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == manifest["input_sha256"][group]
        packets.extend(json.loads(line) for line in raw.splitlines())
    task = next(row for row in packets if row["instance_id"] == args.instance_id)
    assert set(task) == {"instance_id", "repo", "base_commit", "problem_statement", "version", "image_name"}
    os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(RUNTIME / "config")
    os.environ["MSWEA_SILENT_STARTUP"] = "1"
    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    sys.path.insert(0, str(upstream / "src"))
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.models import get_model
    from minisweagent.run.benchmarks.swebench import get_sb_environment
    from minisweagent.utils.serialize import recursive_merge

    base = yaml.safe_load((upstream / "src/minisweagent/config/benchmarks/swebench.yaml").read_text())
    config = recursive_merge(base, yaml.safe_load((ROOT / "pilot.yaml").read_text()))
    config["environment"]["executable"] = str(DOCKER)
    config["agent"]["output_path"] = str(args.output / "trajectory.json")
    (args.output / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
    start = time.time()
    env = get_sb_environment(config, task)
    (args.output / "container-id.txt").write_text(env.container_id)
    try:
        def execute_check(command):
            check = env.execute({"command": command})
            return check["returncode"], check["output"]
        initial = inspect_initial_state(execute_check, task)
        (args.output / "initial-state.json").write_text(json.dumps(initial, indent=2))
        # 密钥只供宿主模型适配器读取，绝不写进配置/轨迹或传给容器。
        agent = DefaultAgent(get_model(config=config["model"]), env, **config["agent"])
        inference_start = time.time()
        info = agent.run(task["problem_statement"])
        prediction = {"instance_id": task["instance_id"], "model_name_or_path": config["model"]["model_name"],
                      "model_patch": info.get("submission", "")}
        (args.output / "prediction.json").write_text(json.dumps(prediction, ensure_ascii=False, indent=2))
        (args.output / "patch.diff").write_text(prediction["model_patch"])
        result = {"instance_id": task["instance_id"], "exit_status": info.get("exit_status"),
                  "api_calls": agent.n_calls, "cost_usd_peak_estimate": agent.cost,
                  "environment_seconds": inference_start - start,
                  "inference_seconds": time.time() - inference_start,
                  "mini_commit": manifest["mini_commit"], "core_unmodified": True}
        (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        subprocess.run([config["environment"]["executable"], "rm", "-f", env.container_id],
                       capture_output=True, timeout=30)
        env.container_id = None


if __name__ == "__main__":
    main()
