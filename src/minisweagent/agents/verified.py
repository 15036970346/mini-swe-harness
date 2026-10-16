import json
import time
from pathlib import Path

from pydantic import Field

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.exceptions import Submitted, TimeExceeded
from minisweagent.harness.contracts import CandidateRejected
from minisweagent.harness.events import Events
from minisweagent.harness.snapshot import Snapshotter
from minisweagent.harness.validation import PublicValidator


class VerifiedAgentConfig(AgentConfig):
    max_repair_cycles: int = Field(default=2, ge=0, le=2)
    no_progress_limit: int = Field(default=2, ge=1)


class VerifiedAgent(DefaultAgent):
    def __init__(self, model, env, *, snapshotter: Snapshotter, validator: PublicValidator, events: Events, **kwargs):
        super().__init__(model, env, config_class=VerifiedAgentConfig, **kwargs)
        self.snapshotter, self.validator, self.events = snapshotter, validator, events
        self.started = time.monotonic()
        self.deadline = self.started + (self.config.wall_time_limit_seconds or float("inf"))
        self.repair_cycles = self.validation_runs = self.repeats = 0
        self.last_key = ""
        self.results = {}
        self.last_candidate = None
        self.last_validation = None
        self.has_run = False

    def query(self) -> dict:
        if time.monotonic() >= self.deadline:
            raise TimeExceeded(
                {"role": "exit", "content": "deadline", "extra": {"exit_status": "TimeExceeded", "submission": ""}}
            )
        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
            return super().query()
        self.events.emit("model_request_started", call=self.n_calls + 1)
        result = super().query()
        self.events.emit("model_request_finished", call=self.n_calls, cost=result.get("extra", {}).get("cost"))
        return result

    def run(self, task: str = "", **kwargs) -> dict:
        if self.has_run:
            raise RuntimeError("一个 VerifiedAgent 只能运行一题；重修由同一循环继续")
        self.has_run = True
        try:
            return super().run(task, **kwargs)
        finally:
            if self.messages and self.messages[-1].get("role") == "exit":
                extra = self.messages[-1].setdefault("extra", {})
                reason = extra.get("termination_reason", extra.get("exit_status", "runtime_error"))
                if reason in ("LimitsExceeded", "TimeExceeded"):
                    if time.monotonic() < self.deadline:
                        try:
                            self.last_candidate = self.snapshotter.capture()
                        except CandidateRejected:
                            pass
                    reason = (
                        "deadline"
                        if reason == "TimeExceeded"
                        else ("step_limit" if 0 < self.config.step_limit <= self.n_calls else "cost_limit")
                    )
                extra.update(termination_reason=reason, **self.harness_info())
                self.events.emit("task_terminated", reason=reason, **self.harness_info())
                self.save(self.config.output_path)

    def harness_info(self) -> dict:
        return {
            "repair_cycles_started": self.repair_cycles,
            "validation_runs": self.validation_runs,
            "candidate_artifact": str(self.last_candidate.patch_path) if self.last_candidate else None,
            "public_validation": self.last_validation.status if self.last_validation else None,
        }

    def _submission(self, *, allow_repair: bool = True) -> tuple[dict, dict | None]:
        self.events.emit("submission_requested")
        candidate = getattr(self.env, "last_candidate", None) or self.snapshotter.capture()
        self.last_candidate = candidate
        self.events.emit("candidate_frozen", candidate_id=candidate.candidate_id, patch_hash=candidate.patch_hash)
        key = self.validator.key(candidate)
        if key in self.results:
            self.repeats = self.repeats + 1 if self.last_key == key else 1
            result = self.results[key]
            self.events.emit("validation_reused", candidate_id=candidate.candidate_id)
        else:
            self.repeats = 0
            if self.validation_runs >= self.config.max_repair_cycles + 1:
                return {"reason_code": "repair_limit"}, self._exit("repair_limit")
            self.events.emit("validation_started", candidate_id=candidate.candidate_id)
            result = self.validator.validate(candidate, self.deadline)
            self.validation_runs += 1
            self.results[key] = result
            self.events.emit("validation_finished", **result.model_dump())
        self.last_key, self.last_validation = key, result
        feedback = result.feedback()
        if result.log_path and Path(result.log_path).is_file() and hasattr(self.env, "put_bytes"):
            destination = f"/tmp/mini-public-{candidate.candidate_id}.log"
            self.env.put_bytes(Path(result.log_path).read_bytes(), destination)
            feedback["public_log"] = destination
        feedback.update(
            remaining_calls=max(0, self.config.step_limit - self.n_calls) if self.config.step_limit else None,
            repair_cycles_started=self.repair_cycles,
        )
        if (
            result.identity_key != key
            or result.candidate_id != candidate.candidate_id
            or result.contract_hash != self.validator.contract.sha256
        ):
            return feedback, self._exit("validation_error")
        if result.status == "passed" and result.cleanup_complete:
            patch = candidate.patch()
            self.events.emit("submission_accepted", candidate_id=candidate.candidate_id)
            return feedback, self._exit("verified_submission", patch)
        if result.status != "code_failure":
            return feedback, self._exit("validation_timeout" if result.status == "timeout" else result.status)
        if not allow_repair:
            return feedback, self._exit("budget_without_verified_candidate")
        if self.repeats >= self.config.no_progress_limit:
            return feedback, self._exit("no_progress")
        if not self.repeats:
            if self.repair_cycles >= self.config.max_repair_cycles:
                return feedback, self._exit("repair_limit" if self.repair_cycles else "validation_failed")
            self.repair_cycles += 1
            self.events.emit("repair_started", cycle=self.repair_cycles, candidate_id=candidate.candidate_id)
        return feedback, None

    def _exit(self, reason: str, patch: str = "") -> dict:
        return {
            "role": "exit",
            "content": patch or reason,
            "extra": {
                "exit_status": "Submitted" if reason == "verified_submission" else "HarnessStopped",
                "submission": patch,
                "submission_accepted": reason == "verified_submission",
                "termination_reason": reason,
                **self.harness_info(),
            },
        }

    def execute_actions(self, message: dict) -> list[dict]:
        actions, outputs, terminal = message.get("extra", {}).get("actions", []), [], None
        try:
            for action in actions:
                if time.monotonic() >= self.deadline:
                    terminal = self._exit("deadline")
                    break
                try:
                    output = self.env.execute(
                        action, timeout=min(self.env.config.timeout, self.deadline - time.monotonic())
                    )
                    outputs.append(output)
                    self.events.emit(
                        "tool_finished", tool_call_id=action.get("tool_call_id"), returncode=output["returncode"]
                    )
                except Submitted:
                    feedback, terminal = self._submission()
                    outputs.append(
                        {
                            "output": json.dumps(feedback, ensure_ascii=False),
                            "returncode": 0,
                            "exception_info": "",
                            "extra": {"public_validation": feedback},
                        }
                    )
                    break
        except CandidateRejected as error:
            terminal = self._exit("candidate_rejected")
            outputs.append({"output": str(error), "returncode": 1, "exception_info": "candidate_rejected"})
        finally:
            outputs.extend(
                {"output": "", "returncode": -1, "exception_info": "not_executed_after_interruption"}
                for _ in range(len(actions) - len(outputs))
            )
            observations = self.model.format_observation_messages(message, outputs, self.get_template_vars())
            self.add_messages(*observations)
        if terminal:
            self.add_messages(terminal)
        return observations
