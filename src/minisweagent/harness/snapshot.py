import fnmatch
import io
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from minisweagent.harness.contracts import Candidate, CandidateRejected, PublicContract, digest, identity, save_json

FileMap = dict[str, tuple[bytes, int]]


def unpack(raw: bytes) -> FileMap:
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for entry in archive:
            if entry.isdir():
                continue
            path = PurePosixPath(entry.name)
            if path.is_absolute() or ".." in path.parts or ".git" in path.parts or not entry.isfile():
                raise CandidateRejected(f"不支持的文件或路径：{entry.name}")
            if entry.name in files:
                raise CandidateRejected(f"重复文件：{entry.name}")
            files[entry.name] = (archive.extractfile(entry).read(), 0o755 if entry.mode & 0o111 else 0o644)
    return files


def records(files: FileMap) -> dict[str, dict]:
    return {name: {"sha256": digest(raw), "mode": mode} for name, (raw, mode) in sorted(files.items())}


def tree_hash(files: FileMap) -> str:
    return identity(records(files))


def protected(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        any(p in ("tests", "test", ".github", ".git") for p in path.parts)
        or path.name.startswith("test_")
        or path.name
        in {"conftest.py", "setup.py", "setup.cfg", "pyproject.toml", "tox.ini", "pytest.ini", ".gitignore"}
    )


def write_files(directory: Path, files: FileMap) -> None:
    for name, (raw, mode) in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(mode)


def make_patch(initial: FileMap, candidate: FileMap) -> bytes:
    with tempfile.TemporaryDirectory(prefix="mini-harness-patch-") as temporary:
        root = Path(temporary)

        def git(*args: str) -> bytes:
            return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)

        git("init", "-q")
        write_files(root, initial)
        git("add", "-f", "--all")
        base_tree = git("write-tree").decode().strip()
        for name in initial.keys() - candidate.keys():
            (root / name).unlink()
        write_files(root, candidate)
        git("add", "-f", "--all")
        return git("-c", "core.quotePath=false", "diff", "--cached", "--binary", "--no-renames", base_tree)


class Snapshotter:
    def __init__(self, initial: FileMap, read_current: Callable[[], FileMap], contract: PublicContract, output: Path):
        self.initial, self.read_current, self.contract, self.output = initial, read_current, contract, output
        if tree_hash(initial) != contract.initial_tree_hash:
            raise CandidateRejected("初始文件树与公开合同不一致")

    def capture(self) -> Candidate:
        current = self.read_current()
        if records(current) != records(self.read_current()):
            raise CandidateRejected("捕获过程中源码仍在变化")
        changed = sorted(
            name for name in self.initial.keys() | current.keys() if self.initial.get(name) != current.get(name)
        )
        excluded, payload = [], dict(self.initial)
        for name in changed:
            if protected(name) or name in self.contract.protected_hashes:
                raise CandidateRejected(f"受保护文件被改动：{name}")
            if any(fnmatch.fnmatchcase(name, p) for p in self.contract.excluded_globs):
                excluded.append(name)
                continue
            if not any(name.startswith(p) for p in self.contract.source_prefixes):
                raise CandidateRejected(f"源码范围之外：{name}")
            if name in current:
                raw, mode = current[name]
                if b"\0" in raw:
                    raise CandidateRejected(f"不支持二进制补丁：{name}")
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise CandidateRejected(f"不支持非 UTF-8 补丁：{name}") from error
                payload[name] = (raw, mode)
            else:
                payload.pop(name, None)
        patch = make_patch(self.initial, payload)
        patch_hash, candidate_tree = digest(patch), tree_hash(payload)
        candidate_id = identity([patch_hash, candidate_tree, self.contract.initial_tree_hash])
        directory = self.output / candidate_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "patch.diff"
        if path.exists() and path.read_bytes() != patch:
            raise CandidateRejected("已存在的候选内容冲突")
        path.write_bytes(patch)
        candidate = Candidate(
            candidate_id=candidate_id,
            tree_hash=candidate_tree,
            patch_hash=patch_hash,
            initial_tree_hash=self.contract.initial_tree_hash,
            patch_path=path,
            files=records(payload),
            changed=[p for p in changed if p not in excluded],
            excluded=excluded,
        )
        save_json(directory / "snapshot.json", candidate.model_dump(mode="json"))
        return candidate
