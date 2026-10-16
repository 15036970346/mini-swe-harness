"""在目标容器内运行，保持与旧 Python 镜像兼容。"""

import json
import os
import signal
import subprocess
import sys


def main():
    spec = json.loads(sys.argv[1])
    process = subprocess.Popen(spec["argv"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    timed_out = False
    try:
        output, _ = process.communicate(timeout=spec["timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
    # 即使命令已返回，重定向输出的后台子进程仍可能留在同一进程组。
    background_terminated = False
    try:
        os.killpg(process.pid, signal.SIGKILL)
        background_terminated = not timed_out
    except ProcessLookupError:
        pass
    print(
        json.dumps(
            {
                "output": output.decode("utf-8", "replace"),
                "returncode": 124 if timed_out else process.returncode,
                "exception_info": "命令超时，已终止进程组" if timed_out else "",
                "extra": {"timed_out": timed_out, "background_terminated": background_terminated},
            }
        )
    )


if __name__ == "__main__":
    main()
