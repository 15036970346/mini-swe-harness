"""用真实临时 Git 仓库验证初始状态检查，不 mock 进程或环境。"""

import subprocess

import pytest
from initial_state import inspect_initial_state


def make_repo(tmp_path):
    def execute(command):
        result = subprocess.run(command.split(), cwd=tmp_path, capture_output=True, text=True)
        return result.returncode, result.stdout

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "evaluation@example.invalid")
    git("config", "user.name", "Evaluation test")
    for name in ("setup.py", "tox.ini", "source.py"):
        (tmp_path / name).write_text("original\n")
    git("add", ".")
    git("commit", "-qm", "base")
    return execute, git, {"repo": "sphinx-doc/sphinx", "base_commit": git("rev-parse", "HEAD")}


def test_unchanged_image(tmp_path):
    execute, git, task = make_repo(tmp_path)
    report = inspect_initial_state(execute, task)
    assert report["preparation_files"] == [] and "grep" in report["grep_version"].lower()


def test_sphinx_preparation_is_recorded(tmp_path):
    execute, git, task = make_repo(tmp_path)
    (tmp_path / "setup.py").write_text("pinned dependency\n")
    git("commit", "-qam", "SWE-bench")
    report = inspect_initial_state(execute, task)
    assert report["preparation_files"] == ["setup.py"]
    assert "pinned dependency" in report["preparation_diff"]


@pytest.mark.parametrize(("case",), [("source",), ("dirty",), ("other_repo",), ("wrong_commit",)])
def test_unapproved_changes_rejected(tmp_path, case):
    execute, git, task = make_repo(tmp_path)
    target = "source.py" if case == "source" else "setup.py"
    (tmp_path / target).write_text("changed\n")
    if case != "dirty":
        git("commit", "-qam", "not preparation" if case == "wrong_commit" else "SWE-bench")
    if case == "other_repo":
        task["repo"] = "psf/requests"
    with pytest.raises(AssertionError):
        inspect_initial_state(execute, task)
