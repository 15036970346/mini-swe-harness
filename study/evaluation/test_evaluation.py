"""评测夹具的离线检查，不调用模型和 Docker。"""

import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("eval_preflight", ROOT / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def test_frozen_real_packets_have_no_oracle_leakage():
    manifest = json.loads((ROOT / "manifest-v1.json").read_text())
    assert preflight.validate_packets(manifest) == []
    assert len(manifest["tasks"]) == 12
    assert all("@sha256:" in task["image_name"] for task in manifest["tasks"])


def test_extra_oracle_field_is_rejected_even_with_matching_hash(tmp_path):
    source = ROOT.parent / ".runtime/evaluation"
    manifest = json.loads((ROOT / "manifest-v1.json").read_text())
    for group in ("dev", "holdout", "pilot"):
        path = tmp_path / f"inputs/{group}/test.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes((source / f"inputs/{group}/test.jsonl").read_bytes())
    judge = tmp_path / "judge/verified-12.jsonl"
    judge.parent.mkdir(parents=True)
    judge.write_bytes((source / "judge/verified-12.jsonl").read_bytes())
    path = tmp_path / "inputs/dev/test.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["test_patch"] = "禁止流入 Agent 的评测测试"
    content = "".join(json.dumps(row) + "\n" for row in rows)
    path.write_text(content)
    manifest["input_sha256"]["dev"] = hashlib.sha256(content.encode()).hexdigest()
    assert any("白名单" in error for error in preflight.validate_packets(manifest, tmp_path))


def test_edit_after_freeze_is_rejected(tmp_path):
    source = ROOT.parent / ".runtime/evaluation"
    manifest = json.loads((ROOT / "manifest-v1.json").read_text())
    for group in ("dev", "holdout", "pilot"):
        path = tmp_path / f"inputs/{group}/test.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes((source / f"inputs/{group}/test.jsonl").read_bytes())
    judge = tmp_path / "judge/verified-12.jsonl"
    judge.parent.mkdir(parents=True)
    judge.write_bytes((source / "judge/verified-12.jsonl").read_bytes())
    path = tmp_path / "inputs/holdout/test.jsonl"
    path.write_text(path.read_text() + "\n")
    assert "holdout 输入指纹不一致" in preflight.validate_packets(manifest, tmp_path)
