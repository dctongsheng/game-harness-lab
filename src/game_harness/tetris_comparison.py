"""Live paired Tetris demo with the frozen before/after V8 policies."""
from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
import uvicorn

from .games import get_game
from .providers import ROOT, Settings, digest, validate_candidate
from .runtime import run_episode
from .store import atomic_json, utcnow
from .tetris_context import validate_config
from .tetris_vision import render_tetris_png
from .vision_provider import VisionDecisionProvider


DATA_DIR = ROOT / "data/experiences/tetris-comparison-v8"
ARTIFACT_DIR = ROOT / "docs/experiments/tetris-landing-v8"
LABELS = {"baseline": "优化前", "planning": "优化后"}
TERMINAL = {"completed", "stopped", "error"}


def load_harnesses(settings):
    arms = {name: json.loads((ARTIFACT_DIR / filename).read_text()) for name, filename in (
        ("baseline", "baseline-harness.json"), ("planning", "research-harness.json"))}
    for name, harness in arms.items():
        validate_candidate(harness["candidate"])
        validate_config(harness["tetris_context_config"])
        if (harness["game_id"] != "tetris" or harness["decision_model"] != settings.decision_model
                or harness["think_mode"] != "never"
                or harness["game_context"] != get_game("tetris").context(harness["config"])):
            raise ValueError("冻结 Harness 与当前游戏或模型不匹配")
    baseline, planning = arms["baseline"], arms["planning"]
    shared = lambda h: {k: v for k, v in h.items() if k not in {"id", "tetris_context_config"}}
    v2 = {"schema": "tetris-context-v2", "structured_facts": True,
          "decision_goal_memory": True, "replan_every_actions": 4, "control_feedback": True}
    v3 = {**v2, "schema": "tetris-context-v3",
          "goal_instructions": planning["tetris_context_config"].get("goal_instructions")}
    if shared(baseline) != shared(planning) or baseline["tetris_context_config"] != v2 or planning["tetris_context_config"] != v3:
        raise ValueError("两组策略差异超出已验证的 V8 规划方案")
    return arms


