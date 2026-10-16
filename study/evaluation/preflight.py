"""验证冻结清单、信息隔离和本机条件；绝不读取密钥或发起模型调用。"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from harness_support import DOCKER

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT.parent / ".runtime/evaluation"
ALLOWED = {"instance_id", "repo", "base_commit", "problem_statement", "version", "image_name"}


def validate_packets(manifest: dict, runtime: Path = RUNTIME) -> list[str]:
    problems = []
    grouped = {}
    for group, digest in manifest["input_sha256"].items():
        raw = (runtime / f"inputs/{group}/test.jsonl").read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            return [f"{group} 输入指纹不一致"]
        rows = [json.loads(line) for line in raw.splitlines()]
        grouped[group] = {row["instance_id"] for row in rows}
        if len(rows) != len(grouped[group]):
            problems.append(f"{group} 存在重复任务")
        for row in rows:
            if set(row) != ALLOWED:
                problems.append(f"{row['instance_id']} 字段不符合白名单")
    if grouped["dev"] & grouped["holdout"]:
        problems.append("开发组与保留组重叠")
    if not grouped["pilot"] <= grouped["dev"]:
        problems.append("试跑任务不属于开发组")
    for group in ("dev", "holdout"):
        expected = {row["instance_id"] for row in manifest["tasks"] if row["group"] == group}
        if grouped[group] != expected:
            problems.append(f"{group} 与清单任务不一致")
    judge = runtime / "judge/verified-12.jsonl"
    if hashlib.sha256(judge.read_bytes()).hexdigest() != manifest["judge_sha256"]:
        problems.append("裁判数据指纹不一致")
    return problems


def main() -> int:
    manifest = json.loads((ROOT / "manifest-v1.json").read_text())
    problems = validate_packets(manifest)
    docker_path = shutil.which(str(DOCKER))
    environment = dict(os.environ)
    docker_ready = False
    if docker_path:
        result = subprocess.run([docker_path, "info", "--format", "{{.Architecture}}"],
                                text=True, capture_output=True, timeout=15, env=environment)
        docker_ready = result.returncode == 0
    blockers = list(problems)
    if not docker_ready:
        blockers.append("未发现可用的 Docker 容器运行器")
    report = {
        "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "free_disk_GiB": round(shutil.disk_usage(ROOT).free / 2**30, 2),
        "input_integrity_and_isolation_passed": not problems,
        "docker_cli": docker_path,
        "docker_ready": docker_ready,
        "status": "blocked_environment" if blockers else "ready_for_environment_controls",
        "blockers": blockers,
        "model_calls": 0,
        "api_cost": 0,
        "gold_control": "not_run",
        "empty_patch_control": "not_run",
        "resolved": None,
    }
    path = RUNTIME / "preflight.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
