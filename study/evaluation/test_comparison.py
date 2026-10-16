import json
import subprocess

import pytest
from compare import freeze, recovery_accounted, report, verify
from harness_support import REPO


def test_freeze_keeps_original_core_and_report_rebuild_is_stable(tmp_path):
    contract = REPO / "study/evaluation/public-validation-pilot-v1.json"
    if not contract.exists():
        pytest.skip("需先创建公开 pilot 合同")
    run = tmp_path / "run"
    freeze(run, [contract])
    lock = verify(run)
    default = run / "frozen/src/minisweagent/agents/default.py"
    expected = subprocess.check_output(
        ["git", "show", f"{lock['upstream_commit']}:src/minisweagent/agents/default.py"], cwd=REPO
    )
    assert default.read_bytes() == expected
    assert (run / "frozen/src/minisweagent/agents/verified.py").is_file()
    report(run)
    first = (run / "summary.json").read_bytes()
    report(run)
    assert first == (run / "summary.json").read_bytes()
    rows = json.loads(first)["attempts"]
    assert len(rows) == 4 and all(r["status"] == "not_run" and r["resolved"] is None for r in rows)
    default.write_text("changed")
    with pytest.raises(ValueError, match="冻结文件"):
        verify(run)


def test_unknown_usage_requires_complete_per_request_evidence(tmp_path):
    trace = {
        "info": {
            "model_stats": {"api_calls": 3},
            "request_recovery": {
                "version": "request-recovery-v1",
                "unknown_calls": [1, 2],
            },
        },
        "messages": [{"extra": {"response": {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}}}],
    }
    events = tmp_path / "events.jsonl"
    rows = [{"event": "request_attempt_failed", "call": i, "usage_unknown": True} for i in (1, 2)]
    rows.append({"event": "request_attempt_succeeded", "call": 3})
    events.write_text("\n".join(map(json.dumps, rows)))
    assert recovery_accounted(trace, events)
    events.write_text("\n".join(map(json.dumps, rows[1:])))
    assert not recovery_accounted(trace, events)
    events.write_text("\n".join(map(json.dumps, rows + rows[:1])))
    assert not recovery_accounted(trace, events)
    events.write_text("\n".join(map(json.dumps, rows)))
    trace["messages"][0]["extra"]["response"]["usage"] = {}
    assert not recovery_accounted(trace, events)


def test_wrapup_experiment_is_opt_in_and_freezes_both_variants(tmp_path):
    run = tmp_path / "wrapup"
    freeze(run, [REPO / "study/evaluation/public-validation-pilot-v1.json"], request_recovery=True, wrapup=True)
    lock = verify(run)
    assert lock["variants"] == ["B3", "B4"] and lock["request_recovery"]
    path = run / "frozen/src/minisweagent/agents/wrapup.py"
    assert path.read_bytes() == (REPO / "src/minisweagent/agents/wrapup.py").read_bytes()
    report(run)
    assert len(json.loads((run / "summary.json").read_text())["attempts"]) == 2


def test_b3_validation_time_uses_actual_events_not_zero_for_missing_evidence(tmp_path):
    run = tmp_path / "metrics"
    freeze(run, [REPO / "study/evaluation/public-validation-pilot-v1.json"], wrapup=True)
    task_id = verify(run)["tasks"][0]["instance_id"]
    agent = run / "attempts" / task_id / "B3" / "agent"
    agent.mkdir(parents=True)
    (agent / "result.json").write_text(json.dumps({"validation_runs": 1, "submission": "frozen patch"}))
    report(run)
    assert json.loads((run / "summary.json").read_text())["attempts"][0]["validation_seconds"] is None
    (agent / "events.jsonl").write_text(json.dumps({"event": "validation_finished", "elapsed_seconds": 2.5}) + "\n")
    report(run)
    row = json.loads((run / "summary.json").read_text())["attempts"][0]
    assert row["validation_seconds"] == 2.5 and row["nonempty_submission"] is True