class TetrisComparison:
    def __init__(self, settings, data_dir=DATA_DIR):
        self.settings = settings
        self.root = Path(data_dir).resolve()
        if self.root in {ROOT.resolve(), (ROOT / "data").resolve()}:
            raise ValueError("对照体验必须使用独立数据目录")
        self.harnesses = load_harnesses(settings)
        self.root.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition(threading.RLock())
        self.stop_event = threading.Event()
        self.busy, self.paused = False, False
        self.status, self.error = "idle", None
        self.pair_id = None
        self.seed, self.max_steps = secrets.randbelow(2**31), 80
        self.worker = None
        self.arms = self._initial_arms(self.seed, self.max_steps)
        self.history = []
        for path in sorted((self.root / "pairs").glob("*/summary.json"), reverse=True)[:50]:
            try:
                self.history.append(json.loads(path.read_text()))
            except (ValueError, OSError):
                continue
        self.history.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    def _initial_arms(self, seed, max_steps):
        session = get_game("tetris").create(seed, {"max_steps": max_steps})
        try:
            initial = session.snapshot()
        finally:
            session.close()
        return {arm: {"label": label, "status": "idle", "episode_id": None,
                      "snapshot": deepcopy(initial), "last_action": None,
                      "last_latency_ms": None, "error": None, "result": None}
                for arm, label in LABELS.items()}

    def state(self):
        with self.condition:
            return deepcopy({"busy": self.busy, "paused": self.paused,
                             "stop_requested": self.stop_event.is_set(),
                             "status": self.status, "error": self.error,
                             "pair_id": self.pair_id, "seed": self.seed, "max_steps": self.max_steps,
                             "model": self.settings.decision_model, "think_mode": "never", "vision": True,
                             "arms": self.arms, "history": self.history[:30]})

    def start(self, options):
        if set(options) - {"seed", "max_steps"}:
            raise ValueError("只接受 seed 与 max_steps")
        seed = options.get("seed", secrets.randbelow(2**31))
        steps = options.get("max_steps", 80)
        if type(seed) is not int or not 0 <= seed < 2**31:
            raise ValueError("种子必须是 0 到 2147483647 的整数")
        if type(steps) is not int or not 8 <= steps <= 1000:
            raise ValueError("每局动作上限必须是 8 到 1000 的整数")
        with self.condition:
            if self.busy:
                raise ValueError("当前对照还在运行，请先停止或等待结束")
            # Build the public initial board once, then give both players an
            # identical view. Actual sessions are separately created by runtime.
            initial = self._initial_arms(seed, steps)
            identity = "pair-" + uuid.uuid4().hex[:20]
            directory = self.root / "pairs" / identity
            directory.mkdir(parents=True, exist_ok=False)
            atomic_json(directory / "manifest.json", {
                "id": identity, "created_at": utcnow(), "seed": seed, "max_steps": steps,
                "model": self.settings.decision_model, "think_mode": "never", "vision": True,
                "shared_model_visible_episode_id": identity, "harnesses": self.harnesses,
                "harness_hashes": {k: digest(v) for k, v in self.harnesses.items()},
                "scheduling": "two independent sessions, synchronized after native actions",
                "purpose": "live_experience_not_formal_evaluation", "automatic_promotion": False,
            })
            self.pair_id, self.seed, self.max_steps = identity, seed, steps
            self.arms = initial
            for arm in self.arms.values():
                arm.update(status="initializing", episode_id=identity)
            self.stop_event.clear()
            self.busy, self.paused, self.error = True, False, None
            self.status = "initializing"
            self.worker = threading.Thread(target=self._run_pair,
                                           args=(identity, seed, steps, directory), daemon=True)
            self.worker.start()
        return {"ok": True, "pair_id": identity}

    def pause(self):
        with self.condition:
            if not self.busy:
                raise ValueError("当前没有运行中的对照")
            self.paused = True
            self.condition.notify_all()

    def resume(self):
        with self.condition:
            self.paused = False
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stop_event.set()
            self.paused = False
            self.condition.notify_all()

    def _wait(self):
        with self.condition:
            while self.paused and not self.stop_event.is_set():
                self.condition.wait(.2)
            return not self.stop_event.is_set()

    def _on_step(self, arm, record):
        other = "planning" if arm == "baseline" else "baseline"
        with self.condition:
            current = self.arms[arm]
            current.update(snapshot=record["after"], last_action=record["action"],
                           last_latency_ms=record["receipt"]["elapsed_ms"], status="playing")
            self.condition.notify_all()
            # Synchronize presentation at native-action boundaries only. Do not
            # barrier provider calls: the two policies replan at different times.
            while (not self.stop_event.is_set() and self.arms[other]["status"] not in TERMINAL
                   and self.arms[other]["snapshot"]["step"] < record["after"]["step"]):
                self.condition.wait(.2)

    def _run_arm(self, arm, identity, seed, steps, directory):
        provider = None
        try:
            provider = VisionDecisionProvider(self.settings, enabled=True,
                image_store=directory / arm / "images", archive_dir=directory / arm / "calls")
            provider.health()
            with self.condition:
                self.arms[arm]["status"] = "playing"
                self.status = "playing"
            result = run_episode(provider, deepcopy(self.harnesses[arm]), episode_id=identity,
                seed=seed, adapter=get_game("tetris"), config={"max_steps": steps}, max_steps=steps,
                on_step=lambda record: self._on_step(arm, record),
                should_stop=self.stop_event.is_set, wait_if_paused=self._wait,
                log_path=directory / arm / "trace.jsonl")
            atomic_json(directory / arm / "result.json", result)
            with self.condition:
                current = self.arms[arm]
                failed = bool(result.get("infra_error"))
                stopped = result["termination_reason"] == "stopped"
                current.update(snapshot=result["final_state"],
                    status="error" if failed else "stopped" if stopped else "completed",
                    error="模型调用或执行校验失败" if failed else None,
                    result={"score": result["score"], "steps": result["steps"],
                            "termination_reason": result["termination_reason"], "infra_error": failed})
                if failed:
                    self.error = "一侧调用失败，本组不能用于判断提升；已停止另一侧并保留记录。"
                    self.stop_event.set()
                self.condition.notify_all()
        except Exception:
            with self.condition:
                self.arms[arm].update(status="error", error="服务或本地记录失败")
                self.error = "本组运行失败，不能用于判断提升。请检查 Decision 服务。"
                self.stop_event.set()
                self.condition.notify_all()
        finally:
            try:
                if provider is not None:
                    provider.close()
            except Exception:
                with self.condition:
                    self.arms[arm].update(status="error", error="模型连接收尾失败")
                    self.error = "本组未能完整结束，不能用于判断提升。"
                    self.stop_event.set()
            finally:
                with self.condition:
                    if self.arms[arm]["status"] not in TERMINAL:
                        self.arms[arm].update(status="error", error="对局异常结束")
                        self.stop_event.set()
                    self.condition.notify_all()

    def _run_pair(self, identity, seed, steps, directory):
        workers = []
        try:
            # Both workers see the same episode ID and request metadata. Arm
            # labels occur only in storage paths and the presentation layer.
            for arm in LABELS:
                worker = threading.Thread(target=self._run_arm,
                    args=(arm, identity, seed, steps, directory), daemon=True)
                worker.start()
                workers.append(worker)
            for worker in workers:
                worker.join()
            with self.condition:
                statuses = {x["status"] for x in self.arms.values()}
                self.status = "error" if "error" in statuses else "stopped" if "stopped" in statuses else "completed"
                summary = {"id": identity, "created_at": utcnow(), "seed": seed,
                           "max_steps": steps, "status": self.status, "arms": {}}
                for arm, state in self.arms.items():
                    result = state["result"] or {}
                    summary["arms"][arm] = {
                        "score": state["snapshot"]["score"],
                        "lines": state["snapshot"].get("metrics", {}).get("lines", 0),
                        "steps": state["snapshot"]["step"],
                        "reason": result.get("termination_reason", "infra_error"), "error": state["error"],
                    }
                atomic_json(directory / "summary.json", summary)
                self.history.insert(0, summary)
        except Exception:
            self.stop()
            for worker in workers:
                worker.join()
            with self.condition:
                self.status, self.error = "error", "对照记录未能完整保存，本组不能用于判断提升。"
        finally:
            with self.condition:
                self.busy, self.paused = False, False
                self.condition.notify_all()

    def frame(self, arm):
        with self.condition:
            if arm not in self.arms:
                raise ValueError("未知对照组")
            snapshot = deepcopy(self.arms[arm]["snapshot"])
        return render_tetris_png(snapshot)


