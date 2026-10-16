"""可选的预算收尾策略；复用公开门禁，不改变原版控制循环。"""

import json
import time

from minisweagent.agents.recovering import RequestRecoveryMixin
from minisweagent.agents.verified import VerifiedAgent
from minisweagent.exceptions import LimitsExceeded, TimeExceeded
from minisweagent.harness.contracts import CandidateRejected


class WrapupAgent(VerifiedAgent):
    warning_fraction = 0.75
    wrapup_fraction = 0.90
    validation_overhead_seconds = 60

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.warned = self.wrapup_started = False
        self.automatic_checks = 0
        self.wrapup_trigger = self.submission_origin = None

    def budget(self) -> dict:
        reserve = len(getattr(self, "unknown_calls", [])) * getattr(self, "unknown_request_reserve_usd", 0)
        seconds = max(0.0, self.deadline - time.monotonic()) if self.config.wall_time_limit_seconds else None
        fractions = [0.0]
        reason = None
        for name, used, limit in (
            ("step_limit", self.n_calls, self.config.step_limit),
            ("cost_limit", self.cost + reserve, self.config.cost_limit),
            ("deadline", time.monotonic() - self.started, self.config.wall_time_limit_seconds),
        ):
            if limit > 0:
                fractions.append(used / limit)
                if used >= limit:
                    reason = name
        return {
            "calls": self.n_calls,
            "call_limit": self.config.step_limit or None,
            "known_cost_usd": self.cost,
            "unknown_cost_reserve_usd": reserve,
            "cost_limit_usd": self.config.cost_limit or None,
            "remaining_seconds": round(seconds, 1) if seconds is not None else None,
            "used_fraction": max(fractions),
            "limit_reason": reason,
        }

    def get_template_vars(self, **kwargs) -> dict:
        return super().get_template_vars(**kwargs) | {"wrapup_budget": self.budget()}

    def harness_info(self) -> dict:
        return super().harness_info() | {
            "wrapup_version": "wrapup-v1.1",
            "wrapup_trigger": self.wrapup_trigger,
            "automatic_checks": self.automatic_checks,
            "submission_origin": self.submission_origin,
            "candidate_id": self.last_candidate.candidate_id if self.last_candidate else None,
            "patch_hash": self.last_candidate.patch_hash if self.last_candidate else None,
            "validation_candidate_id": self.last_validation.candidate_id if self.last_validation else None,
            "validation_seconds": sum(result.elapsed_seconds for result in self.results.values()),
        }

    def _notice(self, content: str) -> None:
        self.add_messages(self.model.format_message(role="user", content="[宿主收尾控制器] " + content))

    def _submission(self, *, allow_repair: bool = True) -> tuple[dict, dict | None]:
        self.last_candidate = getattr(self.env, "last_candidate", None) or self.snapshotter.capture()
        if not self.last_candidate.patch():
            return {"reason_code": "no_candidate"}, self._exit("no_candidate")
        feedback, terminal = super()._submission(allow_repair=allow_repair)
        if terminal and terminal["extra"].get("submission_accepted"):
            self.submission_origin = "model"
            terminal["extra"].update(self.harness_info())
        return feedback, terminal

    def _automatic_submission(self, trigger: str, *, final: bool = False) -> None:
        self.wrapup_started, self.wrapup_trigger = True, trigger
        self.automatic_checks += 1
        self.events.emit("wrapup_started", trigger=trigger, final=final, budget=self.budget())
        if trigger == "deadline" or time.monotonic() >= self.deadline:
            self.add_messages(self._exit("deadline"))
            return
        try:
            # 模型可能在上次提交后继续改过代码，禁止沿用 env 中旧候选。
            self.last_candidate = self.snapshotter.capture()
            self.env.last_candidate = self.last_candidate
            if not self.last_candidate.patch():
                if final:
                    self.add_messages(self._exit("no_candidate"))
                else:
                    self._notice("当前没有可交付源码修改；请在剩余预算内完成最小修复并提交。")
                return
            feedback, terminal = self._submission(allow_repair=not final)
        except CandidateRejected as error:
            self.events.emit("wrapup_candidate_rejected", detail=str(error))
            self.add_messages(self._exit("candidate_rejected"))
            return
        self._notice(json.dumps(feedback, ensure_ascii=False))
        if terminal and terminal["extra"].get("submission_accepted"):
            self.submission_origin = "host_wrapup"
            terminal["extra"].update(termination_reason="wrapup_verified_submission", **self.harness_info())
        if terminal:
            self.add_messages(terminal)

    def step(self) -> list[dict]:
        budget = self.budget()
        if budget["limit_reason"]:
            self._automatic_submission(budget["limit_reason"], final=True)
            return []
        time_to_close = (
            budget["remaining_seconds"] is not None
            and budget["remaining_seconds"]
            <= self.validator.contract.timeout_seconds + self.validation_overhead_seconds
        )
        if not self.warned and (budget["used_fraction"] >= self.warning_fraction or time_to_close):
            self.warned = True
            self.events.emit("wrapup_warning", budget=budget)
            self._notice(
                "预算接近上限，请停止无关探索，优先最小修复、针对性测试并提交。"
                "容器离线，不要下载依赖或远程仓库；公开回归通过不代表 Issue 已修复。预算："
                + json.dumps(budget, ensure_ascii=False)
            )
        if not self.wrapup_started and (budget["used_fraction"] >= self.wrapup_fraction or time_to_close):
            self._automatic_submission("time_reserve" if time_to_close else "budget_fraction")
            if self.messages[-1].get("role") == "exit":
                return []
        try:
            return super().step()
        except LimitsExceeded as error:
            # 恢复层可能在一次 step 内耗尽预算，不能再发请求，也不重放工具。
            self._automatic_submission(
                "deadline" if isinstance(error, TimeExceeded) else self.budget()["limit_reason"] or "request_budget",
                final=True,
            )
            return []


class RecoveringWrapupAgent(RequestRecoveryMixin, WrapupAgent):
    pass
