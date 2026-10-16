"""Single-worker local experiment coordinator. All mutations happen between episodes."""
from __future__ import annotations

import copy
import random
import secrets
import threading
import time
import uuid
from pathlib import Path

from .providers import CoachProvider, DecisionProvider, ProviderError, Settings, digest, validate_candidate
from .runtime import run_episode
from .snake import GAME_RULES, RULES_VERSION, SnakeGame
from .store import Store, atomic_json, utcnow


DEFAULTS = {"board_size": 8, "max_steps": 120, "train_games": 3,
            "validation_games": 3, "promotion_games": 4, "reflection_rounds": 1,
            "episodes": 1, "seed": 42}
LIMITS = {"board_size": (5, 20), "max_steps": (8, 1000), "train_games": (1, 12),
          "validation_games": (1, 16), "promotion_games": (1, 32),
          "reflection_rounds": (1, 5), "episodes": (1, 20), "seed": (0, 2**31 - 1)}


class ExperimentStopped(InterruptedError):
    pass


class Lab:
    def __init__(self, settings: Settings, data_dir: Path):
        self.settings = settings
        self.store = Store(data_dir)
        self.store.recover_episode_indexes()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.paused = False
        self.busy = False
        self.status = "idle"
        self.error = None
        self.current_episode = None
        self.config = dict(DEFAULTS)
        self.scope = None
        self.provider_health = {}
        self.worker = None
        for job in self.store.list("job", 10000):
            if job.get("status") in {"running", "pending", "evaluating"}:
                job.update(status="interrupted", error="服务重启，下一次同配置运行会重试此优化任务")
                self.store.put("job", job)
        last = self.store.get("metadata", "last_run")
        if last:
            self.config = last["config"]
            self.scope = last["scope"]
        # Close the crash window between durable episode completion and job creation.
        for episode in reversed(self.store.list("episode", 100000)):
            if episode.get("purpose") == "play" and episode.get("config"):
                if not self.store.get("job", "job-" + episode["episode_id"]):
                    result = self.store.load_episode(episode["episode_id"])
                    if result:
                        self.new_job(result, episode["config"], episode["scope"])

    def log(self, message, stage=None, **extra):
        event = {"id": uuid.uuid4().hex, "time": utcnow(), "message": message,
                 "stage": stage or self.status, **extra}
        self.store.put("event", event)

    def state(self):
        with self.lock:
            active = self.store.active(self.scope) if self.scope else None
            approved = {j.get("candidate_id") for j in self.store.list("job", 100000)
                        if j.get("status") == "promoted"}
            versions = self.store.list("version", 50)
            for version in versions:
                version["can_rollback"] = (version["scope"] == self.scope and
                    (version["provenance"].get("type") == "bootstrap" or version["id"] in approved))
            return copy.deepcopy({"busy": self.busy, "status": self.status, "paused": self.paused,
                                  "stop_requested": self.stop_event.is_set(), "error": self.error,
                                  "current_episode": self.current_episode, "active_harness": active,
                                  "episodes": self.store.list("episode", 120),
                                  "jobs": self.store.list("job", 30),
                                  "versions": versions,
                                  "logs": self.store.list("event", 60),
                                  "settings": {**self.config, "decision_url": self.settings.decision_url,
                                               "decision_model": self.settings.decision_model,
                                               "llm_model": self.settings.coach_model,
                                               "think_mode": "never"},
                                  "provider_health": self.provider_health})

    def check_health(self):
        decision = DecisionProvider(self.settings)
        try:
            health = decision.health()
        except ProviderError as exc:
            health = {"ok": False, "error": str(exc)}
        finally:
            decision.close()
        with self.lock:
            self.provider_health["decision"] = health
            self.provider_health.setdefault("llm", {"ok": None, "configured": bool(self.settings.coach_api_key),
                                                    "model": self.settings.coach_model,
                                                    "message": "初始化/反思时验证真实调用"})
            return copy.deepcopy(self.provider_health)

    def start(self, options):
        unknown = set(options) - set(DEFAULTS)
        if unknown:
            raise ValueError("未知运行设置：" + ", ".join(sorted(unknown)))
        config = {**self.config, **options}
        for key, (low, high) in LIMITS.items():
            if type(config[key]) is not int or not low <= config[key] <= high:
                raise ValueError(f"{key} 必须是 {low} 到 {high} 的整数")
        with self.lock:
            if self.busy:
                raise ValueError("已有实验正在运行")
            self.config = config
            self.scope = digest({"rules": RULES_VERSION, "size": config["board_size"],
                                 "max_steps": config["max_steps"], "model": self.settings.decision_model,
                                 "endpoint": self.settings.decision_url, "think_mode": "never"})[:20]
            self.store.put("metadata", {"id": "last_run", "scope": self.scope, "config": config})
            self.stop_event.clear()
            self.paused = False
            self.busy = True
            self.error = None
            self.status = "initializing"
            self.worker = threading.Thread(target=self._run, args=(dict(config), self.scope), daemon=True)
            self.worker.start()
        return {"ok": True}

    def pause(self):
        with self.lock:
            if not self.busy:
                raise ValueError("当前没有运行中的实验")
            self.paused = True
        self.log("已请求暂停；模型当前请求完成后停止推进游戏")

    def resume(self):
        with self.lock:
            self.paused = False
        self.log("继续实验")

    def stop(self):
        self.stop_event.set()
        with self.lock:
            self.paused = False
        self.log("已请求停止；等待当前模型请求返回，未完成优化保留为待恢复")

    def wait_if_paused(self):
        while self.paused and not self.stop_event.is_set():
            time.sleep(.1)
        return not self.stop_event.is_set()

    def guard(self):
        if not self.wait_if_paused():
            raise ExperimentStopped("用户停止实验")

    def coach_called(self, info):
        with self.lock:
            self.provider_health["llm"] = {"ok": True, "model": info["returned_model"],
                                           "elapsed_ms": info["elapsed_ms"]}
        self.log("GLM-5.3 完成" + ("初始化" if info["purpose"] == "initialization" else "反思"),
                 "coach", call=info)

    def version(self, candidate, scope, provenance, parent=None, brief=None):
        candidate = validate_candidate(candidate)
        if brief is None and parent:
            brief = (self.store.get("version", parent) or {}).get("brief")
        identity = "h-" + digest({"candidate": candidate, "scope": scope})[:16]
        existing = self.store.get("version", identity)
        if existing:
            return existing
        result = {"id": identity, "candidate": candidate, "scope": scope, "parent_id": parent,
                  "provenance": provenance, "brief": brief, "created_at": utcnow(),
                  "rules_version": RULES_VERSION, "decision_model": self.settings.decision_model,
                  "think_mode": "never", "identity_verification": "service_reported_name"}
        atomic_json(self.store.root / "versions" / f"{identity}.json", result)
        return self.store.put("version", result)

    def play(self, provider, harness, config, seed, purpose, job_id=None):
        self.guard()
        identity = "ep-" + uuid.uuid4().hex[:16]
        initial = SnakeGame(size=config["board_size"], seed=seed, max_steps=config["max_steps"]).snapshot()
        with self.lock:
            self.status = "playing" if purpose == "play" else "evaluating"
            self.current_episode = {"episode_id": identity, "purpose": purpose,
                                    "harness_id": harness["id"], "snapshot": initial,
                                    "last_action": None, "last_latency_ms": None}

        def on_step(record):
            with self.lock:
                self.current_episode.update(snapshot=record["after"], last_action=record["action"],
                                            last_latency_ms=record["receipt"]["elapsed_ms"],
                                            last_model=record["receipt"]["model"])

        result = run_episode(provider, harness, episode_id=identity, seed=seed,
                             size=config["board_size"], max_steps=config["max_steps"],
                             on_step=on_step, should_stop=self.stop_event.is_set,
                             wait_if_paused=self.wait_if_paused,
                             log_path=self.store.root / "traces" / f"{identity}.jsonl")
        result.update(purpose=purpose, job_id=job_id, created_at=utcnow(), scope=harness["scope"],
                      config=dict(config))
        self.store.save_episode(result)
        self.log(f"{'游玩' if purpose == 'play' else '评测'}结束：{result['score']} 分 / {result['steps']} 步 / {result['termination_reason']}",
                 "episode", episode_id=identity, purpose=purpose)
        return result

    def new_job(self, result, config, scope):
        identity = "job-" + result["episode_id"]
        existing = self.store.get("job", identity)
        if existing:
            return existing
        used = {row["seed"] for row in self.store.list("episode", 100000)}
        used.add(result["seed"])

        def seeds(n):
            values = []
            while len(values) < n:
                seed = secrets.randbelow(2**31)
                if seed not in used:
                    used.add(seed)
                    values.append(seed)
            return values

        job = {"id": identity, "episode_id": result["episode_id"], "scope": scope,
               "status": "pending", "parent_id": result["harness_id"], "config": config,
               "train_seeds": [result["seed"], *seeds(config["train_games"] - 1)],
               "validation_seeds": seeds(config["validation_games"]),
               "promotion_seeds": seeds(config["promotion_games"]),
               "attempts": 0, "created_at": utcnow(), "decision": "等待局后 GEPA 优化"}
        return self.store.put("job", job)

    def optimize(self, job, provider, coach):
        from .evolution import evolve_harness

        self.guard()
        config = job["config"]
        baseline = self.store.active(job["scope"])
        trigger = self.store.load_episode(job.get("trigger_episode_id", job["episode_id"]))
        job.update(status="running", attempts=job.get("attempts", 0) + 1,
                   parent_id=baseline["id"], error=None, decision="GEPA 正在根据回合轨迹反思")
        self.store.put("job", job)
        workdir = self.store.root / "jobs" / job["id"] / f"attempt-{job['attempts']}"
        workdir.mkdir(parents=True, exist_ok=False)
        atomic_json(workdir / "manifest.json", job)
        with self.lock:
            self.status = "optimizing"
        self.log("启动局后 GEPA：新作业固定数据清单，只优化两段策略文字", "optimizing", job_id=job["id"])

        def evaluate(candidate, seed, purpose):
            self.guard()
            harness = self.version(candidate, job["scope"], {"type": "gepa_candidate", "job_id": job["id"]}, baseline["id"])
            result = self.play(provider, harness, config, seed, purpose, job["id"])
            if result["termination_reason"] == "stopped":
                raise ExperimentStopped("用户停止评测")
            if result.get("infra_error"):
                raise ProviderError("评测发生基础设施错误，未将其记为策略零分")
            return result

        def event(data):
            self.guard()
            with self.lock:
                self.status = "optimizing"
            self.log(str(data.get("message", data.get("type", data.get("event", "GEPA 进度")))),
                     "optimizing", job_id=job["id"], detail=data)

        try:
            if trigger.get("infra_error") or trigger.get("termination_reason") == "stopped":
                self.log("原局因故障或停止中断：先在同种子重跑，取得有效反馈后再优化", "recovery", job_id=job["id"])
                recovered = self.play(provider, baseline, config, trigger["seed"], "recovery", job["id"])
                if recovered["termination_reason"] == "stopped":
                    raise ExperimentStopped("用户停止故障恢复")
                if recovered.get("infra_error"):
                    raise ProviderError("故障恢复回合仍失败；保留待恢复任务")
                trigger = recovered
                job["trigger_episode_id"] = recovered["episode_id"]
                self.store.put("job", job)
            outcome = evolve_harness(candidate=baseline["candidate"], trigger_episode=trigger,
                                     train_seeds=job["train_seeds"], validation_seeds=job["validation_seeds"],
                                     evaluate_episode=evaluate, coach=coach, run_dir=workdir / "gepa",
                                     reflection_rounds=config["reflection_rounds"], on_event=event,
                                     should_stop=self.stop_event.is_set)
            self.guard()
            atomic_json(workdir / "outcome.json", outcome)
            job["proposals"] = outcome.get("proposals", [])
            job["gepa"] = outcome.get("gepa", {})
            selected = self.version(outcome["candidate"], job["scope"],
                                    {"type": "gepa", "job_id": job["id"]}, baseline["id"])
            job["candidate_id"] = selected["id"]
            if selected["id"] == baseline["id"]:
                job.update(status="retained", decision="GEPA 未选出更优候选，保留当前版本")
            else:
                job.update(status="evaluating", decision="使用新种子进行新旧版本配对验收")
                self.store.put("job", job)
                pairs = []
                for index, seed in enumerate(job["promotion_seeds"]):
                    self.guard()
                    ordered = [("baseline", baseline), ("candidate", selected)]
                    if index % 2:
                        ordered.reverse()
                    pair = {"seed": seed}
                    for label, harness in ordered:
                        res = self.play(provider, harness, config, seed, "promotion_" + label, job["id"])
                        if res["termination_reason"] == "stopped":
                            raise ExperimentStopped()
                        if res.get("infra_error"):
                            raise ProviderError("发布验收发生基础设施错误，保留旧版")
                        pair[label] = res["score"]
                        pair[label + "_episode_id"] = res["episode_id"]
                    pairs.append(pair)
                    job["pairs"] = pairs
                    self.store.put("job", job)
                delta = [p["candidate"] - p["baseline"] for p in pairs]
                baseline_score = sum(p["baseline"] for p in pairs) / len(pairs)
                candidate_score = sum(p["candidate"] for p in pairs) / len(pairs)
                # Explicit empirical V1 gate, NOT a claim of statistical significance.
                promoted = len(pairs) >= 4 and sum(delta) / len(delta) >= .5 and min(delta) >= 0
                job.update(baseline_score=baseline_score, candidate_score=candidate_score,
                           mean_gain=candidate_score - baseline_score,
                           gate={"version": "v1-empirical-paired", "minimum_pairs": 4,
                                 "minimum_mean_gain": .5, "no_pair_regression": True,
                                 "statistical_claim": False})
                if promoted:
                    job.update(status="promoted", decision="通过小样本配对门槛，下一局使用新版本；不代表泛化证明")
                else:
                    job.update(status="retained", decision="候选未通过配对门槛或样本不足，保留当前版本")
            job["finished_at"] = utcnow()
            if job["status"] == "promoted":
                self.store.activate(job["scope"], selected["id"], baseline["id"], publication=job)
            else:
                self.store.put("job", job)
        except Exception as exc:
            interrupted = self.stop_event.is_set() or isinstance(exc, (ExperimentStopped, InterruptedError))
            job.update(status="interrupted" if interrupted else "failed", error=str(exc)[:500],
                       decision="优化中止，下一次同配置运行可重试" if interrupted else "优化失败，旧版本保留；下一次同配置运行重试")
            partial = getattr(exc, "partial_result", None)
            if partial:
                atomic_json(workdir / "partial.json", partial)
            self.store.put("job", job)
            raise
        # A later log failure must never downgrade an already committed publication.
        self.log(job["decision"], "optimization_complete", job_id=job["id"])

    def _run(self, config, scope):
        provider = coach = None
        try:
            provider = DecisionProvider(self.settings)
            coach = CoachProvider(self.settings, on_call=self.coach_called)
            health = provider.health()
            with self.lock:
                self.provider_health["decision"] = health
            self.guard()
            active = self.store.active(scope)
            if active is None:
                self.log("GLM-5.3 正在生成游戏背景、规则说明与初版 Harness", "initializing")
                brief, candidate = coach.initialize(GAME_RULES, config["board_size"], config["max_steps"])
                self.guard()
                active = self.version(candidate, scope, {"type": "bootstrap", "coach": self.settings.coach_model}, brief=brief)
                self.store.activate(scope, active["id"], None)
                self.log("初始化完成：" + active["id"], "initializing")
            # Every interrupted gameplay episode keeps one durable job; resume before new gameplay.
            pending = [j for j in reversed(self.store.list("job", 10000))
                       if j["scope"] == scope and j["status"] in {"pending", "failed", "interrupted"}]
            for job in pending:
                self.optimize(job, provider, coach)
            for index in range(config["episodes"]):
                self.guard()
                active = self.store.active(scope)
                result = self.play(provider, active, config, config["seed"] + index, "play")
                job = self.new_job(result, config, scope)
                if result.get("infra_error"):
                    job.update(status="failed", decision="游玩发生基础设施故障，暂停后续实验", error=result["infra_error"]["message"])
                    self.store.put("job", job)
                    raise ProviderError(job["error"])
                self.guard()
                self.optimize(job, provider, coach)
            with self.lock:
                self.status = "complete"
            self.log("本轮游玩与局后优化全部完成", "complete")
        except Exception as exc:
            with self.lock:
                if self.stop_event.is_set() or isinstance(exc, (ExperimentStopped, InterruptedError)):
                    self.status = "stopped"
                    self.log("实验已停止，已完成回合和优化任务保留", "stopped")
                else:
                    self.status = "error"
                    self.error = str(exc)[:500]
                    self.log("实验中止：" + self.error, "error")
        finally:
            if provider is not None:
                provider.close()
            if coach is not None:
                coach.close()
            with self.lock:
                self.busy = False
                self.paused = False

    def rollback(self, version_id):
        with self.lock:
            if self.busy:
                raise ValueError("请先停止实验，再切换下一局的版本")
            version = self.store.get("version", version_id)
            if not version or version["scope"] != self.scope:
                raise ValueError("只能回滚当前游戏配置下的版本")
            # Only allow versions already used as champion, never an unvalidated candidate.
            approved = version["provenance"].get("type") == "bootstrap" or any(
                j.get("status") == "promoted" and j.get("candidate_id") == version_id for j in self.store.list("job", 100000))
            if not approved:
                raise ValueError("该候选未通过发布，不能通过回滚绕过评测")
            current = self.store.active(self.scope)
            self.store.activate(self.scope, version_id, current["id"])
            self.log("已回滚，下一局使用 " + version_id, "rollback")
        return {"ok": True}
