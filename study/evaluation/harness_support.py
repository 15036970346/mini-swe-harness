"""公开版实验配置；沿用 Docker 环境，不导入或读取隐藏裁判。"""

import json
import os
import shutil
from pathlib import Path

REPO = Path(os.environ.get("MINI_HARNESS_REPO", Path(__file__).resolve().parents[2]))
ROOT = REPO / "study/evaluation"
RUNTIME = ROOT.parent / ".runtime/evaluation"
DOCKER = os.environ.get("MINI_HARNESS_DOCKER") or shutil.which("docker") or "docker"


def setup_environment() -> None:
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        MSWEA_SILENT_STARTUP="1",
        MSWEA_GLOBAL_CONFIG_DIR=str(RUNTIME / "config"),
        MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )


def task_info(instance_id: str) -> dict:
    manifest = json.loads((ROOT / "manifest-v1.json").read_text())
    return next(t for t in manifest["tasks"] if t["instance_id"] == instance_id)


def environment_config(task: dict) -> dict:
    return {
        "image": task["image_name"],
        "cwd": "/testbed",
        "timeout": 120,
        "executable": str(DOCKER),
        "interpreter": ["bash", "-c"],
        "env": {"BASH_ENV": "/root/.bashrc", "PAGER": "cat"},
        "container_timeout": "40m",
        "run_args": [
            "--rm",
            "--platform",
            "linux/amd64",
            "--cpus",
            "2",
            "--memory",
            "4g",
            "--pids-limit",
            "512",
            "--network",
            "mini-eval-internal",
            "--dns",
            "127.0.0.1",
        ],
    }
