"""真实容器中的确定性收尾回归，不调用模型 API，也不代表 SWE-bench 成绩。"""

import json
import time

import pytest

from minisweagent.agents.verified import VerifiedAgent
from minisweagent.agents.wrapup import WrapupAgent
from minisweagent.harness.events import Events
from minisweagent.models.test_models import DeterministicToolcallModel
from tests.harness.test_docker_integration import pytestmark, turn  # noqa: F401
from tests.harness.test_docker_integration import setup as setup

EDIT = "printf '\n# 收尾机制测试，并非真实修复\n' >> requests/structures.py"
BROKEN = "printf '\nCaseInsensitiveDict.__getitem__ = lambda self, key: None\n' >> requests/structures.py"
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def agent_for(setup, tmp_path, commands, agent_class=WrapupAgent, **budget):
    env, snapshotter, validator, _ = setup
    return agent_class(
        DeterministicToolcallModel(
            outputs=[turn(batch) for batch in commands],
            observation_template="{{ output.output }} {{ output.exception_info }}\n{{ wrapup_budget | default({}) | tojson }}",
        ),
        env,
        snapshotter=snapshotter,
        validator=validator,
        events=Events(tmp_path / "events.jsonl", "收尾机制验收"),
        system_template="无模型费用的机制测试",
        instance_template="{{ task }}",
        output_path=tmp_path / "trajectory.json",
        **({"step_limit": 10, "cost_limit": 0, "wall_time_limit_seconds": 300} | budget),
    )


@pytest.mark.parametrize(
    ("agent_class", "reason", "calls"),
    [(VerifiedAgent, "step_limit", 10), (WrapupAgent, "wrapup_verified_submission", 9)],
)
def test_same_wandering_script_reproduces_empty_baseline_and_recovers_delivery(
    setup, tmp_path, agent_class, reason, calls
):
    agent = agent_for(setup, tmp_path, [[EDIT]] + [["printf 探索"]] * 9, agent_class)
    result = agent.run("已有源码修改但始终不提交")
    assert result["termination_reason"] == reason and agent.n_calls == calls
    assert bool(result["submission"]) is (agent_class is WrapupAgent)
    if agent_class is WrapupAgent:
        assert result["submission"] == agent.last_candidate.patch()
        assert result["submission_origin"] == "host_wrapup" and result["validation_runs"] == 1
        assert result["patch_hash"] == agent.last_candidate.patch_hash
        events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
        assert len([e for e in events if e["event"] == "wrapup_warning"]) == 1
        assert any('"calls": 1' in m.get("content", "") for m in agent.messages if m["role"] == "tool")
    assert len([m for m in agent.messages if m["role"] == "tool"]) == calls


@pytest.mark.parametrize(("repair",), [(True,), (False,)])  # noqa: PT006
def test_automatic_failure_then_changed_or_identical_candidate(setup, tmp_path, repair):
    env, _, _, initial = setup
    env.put_bytes(initial["requests/structures.py"][0], "/tmp/original-structures.py")
    last = ["cp /tmp/original-structures.py requests/structures.py", EDIT] if repair else ["printf 不改源码"]
    agent = agent_for(setup, tmp_path, [[BROKEN]] + [["printf 探索"]] * 8 + [last])
    result = agent.run("失败反馈后只验证对应版本")
    assert agent.n_calls == 10 and result["automatic_checks"] == 2
    assert result["validation_runs"] == (2 if repair else 1)
    assert bool(result["submission"]) is repair
    assert result["termination_reason"] == (
        "wrapup_verified_submission" if repair else "budget_without_verified_candidate"
    )
    assert any("tests_failed" in m.get("content", "") for m in agent.messages if m["role"] == "user")
    assert len(agent.results) == (2 if repair else 1)
    if repair:
        assert "CaseInsensitiveDict.__getitem__" not in result["submission"]


