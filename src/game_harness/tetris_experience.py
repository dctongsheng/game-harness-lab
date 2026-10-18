"""Isolated live viewer for the frozen V8 research Harness; no evolution or promotion."""
from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import secrets
import threading

from fastapi import HTTPException
from fastapi.responses import Response
import uvicorn

from .app import create_app as dashboard_app
from .games import get_game
from .providers import ROOT, Settings, digest, validate_candidate
from .service import DEFAULTS, ExperimentStopped, Lab
from .tetris_context import validate_config
from .tetris_vision import render_tetris_png
from .vision_provider import VisionDecisionProvider


HARNESS_PATH = ROOT / "docs/experiments/tetris-landing-v8/research-harness.json"
DATA_DIR = ROOT / "data/experiences/tetris-v8"


class TetrisExperience(Lab):
    def __init__(self, settings, data_dir):
        data_dir = Path(data_dir).resolve()
        if data_dir == (ROOT / "data").resolve() or (ROOT / "data").resolve() not in data_dir.parents:
            raise ValueError("体验记录必须存放在独立的 data 子目录")
        harness = json.loads(HARNESS_PATH.read_text())
        validate_candidate(harness["candidate"])
        context = validate_config(harness["tetris_context_config"])
        if (context["schema"] != "tetris-context-v3" or harness["game_id"] != "tetris"
                or harness["decision_model"] != settings.decision_model
                or harness["think_mode"] != "never"
                or harness["game_context"] != get_game("tetris").context(harness["config"])):
            raise ValueError("V8 Harness 与当前模型或游戏规则不匹配")
        # A separate store ensures starting/restarting a viewer cannot recover
        # production jobs, activate a candidate, or export it as approved.
        super().__init__(settings, data_dir)
        self.harness = deepcopy(harness)
        self.scope = "experience-" + digest(harness)[:20]
        self.harness["scope"] = self.scope
        self.config = {**DEFAULTS, "game_id": "tetris", "max_steps": 80,
                       "seed": secrets.randbelow(2**31)}
        session = get_game("tetris").create(self.config["seed"], self.config)
        try:
            self.preview = session.snapshot()
        finally:
            session.close()

    def state(self):
        result = super().state()
        result["active_harness"] = deepcopy(self.harness)
        result["experience"] = {
            "name": "俄罗斯方块 V8 · Decision 实时体验", "vision": True,
            "goal_instructions": self.harness["tetris_context_config"]["goal_instructions"],
            "research_only": True, "harness_id": self.harness["id"],
        }
        return result

    def start(self, options):
        if set(options) - {"seed", "max_steps"}:
            raise ValueError("体验页仅接受 seed 与 max_steps")
        seed = options.get("seed", secrets.randbelow(2**31))
        max_steps = options.get("max_steps", 80)
        if type(seed) is not int or not 0 <= seed < 2**31:
            raise ValueError("种子必须是 0 到 2147483647 的整数")
        if type(max_steps) is not int or not 8 <= max_steps <= 1000:
            raise ValueError("动作上限必须是 8 到 1000 的整数")
        with self.lock:
            if self.busy:
                raise ValueError("当前对局尚未结束；请先停止或等待结束")
            self.config = {**self.config, "seed": seed, "max_steps": max_steps}
            self.busy, self.paused, self.error = True, False, None
            self.status = "initializing"
            self.current_episode = None
            self.stop_event.clear()
            self.worker = threading.Thread(target=self._experience,
                                           args=(dict(self.config),), daemon=True)
            self.worker.start()
        return {"ok": True}

    def _experience(self, config):
        provider = None
        try:
            provider = VisionDecisionProvider(
                self.settings, enabled=True, image_store=self.store.root / "images",
                archive_dir=self.store.root / "calls")
            health = provider.health()
            with self.lock:
                self.provider_health["decision"] = health
            result = self.play(provider, self.harness, config, config["seed"], "experience")
            with self.lock:
                if result.get("infra_error"):
                    self.error = "Decision 请求或执行校验失败，本局已停止；可查看本地轨迹。"
                    self.status = "error"
                else:
                    self.status = "stopped" if result["termination_reason"] == "stopped" else "completed"
        except ExperimentStopped:
            with self.lock:
                self.status = "stopped"
        except Exception:
            # Do not return provider/client internals to the browser.
            with self.lock:
                self.error = "体验启动失败，请检查 Decision 服务与本地游戏依赖。"
                self.status = "error"
        finally:
            if provider is not None:
                provider.close()
            with self.lock:
                self.busy, self.paused = False, False

    def rollback(self, version_id):
        raise ValueError("体验页使用冻结的 V8 Harness，不提供生产版本切换")

    def frame(self):
        with self.lock:
            snapshot = deepcopy((self.current_episode or {}).get("snapshot", self.preview))
        return render_tetris_png(snapshot)


def create_app(data_dir=DATA_DIR, settings=None):
    lab = TetrisExperience(settings or Settings.load(), data_dir)
    app = dashboard_app(lab=lab, index_file=Path(__file__).parent / "static/tetris-experience.html")

    @app.get("/api/tetris/frame")
    def frame():
        try:
            return Response(lab.frame(), media_type="image/png")
        except ValueError:
            raise HTTPException(503, "当前棋盘画面暂不可用") from None

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18768)
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("V8 体验页已在运行，请打开已有页面") from None
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
