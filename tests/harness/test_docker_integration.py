import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from minisweagent.agents.verified import VerifiedAgent
from minisweagent.environments.managed_docker import ManagedDockerEnvironment
from minisweagent.harness.contracts import PublicContract
from minisweagent.harness.events import Events
from minisweagent.harness.snapshot import Snapshotter
from minisweagent.harness.validation import PublicValidator
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "study/evaluation"))
from harness_support import environment_config, setup_environment, task_info  # noqa: E402

pytestmark = [
    pytest.mark.docker_integration,
    pytest.mark.skipif(os.getenv("MINI_HARNESS_DOCKER_TESTS") != "1", reason="需显式开启本机 Docker 集成测试"),
]


@pytest.fixture
def setup(tmp_path):
    setup_environment()
    spec = PublicContract.model_validate_json((ROOT / "study/evaluation/public-validation-pilot-v1.json").read_text())
    config = environment_config(task_info(spec.task_id))
    env = ManagedDockerEnvironment(**config)
    initial = env.initial_files()
    snapshotter = Snapshotter(initial, lambda: env.workspace_files(list(initial)), spec, tmp_path / "candidates")
    env.capture_candidate = snapshotter.capture
    validator = PublicValidator(spec, config, tmp_path / "validation")
    try:
        yield env, snapshotter, validator, initial
    finally:
        env.cleanup()


def turn(commands):
    actions = [{"command": command, "tool_call_id": f"call_{i}"} for i, command in enumerate(commands)]
    calls = [
        {
            "id": a["tool_call_id"],
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": a["command"]})},
        }
        for a in actions
    ]
    return make_toolcall_output("机制测试脚本，不是真实模型成绩", calls, actions)


def agent_for(setup, tmp_path, turns, repairs=2, steps=10):
    env, snapshotter, validator, _ = setup
    return VerifiedAgent(
        DeterministicToolcallModel(outputs=turns),
        env,
        snapshotter=snapshotter,
        validator=validator,
        events=Events(tmp_path / "events.jsonl", "test"),
        system_template="公开测试",
        instance_template="{{task}}",
        step_limit=steps,
        cost_limit=0,
        wall_time_limit_seconds=300,
        max_repair_cycles=repairs,
        output_path=tmp_path / "trajectory.json",
    )


def test_real_gate_pairs_tools_blocks_later_write_and_submits_frozen_patch(setup, tmp_path):
    commands = [
        "printf '\n# harness integration\n' >> requests/structures.py",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
        "touch /tmp/must-not-execute",
    ]
    agent = agent_for(setup, tmp_path, [turn(commands)], steps=1)
    info = agent.run("一次提交")
    assert info["termination_reason"] == "verified_submission"
    assert info["validation_runs"] == 1 and info["repair_cycles_started"] == 0
    tools = [m for m in agent.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["call_0", "call_1", "call_2"]
    assert "not_executed_after_interruption" in tools[2]["content"]
    assert setup[0].raw("test ! -e /tmp/must-not-execute && printf ok") == b"ok"
    assert info["submission"] == agent.last_candidate.patch()


@pytest.mark.parametrize(("repairs", "expected", "calls"), [(0, "validation_failed", 1), (2, "verified_submission", 2)])
def test_real_failure_feedback_then_restore(setup, tmp_path, repairs, expected, calls):
    env, _, _, initial = setup
    env.put_bytes(initial["requests/structures.py"][0], "/tmp/original-structures.py")
    broken = "printf '\nCaseInsensitiveDict.__getitem__ = lambda self, key: None\n' >> requests/structures.py"
    turns = [
        turn([broken, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]),
        turn(
            [
                "cp /tmp/original-structures.py requests/structures.py; printf '\n# repaired\n' >> requests/structures.py",
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
            ]
        ),
    ]
    agent = agent_for(setup, tmp_path, turns, repairs)
    info = agent.run("有界重修")
    assert info["termination_reason"] == expected
    assert agent.n_calls == calls and agent.validation_runs == calls
    assert any("tests_failed" in m.get("content", "") for m in agent.messages if m.get("role") == "tool")


def test_timeout_reaps_child_and_preserves_partial_output(setup):
    env = setup[0]
    child = "import time; time.sleep(5); open('/tmp/leaked-child','w').write('bad')"
    command = f"python -c {shlex.quote(child)} & echo 已开始; sleep 10"
    output = env.execute({"command": command}, timeout=2)
    assert output["returncode"] == 124 and "已开始" in output["output"]
    time.sleep(5.2)
    assert env.raw("test ! -e /tmp/leaked-child && printf ok") == b"ok"


@pytest.mark.parametrize(
    ("scenario", "reason", "validations", "calls"),
    [("different", "repair_limit", 3, 3), ("same", "no_progress", 1, 3), ("budget", "step_limit", 1, 1)],
)
def test_repair_and_budget_are_bounded(setup, tmp_path, scenario, reason, validations, calls):
    broken = "printf '\nCaseInsensitiveDict.__getitem__ = lambda self, key: None\n' >> requests/structures.py"
    turns = [turn([broken, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"])]
    for i in range(2):
        commands = [f"printf '\n# repair attempt {i}\n' >> requests/structures.py"] if scenario == "different" else []
        turns.append(turn([*commands, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]))
    agent = agent_for(setup, tmp_path, turns, steps=1 if scenario == "budget" else 10)
    info = agent.run("限制测试")
    assert info["termination_reason"] == reason
    assert info["submission"] == "" and agent.n_calls == calls and agent.validation_runs == validations
    assert Path(info["candidate_artifact"]).is_file()


def test_protected_test_change_rejected_without_running_validation(setup, tmp_path):
    agent = agent_for(
        setup,
        tmp_path,
        [turn(["printf '\n# changed\n' >> tests/test_hooks.py", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"])],
    )
    info = agent.run("测试文件保护")
    assert info["termination_reason"] == "candidate_rejected"
    assert agent.validation_runs == 0 and info["submission"] == ""
