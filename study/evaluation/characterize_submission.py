"""在固定原版执行器上重现提交语义；真实执行 shell，但不使用 LLM/API。"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
COMMIT = "04d809ceab9df28f9adaed044884180159172930"
RUNTIME = ROOT.parent / ".runtime/evaluation"


def main() -> None:
    # 单独导出原版；不重置或覆盖已有中文注释与学习改动。
    raw = subprocess.check_output(["git", "archive", COMMIT, "src/minisweagent", "LICENSE.md"], cwd=REPO)
    upstream = RUNTIME / "upstream-04d809ce"
    if not upstream.exists():
        upstream.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            archive.extractall(upstream, filter="data")
    source = upstream / "src/minisweagent/environments/local.py"
    expected = subprocess.check_output(["git", "show", f"{COMMIT}:src/minisweagent/environments/local.py"], cwd=REPO)
    assert source.read_bytes() == expected
    # 导入原版包时也隔离配置目录，避免加载用户全局凭据。
    os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(RUNTIME / "config")
    os.environ["MSWEA_SILENT_STARTUP"] = "1"
    sys.path.insert(0, str(upstream / "src"))
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.exceptions import Submitted

    cases = []
    for name, previous_command in [("未执行验证就提交", None), ("上一条命令失败后提交", "false"), ("上一条命令成功后提交", "true")]:
        with tempfile.TemporaryDirectory(prefix="mini-submission-check-") as cwd:
            env = LocalEnvironment(cwd=cwd, timeout=5)
            previous = env.execute({"command": previous_command}) if previous_command else None
            submitted = False
            try:
                env.execute({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
            except Submitted:
                submitted = True
            cases.append({"case": name, "previous_returncode": previous["returncode"] if previous else None,
                          "submitted": submitted})
    assert all(row["submitted"] for row in cases)
    report = {
        "kind": "原生执行器提交语义检查，非真实模型实验，非 SWE-bench 成绩",
        "mini_commit": COMMIT,
        "source_sha256": hashlib.sha256(expected).hexdigest(),
        "cases": cases,
        "conclusion": "提交哨兵可在未验证或上一条命令失败后终止任务；这不说明真实模型发生此行为的频率。",
        "model_api_calls": 0,
        "api_cost": 0,
    }
    target = RUNTIME / "native-submission-characterization.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
