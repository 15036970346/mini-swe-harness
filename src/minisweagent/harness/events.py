import json
import time
from datetime import datetime, timezone
from pathlib import Path


class Events:
    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id, self.seq = path, run_id, 0
        self.started = time.monotonic()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"运行已开始，不能覆盖：{path}")

    def emit(self, event: str, **fields) -> None:
        self.seq += 1
        row = {
            "seq": self.seq,
            "run_id": self.run_id,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.monotonic() - self.started,
            **fields,
        }
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
