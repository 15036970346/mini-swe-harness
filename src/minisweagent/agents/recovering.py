"""在原循环之外恢复瞬时请求错误；不重复工具动作、不重置预算。"""

import os
import time

from litellm.exceptions import (
    APIConnectionError,
    BadGatewayError,
    InternalServerError,
    ServiceUnavailableError,
    Timeout,
)

from minisweagent.agents.default import DefaultAgent
from minisweagent.agents.verified import VerifiedAgent
from minisweagent.exceptions import FormatError, LimitsExceeded, TimeExceeded
from minisweagent.harness.events import Events


class RequestRecoveryMixin:
    retry_delays = (4.0, 8.0)
    unknown_request_reserve_usd = 0.5  # 风险预留，不是账单或已证明的费用上界。

    def __init__(self, *args, request_events: Events, **kwargs):
        super().__init__(*args, **kwargs)
        if (
            os.getenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "10") != "1"
            or self.model.config.model_kwargs.get("num_retries") != 0
        ):
            raise ValueError("恢复层要求模型内层只尝试一次且SDK num_retries=0，避免隐藏请求和重复计数")
        self.request_events = request_events
        self.unknown_calls: list[int] = []
        self.request_retries = 0

    def recovery_info(self) -> dict:
        return {
            "version": "request-recovery-v1",
            "retry_delays_seconds": list(self.retry_delays),
            "retry_attempts": self.request_retries,
            "unknown_calls": self.unknown_calls.copy(),
            "unknown_cost_reserve_usd": len(self.unknown_calls) * self.unknown_request_reserve_usd,
        }

    def serialize(self, *extra_dicts) -> dict:
        data = super().serialize(*extra_dicts)
        data["info"]["request_recovery"] = self.recovery_info()
        return data

    def _recovery_budget(self, delay: float = 0) -> None:
        if (0 < self.config.step_limit <= self.n_calls) or (
            0 < self.config.cost_limit <= self.cost + len(self.unknown_calls) * self.unknown_request_reserve_usd
        ):
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "请求预算耗尽",
                    "extra": {
                        "exit_status": "LimitsExceeded",
                        "submission": "",
                    },
                }
            )
        if 0 < self.config.wall_time_limit_seconds <= time.time() - self._start_time + delay:
            raise TimeExceeded(
                {
                    "role": "exit",
                    "content": "剩余时间不足以重试",
                    "extra": {
                        "exit_status": "TimeExceeded",
                        "submission": "",
                    },
                }
            )

    def query(self) -> dict:
        for attempt in range(len(self.retry_delays) + 1):
            self._recovery_budget()
            before = self.n_calls
            if attempt:
                self.request_retries += 1
            self.request_events.emit("request_attempt_started", call=before + 1, retry=attempt)
            try:
                response = super().query()
            except FormatError:
                self.request_events.emit("request_response_format_error", call=self.n_calls)
                raise
            except (
                APIConnectionError,
                InternalServerError,
                BadGatewayError,
                ServiceUnavailableError,
                Timeout,
            ) as error:
                # 上游与SDK内部重试必须关闭，才能把一次query等同一次物理请求。
                if self.n_calls != before + 1:
                    raise
                self.unknown_calls.append(self.n_calls)
                self.request_events.emit(
                    "request_attempt_failed", call=self.n_calls, error_type=type(error).__name__, usage_unknown=True
                )
                self.save(self.config.output_path)
                if attempt == len(self.retry_delays):
                    raise
                delay = self.retry_delays[attempt]
                self._recovery_budget(delay)
                self.request_events.emit("request_retry_scheduled", after_call=self.n_calls, delay_seconds=delay)
                time.sleep(delay)
            else:
                self.request_events.emit("request_attempt_succeeded", call=self.n_calls)
                return response
        raise AssertionError("不可达的重试状态")


class RecoveringDefaultAgent(RequestRecoveryMixin, DefaultAgent):
    pass


class RecoveringVerifiedAgent(RequestRecoveryMixin, VerifiedAgent):
    pass
