"""Local launcher; read .env before resolving the dashboard port."""
import os
import fcntl

import uvicorn
from dotenv import load_dotenv

from .providers import ROOT


def main():
    load_dotenv(ROOT / ".env", override=False)
    port = int(os.getenv("APP_PORT", "18765"))
    if not 1 <= port <= 65535:
        raise ValueError("APP_PORT 必须在 1 到 65535 之间")
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "server.lock").open("a") as instance_lock:
        try:
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("实验台已在运行，请使用已有页面；不能同时修改同一个 data 目录。") from None
        uvicorn.run("game_harness.app:create_app", factory=True, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
