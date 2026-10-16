import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(value: object) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


class PublicContract(BaseModel):
    schema_version: int = 1
    task_id: str
    image: str
    initial_tree_hash: str
    command: list[str]
    expected_nodeids: list[str] = Field(min_length=1)
    allowed_skips: list[str] = []
    allowed_xfails: list[str] = []
    protected_hashes: dict[str, str]
    source: str
    runner_hash: str
    timeout_seconds: float = Field(default=120, gt=0)
    source_prefixes: list[str] = Field(default_factory=lambda: [""])
    excluded_globs: list[str] = ["patch.txt", "reproduce*.py", "repro*.py", "__pycache__/*", ".pytest_cache/*"]
    baseline_outcomes: dict[str, str] = {}

    @model_validator(mode="after")
    def check_contract(self):
        if len(set(self.expected_nodeids)) != len(self.expected_nodeids):
            raise ValueError("公开测试 nodeid 重复")
        if not self.command or self.command[:3] != ["python", "-m", "pytest"]:
            raise ValueError("第一版只支持 python -m pytest 命令")
        if not set(self.allowed_skips + self.allowed_xfails) <= set(self.expected_nodeids):
            raise ValueError("skip/xfail 清单必须属于预期测试")
        return self

    @property
    def sha256(self) -> str:
        return identity(self.model_dump())


class Candidate(BaseModel):
    candidate_id: str
    tree_hash: str
    patch_hash: str
    initial_tree_hash: str
    patch_path: Path
    files: dict[str, dict]
    changed: list[str]
    excluded: list[str]

    def patch(self) -> str:
        raw = self.patch_path.read_bytes()
        if digest(raw) != self.patch_hash:
            raise ValueError("已冻结补丁的字节发生变化")
        return raw.decode("utf-8")


class ValidationResult(BaseModel):
    status: Literal["passed", "code_failure", "validation_error", "timeout", "candidate_rejected"]
    reason_code: str
    candidate_id: str
    contract_hash: str
    identity_key: str
    elapsed_seconds: float = 0
    returncode: int | None = None
    cases: dict[str, str] = {}
    failures: list[dict] = []
    log_path: str = ""
    cleanup_complete: bool = False

    def feedback(self) -> dict:
        failures, size = [], 0
        for failure in self.failures:
            item = {**failure, "detail": str(failure.get("detail", ""))[:2500]}
            length = len(json.dumps(item, ensure_ascii=False))
            if size + length > 6000:
                break
            failures.append(item)
            size += length
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "candidate_id": self.candidate_id,
            "failures": failures,
            "omitted_count": len(self.failures) - len(failures),
            "public_log": self.log_path,
        }


class CandidateRejected(ValueError):
    pass
