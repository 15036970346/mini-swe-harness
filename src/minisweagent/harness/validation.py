import json
import shlex
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from minisweagent.environments.managed_docker import ManagedDockerEnvironment
from minisweagent.harness import pytest_reporter
from minisweagent.harness.contracts import Candidate, PublicContract, ValidationResult, digest, identity, save_json
from minisweagent.harness.snapshot import records, tree_hash


def classify(report: dict, contract: PublicContract, returncode: int) -> tuple[str, str, dict, list]:
    if report.get("collection_errors"):
        return "validation_error", "collection_error", {}, report["collection_errors"]
    collected = report.get("collected", [])
    if any(row.get("nodeid") not in contract.expected_nodeids for row in report.get("reports", [])):
        return "validation_error", "unexpected_test_report", {}, []
    if len(collected) != len(set(collected)) or set(collected) != set(contract.expected_nodeids):
        return "validation_error", "incomplete_collection", {}, []
    cases, failures = {}, []
    for nodeid in contract.expected_nodeids:
        stages = [r for r in report.get("reports", []) if r["nodeid"] == nodeid]
        if len({r["when"] for r in stages}) != len(stages):
            return "validation_error", "duplicate_report", cases, failures
        by_phase = {r["when"]: r for r in stages}
        if "setup" not in by_phase or "teardown" not in by_phase:
            return "validation_error", "incomplete_report", cases, failures
        if any(r["outcome"] == "failed" for r in stages):
            cases[nodeid] = "failed"
            failures.extend(r for r in stages if r["outcome"] == "failed")
        elif any(r["outcome"] == "skipped" for r in stages):
            xfail = any(r.get("wasxfail") for r in stages)
            cases[nodeid] = "xfail" if xfail else "skipped"
            allowed = contract.allowed_xfails if xfail else contract.allowed_skips
            if nodeid not in allowed:
                return "validation_error", "unexpected_skip", cases, failures
        elif by_phase.get("call", {}).get("outcome") == "passed":
            cases[nodeid] = "passed"
        else:
            return "validation_error", "incomplete_report", cases, failures
    if failures:
        return "code_failure", "tests_failed", cases, failures
    if "passed" not in cases.values():
        return "validation_error", "zero_executed", cases, []
    if returncode != 0 or report.get("exitstatus") != 0:
        return "validation_error", "unexpected_exit", cases, []
    return "passed", "verified_by_dev_tests", cases, []


class PublicValidator:
    def __init__(self, contract: PublicContract, environment_config: dict, output: Path):
        self.contract, self.environment_config, self.output = contract, dict(environment_config), output
        if self.environment_config["image"] != contract.image:
            raise ValueError("验证镜像与公开合同不一致")
        if digest(Path(pytest_reporter.__file__).read_bytes()) != contract.runner_hash:
            raise ValueError("公开测试报告器版本变化，需重新冻结合同")

    def key(self, candidate: Candidate) -> str:
        return identity(
            [
                candidate.tree_hash,
                candidate.patch_hash,
                self.contract.sha256,
                self.environment_config,
                self.contract.runner_hash,
            ]
        )

    def validate(self, candidate: Candidate, deadline: float) -> ValidationResult:
        started = time.monotonic()
        directory = self.output / uuid.uuid4().hex
        directory.mkdir(parents=True)
        result = ValidationResult(
            status="validation_error",
            reason_code="not_started",
            candidate_id=candidate.candidate_id,
            contract_hash=self.contract.sha256,
            identity_key=self.key(candidate),
            log_path=str(directory / "stdout.log"),
        )
        env = None
        try:
            if deadline <= started:
                result.status, result.reason_code = "timeout", "deadline"
                return result
            env = ManagedDockerEnvironment(**self.environment_config)
            initial = env.initial_files()
            if (
                tree_hash(initial) != candidate.initial_tree_hash
                or tree_hash(initial) != self.contract.initial_tree_hash
            ):
                result.reason_code = "initial_tree_mismatch"
                return result
            if any(
                name not in initial or digest(initial[name][0]) != sha
                for name, sha in self.contract.protected_hashes.items()
            ):
                result.reason_code = "protected_hash_mismatch"
                return result
            patch = candidate.patch().encode()
            if patch:
                env.put_bytes(patch, "/tmp/mini-candidate.patch")
                env.raw("git apply -- /tmp/mini-candidate.patch")
            names = sorted(set(initial) | set(candidate.files))
            if records(env.workspace_files(names)) != candidate.files:
                result.reason_code = "candidate_tree_mismatch"
                return result
            env.put_bytes(Path(pytest_reporter.__file__).read_bytes(), "/tmp/mini_public_reporter.py")
            command = (
                "PYTHONPATH=/tmp${PYTHONPATH:+:$PYTHONPATH} MINI_PUBLIC_REPORT=/tmp/mini-cases.json "
                + shlex.join([*self.contract.command, "-p", "mini_public_reporter", "--junitxml=/tmp/mini-junit.xml"])
            )
            remaining = min(self.contract.timeout_seconds, deadline - time.monotonic())
            if remaining <= 0:
                result.status, result.reason_code = "timeout", "deadline"
                return result
            output = env.execute({"command": command}, timeout=remaining)
            Path(result.log_path).write_text(output["output"])
            result.returncode = output["returncode"]
            if output.get("extra", {}).get("timed_out") or time.monotonic() >= deadline:
                result.status, result.reason_code = "timeout", "test_timeout"
                return result
            if records(env.workspace_files(names)) != candidate.files:
                result.status, result.reason_code = "candidate_rejected", "candidate_changed"
                return result
            report_bytes = env.raw("cat /tmp/mini-cases.json")
            junit = env.raw("cat /tmp/mini-junit.xml")
            (directory / "cases.json").write_bytes(report_bytes)
            (directory / "junit.xml").write_bytes(junit)
            junit_root = ET.fromstring(junit)
            if len(list(junit_root.iter("testcase"))) != len(self.contract.expected_nodeids):
                result.reason_code = "incomplete_junit"
                return result
            report = json.loads(report_bytes)
            result.status, result.reason_code, result.cases, result.failures = classify(
                report, self.contract, output["returncode"]
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, ET.ParseError) as error:
            result.status, result.reason_code = "validation_error", type(error).__name__
            result.failures = [{"detail": str(error)}]
        finally:
            if env is not None:
                try:
                    env.cleanup()
                    result.cleanup_complete = True
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                    result.status, result.reason_code = "validation_error", "cleanup_failed"
                    result.failures.append({"detail": str(error)})
            result.elapsed_seconds = time.monotonic() - started
            save_json(directory / "result.json", result.model_dump())
        return result
