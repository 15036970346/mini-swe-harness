"""容器内的 pytest 观察器；只提供反馈，不替代独立提交验收。"""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def summarize(report: Path, returncode: int) -> dict:
    result = {
        "pytest_returncode": returncode,
        "status": "report_error",
        "counts": None,
        "failures": [],
        "omitted_failures": 0,
    }
    try:
        cases = list(ET.parse(report).getroot().iter("testcase"))
    except (OSError, ET.ParseError) as error:
        result["detail"] = "未取得完整 JUnit 报告：" + str(error)[:500]
        return result
    counts = dict(passed=0, failed=0, errors=0, skipped=0)
    for case in cases:
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            counts["failed" if problem.tag == "failure" else "errors"] += 1
            if len(result["failures"]) < 3:
                result["failures"].append(
                    {
                        "test": (case.get("classname", "") + "::" + case.get("name", ""))[:400],
                        "detail": (problem.text or problem.get("message", ""))[:1600],
                    }
                )
            else:
                result["omitted_failures"] += 1
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    result["counts"] = counts
    if returncode not in (0, 1, 5):
        result["status"] = "pytest_error"
    elif counts["failed"] or counts["errors"] or returncode == 1:
        result["status"] = "failed"
    elif returncode == 5 or not counts["passed"]:
        result["status"] = "no_executed_tests"
    else:
        result["status"] = "passed"
    return result


def main() -> None:
    spec = json.loads(sys.argv[1])
    directory = Path(spec["directory"])
    report, log = directory / "junit.xml", directory / "stdout.log"
    # 不经过 shell，也不把日志过滤器的退出码当成 pytest 的退出码。
    with log.open("wb") as stream:
        process = subprocess.run(
            [sys.executable, "-m", "pytest", *spec["args"], "--junitxml=" + str(report)],
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    result = summarize(report, process.returncode)
    result.update(log_path=str(log), report_path=str(report))
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    # 超时与进程组清理由外层 supervisor 统一处理，不创建脱离监督的新进程组。
    sys.exit(process.returncode if process.returncode else (0 if result["status"] == "passed" else 2))


if __name__ == "__main__":
    main()
