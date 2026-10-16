"""真实HTTP断连/状态码注入；可选实网验证不伪造大模型回答。"""

import json
import os
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import getproxies

import httpx
import pytest
from litellm.exceptions import APIConnectionError, AuthenticationError, InternalServerError, Timeout

from minisweagent.agents.recovering import RecoveringDefaultAgent, RecoveringVerifiedAgent
from minisweagent.agents.verified import VerifiedAgent
from minisweagent.agents.wrapup import RecoveringWrapupAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import LimitsExceeded, TimeExceeded
from minisweagent.harness.contracts import PublicContract
from minisweagent.harness.events import Events
from minisweagent.harness.snapshot import Snapshotter, tree_hash
from minisweagent.harness.validation import PublicValidator
from minisweagent.models.litellm_model import LitellmModel

REPO = Path(__file__).resolve().parents[2]


@contextmanager
def fault_endpoint(*, drops: int, live: bool = False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            server.bodies.append(json.loads(body))
            if len(server.bodies) <= drops:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if not live:
                self.send_error(401, explain="本机鉴权故障注入")
                return
            # 前面的断连没有转发；只有此分支真实调用已配置的提供方。
            with httpx.Client(timeout=40) as client:
                response = client.post(
                    "https://api.deepseek.com/chat/completions",
                    content=body,
                    headers={
                        "Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"],
                        "Content-Type": "application/json",
                    },
                )
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.bodies = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def build_agent(tmp_path, server, agent_class=RecoveringDefaultAgent, **budget):
    model = LitellmModel(
        model_name="deepseek/deepseek-flash",
        model_kwargs={
            "api_base": f"http://127.0.0.1:{server.server_port}/v1",
            "api_key": "local-transport-test",
            "num_retries": 0,
            "timeout": 10,
            "max_tokens": 256,
            "thinking": {"type": "disabled"},
        },
    )
    events = Events(tmp_path / "events.jsonl", "真实连接故障测试")
    extra = {}
    if issubclass(agent_class, VerifiedAgent):
        contract = PublicContract.model_validate_json(
            (REPO / "study/evaluation/public-validation-pilot-v1.json").read_text()
        )

        def files():
            return {p.name: (p.read_bytes(), 0o644) for p in (tmp_path / "workspace").iterdir() if p.is_file()}

        contract = contract.model_copy(update={"initial_tree_hash": tree_hash(files()), "protected_hashes": {}})
        extra = {
            "events": events,
            "snapshotter": Snapshotter(files(), files, contract, tmp_path / "candidates"),
            "validator": PublicValidator(contract, {"image": contract.image}, tmp_path / "validations"),
        }
    agent = agent_class(
        model,
        LocalEnvironment(cwd=str(tmp_path / "workspace")),
        request_events=events,
        system_template="请求恢复测试",
        instance_template="{{ task }}",
        output_path=tmp_path / "trajectory.json",
        **({"step_limit": 60, "cost_limit": 2, "wall_time_limit_seconds": 30} | budget),
        **extra,
    )
    agent.add_messages(
        model.format_message(role="user", content="只调用bash工具一次，command为 printf recovery_ok，不要解释。")
    )
    return agent


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/已完成步骤.txt").write_text("重试不得重新初始化或删除这里")
    # HTTPX 会读取 macOS 系统代理；保留外网代理，仅让本机故障端点直连。
    proxies = getproxies()
    settings = {f"{scheme}_proxy": value for scheme, value in proxies.items() if scheme in ("http", "https", "all")}
    settings.update(
        no_proxy=",".join(filter(None, [proxies.get("no", ""), "127.0.0.1", "localhost", "::1"])),
        MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT="1",
    )
    previous = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    try:
        yield tmp_path
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.mark.parametrize(
    ("budget", "error", "requests"),
    [
        ({"step_limit": 2}, LimitsExceeded, 2),
        ({"cost_limit": 0.5}, LimitsExceeded, 1),
        ({"wall_time_limit_seconds": 2}, TimeExceeded, 1),
        ({}, (APIConnectionError, InternalServerError, Timeout), 3),
    ],
)
def test_real_disconnect_is_bounded_by_requests_cost_and_time(workspace, budget, error, requests):
    with fault_endpoint(drops=100) as server:
        agent = build_agent(workspace, server, **budget)
        original_messages = agent.messages.copy()
        with pytest.raises(error):
            agent.query()
        assert agent.n_calls == len(server.bodies) == requests
        assert agent.messages == original_messages
        assert agent.unknown_calls == list(range(1, requests + 1))
        assert all(body == server.bodies[0] for body in server.bodies)
        assert agent.cost == 0 and agent.recovery_info()["unknown_cost_reserve_usd"] == requests * 0.5
        assert (workspace / "workspace/已完成步骤.txt").is_file()


def test_authentication_error_is_not_retried(workspace):
    with fault_endpoint(drops=0) as server:
        agent = build_agent(workspace, server)
        with pytest.raises(AuthenticationError):
            agent.query()
        assert agent.n_calls == len(server.bodies) == 1
        assert agent.request_retries == 0


@pytest.mark.parametrize(("budget", "calls"), [({"cost_limit": 0.5}, 1), ({"step_limit": 2}, 2)])
def test_wrapup_keeps_unknown_cost_and_call_limits_during_recovery(workspace, budget, calls):
    with fault_endpoint(drops=100) as server:
        agent = build_agent(workspace, server, RecoveringWrapupAgent, **budget)
        result = agent.run("断连耗尽预算时仍不可再发请求")
        assert agent.n_calls == len(server.bodies) == calls
        assert agent.unknown_calls == list(range(1, calls + 1))
        assert agent.recovery_info()["unknown_cost_reserve_usd"] == calls * 0.5
        assert result["termination_reason"] == "no_candidate" and not result["submission"]
        assert result["validation_runs"] == 0


def test_wrapup_does_not_turn_authentication_failure_into_a_submission(workspace):
    with fault_endpoint(drops=0) as server:
        agent = build_agent(workspace, server, RecoveringWrapupAgent)
        with pytest.raises(AuthenticationError):
            agent.run("鉴权失败不得伪装成交付成功")
        assert agent.n_calls == len(server.bodies) == 1 and agent.request_retries == 0
        assert not agent.messages[-1]["extra"]["submission"]


@pytest.mark.parametrize(("agent_class",), [(RecoveringDefaultAgent,), (RecoveringVerifiedAgent,)])  # noqa: PT006
@pytest.mark.skipif(os.getenv("MINI_LIVE_RECOVERY_TEST") != "1", reason="显式启用后进行两次小额真实API恢复验证")
def test_live_response_after_disconnects_keeps_context_and_counts_every_request(workspace, agent_class):
    with fault_endpoint(drops=2, live=True) as server:
        agent = build_agent(workspace, server, agent_class, wall_time_limit_seconds=90)
        assert agent.query()["extra"]["actions"]
        agent.save(workspace / "trajectory.json")
        assert agent.n_calls == len(server.bodies) == 3
        assert agent.request_retries == 2 and agent.unknown_calls == [1, 2]
        assert len(agent.messages) == 2 and agent.cost > 0
        assert all(body == server.bodies[0] for body in server.bodies)
        assert (workspace / "workspace/已完成步骤.txt").read_text() == "重试不得重新初始化或删除这里"
