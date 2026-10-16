"""验证费用口径、失败计数和固定分母；不调用模型或容器。"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("eval_batch", ROOT / "batch.py")
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def test_cached_input_and_failed_attempt_remain_in_cost(tmp_path):
    trace = {"info": {"model_stats": {"api_calls": 2, "instance_cost": .42}, "exit_status": "TimeExceeded"},
             "messages": [{"extra": {"response": {"usage": {
                 "prompt_tokens": 1_000_000, "completion_tokens": 100_000,
                 "prompt_cache_hit_tokens": 500_000, "prompt_tokens_details": None}}}}]}
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace))
    metrics = batch.inference_metrics(path)
    assert metrics["cny_peak_estimate"] == pytest.approx(1.82)
    assert metrics["cny_offpeak_estimate"] == pytest.approx(.91)
    assert metrics["api_calls_recorded"] == 2 and metrics["responses_with_usage"] == 1
    assert metrics["usage_complete"] is False


def test_fixed_denominator_does_not_discard_failed_or_unrun_tasks(tmp_path):
    tasks = [{"instance_id": name, "group": "dev"} for name in ("ok", "failed", "infra", "pending")]
    for name, resolved in (("ok", True), ("failed", False), ("infra", None)):
        batch.save(tmp_path / name / "task-result.json", {
            "instance_id": name, "status": "graded" if resolved is not None else "gold_control_failed",
            "resolved": resolved, "model": {"usd_peak_estimate": .2 if name != "infra" else 0}})
    result = batch.summarize(tmp_path, tasks)
    assert result["planned_tasks"] == 4 and result["assessed_tasks"] == 2
    assert result["resolved_over_planned"] == .25 and result["resolved_over_assessed"] == .5
    assert result["usd_peak_estimate"] == pytest.approx(.4) and result["not_run"] == 1