def create_app(data_dir=DATA_DIR, settings=None):
    comparison = TetrisComparison(settings or Settings.load(), data_dir)
    app = FastAPI(title="Tetris Harness 对照", docs_url=None, redoc_url=None)
    app.state.comparison = comparison

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        if request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "仅允许本地访问"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method != "GET" and origin:
            parsed = urlparse(origin)
            if parsed.netloc != request.headers.get("host") or parsed.scheme not in {"http", "https"}:
                return JSONResponse({"detail": "拒绝跨站控制请求"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static/tetris-comparison.html")

    @app.get("/api/compare/state")
    def state():
        return comparison.state()

    @app.get("/api/compare/frame/{arm}")
    def frame(arm: str):
        try:
            return Response(comparison.frame(arm), media_type="image/png")
        except ValueError:
            raise HTTPException(400, "棋盘不可用") from None

    @app.post("/api/compare/start")
    def start(options: dict):
        try:
            return comparison.start(options)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/compare/pause")
    def pause():
        try:
            comparison.pause()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True}

    @app.post("/api/compare/resume")
    def resume():
        comparison.resume()
        return {"ok": True}

    @app.post("/api/compare/stop")
    def stop():
        comparison.stop()
        return {"ok": True}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18769)
    args = parser.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("对照页已在运行，请打开已有页面") from None
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