def test_candidate_created_only_on_last_call_is_not_lost(setup, tmp_path):
    agent = agent_for(setup, tmp_path, [["printf 探索"]] * 9 + [[EDIT]])
    result = agent.run("最后一轮才形成候选")
    assert result["termination_reason"] == "wrapup_verified_submission"
    assert result["wrapup_trigger"] == "step_limit" and result["automatic_checks"] == 2
    assert result["validation_runs"] == 1 and agent.n_calls == 10


@pytest.mark.parametrize(
    ("command", "reason", "calls"),
    [
        ("printf 无修改", "no_candidate", 10),
        ("touch reproduce_case.py", "no_candidate", 10),
        ("touch tests/test_new.py", "candidate_rejected", 9),
    ],
)
def test_empty_excluded_or_protected_changes_do_not_become_success(setup, tmp_path, command, reason, calls):
    agent = agent_for(setup, tmp_path, [[command]] + [["printf 探索"]] * 9)
    result = agent.run("非法或空候选不能通过收尾")
    assert result["termination_reason"] == reason
    assert not result["submission"] and result["validation_runs"] == 0 and agent.n_calls == calls


@pytest.mark.parametrize(("empty",), [(True,), (False,)])  # noqa: PT006
def test_normal_submission_still_pairs_every_tool_and_blocks_later_write(setup, tmp_path, empty):
    agent = agent_for(
        setup, tmp_path, [["printf 无修改" if empty else EDIT, SUBMIT, "touch /tmp/forbidden-later-write"]]
    )
    result = agent.run("模型主动提交")
    assert result["termination_reason"] == ("no_candidate" if empty else "verified_submission")
    assert agent.n_calls == 1 and result["automatic_checks"] == 0
    tools = [m for m in agent.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["call_0", "call_1", "call_2"]
    assert "not_executed_after_interruption" in tools[-1]["content"]
    assert setup[0].raw("test ! -e /tmp/forbidden-later-write && printf ok") == b"ok"
    if not empty:
        assert result["submission_origin"] == "model"


def test_cost_alone_can_trigger_wrapup(setup, tmp_path):
    agent = agent_for(setup, tmp_path, [[EDIT]] + [["printf 探索"]] * 9, step_limit=0, cost_limit=10)
    result = agent.run("费用阈值独立生效")
    assert agent.n_calls == 9 and result["termination_reason"] == "wrapup_verified_submission"
    assert result["automatic_checks"] == 1


def test_deadline_does_not_start_validation_or_request(setup, tmp_path):
    agent = agent_for(setup, tmp_path, [], wall_time_limit_seconds=1)
    setup[0].execute({"command": EDIT})
    time.sleep(1.05)
    result = agent.run("到期后不补跑")
    assert result["termination_reason"] == "deadline" and not result["submission"]
    assert agent.n_calls == result["validation_runs"] == 0


def test_time_reserve_can_trigger_without_any_model_request(setup, tmp_path):
    setup[0].execute({"command": EDIT})
    agent = agent_for(setup, tmp_path, [], wall_time_limit_seconds=180)
    result = agent.run("独立时间预留触发")
    assert result["termination_reason"] == "wrapup_verified_submission"
    assert result["wrapup_trigger"] == "time_reserve" and agent.n_calls == 0


def test_last_call_failure_does_not_start_impossible_repair(setup, tmp_path):
    agent = agent_for(setup, tmp_path, [[BROKEN]], step_limit=1)
    result = agent.run("没有剩余调用不能宣称开始重修")
    assert result["termination_reason"] == "budget_without_verified_candidate"
    assert result["repair_cycles_started"] == 0 and agent.n_calls == result["validation_runs"] == 1


def test_validation_report_mismatch_fails_closed(setup, tmp_path):
    setup[2].contract = setup[2].contract.model_copy(update={"expected_nodeids": ["不存在的公开测试"]})
    agent = agent_for(setup, tmp_path, [[EDIT]] + [["printf 探索"]] * 9)
    result = agent.run("验证器报告不完整不能交付")
    assert result["termination_reason"] == "validation_error" and not result["submission"]
    assert agent.last_validation.reason_code == "incomplete_junit"
    assert agent.n_calls == 9 and result["validation_runs"] == 1
