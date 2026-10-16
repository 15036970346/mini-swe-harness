"""真实 pytest 子进程和容器验证，不使用 mock，不调用模型 API。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from minisweagent.harness import test_feedback
from tests.harness.test_docker_integration import pytestmark as docker_marks
from tests.harness.test_docker_integration import setup as setup


def run_case(tmp_path, source, args=None):
    (tmp_path / "test_case.py").write_text(source)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            test_feedback.__file__,
            json.dumps({"directory": str(artifacts), "args": args or ["-q", "test_case.py"]}),
        ],
        cwd=tmp_path,
        env=os.environ | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, json.loads((artifacts / "result.json").read_text()), artifacts


@pytest.mark.parametrize(
    ("source", "status", "returncode", "counts"),
    [
        ("def test_ok(): assert True\n", "passed", 0, dict(passed=1, failed=0, errors=0, skipped=0)),
        ("def test_bad(): assert 1 == 2\n", "failed", 1, dict(passed=0, failed=1, errors=0, skipped=0)),
        (
            "import pytest\ndef test_skip(): pytest.skip('明确跳过')\n",
            "no_executed_tests",
            0,
            dict(passed=0, failed=0, errors=0, skipped=1),
        ),
        ("value = 1\n", "no_executed_tests", 5, dict(passed=0, failed=0, errors=0, skipped=0)),
        ("def broken(:\n", "pytest_error", 2, dict(passed=0, failed=0, errors=1, skipped=0)),
        (
            "import pytest\n@pytest.fixture\ndef broken(): raise RuntimeError('fixture失败')\n"
            "def test_error(broken): pass\n",
            "failed",
            1,
            dict(passed=0, failed=0, errors=1, skipped=0),
        ),
    ],
)
def test_actual_pytest_outcomes(tmp_path, source, status, returncode, counts):
    process, result, artifacts = run_case(tmp_path, source)
    assert result["status"] == status and result["pytest_returncode"] == returncode
    assert result["counts"] == counts and (artifacts / "stdout.log").is_file()
    assert (process.returncode == 0) is (status == "passed")


def test_failure_details_bounded_but_original_log_complete(tmp_path):
    process, result, artifacts = run_case(
        tmp_path, "def test_failure():\n    print('Z' * 20000)\n    assert False, '关键断言'\n"
    )
    assert process.returncode == result["pytest_returncode"] == 1
    assert "关键断言" in result["failures"][0]["detail"]
    assert len(result["failures"][0]["detail"]) <= 1600
    assert "Z" * 20000 in (artifacts / "stdout.log").read_text()


@pytest.mark.parametrize(("content",), [(None,), ("broken xml",)])  # noqa: PT006
def test_missing_or_malformed_report_is_not_passed(tmp_path, content):
    report = tmp_path / "report.xml"
    if content is not None:
        report.write_text(content)
    assert test_feedback.summarize(report, 0)["status"] == "report_error"


@pytest.mark.parametrize(("mark",), [("skip",), ("xfail",)])  # noqa: PT006
def test_all_skipped_or_xfailed_is_not_success(tmp_path, mark):
    _, result, _ = run_case(tmp_path, f"import pytest\ndef test_case(): pytest.{mark}('不能冒充通过')\n")
    assert result["status"] == "no_executed_tests" and result["counts"]["passed"] == 0


@pytest.mark.parametrize(("mode",), [("bad_option",), ("collect_only",), ("missing_target",)])  # noqa: PT006
def test_non_execution_is_not_success(tmp_path, mode):
    arguments = {
        "bad_option": ["--mini-invalid-option"],
        "collect_only": ["--collect-only", "test_case.py"],
        "missing_target": ["missing.py"],
    }[mode]
    process, result, _ = run_case(tmp_path, "def test_ok(): assert True\n", arguments)
    assert result["status"] != "passed" and process.returncode != 0


@docker_marks[0]
@docker_marks[1]
def test_docker_pipeline_failure_and_preserved_shell_semantics(setup, tmp_path):
    env = setup[0]
    env.put_bytes(b"def test_failure(): assert 1 == 2\n", "/tmp/test_feedback_case.py")
    old = env.execute({"command": "python -m pytest -q /tmp/test_feedback_case.py 2>&1 | tail -5"})
    assert old["returncode"] == 0 and "1 failed" in old["output"]
    env.config.test_feedback = True
    env.config.test_feedback_dir = str(tmp_path / "tool-tests")
    output = env.execute({"command": "mini-test -q /tmp/test_feedback_case.py"})
    result = output["extra"]["test_feedback"]
    assert output["returncode"] == result["pytest_returncode"] == 1
    assert result["status"] == "failed" and result["counts"]["failed"] == 1
    assert "assert 1 == 2" in result["failures"][0]["detail"]
    assert "1 failed" in (Path(output["extra"]["test_feedback_artifact"]) / "stdout.log").read_text()
    assert env.execute({"command": "false | cat"})["returncode"] == 0
    assert env.execute({"command": "mini-test -q /tmp/test_feedback_case.py | tail -1"})["returncode"] == 2


@docker_marks[0]
@docker_marks[1]
def test_docker_timeout_reaps_test_children_and_keeps_partial_log(setup, tmp_path):
    env = setup[0]
    env.config.test_feedback = True
    env.config.test_feedback_dir = str(tmp_path / "tool-tests")
    env.put_bytes(
        b"import subprocess, sys, time\ndef test_slow():\n"
        b"    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        b"    print('partial-log', flush=True)\n    time.sleep(300)\n",
        "/tmp/test_feedback_slow.py",
    )
    output = env.execute({"command": "mini-test -s /tmp/test_feedback_slow.py"}, timeout=3)
    result = output["extra"]["test_feedback"]
    assert output["returncode"] == 124 and result["status"] == "timeout"
    assert result["pytest_returncode"] is None and "partial-log" in result["log_tail"]
    assert (Path(output["extra"]["test_feedback_artifact"]) / "stdout.log").is_file()
    processes = env.raw("ps -eo args").decode()
    assert "time.sleep(300)" not in processes and "runner.py" not in processes


def test_frozen_experiment_records_feedback_switch(tmp_path):
    root = Path(__file__).resolve().parents[2]
    run = tmp_path / "feedback"
    process = subprocess.run(
        [
            sys.executable,
            str(root / "study/evaluation/compare.py"),
            "freeze",
            str(run),
            "--wrapup",
            "--test-feedback",
            "--contract",
            str(root / "study/evaluation/public-validation-pilot-v1.json"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["test_feedback"] is True and manifest["variants"] == ["B3", "B4"]
    assert "frozen/src/minisweagent/harness/test_feedback.py" in manifest["sha256"]
    help_result = subprocess.run(
        [sys.executable, str(run / "frozen/run_verified.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert help_result.returncode == 0 and "test-feedback" in help_result.stdout
