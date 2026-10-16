"""隔离容器内运行官方 4.1.0 测试脚本与判分器；仅宿主裁判进程读取答案。"""

import argparse
import hashlib
import io
import json
import os
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
import docker
from swebench.harness.grading import get_eval_report, get_logs_eval
from swebench.harness.test_spec.test_spec import make_test_spec
from unidiff import PatchSet

from initial_state import inspect_initial_state

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT.parent / ".runtime/evaluation"


def put_text(container, name: str, content: str) -> None:
    buffer = io.BytesIO()
    raw = content.encode()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(name)
        entry.size = len(raw)
        entry.mode = 0o600
        archive.addfile(entry, io.BytesIO(raw))
    container.put_archive("/", buffer.getvalue())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance_id")
    parser.add_argument("mode", choices=["empty", "gold", "prediction"])
    parser.add_argument("output", type=Path)
    parser.add_argument("--prediction", type=Path)
    parser.add_argument("--network", choices=["none", "bridge", "mini-eval-internal"], default="mini-eval-internal")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((ROOT / "manifest-v1.json").read_text())
    raw = (RUNTIME / "judge/verified-12.jsonl").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest["judge_sha256"]
    row = next(r for r in map(json.loads, raw.splitlines()) if r["instance_id"] == args.instance_id)
    task = next(t for t in manifest["tasks"] if t["instance_id"] == args.instance_id)
    spec = make_test_spec(row, namespace="swebench", arch="x86_64")
    patch = row["patch"] if args.mode == "gold" else ""
    if args.mode == "prediction":
        prediction = json.loads(args.prediction.read_text())
        assert prediction["instance_id"] == args.instance_id
        patch = prediction["model_patch"] or ""
    for change in PatchSet(patch):
        path = PurePosixPath(change.path)
        assert not path.is_absolute() and ".." not in path.parts and ".git" not in path.parts
    start = time.time()
    client = docker.from_env(timeout=1000)
    container = client.containers.create(
        task["image_name"], command="sleep 1100", platform="linux/amd64", network_mode=args.network,
        mem_limit="4g", nano_cpus=2_000_000_000, pids_limit=512, working_dir="/testbed",
        dns=["127.0.0.1"] if args.network == "mini-eval-internal" else None,
        name=f"mini-eval-judge-{uuid.uuid4().hex[:12]}", labels={"mini-study-evaluation": "v1"},
    )
    result = {"instance_id": args.instance_id, "mode": args.mode, "evaluator": "swebench==4.1.0",
              "status": "started", "resolved": None, "image": task["image_name"], "network": args.network,
              "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()}
    try:
        container.start()
        def execute_check(command):
            check = container.exec_run(command, workdir="/testbed")
            return check.exit_code, check.output.decode()
        initial = inspect_initial_state(execute_check, task)
        result["initial_tree"] = initial["image_tree"]
        (args.output / "initial-state.json").write_text(json.dumps(initial, indent=2))
        if patch:
            put_text(container, "model.patch", patch)
            applied = container.exec_run("git apply --verbose /model.patch", workdir="/testbed")
            (args.output / "patch-apply.log").write_bytes(applied.output)
            if applied.exit_code != 0:
                result.update(status="patch_apply_failed", resolved=False)
                return
        elif args.mode == "prediction":
            result.update(status="empty_prediction", resolved=False)
            return
        put_text(container, "eval.sh", spec.eval_script)
        (args.output / "eval.sh").write_text(spec.eval_script)
        output = container.exec_run("timeout --kill-after=10s 900s /bin/bash /eval.sh")
        log = args.output / "test_output.txt"
        log.write_bytes(output.output)
        result["process_exit_code"] = output.exit_code
        if output.exit_code in (124, 137):
            result.update(status="judge_timeout", resolved=None)
            return
        pred = {"instance_id": args.instance_id, "model_patch": patch, "model_name_or_path": args.mode}
        official = get_eval_report(spec, pred, log, include_tests_status=True)[args.instance_id]
        statuses, found = get_logs_eval(spec, log)
        expected = spec.FAIL_TO_PASS + spec.PASS_TO_PASS
        missing = [name for name in expected if name not in statuses]
        strict_pass = all(statuses.get(name) in ("PASSED", "XFAIL") for name in expected)
        result.update(status="graded" if found and not missing else "incomplete_test_report",
                      resolved=(official["resolved"] and strict_pass) if found and not missing else None,
                      official_report=official, parsed_test_count=len(statuses), missing_tests=missing,
                      f2p_passed=sum(statuses.get(n) in ("PASSED", "XFAIL") for n in spec.FAIL_TO_PASS),
                      f2p_total=len(spec.FAIL_TO_PASS),
                      p2p_passed=sum(statuses.get(n) in ("PASSED", "XFAIL") for n in spec.PASS_TO_PASS),
                      p2p_total=len(spec.PASS_TO_PASS))
    finally:
        if result["status"] == "started":
            result["status"] = "judge_runtime_error"
        result["elapsed_seconds"] = time.time() - start
        (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        container.remove(force=True)
        client.close()
        print(json.dumps({k: result[k] for k in ("instance_id", "mode", "status", "resolved", "elapsed_seconds")},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
