"""受控机制基线：真实原生执行器与预算边界；不调用 LLM，不算真实修复率。"""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT.parent / ".runtime/evaluation"
REPO = ROOT.parents[1]
COMMIT = "04d809ceab9df28f9adaed044884180159172930"
UPSTREAM = RUNTIME / "upstream-04d809ce"
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(RUNTIME / "config")
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
sys.path.insert(0, str(UPSTREAM / "src"))

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import Submitted
from minisweagent.models.litellm_model import LitellmModel

GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
CHECK = "from calc import add\nassert add(2, 3) == 5\nassert add(-2, -3) == -5\n"


def main() -> None:
    for name in ("agents/default.py", "environments/local.py", "models/litellm_model.py"):
        expected = subprocess.check_output(["git", "show", f"{COMMIT}:src/minisweagent/{name}"], cwd=REPO)
        assert (UPSTREAM / "src/minisweagent" / name).read_bytes() == expected
    rows = []
    python = shlex.quote(sys.executable)
    for number in (1, 2, 3, 4, 5, 6, 7, 8, 10):
        with tempfile.TemporaryDirectory(prefix="mini-mechanism-") as directory:
            path = Path(directory)
            code = path / "calc.py"
            checks = path / "check.py"
            code.write_text(GOOD)
            checks.write_text(CHECK)
            env = LocalEnvironment(cwd=directory, timeout=5)
            observations = []
            if number in (1, 3, 10):
                observations.append(env.execute({"command": f"{python} -B check.py"}))
            if number == 3:
                code.write_text(BAD)
            elif number == 4:
                code.write_text(BAD)
                checks.write_text("from calc import add\nassert add(2, 3) == -1\n")
                observations.append(env.execute({"command": f"{python} -B check.py"}))
            elif number == 5:
                observations.append(env.execute({"command": f"{python} -c 'import time; time.sleep(2)'"}, timeout=1))
            elif number == 6:
                observations.append(env.execute({"command": f"{python} -m unittest -q"}))
            elif number == 7:
                code.write_text("def add(a, b):\n    return a + b if a >= 0 else 0\n")
                observations.append(env.execute({"command": f"{python} -B check.py"}))
            elif number == 10:
                checks.write_text(CHECK + "assert add(1, 2) == 99\n")
            elif number == 8:
                agent = DefaultAgent(LitellmModel(model_name="deepseek/deepseek-flash"), env,
                                     system_template="Budget boundary test", instance_template="{{task}}", step_limit=1)
                # 显式设置已耗尽计数，验证边界；没有伪造一段真实模型交互。
                agent.n_calls = 1
                info = agent.run("资源已经耗尽，不应继续请求模型。")
                assert info["exit_status"] == "LimitsExceeded"
                rows.append({"id": "M08", "kind": "状态边界单元检查，不是真实模型重复失败轨迹",
                             "observed": info["exit_status"], "contract_met": None,
                             "coverage": "partial：只验证预算耗尽边界，未覆盖重复失败轨迹和最后补丁留存"})
                continue
            submitted = False
            try:
                env.execute({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
            except Submitted:
                submitted = True
            assert submitted
            rows.append({"id": f"M{number:02}", "submitted": submitted,
                         "prior_returncodes": [o["returncode"] for o in observations],
                         "observations": observations,
                         "source_sha256": hashlib.sha256(code.read_bytes()).hexdigest(),
                         "contract_met": number == 1})
    rows.append({"id": "M09", "kind": "不适用：原版没有自动验证及验证缓存，不能把从不验证算成缓存命中",
                 "contract_met": None})
    report = {"mini_commit": COMMIT, "model_api_calls": 0, "api_cost": 0,
              "meaning": "验证原生程序的确定性行为，不代表模型错误发生率，不属于 SWE-bench 修复成绩。",
              "executed_checks": 9, "contract_met": 1, "contract_not_met": 7,
              "partially_checked": 1, "not_applicable": 1,
              "cases": sorted(rows, key=lambda r: r["id"])}
    destination = ROOT.parent / "artifacts/native-mechanism-baseline-v2.json"
    if destination.exists():
        raise SystemExit("已有机制检查记录，不覆盖；需要新文件名。")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
