import subprocess

import pytest

from minisweagent.harness.contracts import CandidateRejected, PublicContract, digest
from minisweagent.harness.snapshot import Snapshotter, records, tree_hash, write_files


def contract(initial, **kwargs):
    return PublicContract(
        task_id="fixture",
        image="fixture@sha256:test",
        initial_tree_hash=tree_hash(initial),
        command=["python", "-m", "pytest", "tests"],
        expected_nodeids=["tests/test_a.py::test_ok"],
        protected_hashes={n: digest(v[0]) for n, v in initial.items() if n.startswith("tests/")},
        source="固定公开单元测试",
        runner_hash="test",
        **kwargs,
    )


def test_multifile_snapshot_includes_new_deleted_and_mode_without_touching_source(tmp_path):
    initial = {"a.py": (b"old\n", 0o644), "deleted.py": (b"old\n", 0o644), "tests/test_a.py": (b"assert True\n", 0o644)}
    current = {
        "a.py": (b"new\n", 0o755),
        "new.py": (b"added\n", 0o644),
        "tests/test_a.py": initial["tests/test_a.py"],
        "repro.py": (b"scratch\n", 0o644),
    }
    snapshotter = Snapshotter(initial, lambda: dict(current), contract(initial), tmp_path / "artifacts")
    candidate = snapshotter.capture()
    assert candidate.changed == ["a.py", "deleted.py", "new.py"]
    assert candidate.excluded == ["repro.py"]
    target = tmp_path / "verify"
    target.mkdir()
    write_files(target, initial)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "apply", str(candidate.patch_path)], cwd=target, check=True)
    assert (target / "new.py").read_bytes() == b"added\n"
    assert not (target / "deleted.py").exists()
    assert (target / "a.py").stat().st_mode & 0o111
    assert candidate.patch_hash == digest(candidate.patch().encode())
    assert snapshotter.capture().candidate_id == candidate.candidate_id
    candidate.patch_path.write_text("tampered")
    with pytest.raises(ValueError, match="冻结补丁"):
        candidate.patch()


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("tests/test_a.py", b"assert True", "受保护"),
        ("conftest.py", b"pass", "受保护"),
        ("new.py", b"\x00", "二进制"),
    ],
)
def test_reject_before_exclusion(tmp_path, name, raw, message):
    initial = {"a.py": (b"x=1", 0o644)}
    current = initial | {name: (raw, 0o644)}
    with pytest.raises(CandidateRejected, match=message):
        Snapshotter(initial, lambda: current, contract(initial), tmp_path).capture()


def test_snapshot_refuses_moving_tree(tmp_path):
    initial = {"a.py": (b"one", 0o644)}
    reads = iter([initial, {"a.py": (b"two", 0o644)}])
    with pytest.raises(CandidateRejected, match="仍在变化"):
        Snapshotter(initial, lambda: next(reads), contract(initial), tmp_path).capture()
    assert records(initial)["a.py"]["mode"] == 0o644
