"""公开版的数据恢复必须拒绝漂移，并保持既有清单不变。"""

import hashlib
import importlib.util
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SOURCE = Path(__file__).resolve().parents[2] / "study/evaluation/prepare.py"
spec = importlib.util.spec_from_file_location("public_prepare", SOURCE)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_restore_checks_all_hashes_before_writing(tmp_path):
    row = {
        "instance_id": "fixture",
        "repo": "sample/repo",
        "base_commit": "abc",
        "problem_statement": "测试题面",
        "version": "1",
        "patch": "参考补丁",
    }
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([row]), buffer)
    raw = buffer.getvalue()
    packet = {**{k: row[k] for k in prepare.SAFE_FIELDS}, "image_name": "fixture-image"}

    def sha(value):
        return hashlib.sha256(value.encode()).hexdigest()

    manifest = {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "judge_sha256": sha(prepare.jsonl([row])),
        "input_sha256": {"dev": sha(prepare.jsonl([packet])), "pilot": sha(prepare.jsonl([packet]))},
        "tasks": [{"instance_id": "fixture", "group": "dev", "pilot": True, "image_name": "fixture-image"}],
    }
    invalid = manifest | {"judge_sha256": "0" * 64}
    with pytest.raises(ValueError, match="摘要"):
        prepare.restore_runtime(invalid, raw, tmp_path)
    assert not list(tmp_path.iterdir())
    prepare.restore_runtime(manifest, raw, tmp_path)
    prepare.restore_runtime(manifest, raw, tmp_path)
    assert (tmp_path / "inputs/dev/test.jsonl").read_text() == prepare.jsonl([packet])
    assert (tmp_path / "judge/verified-12.jsonl").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="字节摘要"):
        prepare.restore_runtime(manifest, b"changed", tmp_path)
