"""固定小样本、下载题面与评测数据，查询镜像体量；不运行模型或容器。"""

import argparse
import concurrent.futures
import hashlib
import io
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT.parent / ".runtime/evaluation"
DATASET = "princeton-nlp/SWE-bench_Verified"
REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
UPSTREAM = "04d809ceab9df28f9adaed044884180159172930"
# 在任何模型推理前固定分组；pilot 是 dev 的子集，不单独算一次独立测试。
GROUPS = {
    "dev": [
        "psf__requests-5414",
        "psf__requests-1921",
        "pytest-dev__pytest-5262",
        "pytest-dev__pytest-5631",
        "sphinx-doc__sphinx-9367",
        "sphinx-doc__sphinx-8593",
    ],
    "holdout": [
        "psf__requests-2931",
        "psf__requests-1766",
        "pytest-dev__pytest-6202",
        "pytest-dev__pytest-7236",
        "sphinx-doc__sphinx-8056",
        "sphinx-doc__sphinx-9591",
    ],
}
PILOT = ["psf__requests-5414", "pytest-dev__pytest-5262", "sphinx-doc__sphinx-9367"]
SAFE_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement", "version")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "agent-study-eval/1"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def inspect_image(instance_id: str) -> dict:
    name = "swebench/sweb.eval.x86_64." + instance_id.replace("__", "_1776_")
    data = json.loads(fetch(f"https://hub.docker.com/v2/repositories/{name}/tags/latest"))
    image = next(item for item in data["images"] if item.get("architecture") == "amd64")
    return {
        "image_name": f"docker.io/{name}@{image['digest']}",
        "compressed_bytes": image["size"],
        "architecture": image["architecture"],
        "metadata_url": f"https://hub.docker.com/v2/repositories/{name}/tags/latest",
    }


def write_once(path: Path, content: str) -> None:
    if path.exists() and path.read_text() != content:
        raise SystemExit(f"拒绝覆盖已冻结文件：{path}；需要新版本清单。")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def restore_runtime(manifest: dict, raw: bytes, runtime: Path = RUNTIME) -> None:
    """按已发布摘要恢复下载数据，不刷新任务、镜像或冻结清单。"""
    if hashlib.sha256(raw).hexdigest() != manifest["dataset_sha256"]:
        raise ValueError("数据集字节摘要不一致，停止恢复")
    by_id = {row["instance_id"]: row for row in pq.read_table(io.BytesIO(raw)).to_pylist()}
    tasks = manifest["tasks"]
    contents = {"judge/verified-12.jsonl": jsonl([by_id[t["instance_id"]] for t in tasks])}
    for group in manifest["input_sha256"]:
        selected = [t for t in tasks if (t["pilot"] if group == "pilot" else t["group"] == group)]
        contents[f"inputs/{group}/test.jsonl"] = jsonl(
            [
                {**{key: by_id[t["instance_id"]][key] for key in SAFE_FIELDS}, "image_name": t["image_name"]}
                for t in selected
            ]
        )
    expected = {"judge/verified-12.jsonl": manifest["judge_sha256"]}
    expected.update({f"inputs/{g}/test.jsonl": value for g, value in manifest["input_sha256"].items()})
    for name, content in contents.items():
        if hashlib.sha256(content.encode()).hexdigest() != expected[name]:
            raise ValueError(f"恢复内容不匹配冻结摘要：{name}")
    for name, content in contents.items():
        write_once(runtime / name, content)
    (runtime / "judge/verified-12.jsonl").chmod(0o600)
    print("已恢复固定数据；未修改清单、未启动容器、未调用模型。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-runtime", action="store_true", help="按冻结清单重新下载并校验运行数据")
    args = parser.parse_args()
    manifest_path = ROOT / "manifest-v1.json"
    if args.restore_runtime:
        manifest = json.loads(manifest_path.read_text())
        url = (
            f"https://huggingface.co/datasets/{manifest['dataset']}/resolve/"
            f"{manifest['dataset_revision']}/data/test-00000-of-00001.parquet"
        )
        restore_runtime(manifest, fetch(url))
        return
    if manifest_path.exists():
        raise SystemExit("manifest-v1.json 已存在且已冻结；不自动重新抽样或刷新镜像。")
    url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/data/test-00000-of-00001.parquet"
    raw = fetch(url)
    all_rows = pq.read_table(io.BytesIO(raw)).to_pylist()
    by_id = {row["instance_id"]: row for row in all_rows}
    ids = GROUPS["dev"] + GROUPS["holdout"]
    assert len(ids) == len(set(ids)) == 12
    assert set(PILOT) <= set(GROUPS["dev"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        images = dict(zip(ids, pool.map(inspect_image, ids)))

    tasks = []
    for group, group_ids in GROUPS.items():
        for iid in group_ids:
            row = by_id[iid]
            tasks.append(
                {
                    "instance_id": iid,
                    "group": group,
                    "pilot": iid in PILOT,
                    "repo": row["repo"],
                    "base_commit": row["base_commit"],
                    "difficulty": row["difficulty"],
                    "title": row["problem_statement"].splitlines()[0],
                    "problem_utf8_bytes": len(row["problem_statement"].encode()),
                    "fail_to_pass_count": len(json.loads(row["FAIL_TO_PASS"])),
                    "pass_to_pass_count": len(json.loads(row["PASS_TO_PASS"])),
                    **images[iid],
                }
            )

    # 仅裁判数据包含 gold/test patch；此目录绝不能挂载给 Agent 容器。
    judge_path = RUNTIME / "judge/verified-12.jsonl"
    write_once(judge_path, jsonl([by_id[iid] for iid in ids]))
    judge_path.chmod(0o600)
    packet_hashes = {}
    for group, group_ids in {**GROUPS, "pilot": PILOT}.items():
        packets = [
            {**{key: by_id[iid][key] for key in SAFE_FIELDS}, "image_name": images[iid]["image_name"]}
            for iid in group_ids
        ]
        content = jsonl(packets)
        path = RUNTIME / f"inputs/{group}/test.jsonl"
        write_once(path, content)
        packet_hashes[group] = hashlib.sha256(content.encode()).hexdigest()

    manifest = {
        "protocol_version": "v1",
        "prepared_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "dataset": DATASET,
        "dataset_revision": REVISION,
        "dataset_source_rows": len(all_rows),
        "dataset_download_bytes": len(raw),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "mini_commit": UPSTREAM,
        "selection": "先按仓库与人工难度筛选，再在运行前固定12个候选；是定向便利样本，非全量随机样本。",
        "input_sha256": packet_hashes,
        "judge_sha256": hashlib.sha256(judge_path.read_bytes()).hexdigest(),
        "preflight_status": "待基准补丁与空补丁环境预检；没有模型成绩",
        "tasks": tasks,
    }
    write_once(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "task_count": len(tasks),
                "dataset_bytes": len(raw),
                "input_bytes": {g: (RUNTIME / f"inputs/{g}/test.jsonl").stat().st_size for g in packet_hashes},
                "image_sum_GiB_before_layer_deduplication": sum(t["compressed_bytes"] for t in tasks) / 2**30,
                "oracle_fields_in_agent_input": False,
                "model_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
