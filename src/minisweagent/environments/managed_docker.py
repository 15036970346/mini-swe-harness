import json
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from minisweagent.environments.docker import DockerEnvironment, DockerEnvironmentConfig
from minisweagent.exceptions import Submitted
from minisweagent.harness import supervisor, test_feedback
from minisweagent.harness.contracts import Candidate, CandidateRejected
from minisweagent.harness.snapshot import FileMap, unpack

_EXPORT = """
import io,json,os,subprocess,sys,tarfile
names=set(json.loads(sys.argv[1]))
names.update(subprocess.check_output(["git","ls-files","--cached","--others","--exclude-standard","-z"]).decode().split("\\0"))
root=os.path.realpath(".")
with tarfile.open(fileobj=sys.stdout.buffer,mode="w|") as archive:
    for name in sorted(names):
        if not name or not os.path.lexists(name): continue
        if os.path.isabs(name) or ".." in name.split("/") or ".git" in name.split("/"):
            raise ValueError("unsafe path")
        if not os.path.realpath(name).startswith(root+os.sep): raise ValueError("symlink escapes repository")
        archive.add(name,arcname=name,recursive=False)
"""


class ManagedDockerEnvironmentConfig(DockerEnvironmentConfig):
    test_feedback: bool = False
    test_feedback_dir: str = ""


class ManagedDockerEnvironment(DockerEnvironment):
    capture_candidate = None
    last_candidate: Candidate | None = None

    def __init__(self, *, config_class=ManagedDockerEnvironmentConfig, **kwargs):
        super().__init__(config_class=config_class, **kwargs)

    def raw(self, command: str, *, input_bytes: bytes | None = None, timeout: float = 30) -> bytes:
        if not self.container_id:
            raise RuntimeError("容器已关闭")
        cmd = [self.config.executable, "exec", "-i", "-w", self.config.cwd]
        for key, value in self.config.env.items():
            cmd.extend(["-e", f"{key}={value}"])
        cmd.extend([self.container_id, *self.config.interpreter, command])
        return subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout, check=True).stdout

    def execute(self, action: dict, cwd: str = "", *, timeout: float | None = None) -> dict:
        if cwd and cwd != self.config.cwd:
            raise ValueError("第一版固定任务工作目录；请在命令中显式 cd")
        seconds = self.config.timeout if timeout is None else timeout
        if seconds <= 0:
            raise TimeoutError("已无剩余执行时间")
        if self.config.test_feedback and action["command"].split(maxsplit=1)[:1] == ["mini-test"]:
            return self._run_tests(action["command"], seconds)
        return self._execute(action, seconds)

    def _execute(self, action: dict, seconds: float) -> dict:
        spec = json.dumps({"argv": [*self.config.interpreter, action["command"]], "timeout": seconds})
        command = shlex.join(["python", "-c", Path(supervisor.__file__).read_text(), spec])
        try:
            output = json.loads(self.raw(command, timeout=seconds + 5))
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError):
            self.cleanup()
            raise
        self._check_finished(output)
        return output

    def _run_tests(self, command: str, seconds: float) -> dict:
        started = time.monotonic()
        try:
            args = shlex.split(command)[1:]
        except ValueError:
            return {"returncode": 2, "output": "mini-test 参数引号不匹配，请检查命令。", "exception_info": ""}
        if any(arg in {"|", "||", "&&", ";", "&", ">", ">>", "<"} for arg in args):
            return {
                "returncode": 2,
                "output": "mini-test 只接收 pytest 参数，不支持管道或重定向。",
                "exception_info": "",
            }
        run_id = uuid.uuid4().hex
        directory = f"/tmp/mini-test-{run_id}"
        self.raw("mkdir -- " + directory)
        script = directory + "/runner.py"
        self.put_bytes(Path(test_feedback.__file__).read_bytes(), script)
        spec = json.dumps({"directory": directory, "args": args})
        remaining = seconds - (time.monotonic() - started)
        output = (
            self._execute({"command": shlex.join(["python", script, spec])}, remaining)
            if remaining > 0
            else {"returncode": 124, "exception_info": "准备测试入口时预算耗尽", "extra": {"timed_out": True}}
        )
        if output.get("extra", {}).get("timed_out"):
            result = {"status": "timeout", "pytest_returncode": None, "counts": None, "failures": []}
        else:
            raw = self.raw(f"if test -f {directory}/result.json; then cat {directory}/result.json; fi")
            result = json.loads(raw) if raw else {"status": "report_error", "pytest_returncode": None, "counts": None}
            if result["status"] != "passed" and output["returncode"] == 0:
                output["returncode"] = 2
        log = self.raw(f"if test -f {directory}/stdout.log; then cat {directory}/stdout.log; fi")
        result["log_path"] = directory + "/stdout.log"
        result["log_tail"] = log[-1600:].decode("utf-8", "replace")
        output.setdefault("extra", {})["test_feedback"] = result
        if self.config.test_feedback_dir:
            saved = Path(self.config.test_feedback_dir) / run_id
            saved.mkdir(parents=True, exist_ok=False)
            (saved / "stdout.log").write_bytes(log)
            (saved / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            output["extra"]["test_feedback_artifact"] = str(saved)
        output["output"] = "[测试观察，不是独立验收]\n" + json.dumps(result, ensure_ascii=False)
        return output

    def _check_finished(self, output: dict) -> None:
        try:
            super()._check_finished(output)
        except Submitted as signal:
            if self.capture_candidate is None:
                raise
            self.last_candidate = self.capture_candidate()
            patch = self.last_candidate.patch()
            raise Submitted(
                {
                    "role": "exit",
                    "content": patch,
                    "extra": {
                        "exit_status": "Submitted",
                        "submission": patch,
                        "candidate_id": self.last_candidate.candidate_id,
                        "raw_submission": signal.messages[0]["extra"]["submission"],
                    },
                }
            ) from signal

    def initial_files(self) -> FileMap:
        return unpack(self.raw("git archive HEAD", timeout=60))

    def workspace_files(self, initial_names: list[str]) -> FileMap:
        try:
            return unpack(self.raw(shlex.join(["python", "-c", _EXPORT, json.dumps(initial_names)]), timeout=60))
        except subprocess.CalledProcessError as error:
            raise CandidateRejected("无法导出候选文件；检查路径、符号链接或活动写入") from error

    def put_bytes(self, raw: bytes, destination: str) -> None:
        with tempfile.NamedTemporaryFile(prefix="mini-harness-copy-") as temporary:
            temporary.write(raw)
            temporary.flush()
            subprocess.run(
                [self.config.executable, "cp", temporary.name, f"{self.container_id}:{destination}"],
                check=True,
                capture_output=True,
                timeout=30,
            )

    def cleanup(self) -> None:
        if getattr(self, "container_id", None):
            subprocess.run(
                [self.config.executable, "rm", "-f", self.container_id], capture_output=True, check=True, timeout=30
            )
            self.container_id = None

    def __del__(self):
        # 生命周期由调用方的 finally 管理，析构不启动不可追踪的后台清理。
        pass
